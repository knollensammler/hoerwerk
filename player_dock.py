"""Experimental embedding of our Chromium app window on an X11 desktop."""

import os
from pathlib import Path
import sys
import time

from core import HoerwerkError


def can_dock(root):
    return (sys.platform.startswith("linux") and bool(os.environ.get("DISPLAY"))
            and not os.environ.get("WAYLAND_DISPLAY")
            and os.environ.get("XDG_SESSION_TYPE", "x11").lower() == "x11"
            and root.tk.call("tk", "windowingsystem") == "x11")


def uses_player_profile(pid, profile_dir):
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        process = Path(f"/proc/{pid}")
        return (process.stat().st_uid == os.getuid()
                and os.fsencode(f"--user-data-dir={profile_dir}") in (process / "cmdline").read_bytes().split(b"\0"))
    except OSError:
        return False


class PlayerDock:
    """Find one new window belonging to the profile launched by Hörwerk."""

    def __init__(self, root, container, on_status):
        if not can_dock(root):
            raise HoerwerkError("Die Seitenleisten-Vorschau benötigt einen Linux-Desktop mit X11. "
                                "Das separate Playerfenster steht im selben Menü bereit.")
        try:
            from Xlib import X, display, error
            self.display = display.Display()
        except ImportError as exc:
            raise HoerwerkError("Die X11-Unterstützung für die Seitenleiste fehlt.") from exc
        except Exception as exc:
            raise HoerwerkError("Hörwerk konnte die Verbindung zur X11-Anzeige nicht öffnen.") from exc
        self.X, self.error = X, error
        self.container, self.on_status = container, on_status
        self.root = self.display.screen().root
        self.clients_atom = self.display.intern_atom("_NET_CLIENT_LIST")
        self.pid_atom = self.display.intern_atom("_NET_WM_PID")
        self.initial = self.client_ids()
        self.window = None
        self.attached = False
        self.timer = None
        self.deadline = 0

    def client_ids(self):
        value = self.root.get_full_property(self.clients_atom, self.X.AnyPropertyType)
        return set(value.value) if value is not None else set()

    def matching_windows(self, profile_dir):
        matches = []
        for xid in self.client_ids() - self.initial:
            window = self.display.create_resource_object("window", xid)
            try:
                prop = window.get_full_property(self.pid_atom, self.X.AnyPropertyType)
                pid = int(prop.value[0]) if prop is not None and len(prop.value) == 1 else 0
                if uses_player_profile(pid, profile_dir):
                    matches.append(window)
            except self.error.XError:
                continue
        return matches

    def start(self, profile_dir):
        self.profile_dir = profile_dir
        self.deadline = time.monotonic() + 12
        self.timer = self.container.after(150, self.poll)

    def poll(self):
        self.timer = None
        try:
            if self.window is None:
                matches = self.matching_windows(self.profile_dir)
                if len(matches) == 1:
                    self.window = matches[0]
                    self.window.unmap()
                    self.display.sync()
            if self.window is not None and self.window.query_tree().parent.id == self.root.id:
                host = self.display.create_resource_object("window", self.container.winfo_id())
                request_error = self.error.CatchError()
                self.window.change_attributes(override_redirect=True, onerror=request_error)
                self.window.reparent(host, 0, 0, onerror=request_error)
                self.display.sync()
                if request_error.get_error() is not None:
                    raise request_error.get_error()
                self.attached = True
                self.resize()
                self.window.map(onerror=request_error)
                self.display.sync()
                if request_error.get_error() is not None:
                    raise request_error.get_error()
                self.on_status("Audible-Player in der Seitenleiste.")
                return
        except (OSError, self.error.XError):
            self.on_status("Der Audible-Player konnte nicht angedockt werden.", failed=True)
            return
        if time.monotonic() >= self.deadline:
            self.on_status("Kein eindeutiges Audible-Fenster zum Andocken gefunden.", failed=True)
            return
        self.timer = self.container.after(150, self.poll)

    def resize(self, _event=None):
        if self.attached:
            self.window.configure(width=max(1, self.container.winfo_width()),
                                  height=max(1, self.container.winfo_height()))
            self.display.flush()

    def close(self):
        if self.timer is not None:
            self.container.after_cancel(self.timer)
            self.timer = None
        try:
            if self.window is not None:
                if self.attached:
                    self.window.unmap()
                    self.window.reparent(self.root, 60, 60)
                    self.window.configure(width=560, height=710)
                self.window.change_attributes(override_redirect=False)
                self.window.map()
                self.display.sync()
        except (OSError, self.error.XError):
            pass
        finally:
            self.display.close()
