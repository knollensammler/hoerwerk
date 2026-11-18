"""Portabler Anwendungskern für Hörwerk (Python 3.11+, nur Standardbibliothek)."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Callable, Sequence


class HoerwerkError(Exception):
    """Erwarteter, für die Oberfläche geeigneter Anwendungsfehler."""


class InvalidIdentifier(HoerwerkError):
    pass


class BackendUnavailable(HoerwerkError):
    pass


class BackendFailed(HoerwerkError):
    pass


class InvalidBackendResult(HoerwerkError):
    pass


class UnsupportedAudio(HoerwerkError):
    pass


class DestinationExists(HoerwerkError):
    pass


class ExportFailed(HoerwerkError):
    pass


from audio_formats import FORMATS


Runner = Callable[..., subprocess.CompletedProcess[str]]
_PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ASIN = re.compile(r"[A-Z0-9]{10}\Z")
_DOWNLOAD_MARKER = "HOERWERK-CLI-V1 "
_DOWNLOAD_DIAGNOSTICS = {f"D-{phase}-{kind}" for phase in "CLAMFU" for kind in "KVRABNOX"}
_DOWNLOAD_MESSAGES = {
    "tls": "Der Download scheiterte an der sicheren Zertifikatsprüfung.",
    "dns": "Der Downloadserver konnte nicht aufgelöst werden.",
    "timeout": "Der Download hat das feste Zeitlimit überschritten.",
    "proxy": "Der Download konnte den eingestellten Proxy nicht verwenden.",
    "missing_component": "Eine lokale Download-Komponente fehlt.",
    "local_file": "Der Download konnte eine lokale Datei nicht lesen oder schreiben.",
    "license_denied": "Die Download-Lizenz wurde abgelehnt.",
    "license_denied_throttled": "Audible meldet eine Drosselung bei der Download-Lizenz.",
    "license_denied_client": "Audible meldet, dass dieser Client die Download-Lizenz nicht anfordern darf.",
    "license_denied_ownership": "Audible meldet bei dieser Anfrage fehlende Kaufberechtigung.",
    "license_denied_membership": "Audible meldet bei dieser Anfrage fehlende Mitgliedschaftsberechtigung.",
    "title_unavailable": "Der Titel ist für den Download nicht verfügbar.",
    "access_denied": "Der Downloadzugriff wurde abgelehnt; die genaue Ursache ist noch nicht bekannt.",
    "resource_missing": "Eine angeforderte Downloadressource wurde nicht gefunden.",
    "throttled": "Der Downloadserver hat zu viele Anfragen gemeldet. Bitte später erneut versuchen.",
    "unknown": "Der Download ist aus unbekannter Ursache fehlgeschlagen.",
}
_WINDOWS_DEVICES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_NO_HARDLINK = {errno.EPERM, errno.EXDEV}
for _errno_name in ("ENOTSUP", "EOPNOTSUPP"):
    if hasattr(errno, _errno_name):
        _NO_HARDLINK.add(getattr(errno, _errno_name))


def _identifier(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise InvalidIdentifier(f"Ungültige {label}.")
    return value


def _profile_id(value: str) -> str:
    value = _identifier(value, _PROFILE, "Profil-ID")
    if value.upper() in _WINDOWS_DEVICES:
        raise InvalidIdentifier("Ungültige Profil-ID.")
    # Ein kanonischer Name verhindert getrennte Konten, die unter Windows
    # durch Groß-/Kleinschreibung zum selben Verzeichnis würden.
    return value.lower()


@dataclass(frozen=True)
class PortablePaths:
    """Alle Laufzeitpfade, verankert an einem expliziten App-Verzeichnis."""

    app_root: Path
    data_root: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "app_root", Path(self.app_root).expanduser().resolve())
        if self.data_root is not None:
            object.__setattr__(self, "data_root", Path(self.data_root).expanduser().resolve())

    @property
    def data(self) -> Path:
        return self.data_root if self.data_root is not None else self.app_root / "data"

    def account(self, profile_id: str) -> Path:
        return self.data / "accounts" / _profile_id(profile_id)

    def downloads(self, profile_id: str) -> Path:
        return self.account(profile_id) / "downloads"

    def bundled_program(self, name: str) -> Path | None:
        suffix = ".exe" if os.name == "nt" else ""
        candidates = (
            self.app_root / "bin" / (name + suffix),
            self.app_root / "backend" / (name + suffix),
        )
        return next((p for p in candidates if p.is_file()), None)


class Core:
    def __init__(
        self,
        paths: PortablePaths,
        *,
        audible: Path | str | None = None,
        ffmpeg: Path | str | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.paths = paths
        self.audible = Path(audible) if audible is not None else paths.bundled_program("audible")
        self.ffmpeg = Path(ffmpeg) if ffmpeg is not None else paths.bundled_program("ffmpeg")
        self.runner = runner

    def _run(self, program: Path | None, args: Sequence[str], env: dict[str, str] | None = None,
             *, download_protocol: bool = False) -> None:
        if program is None or (self.runner is subprocess.run and not program.is_file()):
            raise BackendUnavailable("Das benötigte lokale Backend ist nicht verfügbar.")
        try:
            result = self.runner(
                [str(program), *args], shell=False, check=False,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env,
            )
        except OSError as exc:
            raise BackendUnavailable("Das benötigte lokale Backend konnte nicht gestartet werden.") from exc
        if download_protocol:
            line=getattr(result,"stdout","")
            value=None
            if (isinstance(line,str) and len(line)<=1024 and line.count("\n")==1
                    and line.endswith("\n") and line.startswith(_DOWNLOAD_MARKER)):
                try:value=json.loads(line[len(_DOWNLOAD_MARKER):-1])
                except (TypeError,ValueError):pass
            if (isinstance(value,dict) and set(value)=={"status"}
                    and value.get("status")=="ok" and result.returncode==0): return
            if value is None:
                raise BackendFailed(_DOWNLOAD_MESSAGES["unknown"]+" Diagnose P01: Keine gültige Backendantwort.")
            if (not isinstance(value,dict) or set(value) not in ({"status","reason"},{"status","reason","diagnostic"})
                    or value.get("status")!="error" or not isinstance(value.get("reason"),str)
                    or value["reason"] not in _DOWNLOAD_MESSAGES
                    or not isinstance(value.get("diagnostic","D-U-X"),str)
                    or value.get("diagnostic","D-U-X") not in _DOWNLOAD_DIAGNOSTICS):
                raise BackendFailed(_DOWNLOAD_MESSAGES["unknown"]+" Diagnose P02: Ungültiges Antwortschema.")
            raise BackendFailed(_DOWNLOAD_MESSAGES[value["reason"]]+" Diagnose "+value.get("diagnostic","D-U-X")+".")
        if result.returncode != 0:
            # Backendausgaben können Kontodaten enthalten und werden verworfen.
            raise BackendFailed("Das lokale Backend hat den Vorgang nicht erfolgreich beendet.")

    def _audible_env(self, profile_id: str) -> tuple[Path, dict[str, str]]:
        account = self.paths.account(profile_id)
        account.mkdir(parents=True, exist_ok=True)
        env = os.environ.copy()
        env["AUDIBLE_CONFIG_DIR"] = str(account)
        return account, env

    @staticmethod
    def _read_library(path):
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
                raise OSError
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError) as exc:
            raise InvalidBackendResult("Die gelieferte Bibliothek ist kein gültiges UTF-8-JSON.") from exc
        if not isinstance(value, list) or not all(
            isinstance(item, dict) and isinstance(item.get("asin"), str)
            and _ASIN.fullmatch(item["asin"]) is not None and isinstance(item.get("title"), str)
            for item in value
        ):
            raise InvalidBackendResult("Die gelieferte Bibliothek hat ein unerwartetes Format.")
        return value

    def saved_library(self, profile_id):
        account = self.paths.account(profile_id)
        if account.resolve() != account:
            raise InvalidBackendResult("Der Kontopfad darf keine Umleitung enthalten.")
        library = account / "library.json"
        return self._read_library(library) if library.exists() else []

    def refresh_library(self, profile_id: str) -> list[dict[str, object]]:
        account, env = self._audible_env(profile_id)
        final = account / "library.json"
        temporary = account / ".library.json.tmp"
        temporary.unlink(missing_ok=True)
        try:
            self._run(self.audible,
                      ["--hoerwerk-library", "--output", str(temporary)], env)
            if not temporary.is_file():
                raise InvalidBackendResult("Das Backend hat keine Bibliothek geliefert.")
            value = self._read_library(temporary)
            os.replace(temporary, final)
            return value
        finally:
            temporary.unlink(missing_ok=True)

    def download(self, profile_id: str, asin: str) -> tuple[Path, ...]:
        asin = _identifier(asin, _ASIN, "ASIN")
        _, env = self._audible_env(profile_id)
        output = self.paths.downloads(profile_id) / asin
        output.mkdir(parents=True, exist_ok=True)
        before = {p.name for p in output.iterdir()}
        self._run(
            self.audible,
            ["--hoerwerk-download", "download", "--aaxc", "--asin", asin, "--output-dir", str(output),
             "--no-confirm", "--no-progress", "--jobs", "1", "--filename-mode", "asin_only"], env,
            download_protocol=True)
        created = tuple(
            p for p in output.iterdir()
            if p.name not in before and p.is_file() and p.stat().st_size
        )
        audio = [p for p in created if p.suffix.lower() in {".aaxc", ".mp3", ".m4a"}]
        if len(audio) != 1:
            raise InvalidBackendResult("Das Download-Ergebnis enthält keine eindeutig erkennbare Audiodatei.")
        if audio[0].suffix.lower() == ".aaxc" and not any(
            p.suffix.lower() == ".voucher" and p.stem == audio[0].stem for p in created
        ):
            raise InvalidBackendResult("Zum AAXC-Download fehlt der zugehörige Voucher.")
        return created

    def download_group(self, profile_id, asins, output):
        """Run one protected CLI session and let its queue download in parallel."""
        ids = tuple(dict.fromkeys(_identifier(asin, _ASIN, "ASIN") for asin in asins))
        if not 1 <= len(ids) <= 24:
            raise InvalidBackendResult("Die Download-Gruppe hat eine ungültige Größe.")
        _, env = self._audible_env(profile_id)
        args = ["--hoerwerk-download", "download", "--aaxc", "--output-dir", str(output),
                "--no-confirm", "--no-progress", "--ignore-errors", "--jobs",
                str(min(3, len(ids))), "--filename-mode", "asin_only"]
        for asin in ids:
            args.extend(("--asin", asin))
        return self._run(self.audible, args, env, download_protocol=True)

    @staticmethod
    def _publish(temporary: Path, target: Path) -> None:
        """Ohne Überschreiben veröffentlichen; Hardlink, sonst exklusives Kopieren."""
        try:
            os.link(temporary, target)
            return
        except FileExistsError as exc:
            raise DestinationExists("Die Zieldatei existiert bereits und wird nicht überschrieben.") from exc
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK:
                raise ExportFailed("Die MP3-Datei konnte nicht veröffentlicht werden.") from exc

        fd = None
        reserved_identity = None
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o666)
            reserved_identity = os.fstat(fd)
            with temporary.open("rb") as source, os.fdopen(fd, "wb") as destination:
                fd = None
                while block := source.read(1024 * 1024):
                    destination.write(block)
                destination.flush()
                os.fsync(destination.fileno())
        except FileExistsError as exc:
            raise DestinationExists("Die Zieldatei existiert bereits und wird nicht überschrieben.") from exc
        except BaseException as exc:
            # Auch einen kontrollierten Abbruch bereinigen, danach weiterreichen.
            # Nur die von uns reservierte Datei entfernen, nie einen inzwischen
            # unter demselben Namen eingesetzten fremden Verzeichniseintrag.
            try:
                current = target.stat()
                if reserved_identity and (current.st_dev, current.st_ino) == (
                    reserved_identity.st_dev, reserved_identity.st_ino
                ):
                    target.unlink()
            except OSError:
                pass
            if isinstance(exc, OSError):
                raise ExportFailed("Die MP3-Datei konnte nicht vollständig veröffentlicht werden.") from exc
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def convert_playable_to_mp3(self, source: Path, destination_dir: Path, title: str) -> Path:
        return self.convert_playable(source, destination_dir, title)

    def convert_playable(self, source: Path, destination_dir: Path, title: str, output_format="MP3", *, metadata=()) -> Path:
        if output_format not in FORMATS:
            raise UnsupportedAudio("Unbekanntes Exportformat.")
        extension, encoding = FORMATS[output_format]
        source = Path(source).resolve()
        if source.suffix.lower() in {".aax", ".aaxc"}:
            raise UnsupportedAudio("AAX/AAXC benötigt einen noch nicht implementierten Entitlement-Konverter.")
        if not source.is_file():
            raise InvalidBackendResult("Die Quelldatei ist nicht verfügbar.")
        destination_dir = Path(destination_dir).expanduser().resolve()
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / (safe_filename(title) + extension)
        if target.exists():
            raise DestinationExists("Die Zieldatei existiert bereits und wird nicht überschrieben.")
        fd, temp_name = tempfile.mkstemp(prefix=".hoerwerk-", suffix=extension + ".tmp", dir=destination_dir)
        os.close(fd)
        temporary = Path(temp_name)
        temporary.unlink()
        try:
            self._run(
                self.ffmpeg,
                ["-nostdin", "-v", "error", "-i", str(source), "-map", "0:a:0", "-vn",
                 *metadata, *encoding, str(temporary)])
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise InvalidBackendResult("FFmpeg hat keine nichtleere Audiodatei erzeugt.")
            self._publish(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)


def safe_filename(title: str) -> str:
    """Erzeugt einen konservativen Dateinamen für Windows und Linux."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(title))
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = "Hoerbuch"
    # Windows behandelt auch CON.txt, AUX.anything usw. als Gerätenamen.
    if cleaned.split(".", 1)[0].upper() in _WINDOWS_DEVICES:
        cleaned = "_" + cleaned
    return cleaned[:120].rstrip(" .") or "Hoerbuch"

