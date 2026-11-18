"""Gemeinsamer Einstieg für Quellstart und eingefrorenes Hörwerk."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import sys
from typing import Callable


def app_root(*, frozen: bool | None = None, executable: str | None = None) -> Path:
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    return (Path(executable or sys.executable).resolve().parent if frozen
            else Path(__file__).resolve().parent)


def installed_data_root(environ=None, home: Path | None = None) -> Path:
    """Liefert den absoluten benutzereigenen Datenpfad des installierten Modus."""
    import os
    environ = os.environ if environ is None else environ
    value = environ.get("XDG_DATA_HOME")
    if value and Path(value).is_absolute():
        return Path(value).expanduser().resolve() / "hoerwerk"
    return (home or Path.home()).expanduser().resolve() / ".local/share/hoerwerk"


def _self_check() -> bool:
    """Importiert alle Laufzeitmodule, erzeugt aber kein Fenster."""
    modules = ["core", "login", "jobs", "aaxc", "tkinter", "gui"]
    if getattr(sys, "frozen", False):
        modules.append("miniaudio")
    for name in modules:
        try:
            importlib.import_module(name)
        except (ImportError, OSError, RuntimeError) as exc:
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False
    return True


def main(argv: list[str] | None = None, *, login_func: Callable | None = None,
         gui_func: Callable | None = None, frozen: bool | None = None,
         executable: str | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hörwerk")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--login", action="store_true")
    parser.add_argument("--installed", action="store_true")
    parser.add_argument("login_app_root", nargs="?")
    parser.add_argument("profile", nargs="?")
    parser.add_argument("--backend", type=Path)
    args = parser.parse_args(argv)
    mode = "login" if args.login else "self-check" if args.self_check else "gui"
    if args.self_check:
        if args.login or args.login_app_root or args.profile or args.backend:
            parser.error("--self-check kann nicht mit Loginargumenten kombiniert werden")
        ok = _self_check()
        print(json.dumps({"import_ok": ok, "platform": sys.platform, "mode": mode},
                         sort_keys=True))
        return 0 if ok else 2
    if args.login:
        if not args.login_app_root or not args.profile or args.backend is None:
            parser.error("--login benötigt APPROOT PROFIL --backend ABSOLUTPFAD")
        if not args.backend.is_absolute():
            parser.error("--backend muss ein absoluter Pfad sein")
        if login_func is None:
            from login import interactive_login
            login_func = interactive_login
        try:
            state = login_func(Path(args.login_app_root), args.profile, backend=args.backend)
        except Exception as exc:
            from core import HoerwerkError
            if not isinstance(exc, HoerwerkError):
                raise
            print(f"Fehler: {exc}", file=sys.stderr)
            return 2
        print(state)
        return 0
    if args.login_app_root or args.profile or args.backend:
        parser.error("unerwartete Argumente für den GUI-Start")
    root_path = app_root(frozen=frozen, executable=executable)
    data_root = installed_data_root() if args.installed else None
    if gui_func is not None:
        if data_root is None:
            gui_func(root_path)
        else:
            gui_func(root_path, data_root)
        return 0
    import tkinter as tk
    from gui import App
    root = tk.Tk()
    App(root, root_path, data_root=data_root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
