import os
from pathlib import Path
import tempfile
import unittest

from core import (BackendFailed, BackendUnavailable, InvalidBackendResult,
                  InvalidIdentifier, PortablePaths)
from login import (InteractiveTerminalRequired, LoginCancelled,
                   interactive_login, readiness)


class FakeRunner:
    def __init__(self, action=None, returncode=0):
        self.action, self.returncode, self.calls = action, returncode, []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if self.action:
            self.action(argv, kwargs)
        return type("Result", (), {"returncode": self.returncode})()


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        self.backend = self.root / "audible"
        self.backend.write_text("backend", encoding="utf-8")
        self.paths = PortablePaths(self.app)

    def configured(self, profile="Anna", auth="audible.json", content=b"x"):
        account = self.paths.account(profile)
        account.mkdir(parents=True, exist_ok=True)
        (account / "config.toml").write_text(
            f'[APP]\nprimary_profile="audible"\n[profile.audible]\nauth_file="{auth}"\n', encoding="utf-8")
        if Path(auth).name == auth:
            (account / auth).write_bytes(content)
        return account

    def test_launch_isolated_and_inherits_stdio(self):
        account = self.paths.account("anna")
        old = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, old)
        fake = FakeRunner(lambda *_: self.configured())
        state = interactive_login(self.app, "ANNA", backend=self.backend, runner=fake,
                                  terminal=lambda: True)
        argv, options = fake.calls[0]
        self.assertEqual(argv, [str(self.backend), "quickstart"])
        self.assertEqual(options["cwd"], str(account))
        self.assertEqual(options["env"]["AUDIBLE_CONFIG_DIR"], str(account))
        self.assertFalse(options["shell"])
        self.assertNotIn("stdin", options)
        self.assertNotIn("stdout", options)
        self.assertNotIn("stderr", options)
        self.assertEqual(state, "locally_configured_unverified")

    def test_invalid_id_backend_and_terminal(self):
        with self.assertRaises(InvalidIdentifier):
            interactive_login(self.app, "../x", backend=self.backend, terminal=lambda: True)
        with self.assertRaises(BackendUnavailable):
            interactive_login(self.app, "anna", backend=self.root / "missing", terminal=lambda: True)
        with self.assertRaises(BackendUnavailable):
            interactive_login(self.app, "anna", backend=Path("audible"), terminal=lambda: True)
        with self.assertRaises(InteractiveTerminalRequired):
            interactive_login(self.app, "anna", backend=self.backend, terminal=lambda: False)

    def test_exit_error_cancel_and_zero_without_config(self):
        with self.assertRaises(BackendFailed):
            interactive_login(self.app, "anna", backend=self.backend,
                              runner=FakeRunner(returncode=2), terminal=lambda: True)
        with self.assertRaises(LoginCancelled):
            interactive_login(self.app, "anna", backend=self.backend,
                              runner=FakeRunner(lambda *_: (_ for _ in ()).throw(KeyboardInterrupt())),
                              terminal=lambda: True)
        with self.assertRaises(InvalidBackendResult):
            interactive_login(self.app, "bert", backend=self.backend,
                              runner=FakeRunner(), terminal=lambda: True)

    def test_existing_configuration_is_unchanged(self):
        account = self.configured()
        before = (account / "config.toml").read_bytes()
        fake = FakeRunner()
        interactive_login(self.app, "anna", backend=self.backend, runner=fake,
                          terminal=lambda: True)
        self.assertEqual((account / "config.toml").read_bytes(), before)
        self.assertFalse(fake.calls)

    def test_invalid_utf8_is_typed(self):
        account = self.configured()
        (account / "config.toml").write_bytes(b"\xff")
        with self.assertRaises(InvalidBackendResult):
            readiness(self.paths, "anna")

    def test_windows_auth_paths_are_rejected_on_linux_too(self):
        account = self.configured()
        for name in (r"folder\auth.json", r"C:auth.json"):
            (account / "config.toml").write_text(
                '[APP]\nprimary_profile="x"\n[profile.x]\nauth_file=' + repr(name),
                encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(InvalidBackendResult):
                readiness(self.paths, "anna")

    def test_config_symlink_escape_and_account_symlink_are_rejected(self):
        account = self.configured()
        outside = self.root / "outside.toml"
        config = account / "config.toml"
        outside.write_bytes(config.read_bytes())
        config.unlink()
        config.symlink_to(outside)
        with self.assertRaises(InvalidBackendResult):
            readiness(self.paths, "anna")
        other = self.paths.account("bert")
        other.symlink_to(account, target_is_directory=True)
        fake = FakeRunner()
        with self.assertRaises(InvalidBackendResult):
            interactive_login(self.app, "bert", backend=self.backend,
                              runner=fake, terminal=lambda: True)
        self.assertFalse(fake.calls)

    def test_readiness_rejects_bad_toml_types_and_missing_auth(self):
        account = self.paths.account("anna")
        account.mkdir(parents=True)
        config = account / "config.toml"
        for text in ("[", '[APP]\nprimary_profile=3',
                     '[APP]\nprimary_profile="x"\n[profile.x]\nauth_file=3'):
            config.write_text(text, encoding="utf-8")
            with self.subTest(text=text), self.assertRaises(InvalidBackendResult):
                readiness(self.paths, "anna")
        config.write_text('[APP]\nprimary_profile="x"\n[profile.x]\nauth_file="missing.json"', encoding="utf-8")
        with self.assertRaises(InvalidBackendResult):
            readiness(self.paths, "anna")

    def test_empty_traversal_absolute_and_symlink_escape(self):
        self.configured(content=b"")
        with self.assertRaises(InvalidBackendResult):
            readiness(self.paths, "anna")
        account = self.paths.account("anna")
        config = account / "config.toml"
        for name in ("../secret", str((self.root / "secret").resolve())):
            config.write_text(f'[APP]\nprimary_profile="x"\n[profile.x]\nauth_file="{name}"', encoding="utf-8")
            with self.subTest(name=name), self.assertRaises(InvalidBackendResult):
                readiness(self.paths, "anna")
        outside = self.root / "outside"
        outside.write_bytes(b"x")
        link = account / "link.json"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks werden in dieser Umgebung nicht unterstuetzt")
        config.write_text('[APP]\nprimary_profile="x"\n[profile.x]\nauth_file="link.json"', encoding="utf-8")
        with self.assertRaises(InvalidBackendResult):
            readiness(self.paths, "anna")


if __name__ == "__main__":
    unittest.main()
