"""Echter gepinnter CLI-/Lizenz-/Worker-/Dateipfad mit rein synthetischem HTTP."""
import contextlib,hashlib,io,json,subprocess,tempfile,types,unittest
from pathlib import Path
from unittest import mock
import httpx
from audible_cli.cmds import cmd_download, cmd_library
from audible_cli.downloader import Downloader
from transfer_progress import DOWNLOAD_OPTION
from audible_cli.config import Session
from audible_cli.models import Library, LibraryItem
from audible_entry import hoerwerk_download, hoerwerk_library, DOWNLOAD_DIAGNOSTICS
from core import BackendFailed,Core,PortablePaths,_DOWNLOAD_DIAGNOSTICS

class MediaStream(httpx.AsyncByteStream):
    async def __aiter__(self):yield b'synthetic audio data'

class DownloadPipelineTests(unittest.TestCase):
    def pipeline(self,failure=None, progress=False):
        original=cmd_download.DownloadRun
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=PortablePaths(root/'app');account=paths.account('anna');account.mkdir(parents=True)
            (account/'config.toml').write_text('[APP]\nprimary_profile="standard"\n[profile.standard]\nauth_file="auth.json"\ncountry_code="de"\n')
            calls=[]
            def transport(request):
                calls.append(request.method)
                if failure=='media':raise KeyError('SYNTHETIC_SECRET https://secret.invalid/key')
                return httpx.Response(200,stream=MediaStream(),headers={'Content-Type':'audio/aax','Content-Length':'20'})
            api=types.SimpleNamespace(session=httpx.AsyncClient(transport=httpx.MockTransport(transport)))
            async def post(*a,**kw):
                if failure=='license':return {}
                return {'content_license':{'status_code':'Granted','content_metadata':{'content_url':{'offline_url':'https://synthetic.invalid/audio'},'content_reference':{'content_format':'AAXC'}}}}
            api.post=post
            item=LibraryItem({'asin':'B012345678','title':'SYNTHETIC_SECRET','customer_rights':{'is_consumable_offline':True}},api)
            library=types.SimpleNamespace(has_asin=lambda a:True,get_item_by_asin=lambda a:item)
            captured=[]
            updates=[]
            def runner(argv,**kwargs):
                self.assertEqual(argv[1:4],['--hoerwerk-download','download','--aaxc'])
                output,errors=io.StringIO(),io.StringIO()
                with mock.patch.dict('os.environ',kwargs['env'],clear=True), \
                     mock.patch.object(Session,'get_client',return_value=api), \
                     mock.patch.object(cmd_download.Library,'from_api_full_sync',new=mock.AsyncMock(return_value=library)), \
                     mock.patch.object(Downloader, 'MIN_STREAM_LENGTH', 1 if progress else 10*1024*1024), \
                     contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
                    code=hoerwerk_download([*argv[2:], DOWNLOAD_OPTION] if progress else argv[2:])
                self.assertNotIn('SYNTHETIC_SECRET',output.getvalue()+errors.getvalue())
                self.assertNotIn('https://',output.getvalue()+errors.getvalue())
                lines = output.getvalue().splitlines(keepends=True)
                updates.extend(lines[:-1])
                captured.append(json.loads(lines[-1].split(' ',1)[1]))
                return subprocess.CompletedProcess(argv,code,lines[-1],'')
            core=Core(paths,audible='synthetic-backend',runner=runner)
            if failure:
                with self.assertRaises(BackendFailed) as caught:core.download('anna','B012345678')
                message=str(caught.exception)
                self.assertNotIn('SYNTHETIC_SECRET',message)
                self.assertEqual(captured[0]['reason'],'unknown')
                expected='D-A-K' if failure=='license' else 'D-M-K'
                self.assertEqual(captured[0]['diagnostic'],expected)
                self.assertIn(expected,message)
            else:
                files=core.download('anna','B012345678')
                self.assertEqual({p.suffix for p in files},{'.aaxc','.voucher'})
                audio=next(p for p in files if p.suffix=='.aaxc')
                self.assertEqual(audio.read_bytes(),b'synthetic audio data')
                self.assertEqual(captured,[{'status':'ok'}]);self.assertEqual(calls,['GET','GET'])
                if progress:
                    self.assertEqual(updates, ['HOERWERK-PROGRESS B012345678 0 20\n',
                                               'HOERWERK-PROGRESS B012345678 20 20\n'])
                else:
                    self.assertEqual(updates, [])
            self.assertIs(cmd_download.DownloadRun,original)

    def test_library_export_requests_and_normalizes_offline_rights(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "config.toml").write_text(
                '[APP]\nprimary_profile="standard"\n[profile.standard]\n'
                'auth_file="auth.json"\ncountry_code="de"\n', encoding="utf-8")
            output = folder / "export.json"
            api = types.SimpleNamespace(session=httpx.AsyncClient())
            raw_items = [
                {"asin": "B000000001", "title": "Ja", "customer_rights": {
                    "is_consumable_offline": True, "unused": "PRIVATE"}},
                {"asin": "B000000002", "title": "Nein", "customer_rights": {"is_consumable_offline": False}},
                {"asin": "B000000003", "title": "Unklar"},
                {"asin": "B000000004", "title": "Vorbestellung", "publication_datetime": "2099-01-01T00:00:00Z",
                 "customer_rights": {"is_consumable_offline": True}},
            ]
            library = Library(raw_items, api)
            fetch = mock.AsyncMock(return_value=library)
            with mock.patch.dict("os.environ", {"AUDIBLE_CONFIG_DIR": str(folder)}), \
                 mock.patch.object(Session, "get_client", return_value=api), \
                 mock.patch.object(Library, "from_api_full_sync", fetch):
                hoerwerk_library(output)
            self.assertIn("customer_rights", fetch.call_args.kwargs["response_groups"].split(", "))
            self.assertIs(cmd_library.Library, Library)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([item["download_allowed"] for item in exported], [True, False, None, False])
            self.assertEqual([item["title"] for item in exported], ["Ja", "Nein", "Unklar", "Vorbestellung"])
            self.assertTrue(all("customer_rights" not in item for item in exported))

    def test_full_cli_pipeline_success(self):self.pipeline()
    def test_streaming_pipeline_emits_only_numeric_progress(self):self.pipeline(progress=True)
    def test_license_key_error_has_fixed_phase(self):self.pipeline('license')
    def test_media_key_error_has_different_fixed_phase(self):self.pipeline('media')

    def test_protocol_invalid_data_has_own_code(self):
        self.assertEqual(DOWNLOAD_DIAGNOSTICS,_DOWNLOAD_DIAGNOSTICS)
        values=[('', 'P01'),('x'*2000,'P01'),('HOERWERK-CLI-V1 {broken}\n','P01')]
        for obj in [[],{'status':'ok','extra':'SYNTHETIC_SECRET'},
                    {'status':'error','reason':'unknown','diagnostic':[]},
                    {'status':'error','reason':'unknown','diagnostic':'SYNTHETIC_SECRET'},
                    {'status':'error','reason':'unknown','diagnostic':'D-A-K','extra':True}]:
            values.append(('HOERWERK-CLI-V1 '+json.dumps(obj)+'\n','P02'))
        with tempfile.TemporaryDirectory() as tmp:
            for line,code in values:
                with self.subTest(code=code,line=line[:60]):
                    runner=lambda argv,**kw:subprocess.CompletedProcess(argv,2,line,'')
                    with self.assertRaises(BackendFailed) as caught:Core(PortablePaths(Path(tmp)),audible='synthetic',runner=runner).download('anna','B012345678')
                    self.assertIn('Diagnose '+code,str(caught.exception));self.assertNotIn('SYNTHETIC_SECRET',str(caught.exception))

    def test_legacy_error_still_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner=lambda argv,**kw:subprocess.CompletedProcess(argv,2,'HOERWERK-CLI-V1 {"status":"error","reason":"tls"}\n','')
            with self.assertRaisesRegex(BackendFailed,'Zertifikatsprüfung.*D-U-X'):
                Core(PortablePaths(Path(tmp)),audible='synthetic',runner=runner).download('anna','B012345678')
