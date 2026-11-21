"""Deutsche Tkinter-Oberfläche für den lokalen Hörwerk-Kern."""

from __future__ import annotations

from dataclasses import dataclass
import json
import queue
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from aaxc import convert_aaxc
from batch import BatchResult, apply_selected, download_selected
from audio_formats import FORMATS
from catalog import FIELDS, corrections, export_catalog, save, tags
from cover_preview import CoverLoader, is_cover_url
from library_view import ORDERINGS, can_download, download_status, ordered_items
from media_tools import EXTENSIONS, audio_path, import_one, join, load_local, local_source, split
from player_window import PlayerWindow
from player_dock import PlayerDock
from account_window import open_account_window
from device_login import ACTIVATION_URL
import theme
from core import Core, HoerwerkError, InvalidBackendResult, PortablePaths
from jobs import JobController, JobCancelled, require_plain_account, stop_process
from login import readiness
from transfer_progress import DownloadProgress, Progress
from browser_login import BrowserLoginController, browser_environment
from web_player import audible_player_url, open_player_window

_ASIN = re.compile(r"[A-Z0-9]{10}\Z")
_HEX = re.compile(r"[0-9a-fA-F]{32}\Z")



def open_standard_browser(url):
    if sys.platform.startswith("linux"):
        subprocess.Popen(["xdg-open", url], shell=False, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=browser_environment())
    else:
        import webbrowser
        if not webbrowser.open(url, new=2):
            raise OSError("Standardbrowser konnte nicht geöffnet werden.")

def local_audio_files(paths: PortablePaths, profile: str, asin: str) -> list[Path]:
    """Liefert nur ein vollständiges, eindeutiges Ergebnis im eigenen Konto."""
    if not isinstance(asin, str) or not _ASIN.fullmatch(asin):
        raise InvalidBackendResult("Die ASIN ist ungültig.")
    require_plain_account(paths, profile)
    folder = paths.downloads(profile) / asin
    if folder.is_symlink():
        raise InvalidBackendResult("Der Downloadpfad darf keine Umleitung enthalten.")
    if not folder.exists():
        return []
    try:
        if folder.resolve(strict=True) != folder or folder.parent.resolve(strict=True) != folder.parent:
            raise InvalidBackendResult("Der Downloadpfad darf keine Umleitung enthalten.")
        audio = []
        for path in folder.glob(f"{asin}-*"):
            if path.is_symlink() or not path.is_file() or not path.stat().st_size:
                continue
            suffix = path.suffix.lower()
            if suffix in {".mp3", ".m4a"}:
                audio.append(path)
            elif suffix == ".aaxc" and _valid_voucher(path.with_suffix(".voucher"), asin):
                audio.append(path)
    except (OSError, RuntimeError):
        raise InvalidBackendResult("Der lokale Download ist nicht sicher prüfbar.") from None
    if len(audio) > 1:
        raise InvalidBackendResult("Mehrere Audiodateien ergeben keinen eindeutigen Download.")
    return audio


def _valid_voucher(path: Path, asin: str) -> bool:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
            return False
        with path.open("rb") as stream:
            raw = stream.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            return False
        data = json.loads(raw.decode("utf-8"))
        license_data = data.get("content_license") if isinstance(data, dict) else None
        response = license_data.get("license_response") if isinstance(license_data, dict) else None
        return bool(license_data.get("status_code") == "Granted"
                    and license_data.get("asin") == asin and isinstance(response, dict)
                    and isinstance(response.get("key"), str) and _HEX.fullmatch(response["key"])
                    and isinstance(response.get("iv"), str) and _HEX.fullmatch(response["iv"]))
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, RecursionError):
        return False


