"""Local audiobook import and chapter operations for ordinary audio files."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from audio_formats import FORMATS
from core import InvalidBackendResult, safe_filename

EXTENSIONS = {".mp3", ".m4a", ".m4b", ".flac", ".wav", ".aac", ".ogg", ".opus"}
DEMUXERS = "mp3,mov,flac,wav,aac,ogg"
CATALOG_ID = re.compile(r"local-[a-f0-9]{64}\Z")
MAX_METADATA = 1024 * 1024


@dataclass(frozen=True)
class Chapter:
    title: str
    start: Fraction
    end: Fraction


def _input(path):
    return ["-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS, "-i", str(path)]


def audio_path(value):
    path = Path(value).expanduser().resolve()
    if path.suffix.casefold() not in EXTENSIONS or not path.is_file() or not path.stat().st_size:
        raise InvalidBackendResult("Die Datei ist kein unterstütztes lokales Audioformat.")
    return path


def read_metadata(core, path):
    with tempfile.TemporaryDirectory(prefix="hoerwerk-metadata-") as tmp:
        output = Path(tmp) / "audio.ffmeta"
        core._run(core.ffmpeg, ["-nostdin", "-v", "error", *_input(path), "-map_metadata", "0",
                               "-f", "ffmetadata", str(output)])
        if not output.is_file() or output.stat().st_size > MAX_METADATA:
            raise InvalidBackendResult("Die Audioinformationen sind nicht lesbar.")
        return parse_metadata(output.read_text(encoding="utf-8", errors="replace"))


def parse_metadata(text):
    """Parse FFmpeg's metadata file; only well-formed chapter boundaries survive."""
    tags, parts = {}, []
    current = tags
    for line in text.splitlines():
        if line == "[CHAPTER]":
            current = {}
            parts.append(current)
        elif line.startswith("["):
            current = {}
        elif "=" in line and not line.startswith(("#", ";")):
            key, _, value = line.partition("=")
            current[key.upper()] = re.sub(r"\\([\\;#=])", r"\1", value)[:400]
    chapters = []
    for part in parts:
        try:
            scale = Fraction(part["TIMEBASE"])
            start = scale * int(part["START"])
            end = scale * int(part["END"])
            if not 0 <= start < end <= 604800:
                continue
            chapters.append(Chapter(part.get("TITLE", "").strip(), start, end))
        except (KeyError, ValueError, ZeroDivisionError):
            continue
    chapters.sort(key=lambda chapter: chapter.start)
    if len(chapters) > 500:
        raise InvalidBackendResult("Die Datei enthält zu viele Kapitel.")
    return tags, chapters


def _catalog(paths):
    folder = paths.data / "local"
    if folder.is_symlink():
        raise InvalidBackendResult("Der lokale Bibliotheksordner darf keine Umleitung sein.")
    return folder


def local_source(paths, item):
    identifier = item.get("asin")
    source_name = item.get("source_name")
    if (not isinstance(identifier, str) or not CATALOG_ID.fullmatch(identifier)
            or not isinstance(source_name, str) or not source_name.startswith("audio.")
            or Path(source_name).name != source_name or Path(source_name).suffix not in EXTENSIONS):
        raise InvalidBackendResult("Der Bibliothekseintrag ist ungültig.")
    path = _catalog(paths) / identifier / source_name
    if path.resolve() != path or path.is_symlink():
        raise InvalidBackendResult("Die Bibliotheksdatei darf keine Umleitung sein.")
    return audio_path(path)


