"""Streng begrenzter lokaler AAXC-/Voucher-Adapter."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from core import (BackendFailed, BackendUnavailable, Core, HoerwerkError,
                  InvalidBackendResult, safe_filename)

from audio_formats import FORMATS

MAX_VOUCHER_BYTES = 4 * 1024 * 1024
_ASIN = re.compile(r"[A-Z0-9]{10}\Z")
_SECRET = re.compile(r"[0-9A-Fa-f]{32}\Z")


class AaxcCancelled(HoerwerkError):
    """Die laufende lokale Konvertierung wurde kontrolliert abgebrochen."""


def _plain_chain(root: Path, leaf: Path) -> bool:
    """Verlangt einen existierenden, symlinkfreien Pfad unter ``root``."""
    try:
        relative = leaf.relative_to(root)
        current = root
        for part in relative.parts:
            if part in {"", ".", ".."}:
                return False
            current /= part
            if stat.S_ISLNK(current.lstat().st_mode):
                return False
        return True
    except (OSError, ValueError):
        return False


def _inputs(core: Core, profile_id: str, audio_path: Path) -> tuple[Path, str, str]:
    data_root = core.paths.data
    downloads = core.paths.downloads(profile_id)
    audio = Path(audio_path)
    # Auch lexikalische Traversalversuche ablehnen, selbst wenn resolve() sie
    # wieder innerhalb der Downloadwurzel abbilden würde.
    if not audio.is_absolute() or ".." in audio.parts:
        raise InvalidBackendResult("Die AAXC-Quelldatei ist nicht sicher verfügbar.")
    try:
        audio_real = audio.resolve(strict=True)
    except (OSError, RuntimeError):
        raise InvalidBackendResult("Die AAXC-Quelldatei ist nicht sicher verfügbar.") from None
    if (audio_real != audio or data_root.is_symlink()
            or not _plain_chain(data_root, audio)):
        raise InvalidBackendResult("Profil und Quelldatei dürfen keine Umleitungen enthalten.")
    try:
        relative = audio.relative_to(downloads)
    except ValueError:
        raise InvalidBackendResult("Die AAXC-Datei liegt nicht in diesem Profil-Download.") from None
    if len(relative.parts) != 2 or audio.suffix.lower() != ".aaxc":
        raise InvalidBackendResult("Die AAXC-Datei liegt nicht in diesem Profil-Download.")
    asin = relative.parts[0]
    if not _ASIN.fullmatch(asin) or not audio.name.startswith(asin + "-"):
        raise InvalidBackendResult("Verzeichnis und AAXC-Dateiname enthalten keine passende ASIN.")
    try:
        if not audio.is_file() or audio.stat().st_size == 0:
            raise OSError
    except OSError:
        raise InvalidBackendResult("Die AAXC-Quelldatei ist leer oder nicht lesbar.") from None

    voucher = audio.with_suffix(".voucher")
    if not _plain_chain(data_root, voucher):
        raise InvalidBackendResult("Der gleichnamige Voucher ist ungültig.")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(voucher, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
                raise OSError
            raw = stream.read(MAX_VOUCHER_BYTES + 1)
        if len(raw) > MAX_VOUCHER_BYTES:
            raise OSError
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise InvalidBackendResult("Der gleichnamige Voucher ist ungültig.") from None
    license_data = data.get("content_license") if isinstance(data, dict) else None
    response = license_data.get("license_response") if isinstance(license_data, dict) else None
    if (not isinstance(license_data, dict)
            or license_data.get("status_code") != "Granted"
            or license_data.get("asin") != asin
            or not isinstance(response, dict)):
        raise InvalidBackendResult("Der Voucher enthält keine passende gewährte Lizenz.")
    key, iv = response.get("key"), response.get("iv")
    if not isinstance(key, str) or not _SECRET.fullmatch(key):
        raise InvalidBackendResult("Der Voucher enthält keinen gültigen AAXC-Schlüssel.")
    if not isinstance(iv, str) or not _SECRET.fullmatch(iv):
        raise InvalidBackendResult("Der Voucher enthält keinen gültigen AAXC-IV.")
    return audio, key, iv


def _stop(process: object) -> None:
    """Fordert Beendigung an und wartet auch nach Terminate-/Poll-Rennen."""
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        # Ein abschließendes wait bleibt auch nach einem Reaping-Rennen nötig.
        try:
            process.wait()
        except OSError:
            pass
        return
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait()
    except OSError:
        pass


def convert_aaxc(
    core: Core, profile_id: str, audio_path: Path, destination_dir: Path, title: str,
    *, cancelled=lambda: False, process_factory=subprocess.Popen, output_format="MP3", metadata=(), runner=None,
) -> Path:
    """Konvertiert validiertes AAXC lokal und veröffentlicht ohne Überschreiben."""
    if output_format not in FORMATS:
        raise InvalidBackendResult("Unbekanntes Exportformat.")
    extension, encoding = FORMATS[output_format]
    audio, key, iv = _inputs(core, profile_id, audio_path)
    program = Path(core.ffmpeg) if core.ffmpeg is not None else None
    if program is None or not program.is_absolute() or not program.is_file():
        raise BackendUnavailable("Das lokale FFmpeg-Backend ist nicht verfügbar.")
    if cancelled():
        raise AaxcCancelled("Die AAXC-Konvertierung wurde abgebrochen.")
    destination = Path(destination_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / (safe_filename(title) + extension)
    fd, name = tempfile.mkstemp(prefix=".hoerwerk-aaxc-", suffix=extension + ".tmp", dir=destination)
    os.close(fd)
    temporary = Path(name)
    temporary.unlink()
    argv = [
        str(program), "-nostdin", "-v", "error", "-audible_key", key,
        "-audible_iv", iv, "-i", str(audio), "-map", "0:a:0", "-vn",
        *metadata, *encoding, "-y", str(temporary),
    ]
    process = None
    try:
        if runner is not None:
            from jobs import JobCancelled
            try:
                result = runner(argv, shell=False)
            except JobCancelled:
                raise AaxcCancelled("Die AAXC-Konvertierung wurde abgebrochen.") from None
            returncode = result.returncode
            if cancelled():
                raise AaxcCancelled("Die AAXC-Konvertierung wurde abgebrochen.")
        else:
            try:
                process = process_factory(
                    argv, shell=False, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError:
                raise BackendUnavailable("FFmpeg konnte nicht gestartet werden.") from None
            key = iv = ""
            argv = []
            while process.poll() is None:
                if cancelled():
                    _stop(process)
                    raise AaxcCancelled("Die AAXC-Konvertierung wurde abgebrochen.")
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            # Das Signal kann nach dem letzten Poll, aber vor Veröffentlichung
            # eintreffen. Auch dann darf kein fertiges Ziel entstehen.
            if cancelled():
                _stop(process)
                raise AaxcCancelled("Die AAXC-Konvertierung wurde abgebrochen.")
            returncode = process.returncode
        if returncode != 0:
            raise BackendFailed("FFmpeg hat die AAXC-Konvertierung nicht erfolgreich beendet.")
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise InvalidBackendResult("FFmpeg hat keine nichtleere Audiodatei erzeugt.")
        core._publish(temporary, target)
        return target
    except KeyboardInterrupt:
        if process is not None:
            _stop(process)
        raise
    except HoerwerkError:
        raise
    except BaseException:
        if process is not None:
            _stop(process)
        raise HoerwerkError("Die AAXC-Konvertierung ist kontrolliert fehlgeschlagen.") from None
    finally:
        key = iv = ""
        argv = []
        temporary.unlink(missing_ok=True)

