"""Regressionen mit dem echten gepinnten Queue-Worker, ohne Konto oder Netz."""
import asyncio, contextlib, importlib, io, json, socket, ssl, subprocess, sys
import tempfile, threading, unittest
from pathlib import Path
from unittest import mock

from audible_entry import hoerwerk_download
from core import BackendFailed, Core, PortablePaths
from jobs import CancellableRunner


class DownloadWorkerTests(unittest.TestCase):
    def setUp(self):
        # Kein Skip: Ohne echte Abhängigkeit ist diese Abnahme nicht bestanden.
        from audible_cli.cmds import cmd_download
        self.worker=cmd_download
        self.cli=importlib.import_module('audible_cli.cli')
        self.original=self.worker.DownloadRun

    def operation(self, error=None):
        worker=self.worker
        async def run():
            worker.QUEUE=asyncio.Queue()
            async def job():
                if error is not None: raise error
            await worker.QUEUE.put((job,{}))
            state=worker.DownloadRun(False)
            await worker.drain_queue(state,1)
            state.raise_for_errors()
        return lambda **kwargs: asyncio.run(run())

    def invoke(self, error=None):
        output,errors=io.StringIO(),io.StringIO()
        with mock.patch.object(self.cli,'cli',side_effect=self.operation(error)), \
             mock.patch.object(self.worker,'advance_summary'), \
             mock.patch.object(self.worker,'display_counter'), \
             contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            code=hoerwerk_download(['download'])
        self.assertIs(self.worker.DownloadRun,self.original)
        self.assertNotIn('SYNTHETIC_SECRET',output.getvalue()+errors.getvalue())
        self.assertLessEqual(len(output.getvalue()),1024)
        self.assertEqual(output.getvalue().count('\n'),1)
        return code,json.loads(output.getvalue().removeprefix('HOERWERK-CLI-V1 '))

    def test_original_worker_failures_keep_fixed_categories(self):
        failures=[(ssl.SSLCertVerificationError('SYNTHETIC_SECRET'),'tls'),
                  (socket.gaierror('SYNTHETIC_SECRET'),'dns'),
                  (TimeoutError('SYNTHETIC_SECRET'),'timeout'),
                  (PermissionError('SYNTHETIC_SECRET'),'local_file'),
                  (ValueError('SYNTHETIC_SECRET'),'unknown')]
        for error,reason in failures:
            with self.subTest(reason=reason):
                kind={'tls':'N','dns':'N','timeout':'N','local_file':'O','unknown':'V'}[reason]
                self.assertEqual(self.invoke(error),(2,{'status':'error','reason':reason,'diagnostic':'D-U-'+kind}))

    def test_success_and_outer_failure_restore_adapter(self):
        self.assertEqual(self.invoke(),(0,{'status':'ok'}))
        with mock.patch.object(self.cli,'cli',side_effect=RuntimeError('SYNTHETIC_SECRET')), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(hoerwerk_download(['download']),2)
        self.assertIs(self.worker.DownloadRun,self.original)
        self.assertEqual(json.loads(output.getvalue().split(' ',1)[1]),{'status':'error','reason':'unknown','diagnostic':'D-U-R'})

    def test_cancelled_worker_is_not_success(self):
        worker=self.worker
        async def run():
            worker.QUEUE=asyncio.Queue()
            async def job(): await asyncio.sleep(60)
            await worker.QUEUE.put((job,{}))
            asyncio.get_running_loop().call_soon(asyncio.current_task().cancel)
            await worker.drain_queue(worker.DownloadRun(False),1)
        with mock.patch.object(self.cli,'cli',side_effect=lambda **k:asyncio.run(run())), \
             mock.patch.object(worker,'display_counter'),mock.patch.object(worker,'advance_summary'), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(hoerwerk_download(['download']),2)
        self.assertIs(worker.DownloadRun,self.original)
        self.assertEqual(json.loads(output.getvalue().split(' ',1)[1])['status'],'error')

    def test_real_worker_through_child_runner_and_core(self):
        source=Path(__file__).parent.resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); child=root/'audible'
            child.write_text('#!'+sys.executable+'\n'+'''import sys,asyncio,ssl,importlib
sys.path.insert(0,%r)
from audible_cli.cmds import cmd_download as w
from unittest import mock
async def run():
 w.QUEUE=asyncio.Queue()
 async def job():raise ssl.SSLCertVerificationError('SYNTHETIC_SECRET')
 await w.QUEUE.put((job,{}))
 state=w.DownloadRun(False)
 await w.drain_queue(state,1)
 state.raise_for_errors()
module=importlib.import_module('audible_cli.cli')
with mock.patch.object(module,'cli',side_effect=lambda **k:asyncio.run(run())),mock.patch.object(w,'advance_summary'),mock.patch.object(w,'display_counter'):
 from audible_entry import main
 main()
'''%str(source),encoding='utf-8');child.chmod(0o755)
            result=subprocess.run([str(child),'--hoerwerk-download','download'],capture_output=True,text=True,timeout=10)
            self.assertEqual(result.returncode,2)
            self.assertEqual(result.stdout,'HOERWERK-CLI-V1 {"status":"error","reason":"tls","diagnostic":"D-U-N"}\n')
            self.assertNotIn('SYNTHETIC_SECRET',result.stdout+result.stderr)
            with self.assertRaisesRegex(BackendFailed,'Zertifikatsprüfung') as caught:
                Core(PortablePaths(root/'app'),audible=child,runner=CancellableRunner(threading.Event())).download('anna','B012345678')
            self.assertNotIn('SYNTHETIC_SECRET',str(caught.exception))