def load_local(paths):
    root = _catalog(paths)
    if not root.is_dir():
        return []
    rows = []
    for folder in sorted(root.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not CATALOG_ID.fullmatch(folder.name):
            continue
        entry = folder / "info.json"
        try:
            if not entry.is_file() or entry.is_symlink() or entry.stat().st_size > 8192:
                continue
            row = json.loads(entry.read_text(encoding="utf-8"))
            if (not isinstance(row, dict) or row.get("asin") != folder.name
                    or not isinstance(row.get("title"), str)):
                continue
            local_source(paths, row)
            row["kind"] = "local"
        except (OSError, UnicodeError, ValueError, InvalidBackendResult):
            continue
        rows.append(row)
    return rows


def import_one(ctx, value):
    source = audio_path(value)
    tags, chapters = read_metadata(ctx.core(), source)
    root = _catalog(ctx.paths)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve() != root:
        raise InvalidBackendResult("Der lokale Bibliotheksordner darf keine Umleitung sein.")
    with tempfile.TemporaryDirectory(prefix=".import-", dir=root) as tmp:
        temporary = Path(tmp)
        filename = "audio" + source.suffix.lower()
        digest = sha256()
        local_file = temporary / filename
        with source.open("rb") as incoming, local_file.open("xb") as outgoing:
            local_file.chmod(0o600)
            while block := incoming.read(1024 * 1024):
                ctx.check_cancelled()
                digest.update(block)
                outgoing.write(block)
        identifier = "local-" + digest.hexdigest()
        destination = root / identifier
        if destination.exists() or destination.is_symlink():
            raise InvalidBackendResult("Diese Audiodatei ist bereits in der lokalen Bibliothek.")
        row = {"asin": identifier, "kind": "local", "source_name": filename,
               "title": (tags.get("TITLE") or source.stem)[:300],
               "authors": tags.get("ARTIST") or tags.get("ALBUM_ARTIST") or "",
               "date_added": date.today().isoformat()}
        if chapters:
            row["runtime_length_min"] = round(float(max(ch.end for ch in chapters) / 60), 1)
        entry = temporary / "info.json"
        entry.write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        entry.chmod(0o600)
        ctx.check_cancelled()
        # The caller serializes jobs, and the content-derived directory name is reserved atomically.
        try:
            destination.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise InvalidBackendResult("Diese Audiodatei ist bereits in der lokalen Bibliothek.") from exc
        try:
            os.rename(local_file, destination / filename)
            os.rename(entry, destination / "info.json")
        except BaseException:
            for name in (filename, "info.json"):
                target = destination / name
                target.unlink(missing_ok=True)
            destination.rmdir()
            raise
    return row


def split(ctx, value, destination, output_format):
    path = audio_path(value)
    core = ctx.core()
    tags, chapters = read_metadata(core, path)
    if not chapters:
        raise InvalidBackendResult("Diese Datei enthält keine verwendbaren Kapitelmarken.")
    extension, encoding = FORMATS[output_format]
    destination = Path(destination).expanduser().resolve()
    folder = Path(tempfile.mkdtemp(prefix=safe_filename(tags.get("TITLE") or path.stem) + " - ",
                                  dir=destination))
    finished = []
    try:
        for index, chapter in enumerate(chapters, start=1):
            ctx.check_cancelled()
            title = safe_filename(chapter.title or f"Kapitel {index}")
            target = folder / f"{index:03d} - {title}{extension}"
            with tempfile.TemporaryDirectory(prefix=".kapitel-", dir=folder) as tmp:
                scratch = Path(tmp) / ("kapitel" + extension)
                core._run(core.ffmpeg, ["-nostdin", "-v", "error", "-ss", f"{float(chapter.start):.6f}",
                                       *_input(path), "-t", f"{float(chapter.end - chapter.start):.6f}",
                                       "-map", "0:a:0", "-vn", "-map_chapters", "-1",
                                       "-metadata", f"title={chapter.title or title}", *encoding, str(scratch)])
                _publish(core, scratch, target)
            finished.append(target)
    finally:
        if not finished:
            folder.rmdir()
    return folder


def _chapter_index(ctx, core, paths, folder):
    """Use packet timestamps; no additional audio encoding is needed."""
    marks = [";FFMETADATA1"]
    total = 0
    for index, path in enumerate(paths, start=1):
        ctx.check_cancelled()
        progress = folder / f"timing-{index}.txt"
        core._run(core.ffmpeg, ["-nostdin", "-v", "error", "-nostats", "-stats_period", "9999999",
                               "-progress", str(progress), *_input(path), "-map", "0:a:0",
                               "-c:a", "copy", "-f", "null", "-"])
        if not progress.is_file() or progress.stat().st_size > 65536:
            raise InvalidBackendResult("Die Kapitelzeiten konnten nicht ermittelt werden.")
        times = [int(value) for value in re.findall(r"^out_time_us=(\d+)$",
                 progress.read_text(encoding="utf-8"), re.MULTILINE)]
        duration = max(times, default=0)
        if duration <= 0:
            raise InvalidBackendResult("Eine Audiodatei hat keine verwendbare Laufzeit.")
        label = path.stem.replace("\r", " ").replace("\n", " ")
        label = re.sub(r"([\\=;#])", r"\\\1", label)[:300]
        marks.extend(("[CHAPTER]", "TIMEBASE=1/1000000", f"START={total}",
                      f"END={total + duration}", f"title={label}"))
        total += duration
    metadata = folder / "chapters.ffmeta"
    metadata.write_text("\n".join(marks) + "\n", encoding="utf-8")
    return metadata


def join(ctx, values, destination, title, output_format):
    paths = [audio_path(value) for value in values]
    if len(paths) < 2:
        raise InvalidBackendResult("Bitte mindestens zwei Audiodateien auswählen.")
    if len(paths) > 64:
        raise InvalidBackendResult("Pro Zusammenfügen sind bis zu 64 Dateien möglich.")
    core = ctx.core()
    extension, encoding = FORMATS[output_format]
    destination = Path(destination).expanduser().resolve()
    target = destination / (safe_filename(title) + extension)
    filters = [f"[{i}:a:0]aformat=sample_rates=44100:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]"
               for i in range(len(paths))]
    filters.append("".join(f"[a{i}]" for i in range(len(paths))) + f"concat=n={len(paths)}:v=0:a=1[out]")
    with tempfile.TemporaryDirectory(prefix=".hoerwerk-join-", dir=destination) as tmp:
        scratch = Path(tmp) / ("titel" + extension)
        args = ["-nostdin", "-v", "error"]
        for path in paths:
            args.extend(_input(path))
        if output_format == "M4B":
            chapters = _chapter_index(ctx, core, paths, Path(tmp))
            args.extend(("-f", "ffmetadata", "-i", str(chapters), "-map_chapters", str(len(paths))))
        else:
            args.extend(("-map_chapters", "-1"))
        args.extend(("-filter_complex", ";".join(filters), "-map", "[out]",
                     "-metadata", f"title={title}", *encoding, str(scratch)))
        ctx.check_cancelled()
        core._run(core.ffmpeg, args)
        ctx.check_cancelled()
        _publish(core, scratch, target)
    return target


def _publish(core, source, target):
    if not source.is_file() or not source.stat().st_size:
        raise InvalidBackendResult("FFmpeg hat keine abspielbare Ausgabedatei erzeugt.")
    core._publish(source, target)