def login_terminal_argv(app_root: Path, profile: str, backend: Path, *,
                        platform: str | None = None, which=shutil.which,
                        executable: str | None = None, frozen: bool | None = None):
    """Baut nur für Terminals mit synchronem Lebenszyklus eine Argumentliste."""
    platform, executable = platform or sys.platform, executable or sys.executable
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if Path(executable).name.lower() in {"pythonw", "pythonw.exe"}:
        raise HoerwerkError("Die Anmeldung benötigt Python mit Konsolenunterstützung.")
    login = Path(__file__).with_name("login.py")
    if not Path(backend).is_absolute() or (not frozen and not login.is_file()):
        raise HoerwerkError("Der lokale Anmeldestarter ist nicht verfügbar.")
    command = ([executable, "--login", str(Path(app_root)), profile,
                "--backend", str(backend)] if frozen else
               [executable, str(login), str(Path(app_root)), profile,
                "--backend", str(backend)])
    if platform == "win32":
        return command, {"creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0x10)}
    for name in ("gnome-terminal", "konsole", "xterm"):
        terminal = which(name)
        if terminal:
            if name == "gnome-terminal": return [terminal, "--wait", "--", *command], {}
            if name == "konsole": return [terminal, "--nofork", "-e", *command], {}
            return [terminal, "-e", *command], {}
    raise HoerwerkError("Kein synchron unterstütztes lokales Terminal wurde gefunden.")


def run_login_process(ctx, argv, options, factory=subprocess.Popen):
    if ctx.cancelled.is_set():
        raise JobCancelled("Die Anmeldung wurde vor dem Start abgebrochen.")
    server_terminal = Path(argv[0]).name in {"gnome-terminal", "konsole"}
    try:
        process = factory(argv, shell=False, **options)
    except OSError:
        raise HoerwerkError("Das Anmeldeterminal konnte nicht gestartet werden.") from None
    try:
        while process.poll() is None:
            if ctx.cancelled.wait(0.1):
                if server_terminal:
                    # Der Client besitzt den Login-Child im Terminalserver nicht.
                    # Bis zum Ende des wartenden Clients weiter beaufsichtigen.
                    time.sleep(0.05)
                    continue
                stop_process(process)
                raise JobCancelled("Die Anmeldung wurde abgebrochen.")
    except JobCancelled:
        raise
    except BaseException:
        stop_process(process)
        if server_terminal:
            raise HoerwerkError("Anmeldestatus unklar. Bitte das separate Terminal prüfen und beenden.") from None
        raise
    if ctx.cancelled.is_set():
        raise JobCancelled("Die Anmeldung wurde abgebrochen.")
    if process.returncode:
        raise HoerwerkError("Das Anmeldeterminal wurde nicht erfolgreich beendet.")
    return readiness(ctx.paths, ctx.profile)


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    already_present: bool = False


def completed_download(ctx, report_progress=None):
    """Startet nötigenfalls Backend-Reparatur und prüft danach das Gesamtresultat."""
    existing = local_audio_files(ctx.paths, ctx.profile, ctx.asin)
    if existing:
        return DownloadResult(existing[0], already_present=True)
    try:
        progress = DownloadProgress((ctx.asin,), report_progress) if report_progress else None
        core = ctx.core(download_progress=progress.transferred) if progress else ctx.core()
        core.download(ctx.profile, ctx.asin)
    except InvalidBackendResult:
        existing = local_audio_files(ctx.paths, ctx.profile, ctx.asin)
        if len(existing) == 1:
            return DownloadResult(existing[0])
        raise
    existing = local_audio_files(ctx.paths, ctx.profile, ctx.asin)
    if len(existing) != 1:
        raise InvalidBackendResult("Der Download ist nicht vollständig und eindeutig.")
    return DownloadResult(existing[0])


class OperationMeter:
    def __init__(self, parent, name):
        self.frame = ttk.Frame(parent)
        self.name = name
        self.label = ttk.Label(self.frame, text=name, style="Muted.TLabel")
        self.label.pack(anchor="w")
        self.bar = ttk.Progressbar(self.frame, mode="determinate")
        self.bar.pack(fill="x")
        self.indeterminate = False

    def show(self, progress):
        value, detail = progress.value, progress.detail
        if self.indeterminate != (value is None):
            self.indeterminate = value is None
            if self.indeterminate:
                self.bar.configure(mode="indeterminate", value=0)
                self.bar.start(15)
            else:
                self.bar.stop()
                self.bar.configure(mode="determinate")
        if value is not None:
            self.bar.configure(value=value)
        label = " · ".join(part for part in (
            self.name, f"{value} %" if value is not None and not (value == 0 and not detail) else "", detail
        ) if part)
        self.label.configure(text=label)


class App:
    def __init__(self, root: tk.Tk, app_root: Path, *, data_root: Path | None = None):
        self.root, self.paths = root, PortablePaths(app_root, data_root)
        self.jobs = JobController(self.paths)
        self.login_controller = self.login_dialog = None
        self.profile, self.status = tk.StringVar(value="standard"), tk.StringVar(value="Bereit.")
        self.library, self.closing, self.active_kind = {}, False, None
        self.search = tk.StringVar()
        self.order = tk.StringVar(value="Titel A–Z")
        self.local_sizes = {}
        self.remote_items = []
        self.output_format = tk.StringVar(value="MP3")
        self.cover_loader = CoverLoader()
        self.cover_url = self.cover_image = None
        self.busy = self.cancelling = False
        self.player = None
        self.player_dock = None
        root.title("Hörwerk 0.2.23")
        root.geometry("1000x680")
        root.minsize(720, 500)
        theme.apply(root)
        self._build(); root.protocol("WM_DELETE_WINDOW", self.close); root.after(80, self.poll)
        self.profile.trace_add("write", self.profile_changed)
        self.show_saved_library()

    def _build(self):
        body = self.library_view = ttk.Frame(self.root, padding=(24, 18))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(4, weight=1)

        header = ttk.Frame(body)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.file_tools = ttk.Menubutton(header, text="Datei und Kapitel")
        commands = tk.Menu(self.file_tools, tearoff=False)
        commands.add_command(label="Audiodateien importieren …", command=self.import_local)
        commands.add_command(label="Audiodatei abspielen …", command=self.play_file)
        commands.add_command(label="Audible in der Seitenleiste testen …", command=self.try_docked_player)
        commands.add_command(label="Audible im Playerfenster testen …", command=self.try_web_player)
        commands.add_command(label="Metadaten bearbeiten …", command=self.edit_metadata)
        commands.add_command(label="Bibliothek als HTML sichern …", command=lambda: self.save_catalog("html"))
        commands.add_command(label="Bibliothek als CSV sichern …", command=lambda: self.save_catalog("csv"))
        commands.add_separator()
        commands.add_command(label="Nach Kapiteln teilen …", command=self.split_local)
        commands.add_command(label="Dateien zusammenfügen …", command=self.join_local)
        self.file_tools.configure(menu=commands)
        self.file_tools.pack(side="right")
        ttk.Label(header, text="GsG e.V.", style="Brand.TLabel").pack(anchor="w")
        ttk.Label(header, text="Hörwerk", style="Title.TLabel").pack(anchor="w")

        account = ttk.Frame(body, style="Panel.TFrame", padding=(14, 10))
        account.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        ttk.Label(account, text="Profil", style="Panel.TLabel").pack(side="left", padx=(0, 10))
        self.profile_entry = ttk.Entry(account, textvariable=self.profile, width=17)
        self.profile_entry.pack(side="left", padx=(0, 12))
        self.ready_label = ttk.Label(account, text="Anmeldung: nicht geprüft", style="PanelMuted.TLabel")
        self.ready_label.pack(side="left", fill="x", expand=True)
        self.login_button = ttk.Button(account, text="Bei Audible anmelden", command=self.login, style="Accent.TButton")
        self.login_button.pack(side="right")

        toolbar = ttk.Frame(body)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="Bibliothek", style="Section.TLabel").pack(side="left")
        self.count = ttk.Label(toolbar, style="Muted.TLabel")
        self.count.pack(side="left", padx=12)
        self.refresh_button = ttk.Button(toolbar, text="Aktualisieren", command=self.refresh)
        self.refresh_button.pack(side="right")
        self.search_entry = ttk.Entry(toolbar, textvariable=self.search, width=20)
        self.search_entry.pack(side="right", padx=(8, 10))
        ttk.Label(toolbar, text="Suchen", style="Muted.TLabel").pack(side="right")
        self.search.trace_add("write", self.render_library)

        sorting = ttk.Frame(body)
        sorting.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(sorting, text="Sortierung", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.order_picker = ttk.Combobox(sorting, textvariable=self.order, values=tuple(ORDERINGS),
                                         state="readonly", width=24)
        self.order_picker.pack(side="left")
        self.selection_count = ttk.Label(sorting, style="Muted.TLabel")
        self.selection_count.pack(side="right")
        self.order.trace_add("write", self.render_library)

        library_area = ttk.Frame(body)
        library_area.grid(row=4, column=0, sticky="nsew")
        preview = self.preview = ttk.Frame(library_area, style="Panel.TFrame", padding=10, width=188)
        preview.pack(side="right", fill="y", padx=(8, 0))
        preview.pack_propagate(False)
        self.cover_label = ttk.Label(preview, text="Titelbild", style="PanelMuted.TLabel", anchor="center")
        self.cover_label.pack(fill="x", pady=(18, 0))
        self.cover_title = ttk.Label(preview, wraplength=165, style="Panel.TLabel", justify="center", anchor="center")
        self.cover_title.pack(fill="x", pady=(8, 0))
        table = ttk.Frame(library_area)
        table.pack(side="left", fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("title", "author", "duration", "source", "download"),
                                 show="headings", selectmode="extended")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        for col, label, width in (("title", "Titel", 360), ("author", "Autor", 230), ("duration", "Dauer", 82), ("source", "Quelle", 70), ("download", "Download", 115)):
            self.tree.heading(col, text=label)
            self.tree.column(col, width=width, minwidth=62, stretch=col not in {"duration", "source", "download"},
                             anchor="e" if col == "duration" else "w")
        self.tree.tag_configure("even", background=theme.FIELD)
        self.tree.tag_configure("odd", background="#1a3039")
        self.tree.bind("<<TreeviewSelect>>", self.selection_changed)
        self.tree.bind("<Control-a>", self.select_visible)

        actions = ttk.Frame(body)
        actions.grid(row=5, column=0, sticky="ew", pady=(14, 8))
        self.cancel_button = ttk.Button(actions, text="Abbrechen", command=self.cancel_job, state="disabled")
        self.cancel_button.pack(side="left")
        self.play_button = ttk.Button(actions, text="Abspielen", command=self.play_selection)
        self.play_button.pack(side="left", padx=(8, 0))
        self.export_button = ttk.Button(actions, text="Exportieren", command=self.export)
        self.export_button.pack(side="right")
        self.format_picker = ttk.Combobox(actions, textvariable=self.output_format,
                                         values=tuple(FORMATS), state="readonly", width=7)
        self.format_picker.pack(side="right", padx=(0, 6))
        self.download_button = ttk.Button(actions, text="Herunterladen", command=self.download, style="Accent.TButton")
        self.download_button.pack(side="right", padx=(0, 8))
        meters = ttk.Frame(body)
        meters.grid(row=6, column=0, sticky="ew", pady=(2, 8))
        meters.columnconfigure((0, 1), weight=1, uniform="meters")
        download = OperationMeter(meters, "Download")
        conversion = OperationMeter(meters, "Umwandlung")
        download.frame.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        conversion.frame.grid(row=0, column=1, sticky="ew", padx=(8, 0))
        self.meters = {"download": download, "download_group": download, "export": conversion}
        self.progress = ttk.Progressbar(meters, mode="indeterminate")
        self.progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        self.progress.grid_remove()
        status = ttk.Label(body, textvariable=self.status, style="Muted.TLabel")
        status.grid(row=7, column=0, sticky="ew")
        body.bind("<Configure>", lambda event: status.configure(wraplength=max(200, event.width - 24)))
        self.render_library()

    def profile_changed(self, *_):
        self.ready_label.configure(text="Anmeldung: nicht geprüft")
        self.show_saved_library()

    def show_saved_library(self):
        try:
            cached = Core(self.paths).saved_library(self.profile.get())
        except HoerwerkError:
            cached = []
        self.show_library(cached)
        if cached:
            self.status.set("Bitte „Aktualisieren“, um die Downloadfreigaben abzurufen."
                            if any("download_allowed" not in item for item in cached)
                            else "Gespeicherte Audible-Bibliothek angezeigt.")
        else:
            self.status.set("Bereit.")

    def render_library(self, *_):
        if not hasattr(self, "tree"):
            return
        selected = self.tree.selection()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        term = self.search.get().strip()
        rows = ordered_items(self.library.values(), self.order.get(), term, self.local_sizes)
        for index, item in enumerate(rows):
            duration = item.get("runtime_length_min")
            self.tree.insert("", "end", iid=item["asin"], tags=("odd" if index % 2 else "even",),
                             values=(item["title"], item.get("authors") or "", f"{duration} min" if duration else "",
                                     "Lokal" if item.get("kind") == "local" else "Audible", download_status(item)))
        self.tree.selection_set([asin for asin in selected if self.tree.exists(asin)])
        visible = len(rows)
        self.count.configure(text=f"{visible} von {len(self.library)} Titeln" if term else f"{visible} Titel")
        self.selection_changed()

    def select_visible(self, _event=None):
        self.tree.selection_set(self.tree.get_children())
        return "break"

    def selection_changed(self, *_):
        self.sync_actions()
        selection = self.tree.selection()
        item = self.library.get(selection[0], {}) if len(selection) == 1 else {}
        url = item.get("cover_url")
        url = url if is_cover_url(url) else None
        if url != self.cover_url:
            self.cover_url = url
            self.cover_image = None
            self.cover_label.configure(image="", text="Titelbild")
            self.cover_loader.request(url)
        self.cover_title.configure(text=item.get("title", ""))

    def poll_cover(self):
        while not self.cover_loader.results.empty():
            url, image = self.cover_loader.results.get_nowait()
            if url == self.cover_url and image is not None:
                try:
                    from PIL import ImageTk
                    self.cover_image = ImageTk.PhotoImage(image, master=self.root)
                    self.cover_label.configure(image=self.cover_image, text="")
                except (ImportError, OSError, RuntimeError, tk.TclError):
                    self.cover_image = None

    def sync_actions(self, *_):
        count = len(self.tree.selection())
        self.selection_count.configure(text=f"{count} ausgewählt" if count else "")
        self.play_button.configure(state="normal" if count == 1 else "disabled")
        downloads = sum(can_download(self.library.get(asin, {})) for asin in self.tree.selection())
        if count and downloads != count:
            self.selection_count.configure(text=f"{count} ausgewählt · {downloads} zum Download freigegeben")
        self.download_button.configure(state="normal" if not self.busy and downloads else "disabled")
        self.export_button.configure(state="normal" if not self.busy and count else "disabled")

    def selected_many(self):
        selected = self.tree.selection()
        if not selected:
            raise HoerwerkError("Bitte zuerst mindestens einen Titel auswählen.")
        return [self.library[asin] for asin in selected]

    def start(self, kind, asin, operation):
        try:
            if self.login_controller is not None:
                raise HoerwerkError("Eine Anmeldung läuft bereits.")
            self.jobs.start(kind, self.profile.get(), asin, operation)
        except HoerwerkError as exc:
            messagebox.showerror("Hörwerk", str(exc))
            return
        self.active_kind = kind
        self.set_busy(True)
        self.status.set({
            "download": "Download wird gestartet …",
            "download_group": "Downloads werden gestartet …",
            "export": "Umwandlung wird gestartet …",
            "library": "Bibliothek wird aktualisiert …",
        }.get(kind, "Vorgang läuft …"))
        meter = self.meters.get(kind)
        if meter is not None:
            meter.show(Progress(None, "Wird vorbereitet …"))

    def refresh(self): self.start("library", None, lambda ctx: ctx.core().refresh_library(ctx.profile))

    def download(self):
        try:
            items = self.selected_many()
        except HoerwerkError as exc:
            messagebox.showinfo("Hörwerk", str(exc))
            return
        items = [item for item in items if can_download(item)]
        if not items:
            return
        if len(items) == 1:
            self.start("download", items[0]["asin"], lambda ctx: completed_download(ctx, ctx.report_progress))
            return
        group = [(item["asin"], item["title"]) for item in items]
        self.start("download_group", None,
                   lambda ctx: download_selected(ctx, group, local_audio_files, _valid_voucher,
                                                 report_progress=ctx.report_progress))

    def export(self):
        try:
            items = self.selected_many()
        except HoerwerkError as exc:
            messagebox.showinfo("Hörwerk", str(exc))
            return
        found, missing = [], []
        for item in items:
            title, asin = item["title"], item["asin"]
            try:
                if item.get("kind") == "local":
                    audio = [local_source(self.paths, item)]
                else:
                    audio = local_audio_files(self.paths, self.profile.get(), asin)
            except HoerwerkError:
                audio = []
            if len(audio) == 1:
                found.append((asin, title, audio[0], tags(item)))
            else:
                missing.append((title, "Kein vollständiger Download vorhanden."))
        if not found:
            messagebox.showinfo("Hörwerk", "Bitte zuerst mindestens einen der ausgewählten Titel herunterladen.")
            return
        output_format = self.output_format.get()
        destination = filedialog.askdirectory(title=f"Zielordner für {output_format} wählen")
        if not destination:
            return

        index = -1

        def convert(ctx, entry):
            nonlocal index
            index += 1
            _, title, source, metadata = entry

            def report(progress):
                value = None if progress.value is None else (index * 100 + progress.value) // len(found)
                label = f"Titel {index + 1}/{len(found)} · {progress.detail}" if progress.detail else f"Titel {index + 1}/{len(found)}"
                ctx.report_progress(Progress(value, label))

            report(Progress(None))
            core = ctx.core(conversion_progress=report)
            if source.suffix.lower() == ".aaxc":
                return convert_aaxc(core, ctx.profile, source, Path(destination), title,
                                    cancelled=ctx.cancelled.is_set, runner=core.runner,
                                    output_format=output_format, metadata=metadata)
            return core.convert_playable(source, Path(destination), title, output_format, metadata=metadata)

        if any(path.suffix.lower() == ".aaxc" for _, _, path, _ in found):
            if not messagebox.askokcancel("AAXC-Export", "Während des AAXC-Exports kann der lokale Dateischlüssel "
                                          "für andere Prozesse mit ausreichenden Rechten sichtbar sein. Fortfahren?"):
                return

        def operation(ctx):
            result = apply_selected(ctx, found, convert)
            return BatchResult(result.files, tuple(missing) + result.failures)
        self.start("export", None, operation)

    def edit_metadata(self):
        try:
            entries = self.selected_many()
            if len(entries) != 1:
                raise HoerwerkError("Bitte genau einen Titel auswählen.")
            item = entries[0]
            values = MetadataDialog(self.root, item).result
            if values is not None:
                save(self.paths, self.profile.get(), item, values)
                self.show_library(self.remote_items)
                self.status.set("Metadaten gespeichert.")
        except (HoerwerkError, OSError) as exc:
            messagebox.showerror("Metadaten", str(exc), parent=self.root)

    def save_catalog(self, extension):
        if not self.library:
            messagebox.showinfo("Bibliothek", "Die Bibliothek enthält keine Titel.", parent=self.root)
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="Bibliothek speichern",
                                             defaultextension="." + extension,
                                             filetypes=[(extension.upper(), "*." + extension)],
                                             initialfile="Hoerwerk-Bibliothek." + extension)
        if target:
            entries = ordered_items(self.library.values(), self.order.get(), sizes=self.local_sizes)
            self.start("catalog", None, lambda _ctx: export_catalog(entries, target))

    @staticmethod
    def _audio_filters():
        return [("Audiodateien", " ".join("*" + extension for extension in sorted(EXTENSIONS)))]

    def open_player(self, source, identity, title):
        profile = self.paths.account(self.profile.get()).name
        if self.player is not None:
            self.player.close()
        self.player = PlayerWindow(self.root, self.paths, profile, identity, source, title,
                                   lambda: setattr(self, "player", None))

    def play_file(self):
        filename = filedialog.askopenfilename(title="Audiodatei abspielen",
                                              filetypes=self._audio_filters(), parent=self.root)
        if filename:
            try:
                source = audio_path(filename)
                stat = source.stat()
                self.open_player(source, f"file:{source}:{stat.st_size}:{stat.st_mtime_ns}", source.stem)
            except (HoerwerkError, OSError):
                messagebox.showerror("Wiedergabe", "Die Audiodatei oder das Profil ist nicht verfügbar.",
                                     parent=self.root)

    def play_selection(self):
        try:
            items = self.selected_many()
            if len(items) != 1:
                return
            item = items[0]
            if item.get("kind") == "local":
                source = local_source(self.paths, item)
            else:
                files = local_audio_files(self.paths, self.profile.get(), item["asin"])
                if not files or files[0].suffix.lower() == ".aaxc":
                    open_standard_browser(audible_player_url(item["asin"]))
                    self.status.set("Audible-Webplayer im Standardbrowser angefordert.")
                    return
                source = files[0]
            self.open_player(source, "title:" + item["asin"], item["title"])
        except (HoerwerkError, OSError) as exc:
            messagebox.showinfo("Wiedergabe", str(exc), parent=self.root)

    def try_web_player(self):
        try:
            items = self.selected_many()
            if len(items) != 1 or items[0].get("kind") == "local":
                raise HoerwerkError("Wähle genau einen Audible-Titel aus.")
            open_player_window(self.paths, self.profile.get(), asin=items[0]["asin"])
            self.status.set("Audible im Testfenster angefordert. Melde dich dort bei Bedarf an.")
        except (HoerwerkError, OSError) as exc:
            messagebox.showinfo("Audible-Player", str(exc), parent=self.root)

    def try_docked_player(self):
        dock = None
        try:
            if self.player_dock is not None:
                raise HoerwerkError("Löse zuerst den aktuellen Player über „Fenster lösen“.")
            items = self.selected_many()
            if len(items) != 1 or items[0].get("kind") == "local":
                raise HoerwerkError("Wähle genau einen Audible-Titel aus.")
            container = tk.Frame(self.preview, background=theme.FIELD)
            try:
                dock = PlayerDock(self.root, container, self.dock_status)
            except HoerwerkError:
                container.destroy()
                raise
            self.player_dock = dock
            self.cover_label.pack_forget()
            self.cover_title.pack_forget()
            self.preview.configure(width=440)
            self.detach_player_button = ttk.Button(self.preview, text="Fenster lösen",
                                                   command=self.close_dock)
            self.detach_player_button.pack(anchor="e", pady=(0, 6))
            container.pack(fill="both", expand=True)
            container.bind("<Configure>", dock.resize)
            profile_dir = open_player_window(self.paths, self.profile.get(), asin=items[0]["asin"], x11=True)
            dock.start(profile_dir)
            self.status.set("Audible-Player wird für die Seitenleiste geöffnet …")
        except (HoerwerkError, OSError) as exc:
            if dock is not None:
                self.close_dock()
            messagebox.showinfo("Audible-Player", str(exc), parent=self.root)

    def dock_status(self, message, *, failed=False):
        if failed and self.player_dock is not None:
            self.close_dock()
        self.status.set(message)

    def close_dock(self):
        if self.player_dock is None:
            return
        dock, self.player_dock = self.player_dock, None
        dock.close()
        dock.container.destroy()
        self.detach_player_button.destroy()
        self.preview.configure(width=188)
        self.cover_label.pack(fill="x", pady=(18, 0))
        self.cover_title.pack(fill="x", pady=(8, 0))

    def import_local(self):
        if self.busy:
            return
        filenames = filedialog.askopenfilenames(title="Lokale Hörbücher importieren",
                                               filetypes=self._audio_filters(), parent=self.root)
        if not filenames:
            return
        entries = [(None, Path(value).name, value) for value in filenames]
        self.start("import", None, lambda ctx: apply_selected(
            ctx, entries, lambda context, entry: local_source(context.paths, import_one(context, entry[2]))))

    def split_local(self):
        if self.busy:
            return
        chosen = self.tree.selection()
        item = self.library.get(chosen[0], {}) if len(chosen) == 1 else {}
        try:
            source = local_source(self.paths, item) if item.get("kind") == "local" else None
        except HoerwerkError as exc:
            messagebox.showerror("Kapitel teilen", str(exc), parent=self.root)
            return
        if source is None:
            source = filedialog.askopenfilename(title="Audiodatei mit Kapitelmarken wählen",
                                               filetypes=self._audio_filters(), parent=self.root)
        if not source:
            return
        destination = filedialog.askdirectory(title="Ordner für die Kapitel wählen", parent=self.root)
        if destination:
            output_format = self.output_format.get()
            self.start("split", None, lambda ctx: split(ctx, source, destination, output_format))

    def join_local(self):
        if self.busy:
            return
        values = filedialog.askopenfilenames(title="Audiodateien zum Zusammenfügen wählen",
                                             filetypes=self._audio_filters(), parent=self.root)
        if len(values) < 2:
            if values:
                messagebox.showinfo("Zusammenfügen", "Bitte mindestens zwei Dateien auswählen.", parent=self.root)
            return
        order = JoinOrderDialog(self.root, values).result
        if not order:
            return
        title = simpledialog.askstring("Zusammenfügen", "Titel der neuen Audiodatei:",
                                       initialvalue="Zusammengefügtes Hörbuch", parent=self.root)
        if not title or not title.strip():
            return
        destination = filedialog.askdirectory(title="Zielordner wählen", parent=self.root)
        if destination:
            output_format = self.output_format.get()
            self.start("join", None, lambda ctx: join(ctx, order, destination, title.strip(), output_format))

    def login(self):
        try:
            if self.jobs.busy or self.login_controller is not None:
                raise HoerwerkError("Es läuft bereits ein Vorgang.")
            controller = BrowserLoginController(self.paths, self.profile.get(), device_code=True)
            controller.start()
            dialog = BrowserLoginDialog(self, controller)
            self.login_controller, self.login_dialog = controller, dialog
            self.active_kind = "browser_login"
            self.set_busy(True)
            self.library_view.pack_forget()
            dialog.window.pack(fill="both", expand=True)
            dialog.cancel_button.focus_set()
            dialog.poll()
        except HoerwerkError as exc: messagebox.showerror("Anmeldung", str(exc)); return

    def login_finished(self, kind, value, profile):
        self.login_controller = self.login_dialog = None; self.active_kind = None
        self.set_busy(False)
        if self.closing:
            return
        self.library_view.pack(fill="both", expand=True)
        try: current = self.paths.account(self.profile.get()).name
        except HoerwerkError: current = ""
        if kind == "success" and current == profile:
            self.ready_label.configure(text="Bei Audible angemeldet")
            self.status.set("Anmeldung abgeschlossen. Mit „Aktualisieren“ lädst du deine Bibliothek.")
            self.refresh_button.focus_set()
        else:
            self.status.set(value if kind == "error" else "Anmeldung abgebrochen.")
            self.login_button.focus_set()

    def set_busy(self, busy):
        if self.busy != busy:
            self.busy = busy
            if not busy:
                self.cancelling = False
            for widget in (self.profile_entry, self.login_button, self.refresh_button, self.search_entry, self.file_tools):
                widget.configure(state="disabled" if busy else "normal")
            self.cancel_button.configure(state="normal" if busy else "disabled")
            self.order_picker.configure(state="disabled" if busy else "readonly")
            self.format_picker.configure(state="disabled" if busy else "readonly")
            if busy and self.active_kind not in self.meters:
                self.progress.grid()
                self.progress.start(12)
            else:
                self.progress.stop()
                self.progress.grid_remove()
        self.sync_actions()

    @staticmethod
    def update_meter(meter, event):
        if event.state == "progress":
            meter.show(event.result)
        elif event.state == "success":
            result = event.result
            if isinstance(result, BatchResult) and result.failures:
                total = len(result.files) + len(result.failures)
                meter.show(Progress(100 * len(result.files) // total,
                                    f"{len(result.files)}/{total} erfolgreich"))
            elif isinstance(result, DownloadResult) and result.already_present:
                meter.show(Progress(100, "Bereits lokal"))
            elif isinstance(result, BatchResult) and result.already_present:
                meter.show(Progress(100, f"{result.already_present} bereits lokal"))
            else:
                meter.show(Progress(100, "Fertig"))
        else:
            meter.show(Progress(0, "Abgebrochen" if event.state == "cancelled" else "Fehlgeschlagen"))

    def poll(self):
        try:
            self.poll_jobs()
            self.poll_cover()
        finally:
            if self.closing and not self.jobs.busy and self.login_controller is None:
                self.root.destroy()
            else:
                self.root.after(80, self.poll)

    def poll_jobs(self):
        while not self.jobs.events.empty():
            event = self.jobs.events.get_nowait()
            if event.state == "running" or (self.cancelling and event.state == "progress"):
                continue
            meter = self.meters.get(event.kind)
            if meter is not None:
                self.update_meter(meter, event)
            if event.state == "progress":
                continue
            self.status.set(event.message)
            try: current = self.paths.account(self.profile.get()).name
            except HoerwerkError: current = ""
            if event.state == "success" and event.profile == current:
                if event.kind == "library":
                    self.ready_label.configure(text="Mit Audible verbunden")
                    self.show_library(event.result)
                    self.status.set("Bibliothek aktualisiert.")
                elif isinstance(event.result, DownloadResult):
                    self.update_sizes([event.result.path])
                    self.status.set("Datei bereits lokal vorhanden." if event.result.already_present
                                    else f"Download fertig · {event.result.path.name}")
                elif isinstance(event.result, BatchResult):
                    result = event.result
                    if event.kind == "download_group":
                        self.update_sizes(result.files)
                    if event.kind == "import":
                        self.show_library(self.remote_items)
                    verb = {"download_group": "heruntergeladen", "export": "exportiert",
                            "import": "importiert"}.get(event.kind, "verarbeitet")
                    message = f"{len(result.files) - result.already_present} Titel {verb}"
                    if result.already_present:
                        message += f", {result.already_present} bereits lokal"
                    if result.failures:
                        message += f", {len(result.failures)} nicht abgeschlossen"
                        detail = "\n".join(f"{title}: {reason}" for title, reason in result.failures[:8])
                        messagebox.showwarning("Hörwerk", detail, parent=self.root)
                    self.status.set(message + ".")
                elif event.kind == "login": self.ready_label.configure(text="Anmeldung: lokal eingerichtet, nicht online geprüft")
                elif isinstance(event.result, Path):
                    self.status.set(f"Fertig: {event.result}")
        self.set_busy(self.jobs.busy or self.login_controller is not None)

    def show_library(self, items):
        self.remote_items = list(items)
        self.library = {item["asin"]: item for item in self.remote_items}
        try:
            self.library.update({item["asin"]: item for item in load_local(self.paths)})
        except (HoerwerkError, OSError):
            pass
        self.local_sizes.clear()
        profile = self.profile.get()
        self.library = {asin: {**item, **corrections(self.paths, profile, item)}
                        for asin, item in self.library.items()}
        for asin, item in self.library.items():
            try:
                if item.get("kind") == "local":
                    self.local_sizes[asin] = local_source(self.paths, item).stat().st_size
                    continue
                folder = self.paths.downloads(profile) / asin
                if not folder.is_dir() or folder.is_symlink() or folder.resolve() != folder:
                    continue
                for path in folder.glob(f"{asin}-*"):
                    if path.suffix.lower() in {".mp3", ".m4a", ".aaxc"} and not path.is_symlink() and path.is_file():
                        self.local_sizes[asin] = max(self.local_sizes.get(asin, 0), path.stat().st_size)
            except (HoerwerkError, OSError):
                continue
        self.render_library()

    def update_sizes(self, files):
        for path in files:
            try:
                self.local_sizes[path.parent.name] = path.stat().st_size
            except OSError:
                continue
        if ORDERINGS[self.order.get()][0] == "size":
            self.render_library()

    def cancel_job(self):
        if self.login_dialog is not None:
            self.login_dialog.cancel()
        elif self.jobs.busy:
            self.jobs.cancel()
            self.cancelling = True
            self.cancel_button.configure(state="disabled")
            self.status.set("Abbruch läuft …")
            meter = self.meters.get(self.active_kind)
            if meter is not None:
                meter.show(Progress(0, "Abbruch läuft …"))
        if self.active_kind == "login":
            self.status.set("Anmeldung bitte auch im separaten Terminal beenden; das Fenster wartet darauf.")

    def close(self):
        self.close_dock()
        if self.player is not None:
            self.player.close()
        self.cover_loader.closed.set()
        if self.jobs.busy or self.login_controller is not None:
            self.closing = True; self.cancel_job()
            if self.active_kind != "login": self.status.set("Abbruch wird abgewartet …")
        else: self.root.destroy()



