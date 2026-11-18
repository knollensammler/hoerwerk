import contextlib,io,json,subprocess,unittest
from unittest import mock
from pathlib import Path
import audible_entry
from build_backend import check_backend,BackendBuildError

class TLSBundleTests(unittest.TestCase):
    def test_tls_check_real_context_without_network(self):
        try:import certifi
        except ImportError:self.skipTest("certifi nicht installiert")
        output=io.StringIO()
        with mock.patch("sys.argv",["audible","--hoerwerk-tls-self-check"]),contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:audible_entry.main()
        self.assertEqual(stopped.exception.code,0)
        result=json.loads(output.getvalue());self.assertTrue(result["tls_ok"])
        self.assertTrue(result["verify_required"]);self.assertTrue(result["hostname_checked"]);self.assertGreater(result["ca_count"],0)

    def test_tls_check_missing_runtime_file_does_not_expose_path(self):
        output=io.StringIO()
        with mock.patch("sys.argv",["audible","--hoerwerk-tls-self-check"]),mock.patch("ssl.create_default_context",side_effect=FileNotFoundError("PRIVATE_PATH")),contextlib.redirect_stdout(output):
            with self.assertRaises(SystemExit) as stopped:audible_entry.main()
        self.assertEqual(stopped.exception.code,2)
        self.assertEqual(json.loads(output.getvalue()),{"tls_ok":False,"reason":"missing_file"});self.assertNotIn("PRIVATE_PATH",output.getvalue())

    def test_native_gate_rejects_disabled_verification_and_failed_context(self):
        good={"tls_ok":True,"ca_count":119,"verify_required":True,"hostname_checked":True}
        for value in ({**good,"verify_required":False},{**good,"hostname_checked":False},{**good,"ca_count":0},{"tls_ok":False,"reason":"missing_file"}):
            def run(args,**kwargs):
                self.assertEqual(args,["backend","--hoerwerk-tls-self-check"])
                return subprocess.CompletedProcess(args,0,json.dumps(value),"")
            with self.subTest(value=value),self.assertRaises(BackendBuildError):check_backend(Path("backend"),run)

if __name__=="__main__":unittest.main()
