"""Numeric progress for the download subprocess and FFmpeg conversion."""
from dataclasses import dataclass
import re
import threading

DOWNLOAD_PROGRESS = "HOERWERK-PROGRESS "
DOWNLOAD_OPTION = "--hoerwerk-progress"
DOWNLOAD_LINE = re.compile(rb"HOERWERK-PROGRESS ([A-Z0-9]{10}) ([0-9]{1,16}) ([0-9]{1,16})\r?\n")
DURATION_LINE = re.compile(rb"^\s*Duration: (\d{2,5}):(\d{2}):(\d{2})\.(\d{2}),")
OUTPUT_TIME = re.compile(rb"^out_time_us=(-?\d{1,15})\r?\n$")


@dataclass(frozen=True)
class Progress:
    value: int | None
    detail: str = ""


class ConversionProgress:
    """Combine machine-readable output time with the local input duration."""
    def __init__(self, publish):
        self.publish = publish
        self.seconds = 0
        self.duration = None
        self.last = None
        self.lock = threading.Lock()

    def output(self, line):
        if match := OUTPUT_TIME.fullmatch(line):
            with self.lock:
                self.seconds = max(self.seconds, int(match[1]) / 1_000_000)
                self.update()

    def diagnostics(self, line):
        if match := DURATION_LINE.match(line):
            with self.lock:
                if self.duration is None:
                    hours, minutes, seconds, hundredths = map(int, match.groups())
                    self.duration = hours * 3600 + minutes * 60 + seconds + hundredths / 100
                    self.update()

    def update(self):
        pct = min(99, int(100 * self.seconds / self.duration)) if self.duration else None
        minutes, seconds = divmod(int(self.seconds), 60)
        report = Progress(pct, f"{minutes}:{seconds:02d} Audio verarbeitet")
        if report != self.last:
            self.last = report
            self.publish(report)


class DownloadProgress:
    """Weight the selected titles equally, including those still queued."""
    def __init__(self, identifiers, publish):
        self.parts = dict.fromkeys(identifiers, (0, 0))
        self.done = set()
        self.publish = publish

    def transferred(self, asin, received, total):
        if asin in self.parts and asin not in self.done:
            self.parts[asin] = received, total
            self.update()

    def resolved(self, asin):
        self.done.add(asin)
        self.update()

    def update(self):
        total = len(self.parts)
        if len(self.done) == total:
            return  # The terminal job event owns the completed state.
        active = [part for asin, part in self.parts.items() if asin not in self.done]
        received = sum(size for size, _ in active)
        unknown = any(size and not length for size, length in active)
        work = sum(min(1, size / length) for size, length in active if length)
        value = min(99, int(100 * (len(self.done) + work) / total))
        detail = f"{len(self.done)}/{total} bearbeitet" if total > 1 else ""
        if received:
            size_text = f"{received / 1048576:.1f} MiB geladen"
            detail = f"{detail} · {size_text}" if detail else size_text
        self.publish(Progress(None if unknown or not (self.done or work) else value, detail))