class MetadataDialog(simpledialog.Dialog):
    def __init__(self, parent, item):
        self.item = item
        super().__init__(parent, "Metadaten bearbeiten")

    def body(self, master):
        self.entries = {}
        for row, (key, text) in enumerate(FIELDS.items()):
            ttk.Label(master, text=text).grid(row=row, column=0, sticky="w", padx=8, pady=4)
            entry = ttk.Entry(master, width=44)
            entry.insert(0, str(self.item.get(key) or ""))
            entry.grid(row=row, column=1, sticky="ew", pady=4)
            self.entries[key] = entry
        return self.entries["title"]

    def validate(self):
        if self.entries["title"].get().strip():
            return True
        messagebox.showinfo("Metadaten", "Bitte einen Titel eingeben.", parent=self)
        return False

    def apply(self):
        self.result = {key: entry.get() for key, entry in self.entries.items()}


class JoinOrderDialog(simpledialog.Dialog):
    def __init__(self, parent, values):
        self.values = sorted(values, key=lambda value: [
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in re.split(r"(\d+)", Path(value).name)])
        super().__init__(parent, "Abspielreihenfolge")

    def body(self, master):
        ttk.Label(master, text="Die Dateien werden von oben nach unten zusammengefügt.").pack(anchor="w")
        self.listbox = tk.Listbox(master, height=min(12, len(self.values)), width=64,
                                  bg=theme.FIELD, fg=theme.TEXT, selectbackground=theme.BORDER,
                                  exportselection=False)
        self.listbox.pack(fill="both", expand=True, pady=8)
        actions = ttk.Frame(master)
        actions.pack(fill="x")
        ttk.Button(actions, text="Nach oben", command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(actions, text="Nach unten", command=lambda: self.move(1)).pack(side="left", padx=6)
        self.render(0)
        return self.listbox

    def render(self, selected):
        self.listbox.delete(0, "end")
        for value in self.values:
            self.listbox.insert("end", Path(value).name)
        self.listbox.selection_set(selected)
        self.listbox.see(selected)

    def move(self, direction):
        selected = self.listbox.curselection()
        if not selected:
            return
        old = selected[0]
        new = old + direction
        if 0 <= new < len(self.values):
            self.values[new], self.values[old] = self.values[old], self.values[new]
            self.render(new)

    def apply(self):
        self.result = tuple(self.values)

def main():
    root = tk.Tk(); App(root, Path(__file__).resolve().parent); root.mainloop()


class BrowserLoginDialog:
    def __init__(self, app, controller):
        self.app, self.controller, self.profile = app, controller, controller.profile
        self.window = ttk.Frame(app.root, padding=24)
        self.url = ""
        self.done = False
        self.manual_open = False
        self.stop = threading.Event()
        self.browser_events = queue.Queue()
        self.finished_event = None

        content = ttk.Frame(self.window, padding=22)
        content.pack(fill="both", expand=True)
        ttk.Label(content, text="Bei Audible anmelden", style="Section.TLabel").pack(anchor="w")
        self.info = tk.StringVar(value="Der Amazon-Code wird angefordert …")
        ttk.Label(content, textvariable=self.info, wraplength=550, justify="left").pack(anchor="w", pady=(8, 14))
        self.code_panel = ttk.Frame(content, style="Panel.TFrame", padding=12)
        self.player_login = ttk.Button(content, text="Amazon im Playerfenster öffnen (Test)",
                                       command=self.copy_and_open_player)
        self.device_value = tk.StringVar()
        ttk.Label(self.code_panel, textvariable=self.device_value,
                  style="Panel.TLabel", font=("TkDefaultFont", 20, "bold")).pack(side="left", padx=(0, 16))
        ttk.Button(self.code_panel, text="Code kopieren", command=self.copy_code).pack(side="right")
        self.fallback = ttk.Frame(content)
        self.callback = tk.StringVar()
        manual_entry = ttk.Entry(self.fallback, textvariable=self.callback, width=43)
        manual_entry.pack(fill="x")
        fallback_actions = ttk.Frame(self.fallback)
        fallback_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(fallback_actions, text="Browser öffnen", command=self.open_manual).pack(side="left")
        ttk.Button(fallback_actions, text="Adresse übernehmen", command=self.paste).pack(side="right")
        self.fallback_entry_hint = ttk.Label(self.fallback, text="Eingefügte Adresse mit Enter bestätigen.", style="Muted.TLabel")
        self.fallback_entry_hint.pack(anchor="w", pady=(5, 0))
        manual_entry.bind("<Return>", lambda _: self.submit(self.callback.get()))
        actions = ttk.Frame(content)
        actions.pack(fill="x", pady=(12, 0))
        self.manual_button = ttk.Button(actions, text="Im Standardbrowser öffnen",
                                        command=self.manual, state="disabled")
        self.manual_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Abbrechen", command=self.cancel)
        self.cancel_button.pack(side="right")
        self.actions = actions

    def copy_code(self):
        self.window.clipboard_clear()
        self.window.clipboard_append(self.device_value.get())

    def show_device_code(self, value):
        self.url = ACTIVATION_URL
        self.device_value.set(value)
        self.info.set(
            "1. Klicke auf „Code kopieren und Amazon öffnen“.\n\n"
            "2. Melde dich im Browser bei Amazon an. Falls Amazon einen QR-Code zeigt, "
            "scanne ihn zuerst mit dem Handy. Füge danach den Hörwerk-Code bei Amazon ein "
            "(Strg+V) und bestätige.\n\n"
            "3. Klicke in der Taskleiste wieder auf Hörwerk. Nach der Bestätigung erscheint die Bibliothek."
        )
        self.code_panel.pack(fill="x", before=self.actions)
        self.player_login.pack(anchor="w", pady=(8, 0), before=self.actions)
        self.manual_button.configure(text="Code kopieren und Amazon öffnen",
                                     command=self.copy_and_open_amazon, state="normal")
        self.manual_button.focus_set()

    def copy_and_open_amazon(self):
        if self.stop.is_set():
            return
        try:
            self.copy_code()
        except tk.TclError:
            # The visible device code can still be entered by hand.
            pass
        if self.open_manual():
            self.manual_button.configure(text="Code kopieren und Amazon erneut öffnen")

    def copy_and_open_player(self):
        if self.stop.is_set():
            return
        try:
            self.copy_code()
            open_player_window(self.app.paths, self.profile)
        except (HoerwerkError, OSError, tk.TclError) as exc:
            messagebox.showinfo("Audible-Player", str(exc), parent=self.app.root)

    def open_manual(self):
        if not self.url:
            return False
        try:
            open_standard_browser(self.url)
        except OSError:
            messagebox.showerror("Anmeldung", "Der Standardbrowser konnte nicht geöffnet werden.", parent=self.app.root)
            return False
        return True

    def paste(self):
        try:
            self.submit(self.window.clipboard_get())
        except tk.TclError:
            messagebox.showinfo("Anmeldung", "Die Zwischenablage enthält keinen Text.", parent=self.app.root)

    def submit(self, callback):
        if self.done:
            return
        try:
            self.controller.submit_callback(callback)
        except HoerwerkError as exc:
            messagebox.showerror("Anmeldung", str(exc), parent=self.app.root)
            return
        self.submitted()

    def submitted(self):
        self.done = True
        self.callback.set("")
        self.info.set("Amazon bestätigt die Anmeldung …")
        self.fallback.pack_forget()
        self.manual_button.configure(state="disabled")

    def manual(self):
        if self.manual_open or self.done or not self.url:
            return
        self.stop.set()
        self.manual_open = True
        self.manual_button.configure(state="disabled")
        self.info.set("Automatische Rückgabe nicht verfügbar. Melde dich im Browser an. "
                      "Kopiere danach die Adresse der Amazon-Rückgabeseite und wähle „Adresse übernehmen“.")
        self.fallback.pack(fill="x", before=self.actions)
        self.open_manual()

    def cancel(self):
        self.stop.set()
        self.controller.cancel()
        self.manual_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")
        self.player_login.configure(state="disabled")
        self.info.set("Anmeldung wird abgebrochen …")

    def poll(self):
        while not self.controller.events.empty():
            kind, value = self.controller.events.get_nowait()
            if kind == "device_code" and not self.stop.is_set():
                self.show_device_code(value)
            elif kind == "url":
                self.url = value
                self.manual_button.configure(state="normal")
                self.info.set("Melde dich im Amazon-Fenster an. Hörwerk übernimmt die Rückgabe automatisch.")
                threading.Thread(target=open_account_window, args=(value, self.controller, self.stop, self.browser_events),
                                 daemon=True, name="hoerwerk-account-browser").start()
            elif kind in {"success", "error", "cancelled"}:
                self.finished_event = kind, value
        while not self.browser_events.empty() and not self.stop.is_set() and self.finished_event is None:
            event = self.browser_events.get_nowait()
            if event == "submitted":
                self.submitted()
            elif not self.done:
                self.manual()
        if self.controller.busy and self.window.winfo_exists():
            self.window.after(80, self.poll)
        elif not self.controller.busy:
            self.stop.set()
            if self.window.winfo_exists():
                self.window.destroy()
            kind, value = self.finished_event or ("error", "Die Anmeldung wurde nicht abgeschlossen.")
            self.app.login_finished(kind, value, self.profile)


if __name__ == "__main__": main()

