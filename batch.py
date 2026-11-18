"""Bounded library actions and safe publication of completed downloads."""
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from core import BackendFailed, HoerwerkError, InvalidBackendResult
from jobs import JobCancelled
from transfer_progress import DownloadProgress


@dataclass(frozen=True)
class BatchResult:
    files: tuple[Path, ...]
    failures: tuple[tuple[str, str], ...]
    already_present: int = 0


def download_selected(ctx, items, local_files, valid_voucher, *, report_progress=None):
    ctx.check_cancelled()
    progress = DownloadProgress((asin for asin, _ in items), report_progress or (lambda _value: None))
    core = ctx.core(download_progress=progress.transferred) if report_progress else ctx.core()
    downloads = ctx.paths.downloads(ctx.profile)
    if downloads.is_symlink():
        raise InvalidBackendResult("Der Downloadordner darf keine Umleitung enthalten.")
    downloads.mkdir(parents=True, exist_ok=True)
    if downloads.resolve() != downloads:
        raise InvalidBackendResult("Der Downloadordner darf keine Umleitung enthalten.")
    files, pending, failures = [], [], []
    already_present = 0
    for asin, title in items:
        try:
            current = local_files(ctx.paths, ctx.profile, asin)
            if current:
                files.extend(current)
                already_present += 1
                progress.resolved(asin)
            else:
                pending.append((asin, title))
        except HoerwerkError as exc:
            failures.append((title, str(exc)))
            progress.resolved(asin)
    for offset in range(0, len(pending), 24):
        ctx.check_cancelled()
        group = pending[offset:offset + 24]
        with TemporaryDirectory(prefix=".hoerwerk-batch-", dir=downloads) as temporary:
            staging = Path(temporary)
            group_error = ""
            try:
                core.download_group(ctx.profile, (asin for asin, _ in group), staging)
            except BackendFailed as exc:
                group_error = str(exc)
            # --ignore-errors can leave a valid result for other titles.
            for asin, title in group:
                ctx.check_cancelled()
                try:
                    target = _publish_download(core, downloads, staging, asin, valid_voucher)
                    files.append(target)
                except OSError:
                    failures.append((title, "Die lokale Datei konnte nicht gespeichert werden."))
                except HoerwerkError as exc:
                    failures.append((title, group_error or str(exc)))
                progress.resolved(asin)
    return BatchResult(tuple(files), tuple(failures), already_present)


def _publish_download(core, downloads, staging, asin, valid_voucher):
    choices = [file for file in staging.glob(f"{asin}-*")
               if not file.is_symlink() and file.is_file() and file.stat().st_size
               and (file.suffix.lower() in {".mp3", ".m4a"}
                    or (file.suffix.lower() == ".aaxc" and valid_voucher(file.with_suffix(".voucher"), asin)))]
    if len(choices) != 1:
        raise InvalidBackendResult("Kein eindeutiger vollständiger Download verfügbar.")
    audio = choices[0]
    target_dir = downloads / asin
    if target_dir.is_symlink():
        raise InvalidBackendResult("Der Titelordner darf keine Umleitung enthalten.")
    target_dir.mkdir(mode=0o700, exist_ok=True)
    if target_dir.resolve() != target_dir:
        raise InvalidBackendResult("Der Titelordner darf keine Umleitung enthalten.")
    parts = [audio.with_suffix(".voucher"), audio] if audio.suffix.lower() == ".aaxc" else [audio]
    published = []
    try:
        for part in parts:
            part.chmod(0o600)
            target = target_dir / part.name
            core._publish(part, target)
            info = target.stat(follow_symlinks=False)
            published.append((target, info.st_dev, info.st_ino))
    except BaseException:
        for target, device, inode in published:
            try:
                current = target.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) == (device, inode):
                    target.unlink()
            except OSError:
                pass
        raise
    return target_dir / audio.name


def apply_selected(ctx, found, operation):
    files, failures = [], []
    for entry in found:
        ctx.check_cancelled()
        try:
            files.append(operation(ctx, entry))
        except JobCancelled:
            raise
        except HoerwerkError as exc:
            ctx.check_cancelled()
            failures.append((entry[1], str(exc)))
        except OSError:
            ctx.check_cancelled()
            failures.append((entry[1], "Eine lokale Datei konnte nicht gelesen oder geschrieben werden."))
    return BatchResult(tuple(files), tuple(failures))
