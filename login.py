"""Lokale, interaktive audible-cli-Einrichtung fuer Hoerwerk."""

from __future__ import annotations

import argparse
import os
from pathlib import Path, PureWindowsPath
import subprocess
import sys
import tomllib
from typing import Callable

from core import (BackendFailed, BackendUnavailable, HoerwerkError,
                  InvalidBackendResult, PortablePaths)


class InteractiveTerminalRequired(HoerwerkError):
    """Die Einrichtung braucht ein echtes interaktives Terminal."""


class LoginCancelled(HoerwerkError):
    """Die Person hat die Einrichtung kontrolliert abgebrochen."""


Runner = Callable[..., subprocess.CompletedProcess[object]]
TerminalCheck = Callable[[], bool]


def _inside(account: Path, candidate: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(account.resolve())
        return True
    except (OSError, ValueError):
        return False


def _account(paths: PortablePaths, profile_id: str) -> Path:
    account = paths.account(profile_id)
    for component in (paths.data, paths.data / "accounts", account):
        if component.is_symlink():
            raise InvalidBackendResult("Kontoverzeichnisse duerfen keine symbolischen Links sein.")
    return account


def readiness(paths: PortablePaths, profile_id: str) -> str:
    """Prueft nur lokale Metadaten; liest niemals den Inhalt der Authdatei."""
    account = _account(paths, profile_id)
    config = account / "config.toml"
    if not _inside(account, config):
        raise InvalidBackendResult("Die Konfiguration liegt ausserhalb des Kontoverzeichnisses.")
    if not config.is_file():
        raise InvalidBackendResult("Die lokale Konfiguration fehlt.")
    try:
        with config.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InvalidBackendResult("Die lokale Konfiguration ist ungueltig.") from exc

    app = data.get("APP")
    primary = app.get("primary_profile") if isinstance(app, dict) else None
    profiles = data.get("profile")
    selected = profiles.get(primary) if isinstance(profiles, dict) and isinstance(primary, str) else None
    auth_name = selected.get("auth_file") if isinstance(selected, dict) else None
    if not primary or not isinstance(auth_name, str) or not auth_name:
        raise InvalidBackendResult("Das Primaerprofil oder seine Authdatei ist ungueltig.")

    relative = Path(auth_name)
    windows = PureWindowsPath(auth_name)
    if (relative.is_absolute() or relative.name != auth_name or windows.drive
            or windows.name != auth_name or auth_name in {".", ".."}):
        raise InvalidBackendResult("Die Authdatei muss ein einfacher Dateiname sein.")
    auth_file = account / relative
    if not _inside(account, auth_file):
        raise InvalidBackendResult("Die Authdatei liegt ausserhalb des Kontoverzeichnisses.")
    try:
        if not auth_file.is_file():
            raise InvalidBackendResult("Die lokale Authdatei fehlt.")
        if not _inside(account, auth_file.resolve(strict=True)):
            raise InvalidBackendResult("Die Authdatei verweist aus dem Kontoverzeichnis.")
        if auth_file.stat().st_size == 0:
            raise InvalidBackendResult("Die lokale Authdatei ist leer.")
    except InvalidBackendResult:
        raise
    except OSError as exc:
        raise InvalidBackendResult("Die lokale Authdatei ist nicht sicher pruefbar.") from exc
    return "locally_configured_unverified"


def interactive_login(
    app_root: Path,
    profile_id: str,
    *,
    backend: Path | None = None,
    runner: Runner = subprocess.run,
    terminal: TerminalCheck = lambda: all(s.isatty() for s in (sys.stdin, sys.stdout, sys.stderr)),
) -> str:
    """Startet quickstart mit geerbten Ein-/Ausgabestroemen und prueft das Ergebnis."""
    paths = PortablePaths(app_root)
    account = _account(paths, profile_id)
    config = account / "config.toml"
    if config.exists() or config.is_symlink():
        return readiness(paths, profile_id)
    program = Path(backend).expanduser() if backend is not None else paths.bundled_program("audible")
    if program is None or not program.is_absolute() or not program.is_file():
        raise BackendUnavailable("Das lokale audible-Backend ist nicht verfuegbar.")
    if not terminal():
        raise InteractiveTerminalRequired("Die Anmeldung benoetigt ein interaktives Terminal.")
    try:
        account.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InvalidBackendResult("Das Kontoverzeichnis ist nicht beschreibbar.") from exc
    env = os.environ.copy()
    env["AUDIBLE_CONFIG_DIR"] = str(account)
    try:
        result = runner([str(program), "quickstart"], shell=False, check=False,
                        cwd=str(account), env=env)
    except KeyboardInterrupt as exc:
        raise LoginCancelled("Die lokale Einrichtung wurde abgebrochen.") from exc
    except OSError as exc:
        raise BackendUnavailable("Das lokale audible-Backend konnte nicht gestartet werden.") from exc
    if result.returncode != 0:
        raise BackendFailed("Das lokale audible-Backend hat die Einrichtung nicht abgeschlossen.")
    return readiness(paths, profile_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lokale interaktive Hoerwerk-Einrichtung")
    parser.add_argument("app_root", type=Path)
    parser.add_argument("profile_id")
    parser.add_argument("--backend", type=Path)
    args = parser.parse_args(argv)
    try:
        state = interactive_login(args.app_root, args.profile_id, backend=args.backend)
    except HoerwerkError as exc:
        parser.exit(1, f"Fehler: {exc}\n")
    print(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
