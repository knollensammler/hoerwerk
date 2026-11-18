"""Hörwerk player controls, run entirely on the Tk event thread."""
from tkinter import ttk
import tkinter as tk

from core import HoerwerkError
from playback import Playback, load_position, save_position, seek_position


def clock(seconds):
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


class PlayerWindow:
    def __init__(self, root, paths, profile, identity, source, title, on_close):
        self.paths, self.profile, self.identity, self.source = paths, profile, identity, source
        self.on_close = on_close
        self.playback = None
        self.position = load_position(paths, profile, identity)
        self.saved_position = self.position
        self.window = tk.Toplevel(root)
        self.window.title("Hörwerk – Wiedergabe")
        self.window.transient(root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(self.window, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=title, style="Section.TLabel", wraplength=400).pack(anchor="w")
        self.time = ttk.Label(body, text=clock(self.position), style="Muted.TLabel")
        self.time.pack(anchor="w", pady=(8, 0))
        controls = ttk.Frame(body)
        controls.pack(anchor="w", pady=(12, 0))
        ttk.Button(controls, text="−10 Sek.", command=lambda: self.seek(-10)).pack(side="left")
        self.toggle = ttk.Button(controls, text="Abspielen", command=self.toggle_playback,
                                 style="Accent.TButton")
        self.toggle.pack(side="left", padx=8)
        ttk.Button(controls, text="+30 Sek.", command=lambda: self.seek(30)).pack(side="left")
        ttk.Button(controls, text="Zum Anfang", command=lambda: self.seek(None)).pack(side="left", padx=(8, 0))
        self.message = ttk.Label(body, style="Muted.TLabel", wraplength=400)
        self.message.pack(anchor="w", pady=(8, 0))
        self.start()
        self.timer = self.window.after(250, self.tick)

    def start(self):
        try:
            self.playback = Playback(self.paths.bundled_program("ffmpeg"), self.source, self.position)
        except Exception:
            self.message.configure(text="Die Datei konnte nicht abgespielt werden. Prüfe Datei und Audioausgabe.")
            return
        self.message.configure(text="")
        self.toggle.configure(text="Pause")

    def bookmark(self):
        if self.position == self.saved_position:
            return
        try:
            save_position(self.paths, self.profile, self.identity, self.position)
            self.saved_position = self.position
        except (OSError, HoerwerkError):
            self.message.configure(text="Die Hörposition konnte nicht gespeichert werden.")

    def stop(self):
        if self.playback is not None:
            active, self.playback = self.playback, None
            active.close()
            self.position = active.position
        self.toggle.configure(text="Fortsetzen" if self.position else "Abspielen")

    def toggle_playback(self):
        if self.playback is None:
            self.start()
        else:
            self.stop()
            self.bookmark()

    def seek(self, delta):
        running = self.playback is not None
        self.stop()
        self.position = seek_position(self.position + delta) if delta is not None else 0.0
        self.bookmark()
        if running:
            self.start()
        else:
            self.toggle.configure(text="Fortsetzen" if self.position else "Abspielen")

    def tick(self):
        if self.playback is not None:
            self.position = self.playback.position
            if self.playback.finished.is_set():
                failed = self.playback.failed
                self.stop()
                if failed:
                    self.message.configure(text="Die Audiodatei konnte nicht vollständig gelesen werden.")
                else:
                    self.position = 0.0
                    self.toggle.configure(text="Abspielen")
                    self.message.configure(text="Ende erreicht.")
                self.bookmark()
            elif abs(self.position - self.saved_position) >= 10:
                self.bookmark()
        self.time.configure(text=clock(self.position))
        self.timer = self.window.after(250, self.tick)

    def close(self):
        self.window.after_cancel(self.timer)
        self.stop()
        self.bookmark()
        self.window.destroy()
        self.on_close()
