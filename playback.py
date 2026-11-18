"""Stream local audiobook audio without loading an entire book into memory."""
from __future__ import annotations

from hashlib import sha256
import math
import os
from pathlib import Path
import queue
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Thread

from core import InvalidBackendResult
from jobs import require_plain_account, stop_process
from media_tools import _input, audio_path

RATE = 44100
FRAME_BYTES = 4
MAX_SECONDS = 604800


def _bookmark(paths, profile, identity):
    account = require_plain_account(paths, profile)
    folder = account / "listening"
    if folder.resolve() != folder:
        raise InvalidBackendResult("Der Ordner für Hörpositionen ist ungültig.")
    return folder / (sha256(identity.encode("utf-8", errors="surrogatepass")).hexdigest() + ".txt")


def load_position(paths, profile, identity):
    try:
        path = _bookmark(paths, profile, identity)
        if path.is_symlink() or not path.is_file():
            return 0.0
        with path.open(encoding="ascii") as saved:
            position = float(saved.read(40))
        return position if 0 <= position <= MAX_SECONDS else 0.0
    except (OSError, ValueError, InvalidBackendResult):
        return 0.0


def save_position(paths, profile, identity, position):
    path = _bookmark(paths, profile, identity)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="ascii", dir=path.parent, delete=False) as saved:
        temporary = Path(saved.name)
        try:
            saved.write(f"{position:.3f}\n")
            saved.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class Playback:
    def __init__(self, ffmpeg, source, offset, *, device_factory=None):
        if ffmpeg is None:
            raise InvalidBackendResult("FFmpeg fehlt im App-Ordner.")
        self.source = audio_path(source)
        self.offset = offset
        self.frames = 0
        self.buffer = bytearray()
        self.blocks = queue.Queue(maxsize=12)
        self.stopping = Event()
        self.decoded = Event()
        self.finished = Event()
        self.failed = False
        self.device = None
        if device_factory is None:
            import miniaudio
            device_factory = lambda: miniaudio.PlaybackDevice(
                output_format=miniaudio.SampleFormat.SIGNED16, nchannels=2,
                sample_rate=RATE, buffersize_msec=100)
        self.process = subprocess.Popen(
            [str(ffmpeg), "-nostdin", "-v", "error", "-ss", f"{offset:.3f}", *_input(self.source),
             "-map", "0:a:0", "-f", "s16le", "-ac", "2", "-ar", str(RATE), "pipe:1"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = Thread(target=self._decode, daemon=True)
        self.reader.start()
        try:
            self.device = device_factory()
            self.stream = self._stream()
            next(self.stream)
            self.device.start(self.stream)
        except Exception:
            self.close()
            raise

    @property
    def position(self):
        return self.offset + self.frames / RATE

    def _decode(self):
        try:
            with self.process.stdout as output:
                while not self.stopping.is_set() and (block := output.read(16 * 1024)):
                    while not self.stopping.is_set():
                        try:
                            self.blocks.put(block, timeout=0.1)
                            break
                        except queue.Full:
                            continue
            self.failed = self.process.wait() != 0
        except OSError:
            self.failed = True
        finally:
            self.decoded.set()

    def _stream(self):
        requested = yield b""
        while True:
            size = requested * FRAME_BYTES
            while len(self.buffer) < size:
                try:
                    self.buffer.extend(self.blocks.get_nowait())
                except queue.Empty:
                    break
            # Unsupplied frames are silence. Count only frames taken from the book.
            count = min(size, len(self.buffer))
            result = bytes(self.buffer[:count])
            del self.buffer[:count]
            self.frames += count // FRAME_BYTES
            if not count and self.decoded.is_set() and self.blocks.empty():
                self.finished.set()
            requested = yield result.ljust(size, b"\0")

    def close(self):
        self.stopping.set()
        try:
            if self.device is not None:
                self.device.close()
        finally:
            if self.process.poll() is None:
                stop_process(self.process)
            self.reader.join(timeout=1)


def seek_position(value):
    return min(MAX_SECONDS, max(0.0, value)) if math.isfinite(value) else 0.0
