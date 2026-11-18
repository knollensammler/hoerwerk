import errno
import json
import os
from pathlib import Path
import tempfile
import threading
import sys
import unittest
from unittest import mock

from core import (BackendFailed, BackendUnavailable, Core, DestinationExists,
                  ExportFailed, InvalidBackendResult, InvalidIdentifier,
                  PortablePaths, UnsupportedAudio, safe_filename)


class FakeRunner:
    def __init__(self, action=None, returncode=0):
        self.action, self.returncode, self.calls = action, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.action:
            self.action(argv, kwargs)
        stdout = ('HOERWERK-CLI-V1 {"status":"ok"}\n'
                  if "--hoerwerk-download" in argv and self.returncode == 0 else "")
        return type("Result", (), {"returncode": self.returncode,"stdout":stdout})()


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.paths = PortablePaths(self.root / "app")

    def encoded_core(self):
        source = self.root / "audio.m4a"
        source.write_bytes(b"audio")

        def encode(argv, _):
            Path(argv[-1]).write_bytes(b"mp3")
        return source, Core(self.paths, ffmpeg="ffmpeg", runner=FakeRunner(encode))

    def test_paths_ignore_cwd_and_separate_profiles(self):
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        self.assertEqual(self.paths.account("Anna"), self.root / "app/data/accounts/anna")
        self.assertEqual(self.paths.account("ANNA"), self.paths.account("anna"))
        self.assertNotEqual(self.paths.account("anna"), self.paths.account("bert"))
        for value in ("../anna", "-option", "a/b", "", "CON", "con"):
            with self.assertRaises(InvalidIdentifier):
                self.paths.account(value)

    def test_invalid_asin_rejected_before_process(self):
        fake = FakeRunner()
        core = Core(self.paths, audible="audible", runner=fake)
        for asin in ("../BAD", "-123456789", "short"):
            with self.assertRaises(InvalidIdentifier):
                core.download("anna", asin)
        self.assertFalse(fake.calls)

    def test_missing_backend_and_exit_failure_are_typed(self):
        with self.assertRaises(BackendUnavailable):
            Core(self.paths).refresh_library("anna")
        with self.assertRaises(BackendFailed):
            Core(self.paths, audible="audible", runner=FakeRunner(returncode=7)).refresh_library("anna")

    def test_library_success_uses_account_environment(self):
        def export(argv, kwargs):
            Path(argv[argv.index("--output") + 1]).write_text(
                json.dumps([{"asin": "B012345678", "title": "Titel"}]), encoding="utf-8")
        fake = FakeRunner(export)
        items = Core(self.paths, audible="audible", runner=fake).refresh_library("anna")
        self.assertEqual(items[0]["title"], "Titel")
        self.assertEqual(fake.calls[0][1]["env"]["AUDIBLE_CONFIG_DIR"], str(self.paths.account("anna")))
        self.assertFalse(fake.calls[0][1]["shell"])

    def test_bad_library_types_preserve_existing_library(self):
        account = self.paths.account("anna")
        account.mkdir(parents=True)
        final = account / "library.json"
        final.write_text("old", encoding="utf-8")
        invalid = (None, 42, [], {})
        for asin in invalid:
            def export(argv, _, asin=asin):
                Path(argv[-1]).write_text(json.dumps([{"asin": asin, "title": "Book"}]), encoding="utf-8")
            with self.subTest(asin=asin), self.assertRaises(InvalidBackendResult):
                Core(self.paths, audible="audible", runner=FakeRunner(export)).refresh_library("anna")
            self.assertEqual(final.read_text(encoding="utf-8"), "old")

    def test_missing_broken_and_wrong_library_results(self):
        actions = (
            None,
            lambda argv, _: Path(argv[-1]).write_text("{", encoding="utf-8"),
            lambda argv, _: Path(argv[-1]).write_text('{"items": []}', encoding="utf-8"),
        )
        for action in actions:
            with self.subTest(action=action), self.assertRaises(InvalidBackendResult):
                Core(self.paths, audible="audible", runner=FakeRunner(action)).refresh_library("anna")

    def test_download_requires_clear_audio_and_voucher(self):
        def unclear(argv, _):
            (Path(argv[argv.index("--output-dir") + 1]) / "note.txt").write_text("x")
        with self.assertRaises(InvalidBackendResult):
            Core(self.paths, audible="audible", runner=FakeRunner(unclear)).download("anna", "B012345678")

        def no_voucher(argv, _):
            (Path(argv[argv.index("--output-dir") + 1]) / "B012345678-LC.aaxc").write_bytes(b"x")
        with self.assertRaises(InvalidBackendResult):
            Core(self.paths, audible="audible", runner=FakeRunner(no_voucher)).download("anna", "B012345678")

    def test_conversion_sanitizes_and_never_overwrites(self):
        source, core = self.encoded_core()
        target = core.convert_playable_to_mp3(source, self.root / "usb", '../CON: böse?')
        self.assertEqual(target.name, "_CON_ böse_.mp3")
        with self.assertRaises(DestinationExists):
            core.convert_playable_to_mp3(source, self.root / "usb", '../CON: böse?')
        protected = self.root / "book.aaxc"
        protected.write_bytes(b"x")
        with self.assertRaises(UnsupportedAudio):
            core.convert_playable_to_mp3(protected, self.root / "usb", "Buch")
        self.assertEqual(safe_filename("CON.txt"), "_CON.txt")
        self.assertEqual(safe_filename("lpt1.any"), "_lpt1.any")

    def test_unsupported_hardlink_uses_exclusive_fallback(self):
        source, core = self.encoded_core()
        unsupported = OSError(getattr(errno, "EOPNOTSUPP", errno.EPERM), "unsupported")
        with mock.patch("core.os.link", side_effect=unsupported):
            target = core.convert_playable_to_mp3(source, self.root / "usb", "Titel")
        self.assertEqual(target.read_bytes(), b"mp3")

    def test_fallback_failure_is_typed_and_cleans_own_partial_file(self):
        source, core = self.encoded_core()
        target_dir = self.root / "usb"
        unsupported = OSError(getattr(errno, "EOPNOTSUPP", errno.EPERM), "unsupported")
        real_fdopen = os.fdopen

        class BrokenWriter:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.file.close()
            def write(self, _):
                raise OSError(errno.ENOSPC, "full")
            def __init__(self, fd):
                self.file = real_fdopen(fd, "wb")

        with mock.patch("core.os.link", side_effect=unsupported), \
             mock.patch("core.os.fdopen", side_effect=lambda fd, _: BrokenWriter(fd)), \
             self.assertRaises(ExportFailed):
            core.convert_playable_to_mp3(source, target_dir, "Titel")
        self.assertEqual(list(target_dir.iterdir()), [])

    def test_cancelled_fallback_cleans_own_file(self):
        source, core = self.encoded_core()
        target_dir = self.root / "usb"
        with mock.patch("core.os.link", side_effect=OSError(errno.EPERM, "unsupported")), \
             mock.patch("core.os.fsync", side_effect=KeyboardInterrupt), \
             self.assertRaises(KeyboardInterrupt):
            core.convert_playable_to_mp3(source, target_dir, "Titel")
        self.assertEqual(list(target_dir.iterdir()), [])
        self.assertEqual(source.read_bytes(), b"audio")

    def test_concurrent_target_in_fallback_is_preserved(self):
        source, core = self.encoded_core()
        target_dir = self.root / "usb"

        def race(_, target):
            Path(target).write_bytes(b"foreign")
            raise OSError(getattr(errno, "EOPNOTSUPP", errno.EPERM), "unsupported")

        with mock.patch("core.os.link", side_effect=race), self.assertRaises(DestinationExists):
            core.convert_playable_to_mp3(source, target_dir, "Titel")
        self.assertEqual((target_dir / "Titel.mp3").read_bytes(), b"foreign")

    def test_conversion_failure_cleans_temporary_file(self):
        source = self.root / "audio.wav"
        source.write_bytes(b"audio")
        target_dir = self.root / "usb"
        with self.assertRaises(BackendFailed):
            Core(self.paths, ffmpeg="ffmpeg", runner=FakeRunner(returncode=1)).convert_playable_to_mp3(
                source, target_dir, "Titel")
        self.assertEqual(list(target_dir.iterdir()), [])
        self.assertEqual(safe_filename('  bad/<name>:*  '), "bad__name___")

    def executable(self, body):
        path=self.root/"audible"
        path.write_text("#!"+sys.executable+"\n"+body,encoding="utf-8")
        path.chmod(path.stat().st_mode|0o100);return path

    def test_download_wrapper_runner_core_redacts_original_exception(self):
        from jobs import CancellableRunner
        source=Path(__file__).parent
        backend=self.executable("""import sys,importlib,ssl
sys.path.insert(0,%r)
class CLI:
 def __call__(self,**kwargs):raise ssl.SSLCertVerificationError('SYNTHETIC_SECRET')
module=importlib.import_module('audible_cli.cli');module.cli=CLI()
from audible_entry import main
main()
"""%str(source))
        with self.assertRaisesRegex(BackendFailed,"Zertifikatsprüfung") as caught:
            Core(self.paths,audible=backend,runner=CancellableRunner(threading.Event())).download("anna","B012345678")
        self.assertNotIn("SYNTHETIC_SECRET",str(caught.exception))

    def test_download_reader_flood_cancel_and_unknown_output_are_bounded(self):
        from jobs import CancellableRunner,JobCancelled
        flood=self.executable("import sys;sys.stdout.write('x'*200000);sys.stdout.flush()")
        result=CancellableRunner(threading.Event())([str(flood),"--hoerwerk-download","download"])
        self.assertEqual(result.stdout,"")
        unknown=self.executable("print('SECRET untrusted instruction');raise SystemExit(2)")
        with self.assertRaisesRegex(BackendFailed,"unbekannter Ursache") as caught:
            Core(self.paths,audible=unknown,runner=CancellableRunner(threading.Event())).download("anna","B012345678")
        self.assertNotIn("SECRET",str(caught.exception))
        cancel=threading.Event();noisy=self.executable("import sys,time\nwhile True: sys.stdout.write('x'*4096);sys.stdout.flush();time.sleep(.001)")
        timer=threading.Timer(.05,cancel.set);timer.start()
        try:
            with self.assertRaises(JobCancelled):CancellableRunner(cancel)([str(noisy),"--hoerwerk-download","download"])
        finally:timer.cancel()

    def test_download_protocol_rejects_non_string_reason(self):
        for reason in ([],{},True,123,None):
            def runner(argv,**kwargs):
                return type("Result",(),{"returncode":2,"stdout":'HOERWERK-CLI-V1 '+json.dumps({"status":"error","reason":reason})+'\n'})()
            core=Core(self.paths,audible=Path("synthetic"),runner=runner)
            with self.subTest(reason=reason),self.assertRaisesRegex(BackendFailed,"unbekannter Ursache"):
                core._run(core.audible,["--hoerwerk-download","download"],download_protocol=True)

    def test_http_status_is_not_a_license_decision(self):
        from audible_entry import download_error_reason
        for status,reason in ((401,"access_denied"),(403,"access_denied"),(404,"resource_missing"),(410,"resource_missing"),(429,"throttled")):
            error=RuntimeError("SYNTHETIC_PRIVATE_RESPONSE");error.status_code=status
            with self.subTest(status=status):self.assertEqual(download_error_reason(error),reason)
        try:from audible_cli.exceptions import LicenseDenied,NoDownloadUrl
        except ImportError:self.skipTest("Audible-CLI nicht installiert")
        self.assertEqual(download_error_reason(LicenseDenied("PRIVATE")),"license_denied")
        self.assertEqual(download_error_reason(NoDownloadUrl("SYNTHETIC")),"title_unavailable")

    def test_real_pinned_cli_wrapper_help_offline(self):
        try:import audible_cli
        except ImportError:self.skipTest("Audible-CLI nicht installiert")
        import subprocess
        env=os.environ.copy();env["AUDIBLE_CONFIG_DIR"]=str(self.root/"isolated config")
        result=subprocess.run([sys.executable,str(Path(__file__).with_name("audible_entry.py")),"--hoerwerk-download","download","--help"],env=env,capture_output=True,text=True,timeout=10)
        self.assertEqual((result.returncode,result.stdout),(0,'HOERWERK-CLI-V1 {"status":"ok"}\n'))
        self.assertEqual(result.stderr,"")

    def test_export_guard_opens_no_dialog_without_audio(self):
        import gui
        app=object.__new__(gui.App);app.paths=self.paths
        app.profile=type("Profile",(),{"get":lambda _self:"anna"})()
        app.selected_many=lambda:[{"asin":"B012345678","title":"Titel"}]
        with mock.patch.object(gui,"local_audio_files",return_value=[]),mock.patch.object(gui.filedialog,"askdirectory") as directory,mock.patch.object(gui.messagebox,"askokcancel") as warning,mock.patch.object(gui.messagebox,"showinfo") as info:
            app.export()
        directory.assert_not_called();warning.assert_not_called();info.assert_called_once()


if __name__ == "__main__":
    unittest.main()

