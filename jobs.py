"""Asynchrone, von Tk unabhängige Jobsteuerung für Hörwerk."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from queue import Queue
import os
import signal
import subprocess
import threading
from typing import Callable, TypeVar

from core import BackendUnavailable, Core, HoerwerkError, InvalidBackendResult, PortablePaths

from transfer_progress import ConversionProgress, DOWNLOAD_LINE, DOWNLOAD_OPTION

T = TypeVar("T")


class JobBusy(HoerwerkError):
    pass


class JobCancelled(HoerwerkError):
    pass


@dataclass(frozen=True)
class JobEvent:
    state: str
    kind: str
    profile: str
    asin: str | None
    message: str
    result: object = None


def require_plain_account(paths: PortablePaths, profile: str) -> Path:
    """Validiert ein bestehendes Konto ohne Symlink-Umleitungen."""
    account = paths.account(profile)
    current = paths.data
    try:
        if current.is_symlink():
            raise InvalidBackendResult("Kontopfade dürfen keine Umleitungen enthalten.")
        for part in account.relative_to(paths.data).parts:
            current /= part
            if current.is_symlink():
                raise InvalidBackendResult("Kontopfade dürfen keine Umleitungen enthalten.")
    except (OSError, RuntimeError, ValueError):
        raise InvalidBackendResult("Der Kontopfad ist nicht sicher prüfbar.") from None
    return account


def stop_process(process) -> None:
    """Terminate, begrenztes Wait, nötigenfalls Kill und abschließendes Wait."""
    try:
        process.terminate()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait()
    except OSError:
        pass


# The frozen Audible backend has a bootloader and a worker process. Stop
# everything in this job's own group before returning the cancellation.
def stop_backend(process, *, separate_group):
    if separate_group:
        if os.name == "nt":
            taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
            try:
                subprocess.run([str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=4, check=False,
                               creationflags=subprocess.CREATE_NO_WINDOW)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                pass
    stop_process(process)


class CancellableRunner:
    """Für ``Core`` geeigneter Popen-Runner ohne öffentliche Prozessdetails."""

    def __init__(self, cancelled: threading.Event, factory=subprocess.Popen, *,
                 download_progress=None, conversion_progress=None):
        self.cancelled, self.factory = cancelled, factory
        self.separate_group = factory is subprocess.Popen
        self.download_progress = download_progress
        self.conversion_progress = conversion_progress

    def __call__(self, argv, **kwargs):
        if self.cancelled.is_set():
            raise JobCancelled("Der Vorgang wurde abgebrochen.")
        kwargs.pop("check", None)
        kwargs.pop("text", None)
        safe_download = len(argv) > 1 and argv[1] == "--hoerwerk-download"
        conversion = None
        if safe_download and self.download_progress is not None:
            argv = [*argv, DOWNLOAD_OPTION]
        elif self.conversion_progress is not None:
            conversion = ConversionProgress(self.conversion_progress)
            argv = [argv[0], "-nostats", "-progress", "pipe:1", *argv[1:]]
            argv[argv.index("-v") + 1] = "info"
        kwargs["stdin"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.PIPE if conversion else subprocess.DEVNULL
        kwargs["stdout"] = subprocess.PIPE if safe_download or conversion else subprocess.DEVNULL
        if self.separate_group and os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            process = self.factory(argv, **kwargs)
        except OSError:
            raise BackendUnavailable("Das lokale Backend konnte nicht gestartet werden.") from None
        readers = []
        captured = bytearray()
        overflow = False

        def download_line(line):
            nonlocal overflow
            if self.download_progress is not None and (match := DOWNLOAD_LINE.fullmatch(line)):
                self.download_progress(match[1].decode("ascii"), int(match[2]), int(match[3]))
            elif len(captured) + len(line) <= 1024 and not overflow:
                captured.extend(line)
            else:
                overflow = True

        def read(pipe, consume):
            def drain():
                with pipe:
                    while line := pipe.readline(4096):
                        consume(line.encode("utf-8") if isinstance(line, str) else line)

            reader = threading.Thread(target=drain, daemon=True, name="hoerwerk-backend-output")
            reader.start()
            readers.append(reader)

        try:
            if safe_download:
                read(process.stdout, download_line)
            if conversion:
                read(process.stdout, conversion.output)
                read(process.stderr, conversion.diagnostics)
            while process.poll() is None:
                if self.cancelled.wait(0.05):
                    stop_backend(process, separate_group=self.separate_group)
                    raise JobCancelled("Der Vorgang wurde abgebrochen.")
            if self.cancelled.is_set():
                stop_backend(process, separate_group=self.separate_group)
                raise JobCancelled("Der Vorgang wurde abgebrochen.")
            for reader in readers:
                reader.join(timeout=1)
            stdout = "" if overflow or any(reader.is_alive() for reader in readers) else captured.decode("utf-8", "replace")
            return subprocess.CompletedProcess([], process.returncode, stdout)
        except KeyboardInterrupt:
            stop_backend(process, separate_group=self.separate_group)
            raise JobCancelled("Der Vorgang wurde abgebrochen.") from None
        except JobCancelled:
            raise
        except BaseException:
            # Kein erneutes poll(): gerade poll() kann der Auslöser gewesen sein.
            stop_backend(process, separate_group=self.separate_group)
            raise
        finally:
            for reader in readers:
                reader.join(timeout=1)


class JobContext:
    def __init__(self, paths: PortablePaths, profile: str, asin: str | None,
                 cancelled: threading.Event, process_factory, report_progress=lambda _value: None):
        self.paths, self.profile, self.asin = paths, profile, asin
        self.cancelled, self.process_factory = cancelled, process_factory
        self.report_progress = report_progress

    def core(self, *, download_progress=None, conversion_progress=None) -> Core:
        require_plain_account(self.paths, self.profile)
        return Core(self.paths, runner=CancellableRunner(
            self.cancelled, self.process_factory, download_progress=download_progress,
            conversion_progress=conversion_progress))

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise JobCancelled("Der Vorgang wurde abgebrochen.")


class JobController:
    """Hält genau einen Worker; Ereignisse werden nur über ``events`` geliefert."""

    def __init__(self, paths: PortablePaths, *, process_factory=subprocess.Popen):
        self.paths, self.process_factory = paths, process_factory
        self.events: Queue[JobEvent] = Queue()
        self._lock, self._cancel = threading.Lock(), threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._thread is not None

    def start(self, kind: str, profile: str, asin: str | None,
              operation: Callable[[JobContext], T]) -> None:
        profile = self.paths.account(profile).name
        asin = None if asin is None else str(asin)
        with self._lock:
            if self._thread is not None:
                raise JobBusy("Es läuft bereits ein Vorgang.")
            self._cancel = threading.Event()
            context = JobContext(self.paths, profile, asin, self._cancel, self.process_factory,
                                 lambda value: self.events.put(JobEvent("progress", kind, profile, asin, "", value)))
            self._thread = threading.Thread(target=self._work,
                args=(str(kind), context, operation), daemon=False, name="hoerwerk-job")
            self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()

    def _work(self, kind: str, context: JobContext, operation) -> None:
        self.events.put(JobEvent("running", kind, context.profile, context.asin, "Vorgang läuft …"))
        try:
            context.check_cancelled()
            result = operation(context)
            context.check_cancelled()
            event = JobEvent("success", kind, context.profile, context.asin,
                             "Vorgang erfolgreich beendet.", result)
        except JobCancelled:
            event = JobEvent("cancelled", kind, context.profile, context.asin, "Vorgang abgebrochen.")
        except HoerwerkError as exc:
            event = JobEvent("error", kind, context.profile, context.asin, str(exc))
        except BaseException:
            event = JobEvent("error", kind, context.profile, context.asin,
                             "Der Vorgang ist unerwartet fehlgeschlagen.")
        self.events.put(event)
        with self._lock:
            self._thread = None
