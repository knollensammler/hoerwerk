import hashlib, io, json, os
from pathlib import Path
import subprocess, sys, tempfile, unittest
from unittest import mock
import zipfile
import entry
from build_portable import BuildError, build, load_staging
from core import HoerwerkError
from gui import login_terminal_argv
SHA="a"*40
class FakeBuilder:
    def __init__(self,hook=None,selfcheck=None,fail=False): self.calls=[]; self.hook=hook; self.selfcheck=selfcheck; self.fail=fail
    def __call__(self,argv,**kwargs):
        self.calls.append((argv,kwargs))
        if "--version" in argv: return subprocess.CompletedProcess(argv,0,"6.fake\n","")
        if "--self-check" in argv:
            if self.selfcheck is not None: return self.selfcheck
            return subprocess.CompletedProcess(argv,0,json.dumps({"import_ok":True,"mode":"self-check","platform":"linux"}),"")
        if self.fail: return subprocess.CompletedProcess(argv,4,"","")
        dist=Path(argv[argv.index("--distpath")+1])/"Hoerwerk"; dist.mkdir(parents=True)
        app=dist/"Hoerwerk"; app.write_bytes(b"fake"); app.chmod(0o755)
        if self.hook: self.hook(dist)
        return subprocess.CompletedProcess(argv,0,"","")
