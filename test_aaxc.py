import errno
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from aaxc import AaxcCancelled, MAX_VOUCHER_BYTES, _stop, convert_aaxc
from core import (BackendFailed, BackendUnavailable, Core, DestinationExists,
                  HoerwerkError, InvalidBackendResult, PortablePaths)

KEY = "11" * 16
IV = "a2" * 16


class FakeProcess:
    def __init__(self, argv, *, output=True, returncode=0, running=False,
                 terminate_error=None, poll_error=None, **kwargs):
        self.argv, self.kwargs = argv, kwargs
        self.final_code, self.running = returncode, running
        self.returncode = None if running else returncode
        self.terminate_error, self.poll_error = terminate_error, poll_error
        self.terminated = self.killed = False
        self.waits = []
        if output:
            Path(argv[-1]).write_bytes(b"synthetic mp3")

    def poll(self):
        if self.poll_error:
            raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.running:
            raise subprocess.TimeoutExpired("redacted", timeout)
        self.returncode = self.final_code
        return self.returncode

    def terminate(self):
        self.terminated = True
        if self.terminate_error:
            raise self.terminate_error

    def kill(self):
        self.killed = True
        self.running = False
        self.returncode = self.final_code


class AaxcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = PortablePaths(self.root / "app")
        self.ffmpeg = self.root / "ffmpeg"
        self.ffmpeg.write_bytes(b"fake")
        self.core = Core(self.paths, ffmpeg=self.ffmpeg)
        self.asin = "B012345678"
        folder = self.paths.downloads("Anna") / self.asin
        folder.mkdir(parents=True)
        self.audio = folder / f"{self.asin}-LC.aaxc"
        self.audio.write_bytes(b"synthetic audio")
        self.voucher = self.audio.with_suffix(".voucher")
        self.write_voucher()
        self.destination = self.root / "usb"

    def write_voucher(self, **changes):
        license_data = {"asin": self.asin, "status_code": "Granted",
                        "license_response": {"key": KEY, "iv": IV}}
        license_data.update(changes)
        self.voucher.write_text(json.dumps({"content_license": license_data}), encoding="utf-8")

    def factory(self, **options):
        calls = []

        def create(argv, **kwargs):
            process = FakeProcess(argv, **options, **kwargs)
            calls.append(process)
            return process
        return calls, create

    def test_arguments_stdio_and_inputs_unchanged(self):
        before = self.audio.read_bytes(), self.voucher.read_bytes()
        calls, factory = self.factory()
        target = convert_aaxc(self.core, "ANNA", self.audio, self.destination, "Buch",
                              process_factory=factory)
        process = calls[0]
        self.assertLess(process.argv.index("-audible_key"), process.argv.index("-i"))
        self.assertLess(process.argv.index("-audible_iv"), process.argv.index("-i"))
        self.assertFalse(process.kwargs["shell"])
        self.assertEqual(process.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(target.read_bytes(), b"synthetic mp3")
        self.assertEqual(before, (self.audio.read_bytes(), self.voucher.read_bytes()))

    def test_bad_and_oversized_vouchers_rejected_before_start(self):
        bad = ({"status_code": "Denied"}, {"asin": "B999999999"},
               {"license_response": KEY}, {"license_response": None},
               {"license_response": {"key": "z" * 32, "iv": IV}},
               {"license_response": {"key": KEY[:-1], "iv": IV}},
               {"license_response": {"key": KEY, "iv": []}})
        for change in bad:
            self.write_voucher(**change)
            calls, factory = self.factory()
            with self.subTest(change=change), self.assertRaises(InvalidBackendResult):
                convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                             process_factory=factory)
            self.assertFalse(calls)
        for raw in (b"{", b"\xff", b"", b"x" * (MAX_VOUCHER_BYTES + 1)):
            self.voucher.write_bytes(raw)
            with self.subTest(size=len(raw)), self.assertRaises(InvalidBackendResult):
                convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch")

    def test_voucher_read_is_bounded(self):
        real_open = os.open
        real_fdopen = os.fdopen
        reads = []

        class Reader:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def read(self, size): reads.append(size); return self.stream.read(size)

        def fdopen(fd, mode): return Reader(real_fdopen(fd, mode))
        with mock.patch("aaxc.os.open", side_effect=real_open), \
             mock.patch("aaxc.os.fdopen", side_effect=fdopen):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         process_factory=self.factory()[1])
        self.assertEqual(reads, [MAX_VOUCHER_BYTES + 1])

    def test_traversal_cross_profile_and_symlinks_rejected(self):
        raw_traversal = self.audio.parent / ".." / self.asin / self.audio.name
        with self.assertRaises(InvalidBackendResult):
            convert_aaxc(self.core, "anna", raw_traversal, self.destination, "Buch")
        bert = self.paths.downloads("bert") / self.asin
        bert.mkdir(parents=True)
        bert_audio = bert / self.audio.name
        bert_audio.write_bytes(b"x")
        bert_audio.with_suffix(".voucher").write_bytes(self.voucher.read_bytes())
        anna = self.paths.account("anna")
        backup = anna.with_name("anna-real")
        anna.rename(backup)
        try:
            anna.symlink_to(self.paths.account("bert"), target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks werden nicht unterstützt")
        with self.assertRaises(InvalidBackendResult):
            convert_aaxc(self.core, "anna", anna / "downloads" / self.asin / self.audio.name,
                         self.destination, "Buch")
        anna.unlink()
        backup.rename(anna)
        audio_link = self.audio.with_name(f"{self.asin}-LINK.aaxc")
        audio_link.symlink_to(self.audio)
        with self.assertRaises(InvalidBackendResult):
            convert_aaxc(self.core, "anna", audio_link, self.destination, "Buch")
        voucher_real = self.voucher.with_name("real.voucher")
        self.voucher.rename(voucher_real)
        self.voucher.symlink_to(voucher_real)
        with self.assertRaises(InvalidBackendResult):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch")

    def test_self_referencing_audio_link_is_typed(self):
        self.audio.unlink()
        self.audio.symlink_to(self.audio)
        with self.assertRaises(InvalidBackendResult):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch")

    def test_failures_are_typed_redacted_and_clean(self):
        self.core.ffmpeg = None
        with self.assertRaises(BackendUnavailable):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch")
        self.core.ffmpeg = self.ffmpeg
        for options, error in (({"returncode": 7}, BackendFailed),
                               ({"output": False}, InvalidBackendResult)):
            with self.subTest(options=options), self.assertRaises(error) as caught:
                convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                             process_factory=self.factory(**options)[1])
            self.assertNotIn(KEY, str(caught.exception))
            self.assertNotIn(IV, str(caught.exception))
            self.assertEqual(list(self.destination.glob(".hoerwerk-*")), [])

    def test_cancel_before_during_and_before_publish(self):
        calls, factory = self.factory(running=True)
        with self.assertRaises(AaxcCancelled):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         cancelled=lambda: True, process_factory=factory)
        self.assertFalse(calls)
        checks = iter((False, True))
        calls, factory = self.factory(running=True)
        with self.assertRaises(AaxcCancelled):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         cancelled=lambda: next(checks), process_factory=factory)
        self.assertTrue(calls[0].killed)
        checks = iter((False, True))
        with self.assertRaises(AaxcCancelled):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         cancelled=lambda: next(checks), process_factory=self.factory()[1])
        self.assertFalse((self.destination / "Buch.mp3").exists())
        self.assertEqual(list(self.destination.glob(".hoerwerk-*")), [])

    def test_stop_reaps_terminate_race_timeout_and_poll_errors(self):
        exited = FakeProcess([], output=False, terminate_error=ProcessLookupError("gone"))
        _stop(exited)
        self.assertTrue(exited.waits)
        running = FakeProcess([], output=False, running=True)
        _stop(running)
        self.assertTrue(running.killed)
        self.assertEqual(running.waits, [1, None])
        process = FakeProcess([], output=False, poll_error=OSError("secret poll detail"))
        with self.assertRaises(HoerwerkError) as caught:
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         process_factory=lambda *_args, **_kwargs: process)
        self.assertNotIn("secret", str(caught.exception))
        self.assertTrue(process.waits)

    def test_keyboard_interrupt_stops_and_existing_target_survives(self):
        process = FakeProcess([], output=False, poll_error=KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         process_factory=lambda *_args, **_kwargs: process)
        self.assertTrue(process.waits)
        self.destination.mkdir(exist_ok=True)
        target = self.destination / "Buch.mp3"
        target.write_bytes(b"foreign")
        with self.assertRaises(DestinationExists):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         process_factory=self.factory()[1])
        self.assertEqual(target.read_bytes(), b"foreign")
        target.unlink()
        unsupported = OSError(getattr(errno, "EOPNOTSUPP", errno.EPERM), "unsupported")
        with mock.patch("core.os.link", side_effect=unsupported):
            convert_aaxc(self.core, "anna", self.audio, self.destination, "Buch",
                         process_factory=self.factory()[1])
        self.assertEqual(target.read_bytes(), b"synthetic mp3")


if __name__ == "__main__":
    unittest.main()
