import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from core import InvalidBackendResult, PortablePaths
from gui import (App, DownloadResult, completed_download, local_audio_files, login_terminal_argv,
                 run_login_process)
from jobs import (CancellableRunner, JobBusy, JobCancelled, JobController,
                  stop_process)

KEY, IV = "11" * 16, "22" * 16


class FakeProcess:
    def __init__(self, argv=None, *, running=False, code=0, poll_error=None,
                 terminate_error=None, **kwargs):
        self.argv, self.kwargs, self.code = argv, kwargs, code
        self.returncode = None if running else code
        self.running, self.poll_error, self.terminate_error = running, poll_error, terminate_error
        self.terminated = self.killed = False
        self.waits = []

    def poll(self):
        if self.poll_error: raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.running and timeout is not None: raise subprocess.TimeoutExpired("redacted", timeout)
        self.running = False; self.returncode = self.code
        return self.code

    def terminate(self):
        self.terminated = True
        if self.terminate_error: raise self.terminate_error

    def kill(self): self.killed = True; self.running = False; self.returncode = self.code


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = PortablePaths(self.root / "app")
        self.controller = JobController(self.paths)

    def events(self):
        limit = time.monotonic() + 2
        while self.controller.busy and time.monotonic() < limit: time.sleep(.01)
        self.assertFalse(self.controller.busy)
        result = []
        while not self.controller.events.empty(): result.append(self.controller.events.get())
        return result

    def audio(self, profile="anna", asin="B012345678", suffix=".mp3", voucher=True):
        folder = self.paths.downloads(profile) / asin; folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{asin}-LC{suffix}"; path.write_bytes(b"audio")
        if suffix == ".aaxc" and voucher:
            path.with_suffix(".voucher").write_text(json.dumps({"content_license": {
                "status_code": "Granted", "asin": asin,
                "license_response": {"key": KEY, "iv": IV}}}), encoding="utf-8")
        return path

    def test_single_job_busy_profile_capture_and_worker_thread(self):
        gate, seen = threading.Event(), []
        main = threading.get_ident()
        self.controller.start("test", "Anna", "B012345678",
            lambda ctx: (gate.wait(), seen.append((ctx.profile, ctx.asin, threading.get_ident())))[1])
        with self.assertRaises(JobBusy): self.controller.start("other", "bert", None, lambda _: None)
        gate.set(); events = self.events()
        self.assertEqual(seen[0][:2], ("anna", "B012345678"))
        self.assertNotEqual(seen[0][2], main); self.assertEqual(events[-1].state, "success")

    def test_busy_until_cancelled_worker_ends_and_errors_redacted(self):
        entered, release = threading.Event(), threading.Event()
        def work(ctx): entered.set(); release.wait(); ctx.check_cancelled()
        self.controller.start("slow", "anna", None, work); entered.wait()
        self.controller.cancel(); self.assertTrue(self.controller.busy); release.set()
        self.assertEqual(self.events()[-1].state, "cancelled")
        self.controller.start("bad", "anna", None,
                              lambda _: (_ for _ in ()).throw(RuntimeError("SECRET")))
        event = self.events()[-1]
        self.assertEqual(event.state, "error"); self.assertNotIn("SECRET", event.message)

    def test_runner_cancel_poll_error_keyboard_and_stop_races(self):
        cancelled = threading.Event(); cancelled.set(); made = []
        with self.assertRaises(JobCancelled): CancellableRunner(cancelled, lambda *a, **k: made.append(1))(["secret"])
        self.assertFalse(made)
        cancelled.clear(); process = FakeProcess(running=True)
        def factory(*_a, **_k): cancelled.set(); return process
        with self.assertRaises(JobCancelled): CancellableRunner(cancelled, factory)(["secret"])
        self.assertTrue(process.killed); self.assertEqual(process.waits, [1, None])
        broken = FakeProcess(poll_error=OSError("SECRET poll"))
        with self.assertRaises(OSError): CancellableRunner(threading.Event(), lambda *_a, **_k: broken)(["secret"])
        self.assertTrue(broken.terminated); self.assertTrue(broken.waits)
        raced = FakeProcess(terminate_error=ProcessLookupError("gone")); stop_process(raced)
        self.assertTrue(raced.waits)
        interrupted = FakeProcess(poll_error=KeyboardInterrupt())
        with self.assertRaises(JobCancelled): CancellableRunner(threading.Event(), lambda *_a, **_k: interrupted)(["secret"])

    def test_runner_cancels_download_child_process(self):
        heartbeat = self.root / "active"
        child = ("from pathlib import Path; import sys, time\n"
                 "path = Path(sys.argv[1])\n"
                 "while True:\n"
                 "    with path.open('ab') as out: out.write(b'x')\n"
                 "    time.sleep(.04)\n")
        parent = ("import subprocess, sys, time; "
                  "subprocess.Popen([sys.executable, '-c', sys.argv[1], sys.argv[2]]); "
                  "time.sleep(30)")
        self.controller.start("download", "anna", None,
                              lambda ctx: ctx.core().runner([sys.executable, "-c", parent, child, str(heartbeat)]))
        try:
            deadline = time.monotonic() + 6
            while not heartbeat.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(heartbeat.exists(), "Unterprozess hat nicht gestartet")
        finally:
            self.controller.cancel()
        deadline = time.monotonic() + 6
        while self.controller.busy and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual(self.events()[-1].state, "cancelled")
        after = heartbeat.stat().st_size
        time.sleep(.2)
        self.assertEqual(heartbeat.stat().st_size, after)

    def test_runner_separates_download_progress_from_final_response(self):
        process = FakeProcess()
        process.stdout = io.BytesIO(b'HOERWERK-PROGRESS B012345678 8 16\n'
                                    b'HOERWERK-CLI-V1 {"status":"ok"}\n')
        updates = []
        runner = CancellableRunner(threading.Event(), lambda *_a, **_k: process,
                                   download_progress=lambda *args: updates.append(args))
        result = runner(["audible", "--hoerwerk-download", "download"])
        self.assertEqual(updates, [("B012345678", 8, 16)])
        self.assertEqual(result.stdout, 'HOERWERK-CLI-V1 {"status":"ok"}\n')

    def test_runner_reports_conversion_time_without_raw_diagnostics(self):
        process = FakeProcess()
        process.stdout = io.BytesIO(b'out_time_us=5000000\nprogress=continue\n')
        process.stderr = io.BytesIO(b'  Duration: 00:00:10.00, start: 0.000000\n'
                                    b'PRIVATE diagnostics\n')
        updates = []
        runner = CancellableRunner(threading.Event(), lambda *_a, **_k: process,
                                   conversion_progress=updates.append)
        result = runner(["ffmpeg", "-nostdin", "-v", "error", "-i", "private.wav"])
        self.assertEqual(updates[-1].value, 50)
        self.assertEqual(result.stdout, "")

    def test_audio_requires_complete_unique_result(self):
        partial = self.audio(suffix=".aaxc", voucher=False)
        self.assertEqual(local_audio_files(self.paths, "anna", "B012345678"), [])
        partial.with_suffix(".voucher").write_text("{}", encoding="utf-8")
        self.assertEqual(local_audio_files(self.paths, "anna", "B012345678"), [])
        self.audio(suffix=".mp3")
        self.assertEqual(len(local_audio_files(self.paths, "anna", "B012345678")), 1)
        self.audio(suffix=".aaxc", voucher=True)
        with self.assertRaises(InvalidBackendResult):
            local_audio_files(self.paths, "anna", "B012345678")
        with self.assertRaises(InvalidBackendResult):
            local_audio_files(self.paths, "anna", "../escape")

    def test_cross_profile_account_symlink_is_rejected(self):
        bert = self.audio("bert")
        anna = self.paths.account("anna"); anna.parent.mkdir(parents=True, exist_ok=True)
        try: anna.symlink_to(self.paths.account("bert"), target_is_directory=True)
        except OSError: self.skipTest("Symlinks nicht unterstützt")
        with self.assertRaises(InvalidBackendResult):
            local_audio_files(self.paths, "anna", bert.parent.name)
        self.controller.start("library", "anna", None, lambda ctx: ctx.core())
        self.assertEqual(self.events()[-1].state, "error")

    def test_completed_download_accepts_verified_backend_skip(self):
        class Context:
            paths, profile, asin = self.paths, "anna", "B012345678"
            def core(inner):
                class Backend:
                    def download(_self, *_args):
                        self.audio(); raise InvalidBackendResult("skip")
                return Backend()
        self.assertEqual(completed_download(Context()).path.suffix, ".mp3")
        self.audio(asin="B999999999", suffix=".aaxc", voucher=False)
        class Bad(Context):
            asin = "B999999999"
            def core(inner):
                return type("Backend", (), {"download": lambda *_: (_ for _ in ()).throw(InvalidBackendResult("skip"))})()
        with self.assertRaises(InvalidBackendResult): completed_download(Bad())

    def test_completed_download_does_not_restart_complete_file(self):
        expected = self.audio()
        class Context:
            paths, profile, asin = self.paths, "anna", "B012345678"
            def core(inner): raise AssertionError("Backend darf nicht starten")
        self.assertEqual(completed_download(Context()), DownloadResult(expected, already_present=True))

    def test_download_start_clears_previous_success_before_first_worker_update(self):
        app = App.__new__(App)
        app.jobs = mock.Mock()
        app.login_controller = None
        app.profile = mock.Mock()
        app.status = mock.Mock()
        app.set_busy = mock.Mock()
        meter = mock.Mock()
        app.meters = {"download": meter}

        app.start("download", "B012345678", lambda _: None)

        app.status.set.assert_called_once_with("Download wird gestartet …")
        self.assertIsNone(meter.show.call_args.args[0].value)
        app.set_busy.assert_called_once_with(True)

    def test_poll_keeps_scheduling_jobs_after_cover_failure(self):
        app = App.__new__(App)
        app.root = mock.Mock()
        app.closing = False
        app.poll_jobs = mock.Mock()
        app.poll_cover = mock.Mock(side_effect=RuntimeError("simulated callback failure"))

        with self.assertRaises(RuntimeError):
            app.poll()

        app.poll_jobs.assert_called_once_with()
        app.root.after.assert_called_once_with(80, app.poll)

    def test_terminal_platform_arguments_and_boundaries(self):
        backend = self.root / "audible"; backend.write_text("x")
        def which(name): return f"/usr/bin/{name}" if name == "gnome-terminal" else None
        argv, options = login_terminal_argv(self.paths.app_root, "anna", backend,
            platform="linux", which=which, executable="/usr/bin/python3", frozen=False)
        self.assertEqual(argv[1:3], ["--wait", "--"]); self.assertEqual(options, {})
        konsole, _ = login_terminal_argv(self.paths.app_root, "anna", backend,
            platform="linux", which=lambda n: "/bin/konsole" if n == "konsole" else None,
            executable="python3", frozen=False)
        self.assertEqual(konsole[1:3], ["--nofork", "-e"])
        win, options = login_terminal_argv(self.paths.app_root, "anna", backend,
            platform="win32", executable="python.exe", frozen=False)
        self.assertIn("creationflags", options); self.assertNotIn("-e", win)
        frozen, _ = login_terminal_argv(self.paths.app_root, "anna", backend,
            platform="win32", frozen=True, executable="Hoerwerk.exe")
        self.assertEqual(frozen[:2], ["Hoerwerk.exe", "--login"])
        with self.assertRaises(Exception):
            login_terminal_argv(self.paths.app_root, "anna", backend, platform="win32",
                                frozen=False, executable="pythonw.exe")

    def test_login_cancel_uses_bounded_stop(self):
        process = FakeProcess(running=True)
        class Context:
            cancelled = threading.Event(); paths = self.paths; profile = "anna"
        context = Context(); context.cancelled.clear()
        def factory(*_a, **_k): context.cancelled.set(); return process
        with self.assertRaises(JobCancelled):
            run_login_process(context, ["terminal"], {}, factory=factory)
        self.assertTrue(process.terminated); self.assertTrue(process.killed)
        self.assertEqual(process.waits, [1, None])

    def test_login_pre_cancel_never_starts_terminal(self):
        context = type("Ctx", (), {"cancelled": threading.Event()})()
        context.cancelled.set(); made = []
        with self.assertRaises(JobCancelled):
            run_login_process(context, ["terminal"], {}, factory=lambda *a, **k: made.append(1))
        self.assertFalse(made)

    def test_terminal_server_cancel_waits_for_window_without_killing_client(self):
        context = type("Ctx", (), {"cancelled": threading.Event()})()
        process = FakeProcess(running=True)
        polls = iter((None, 0))
        def factory(*_a, **_k):
            context.cancelled.set(); return process
        process.poll = lambda: next(polls)
        with mock.patch("gui.time.sleep"), self.assertRaises(JobCancelled):
            run_login_process(context, ["gnome-terminal", "--wait"], {}, factory=factory)
        self.assertFalse(process.terminated)
        self.assertFalse(process.killed)


if __name__ == "__main__": unittest.main()