class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup); self.root=Path(self.tmp.name)
        self.repo=self.root/"repo"; self.repo.mkdir(); (self.repo/"entry.py").write_text("# entry")
        self.staging=self.root/"staging"; self.staging.mkdir(); self.output=self.root/"output"; self.write_staging()
    def write_staging(self,mutate=None,executable=True):
        items=[]
        for name in ("audible","ffmpeg"):
            binary=self.staging/f"vendor/{name}"; license_file=self.staging/f"legal/{name}.txt"; binary.parent.mkdir(exist_ok=True); license_file.parent.mkdir(exist_ok=True)
            binary.write_bytes(name.encode()); binary.chmod(0o755 if executable else 0o644); license_file.write_text("license")
            items.append({"name":name,"os":"linux","arch":"x86_64","version":"1","source":f"https://example.invalid/{name}","sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),"file":f"vendor/{name}","license":f"legal/{name}.txt"})
        if mutate: mutate(items)
        (self.staging/"manifest.json").write_text(json.dumps({"backends":items}))
    def build(self,runner=None):
        return build(self.repo,self.output,self.staging,pyinstaller="pyinstaller",runner=runner or FakeBuilder(),target_os="linux",target_arch="x86_64",host_os="linux",host_arch="AMD64",source_sha=SHA)
    def test_entry_self_check_includes_tk_and_gui(self):
        names=[]
        with mock.patch.object(entry.importlib,"import_module",side_effect=lambda n:names.append(n)):
            self.assertTrue(entry._self_check())
        self.assertEqual(names[-2:],["tkinter","gui"])
        with mock.patch.object(entry.importlib,"import_module",side_effect=ImportError): self.assertFalse(entry._self_check())
    def test_login_branches_without_gui_import(self):
        code='import builtins,runpy,sys\nreal=builtins.__import__\ndef imp(n,*a,**k):\n if n in ("tkinter","gui"): raise AssertionError(n)\n return real(n,*a,**k)\nbuiltins.__import__=imp\nsys.argv=["entry.py","--login","/tmp","anna","--backend","/tmp/a"]\nimport login\nlogin.interactive_login=lambda *a,**k:"ok"\nrunpy.run_path(sys.argv[0],run_name="__main__")'
        result=subprocess.run([sys.executable,"-c",code],cwd=Path(entry.__file__).parent,text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
    def test_staging_rejections(self):
        for change in (lambda x:x[0].update(sha256="0"*64),lambda x:x[0].update(file="../x"),lambda x:x[0].update(name=[]),lambda x:x[0].update(source="x")):
            self.write_staging(change)
            with self.assertRaises(BuildError): load_staging(self.staging,"linux","x86_64")
        self.write_staging(executable=False)
        with self.assertRaises(BuildError): load_staging(self.staging,"linux","x86_64")
    def test_success_and_native_gate(self):
        archive=self.build(); final=self.output/"Hoerwerk-linux-x86_64"
        self.assertTrue(archive.is_file()); self.assertTrue(os.access(final/"bin/audible",os.X_OK))
        with zipfile.ZipFile(archive) as z:
            self.assertIn("Hoerwerk-linux-x86_64/data/",z.namelist())
            self.assertTrue((z.getinfo("Hoerwerk-linux-x86_64/bin/audible").external_attr>>16)&0o111)
    def test_native_gate_failures_publish_nothing(self):
        results=(subprocess.CompletedProcess([],2,"{}",""),subprocess.CompletedProcess([],0,'{"import_ok":false,"mode":"self-check","platform":"linux"}',""),subprocess.CompletedProcess([],0,"broken",""))
        for result in results:
            with self.subTest(result=result),self.assertRaises(BuildError): self.build(FakeBuilder(selfcheck=result))
            self.assertFalse((self.output/"Hoerwerk-linux-x86_64").exists()); self.assertFalse((self.output/"Hoerwerk-linux-x86_64.zip").exists())
    def test_internal_file_alias_materialized(self):
        def hook(dist):
            d=dist/"_internal"; d.mkdir(); (d/"lib.so.1").write_bytes(b"lib"); (d/"lib.so").symlink_to("lib.so.1")
        archive=self.build(FakeBuilder(hook=hook)); final=self.output/"Hoerwerk-linux-x86_64/_internal/lib.so"
        self.assertTrue(final.is_file()); self.assertFalse(final.is_symlink()); self.assertEqual(final.read_bytes(),b"lib")
        with zipfile.ZipFile(archive) as z: self.assertFalse((z.getinfo("Hoerwerk-linux-x86_64/_internal/lib.so").external_attr>>16)&0o120000==0o120000)
    def test_unsafe_aliases_rejected(self):
        outside=self.root/"outside"; outside.write_bytes(b"x")
        hooks=[lambda d:(d/"x").symlink_to(outside),lambda d:(d/"x").symlink_to("missing"),lambda d:(d/"x").symlink_to("x"),lambda d:((d/"dir").mkdir(),(d/"x").symlink_to("dir")),lambda d:((d/"auth.json").write_bytes(b"x"),(d/"x").symlink_to("auth.json"))]
        for hook in hooks:
            with self.subTest(hook=hook),self.assertRaises(BuildError): self.build(FakeBuilder(hook=hook))
    def test_missing_pyinstaller_and_host_mismatch(self):
        with mock.patch("build_portable.shutil.which",return_value=None),self.assertRaises(BuildError): build(self.repo,self.output,self.staging,source_sha=SHA,host_os="linux",host_arch="x86_64")
        with self.assertRaises(BuildError): build(self.repo,self.output,self.staging,pyinstaller="p",source_sha=SHA,target_os="windows",host_os="linux",host_arch="x86_64")
    def test_sensitive_output_rejected(self):
        for name in ("auth.json","library.json","config.toml","x.voucher"):
            with self.subTest(name=name),self.assertRaises(BuildError): self.build(FakeBuilder(hook=lambda d,n=name:(d/n).write_bytes(b"private")))
    def test_frozen_login_command(self):
        command,_=login_terminal_argv(self.root,"anna",self.root/"audible",platform="win32",executable="Hoerwerk.exe",frozen=True)
        self.assertEqual(command[:2],["Hoerwerk.exe","--login"])
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

import entry
from build_portable import BuildError, build, load_staging
from core import HoerwerkError
from gui import login_terminal_argv

SHA = "a" * 40


class RegressionFakeBuilder:
    def __init__(self, fail=False, hook=None, executable=True):
        self.calls, self.fail = [], fail
        self.hook, self.executable = hook, executable

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if "--self-check" in argv:
            return subprocess.CompletedProcess(argv,0,json.dumps({"import_ok": True, "mode": "self-check", "platform":"linux"}),"")
        if "--version" in argv:
            return type("Result", (), {"returncode": 0, "stdout": "6.fake\n"})()
        if self.fail:
            return type("Result", (), {"returncode": 4, "stdout": ""})()
        dist = Path(argv[argv.index("--distpath") + 1]) / "Hoerwerk"
        dist.mkdir(parents=True)
        app = dist / "Hoerwerk"
        app.write_bytes(b"fake-app")
        app.chmod(0o755 if self.executable else 0o644)
        if self.hook:
            self.hook(dist)
        return type("Result", (), {"returncode": 0, "stdout": ""})()


class PackagingRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "entry.py").write_text("# entry", encoding="utf-8")
        self.staging = self.root / "staging"
        self.staging.mkdir()
        self.output = self.root / "output"
        self.write_staging()

    def write_staging(self, *, target_os="linux", target_arch="x86_64",
                      mutate=None, executable=True):
        items = []
        for name in ("audible", "ffmpeg"):
            binary = self.staging / f"vendor/{name}.bin"
            license_file = self.staging / f"legal/{name}.txt"
            binary.parent.mkdir(exist_ok=True)
            license_file.parent.mkdir(exist_ok=True)
            binary.write_bytes((name + "-binary").encode())
            binary.chmod(0o755 if executable else 0o644)
            license_file.write_text(name + " license", encoding="utf-8")
            items.append({
                "name": name,
                "os": target_os,
                "arch": target_arch,
                "version": "test-1",
                "source": f"https://example.invalid/{name}",
                "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                "file": f"vendor/{name}.bin",
                "license": f"legal/{name}.txt",
            })
        if mutate:
            mutate(items)
        (self.staging / "manifest.json").write_text(
            json.dumps({"backends": items}), encoding="utf-8"
        )

    def build(self, **kwargs):
        return build(
            self.repo,
            self.output,
            self.staging,
            pyinstaller="pyinstaller",
            runner=kwargs.pop("runner", RegressionFakeBuilder()),
            target_os="linux",
            target_arch="x86_64",
            host_os="linux",
            host_arch="AMD64",
            source_sha=SHA,
            **kwargs,
        )

    def test_entry_modes_and_real_self_check_import_failure(self):
        seen = []
        self.assertEqual(
            entry.app_root(
                frozen=True,
                executable=str(self.root / "bundle/App"),
            ),
            self.root / "bundle",
        )
        entry.main([], gui_func=seen.append, frozen=False)
        self.assertEqual(seen, [Path(entry.__file__).resolve().parent])
        login_seen = []
        backend = self.root / "audible"
        self.assertEqual(
            entry.main(
                ["--login", str(self.root), "anna", "--backend", str(backend)],
                login_func=lambda *a, **k: (
                    login_seen.append((a, k))
                    or "locally_configured_unverified"
                ),
            ),
            0,
        )
        self.assertEqual(login_seen[0][0], (self.root, "anna"))
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            self.assertEqual(entry.main(["--self-check"]), 0)
        self.assertTrue(json.loads(output.getvalue())["import_ok"])
        output = io.StringIO()
        with mock.patch.object(entry.sys, "stdout", output), \
             mock.patch.object(
                 entry.importlib, "import_module", side_effect=ImportError
             ):
            self.assertEqual(entry.main(["--self-check"]), 2)
        self.assertFalse(json.loads(output.getvalue())["import_ok"])

    def test_entry_error_and_frozen_login(self):
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(
                entry.main(
                    ["--login", str(self.root), "anna", "--backend",
                     str(self.root / "audible")],
                    login_func=lambda *_a, **_k: (
                        _ for _ in ()
                    ).throw(HoerwerkError("keine Konfiguration")),
                ),
                2,
            )
        command, _ = login_terminal_argv(
            self.root,
            "anna",
            self.root / "audible",
            platform="win32",
            executable="Hoerwerk.exe",
            frozen=True,
        )
        self.assertEqual(command[:2], ["Hoerwerk.exe", "--login"])

    def test_missing_pyinstaller_is_mocked_and_host_mismatch_rejected(self):
        with mock.patch("build_portable.shutil.which", return_value=None), \
             self.assertRaises(BuildError):
            build(
                self.repo,
                self.output,
                self.staging,
                source_sha=SHA,
                host_os="linux",
                host_arch="x86_64",
            )
        self.assertFalse(self.output.exists())
        with self.assertRaises(BuildError):
            build(
                self.repo,
                self.output,
                self.staging,
                pyinstaller="p",
                source_sha=SHA,
                target_os="windows",
                target_arch="x86_64",
                host_os="linux",
                host_arch="x86_64",
            )

    def test_staging_metadata_paths_hash_and_modes(self):
        changes = (
            lambda x: x[0].update(sha256="0" * 64),
            lambda x: x[0].update(os="windows"),
            lambda x: x[0].update(license="missing"),
            lambda x: x[0].update(file="../outside"),
            lambda x: x[0].update(name=["audible"]),
            lambda x: x[0].update(source="not-a-url"),
        )
        for change in changes:
            self.write_staging(mutate=change)
            with self.subTest(change=change), self.assertRaises(BuildError):
                load_staging(self.staging, "linux", "x86_64")
        self.write_staging(executable=False)
        with self.assertRaises(BuildError):
            load_staging(self.staging, "linux", "x86_64")
        self.write_staging()
        binary = self.staging / "vendor/audible.bin"
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        binary.unlink()
        try:
            binary.symlink_to(outside)
        except OSError:
            self.skipTest("Symlinks nicht unterstützt")
        with self.assertRaises(BuildError):
            load_staging(self.staging, "linux", "x86_64")

    def test_success_modes_empty_directory_and_manifests(self):
        archive = self.build()
        final = self.output / "Hoerwerk-linux-x86_64"
        self.assertTrue(os.access(final / "Hoerwerk", os.X_OK))
        self.assertTrue(os.access(final / "bin/audible", os.X_OK))
        manifest = json.loads((final / "build-manifest.json").read_text())
        self.assertEqual(manifest["source_sha"], SHA)
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertIn("Hoerwerk-linux-x86_64/data/", names)
            mode = (
                bundle.getinfo(
                    "Hoerwerk-linux-x86_64/bin/audible"
                ).external_attr >> 16
            )
            self.assertTrue(mode & 0o111)
        self.assertIn(
            "bin/audible",
            json.loads((final / "files.sha256.json").read_text()),
        )

    def test_output_symlink_and_sensitive_files_are_rejected(self):
        private = self.root / "private"
        private.write_bytes(b"secret")

        def linked(dist):
            (dist / "auth.json").symlink_to(private)

        with self.assertRaises(BuildError):
            self.build(runner=RegressionFakeBuilder(hook=linked))
        self.assertFalse(
            self.output.joinpath("Hoerwerk-linux-x86_64").exists()
        )
        for name in (
            "auth.json",
            "library.json",
            "config.toml",
            "token.voucher",
        ):
            def sensitive(dist, name=name):
                (dist / name).write_bytes(b"private")

            with self.subTest(name=name), self.assertRaises(BuildError):
                self.build(runner=RegressionFakeBuilder(hook=sensitive))

    def test_app_executable_and_provenance_are_required(self):
        with self.assertRaises(BuildError):
            self.build(runner=RegressionFakeBuilder(executable=False))
        with self.assertRaises(BuildError):
            build(
                self.repo,
                self.output,
                self.staging,
                pyinstaller="p",
                runner=RegressionFakeBuilder(),
                host_os="linux",
                host_arch="x86_64",
                source_sha="abc123",
            )

    def test_no_clobber_races_and_failures_preserve_foreign_data(self):
        final = self.output / "Hoerwerk-linux-x86_64"
        archive = final.with_suffix(".zip")
        self.output.mkdir()
        archive.write_bytes(b"foreign")
        with self.assertRaises(BuildError):
            self.build()
        self.assertEqual(archive.read_bytes(), b"foreign")
        archive.unlink()
        final.mkdir()
        with self.assertRaises(BuildError):
            self.build()
        self.assertTrue(final.is_dir())
        final.rmdir()
        with self.assertRaises(BuildError):
            self.build(runner=RegressionFakeBuilder(fail=True))
        self.assertFalse(final.exists())
        self.assertFalse(archive.exists())

    def test_zip_publish_race_never_deletes_foreign_zip(self):
        foreign = b"foreign-race"

        def race(source, target):
            Path(target).write_bytes(foreign)
            raise FileExistsError

        with mock.patch("build_portable.os.link", side_effect=race), \
             self.assertRaises(BuildError):
            self.build()
        archive = self.output / "Hoerwerk-linux-x86_64.zip"
        self.assertEqual(archive.read_bytes(), foreign)
        self.assertFalse(
            self.output.joinpath("Hoerwerk-linux-x86_64").exists()
        )

    def test_archive_write_failure_publishes_nothing(self):
        with mock.patch(
            "build_portable._write_zip",
            side_effect=OSError("disk full"),
        ), self.assertRaises(OSError):
            self.build()
        self.assertFalse(
            self.output.joinpath("Hoerwerk-linux-x86_64").exists()
        )
        self.assertFalse(
            self.output.joinpath("Hoerwerk-linux-x86_64.zip").exists()
        )




class NativeGateTypeTests(unittest.TestCase):
    def test_non_object_and_non_boolean_result_rejected(self):
        from build_portable import _native_self_check
        for value in ('[]', 'null', '1', '{"import_ok":1,"mode":"self-check","platform":"linux"}'):
            with self.subTest(value=value), self.assertRaises(BuildError):
                _native_self_check(lambda *a, **k: subprocess.CompletedProcess([], 0, value, ''), Path('/unused'), Path('/tmp'))

if __name__=="__main__": unittest.main()
