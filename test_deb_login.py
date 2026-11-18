import hashlib,io,json,os,shutil,socket,ssl,stat,subprocess,tempfile,time,unittest
from pathlib import Path
from unittest import mock
from audible_entry import error_reason,hoerwerk_login
from browser_login import BACKEND_ERRORS,BrowserLoginController,BrowserLoginError,validate_callback,validate_login_url
from build_deb import DebError,build_deb
from core import PortablePaths
from entry import installed_data_root
SHA="a"*40
class FakeAuth:
    callback=None
    @classmethod
    def from_login_external(cls,**kwargs):
        cls.callback=kwargs["login_url_callback"]("https://www.amazon.de/ap/signin"); return cls()
    def to_file(self,filename,**kwargs): Path(filename).write_text("{}",encoding="utf-8")
class DebLoginTests(unittest.TestCase):
    def setUp(self): self.t=tempfile.TemporaryDirectory(); self.addCleanup(self.t.cleanup); self.root=Path(self.t.name)
    def test_paths_and_urls(self):
        app=self.root/"read only app"; app.mkdir(); data=self.root/"home data"
        self.assertEqual(PortablePaths(app,data).data,data.resolve()); self.assertEqual(PortablePaths(app).data,app.resolve()/"data")
        self.assertEqual(installed_data_root({"XDG_DATA_HOME":str(data)},self.root),data.resolve()/"hoerwerk")
        self.assertEqual(installed_data_root({"XDG_DATA_HOME":"relative"},self.root),self.root/".local/share/hoerwerk")
        self.assertEqual(validate_login_url("https://www.amazon.de/ap/signin"),"https://www.amazon.de/ap/signin")
        good="https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake"; self.assertEqual(validate_callback(good),good)
        bad=("https://www.amazon.de:1234/ap/maplanding?openid.oa2.authorization_code=x","https://www.amazon.de/not-ap/maplanding-evil?openid.oa2.authorization_code=x","https://u@amazon.de/ap/maplanding?openid.oa2.authorization_code=x","https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=x#f")
        for value in bad:
            with self.subTest(value=value),self.assertRaises(BrowserLoginError): validate_callback(value)
    def test_auth_private_no_clobber_and_symlink(self):
        account=self.root/"account"; cb="https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake"
        with mock.patch("sys.stdin.readline",return_value=json.dumps({"type":"callback","url":cb})+"\n"),mock.patch("sys.stdout"):
            self.assertEqual(hoerwerk_login(account,FakeAuth),0)
        self.assertEqual(stat.S_IMODE(account.stat().st_mode),0o700)
        for name in ("auth.json","config.toml"): self.assertEqual(stat.S_IMODE((account/name).stat().st_mode),0o600)
        before=(account/"auth.json").read_bytes()
        with mock.patch("sys.stdout"): self.assertEqual(hoerwerk_login(account,FakeAuth),2)
        self.assertEqual((account/"auth.json").read_bytes(),before)
        outside=self.root/"outside"; outside.mkdir(); link=self.root/"link"; link.symlink_to(outside,target_is_directory=True)
        with mock.patch("sys.stdout"): self.assertEqual(hoerwerk_login(link,FakeAuth),2)
        self.assertFalse((outside/"auth.json").exists())
    def test_controller_cancel_reaps_silent_child(self):
        app=self.root/"app"; (app/"bin").mkdir(parents=True); backend=app/"bin/audible"
        backend.write_text("#!/usr/bin/env python3\nimport signal,time\nsignal.signal(signal.SIGTERM,lambda *a:None)\ntime.sleep(30)\n"); backend.chmod(0o755)
        ctl=BrowserLoginController(PortablePaths(app),"anna",first_timeout=5); ctl.start(); time.sleep(.15); ctl.cancel()
        self.assertTrue(ctl.wait(3)); self.assertFalse(ctl.busy)
        self.assertIn(ctl.events.get(timeout=1)[0],{"cancelled","error"})
    def native(self):
        root=self.root/"native"; shutil.rmtree(root,ignore_errors=True); (root/"bin").mkdir(parents=True); (root/"data").mkdir()
        for rel in ("Hoerwerk","bin/audible","bin/ffmpeg"):
            p=root/rel;p.write_bytes(b"x");p.chmod(0o755)
        manifest={"format":1,"source_sha":SHA,"native_target":{"os":"linux","arch":"x86_64"}}
        (root/"build-manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
        files=["Hoerwerk","bin/audible","bin/ffmpeg","build-manifest.json"]
        hashes={x:hashlib.sha256((root/x).read_bytes()).hexdigest() for x in files}
        (root/"files.sha256.json").write_text(json.dumps(hashes),encoding="utf-8"); return root
    def runner(self,seen):
        def run(argv,**kwargs):
            stage=Path(argv[-2]); seen["stage"]=stage
            seen["control"]=(stage/"DEBIAN/control").read_text(); seen["desktop"]=(stage/"usr/share/applications/hoerwerk.desktop").read_text()
            seen["modes"]={p.relative_to(stage).as_posix():stat.S_IMODE(p.stat().st_mode) for p in stage.rglob("*")}
            Path(argv[-1]).write_bytes(b"deb"); return subprocess.CompletedProcess(argv,0,"","")
        return run
    def test_bundle_exact_modes_and_no_clobber(self):
        native=self.native(); seen={}; out=self.root/"x.deb"; build_deb(native,out,"1.0.0",SHA,runner=self.runner(seen))
        self.assertIn("Package: hoerwerk",seen["control"]);self.assertIn("Terminal=false",seen["desktop"])
        self.assertEqual(seen["modes"]["opt/hoerwerk/Hoerwerk"],0o755);self.assertEqual(seen["modes"]["DEBIAN/control"],0o644)
        with self.assertRaises(DebError):build_deb(native,out,"1.0.0",SHA,runner=self.runner({}))
        (native/"auth.json").write_text("private")
        with self.assertRaises(DebError):build_deb(native,self.root/"y.deb","1.0.0",SHA,runner=self.runner({}))
    def test_bundle_manifest_types_special_and_version(self):
        native=self.native(); (native/"build-manifest.json").write_text("[]")
        with self.assertRaises(DebError):build_deb(native,self.root/"x.deb","1",SHA)
        native=self.native(); fifo=native/"fifo"; os.mkfifo(fifo)
        with self.assertRaises(DebError):build_deb(native,self.root/"z.deb","1",SHA)
        with self.assertRaises(DebError):build_deb(native,self.root/"v.deb","1\nDepends:x",SHA)
    @unittest.skipUnless(shutil.which("dpkg-deb"),"dpkg-deb fehlt")
    def test_real_temporary_deb_fixture(self):
        out=self.root/"fixture.deb"; build_deb(self.native(),out,"1.0.0",SHA)
        field=subprocess.run(["dpkg-deb","--field",out,"Package","Architecture"],text=True,capture_output=True,check=True).stdout
        self.assertIn("Package: hoerwerk",field); self.assertIn("Architecture: amd64",field)
        listing=subprocess.run(["dpkg-deb","--contents",out],text=True,capture_output=True,check=True).stdout
        self.assertIn("./usr/share/applications/hoerwerk.desktop",listing); self.assertNotIn("preinst",listing)
class IndependentDebRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
    def test_portable_data_symlink_is_rejected(self):
        from jobs import require_plain_account
        from core import InvalidBackendResult
        app=self.root/'app';app.mkdir();outside=self.root/'outside';outside.mkdir()
        (app/'data').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(InvalidBackendResult):require_plain_account(PortablePaths(app),'anna')
    def test_backend_rejects_ancestor_link_before_any_write(self):
        outside=self.root/'outside';outside.mkdir();(self.root/'alias').symlink_to(outside,target_is_directory=True)
        with mock.patch('sys.stdout'):
            self.assertEqual(hoerwerk_login(self.root/'alias'/'new'/'account',FakeAuth),2)
        self.assertEqual(list(outside.iterdir()),[])
    def test_failed_config_publish_preserves_foreign_replacement(self):
        import audible_entry
        account=self.root/'account'; publish=audible_entry._publish
        def race(source,target):
            if target.name=='config.toml':
                (account/'auth.json').unlink();(account/'auth.json').write_text('foreign')
                raise OSError('fixture')
            return publish(source,target)
        cb='https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake'
        with mock.patch('sys.stdin.readline',return_value=json.dumps({'type':'callback','url':cb})+'\n'),mock.patch('sys.stdout'),mock.patch.object(audible_entry,'_publish',side_effect=race):
            self.assertEqual(hoerwerk_login(account,FakeAuth),2)
        self.assertEqual((account/'auth.json').read_text(),'foreign')
        self.assertFalse((account/'config.toml').exists())
    def test_sigterm_during_auth_save_cleans_private_transaction(self):
        import sys
        account=self.root/'account'
        code="""import sys,time
from pathlib import Path
from audible_entry import hoerwerk_login
class Auth:
 @classmethod
 def from_login_external(cls,**kw):return cls()
 def to_file(self,filename,**kw):
  Path(filename).write_text('{}');print('WAIT',flush=True);time.sleep(30)
raise SystemExit(hoerwerk_login(Path(sys.argv[1]),Auth))
"""
        process=subprocess.Popen([sys.executable,'-c',code,str(account)],cwd=Path(__file__).parent,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(process.stdout.readline().strip(),'WAIT')
            process.terminate();self.assertEqual(process.wait(timeout=3),2)
            self.assertEqual(list(account.iterdir()),[])
        finally:
            if process.poll() is None:process.kill();process.wait()
            process.stdout.close();process.stderr.close()
    def test_existing_profile_does_not_create_orphan_dialog(self):
        import gui
        from types import SimpleNamespace
        app=object.__new__(gui.App);app.jobs=SimpleNamespace(busy=False);app.login_controller=None
        app.paths=PortablePaths(self.root);app.profile=SimpleNamespace(get=lambda:'anna')
        with mock.patch.object(gui,'BrowserLoginController') as factory,mock.patch.object(gui,'BrowserLoginDialog') as dialog,mock.patch.object(gui.messagebox,'showerror'):
            factory.return_value.start.side_effect=BrowserLoginError('configured')
            app.login();dialog.assert_not_called()
    def test_device_code_waits_for_user_to_copy_and_open_amazon(self):
        import gui
        import threading

        dialog = object.__new__(gui.BrowserLoginDialog)
        dialog.device_value = mock.Mock()
        dialog.info = mock.Mock()
        dialog.code_panel = mock.Mock()
        dialog.actions = mock.Mock()
        dialog.manual_button = mock.Mock()
        dialog.stop = threading.Event()
        browser = mock.Mock()
        dialog.copy_code = browser.copy
        dialog.open_manual = browser.open

        dialog.show_device_code("ABCDE")

        dialog.device_value.set.assert_called_once_with("ABCDE")
        dialog.manual_button.focus_set.assert_called_once_with()
        self.assertEqual(browser.mock_calls, [])

        button_command = dialog.manual_button.configure.call_args.kwargs["command"]
        button_command()
        self.assertEqual(browser.mock_calls, [mock.call.copy(), mock.call.open()])

        dialog.stop.set()
        button_command()
        self.assertEqual(browser.mock_calls, [mock.call.copy(), mock.call.open()])

    def test_dialog_waits_for_worker_cleanup_before_unlocking_app(self):
        import gui,queue,threading
        from types import SimpleNamespace
        done=[];scheduled=[];events=queue.Queue();events.put(('error','fixture'))
        dialog=object.__new__(gui.BrowserLoginDialog)
        dialog.controller=SimpleNamespace(events=events,busy=True)
        dialog.window=SimpleNamespace(winfo_exists=lambda:True,destroy=lambda:done.append('destroy'),after=lambda *a:scheduled.append(a))
        dialog.app=SimpleNamespace(login_finished=lambda *a:done.append(a));dialog.profile='anna';dialog.finished_event=None
        dialog.browser_events=queue.Queue();dialog.stop=threading.Event()
        dialog.poll();self.assertEqual(done,[]);self.assertTrue(scheduled)
        dialog.controller.busy=False;dialog.poll();self.assertEqual(done[-1],('error','fixture','anna'))

class LoginDiagnosisTests(unittest.TestCase):
    def test_start_url_explains_expected_error_page(self):
        with self.assertRaisesRegex(BrowserLoginError,'Startadresse'):
            validate_callback('https://www.amazon.de/ap/signin?fake=1')
    def test_registration_error_protocol_does_not_expose_response(self):
        import io
        class Rejected(FakeAuth):
            @classmethod
            def from_login_external(cls,**kw):
                kw['login_url_callback']('https://www.amazon.de/ap/signin')
                raise Exception({'response':{'error':{'code':'InvalidCodePair','message':'SYNTHETIC_SECRET'}}})
        cb='https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake'
        with tempfile.TemporaryDirectory() as d,mock.patch('sys.stdin.readline',return_value=json.dumps({'type':'callback','url':cb})+'\n'),mock.patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(hoerwerk_login(Path(d)/'account',Rejected),2)
            value=output.getvalue();self.assertNotIn('SYNTHETIC_SECRET',value)
            self.assertEqual(json.loads(value.splitlines()[-1])['reason'],'invalid_code')
    def test_callback_timeout_is_reported_as_timeout(self):
        with tempfile.TemporaryDirectory() as d:
            app=Path(d);(app/'bin').mkdir();program=app/'bin/audible'
            program.write_text('#!/usr/bin/env python3\nimport json,time\nprint(json.dumps({"type":"login_url","url":"https://www.amazon.de/ap/signin"}),flush=True)\ntime.sleep(3)\n');program.chmod(0o755)
            controller=BrowserLoginController(PortablePaths(app),'anna',callback_timeout=.03)
            controller.start();self.assertTrue(controller.wait(3))
            events=list(controller.events.queue)
            errors=[v for k,v in events if k=='error']
            self.assertEqual(len(errors),1);self.assertIn('Wartezeit',errors[0]);self.assertIn('abgelaufen',errors[0])
    def test_typed_backend_error_survives_controller_without_raw_data(self):
        with tempfile.TemporaryDirectory() as d:
            app=Path(d);(app/'bin').mkdir();program=app/'bin/audible'
            program.write_text('#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps({"type":"login_url","url":"https://www.amazon.de/ap/signin"}),flush=True)\nsys.stdin.readline()\nprint(json.dumps({"type":"result","status":"error","reason":"invalid_code"}),flush=True)\nsys.exit(2)\n');program.chmod(0o755)
            ctl=BrowserLoginController(PortablePaths(app),'anna');ctl.start()
            self.assertEqual(ctl.events.get(timeout=2)[0],'url')
            ctl.submit_callback('https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake')
            self.assertTrue(ctl.wait(3));kind,message=ctl.events.get(timeout=1)
            self.assertEqual(kind,'error');self.assertIn('Rückgabecode abgelehnt',message);self.assertNotIn('fake',message)

class RegistrationTransportTests(unittest.TestCase):
    def test_upstream_browser_registration_offline(self):
        try:
            import audible
            import httpx
        except ImportError:
            self.skipTest("Audible-Abhängigkeit nicht installiert")

        synthetic_adp = "{enc:test}{key:test}{iv:test}{name:test}{serial:Mg==}"
        response = {"response": {"success": {
            "tokens": {
                "mac_dms": {"adp_token": synthetic_adp, "device_private_key": "-----BEGIN PRIVATE KEY-----\nTEST\n-----END PRIVATE KEY-----\n"},
                "bearer": {"access_token": "Atna|test", "refresh_token": "Atnr|test", "expires_in": 60},
            },
            "extensions": {"device_info": {}, "customer_info": {}},
        }}}
        cb = "https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=synthetic"
        with tempfile.TemporaryDirectory() as directory:
            account = Path(directory) / "account"
            with mock.patch.object(httpx, "post", return_value=httpx.Response(200, json=response)) as post, \
                 mock.patch("sys.stdin.readline", return_value=json.dumps({"type": "callback", "url": cb}) + "\n"), \
                 mock.patch("sys.stdout", new_callable=io.StringIO) as protocol:
                self.assertEqual(hoerwerk_login(account), 0)
            post.assert_called_once()
            self.assertEqual(post.call_args.args[0], "https://api.amazon.de/auth/register")
            auth_file = json.loads((account / "auth.json").read_text())
            self.assertEqual(auth_file["adp_token"], synthetic_adp)
            self.assertNotIn("synthetic", protocol.getvalue())
            self.assertEqual(json.loads(protocol.getvalue().splitlines()[-1])["status"], "ok")

    def test_fixed_error_categories_follow_bounded_causes(self):
        class ProxyError(Exception): pass
        class ConnectError(Exception): pass
        cases=((ssl.SSLCertVerificationError("SECRET_TLS"),"tls"),
               (socket.gaierror(-2,"SECRET_DNS"),"dns"),
               (TimeoutError("SECRET_TIMEOUT"),"timeout"),
               (ProxyError("SECRET_PROXY"),"proxy"),
               (FileNotFoundError("SECRET_FILE"),"missing_file"),
               (ConnectError("SECRET_CONNECTION"),"connection"),
               (ValueError("SECRET_UNKNOWN"),"registration"))
        for cause,expected in cases:
            outer=RuntimeError("SECRET_OUTER"); outer.__cause__=cause
            with self.subTest(expected=expected): self.assertEqual(error_reason(outer,"registration"),expected)

    def test_protocol_and_ui_never_expose_exception_text(self):
        class Broken(FakeAuth):
            @classmethod
            def from_login_external(cls,**kw):
                kw["login_url_callback"]("https://www.amazon.de/ap/signin")
                outer=RuntimeError("SYNTHETIC_OUTER_SECRET")
                outer.__cause__=ssl.SSLCertVerificationError("SYNTHETIC_TLS_SECRET")
                raise outer
        cb="https://www.amazon.de/ap/maplanding?openid.oa2.authorization_code=fake-secret-code"
        with tempfile.TemporaryDirectory() as d,mock.patch("sys.stdin.readline",return_value=json.dumps({"type":"callback","url":cb})+"\n"),mock.patch("sys.stdout",new_callable=io.StringIO) as output:
            self.assertEqual(hoerwerk_login(Path(d)/"account",Broken),2)
            protocol=output.getvalue(); result=json.loads(protocol.splitlines()[-1])
        self.assertEqual(result,{"type":"result","status":"error","reason":"tls"})
        rendered=" ".join(BACKEND_ERRORS.values())+protocol
        for secret in ("SYNTHETIC_OUTER_SECRET","SYNTHETIC_TLS_SECRET","fake-secret-code"):
            self.assertNotIn(secret,rendered)

if __name__=="__main__":unittest.main()

