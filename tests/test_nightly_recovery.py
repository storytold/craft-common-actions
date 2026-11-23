"""Offline regressions for confirmed-public state and atomic checkpoint recovery."""
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
import io
import http.client
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_prune as prune

class PublicationRecoveryTests(unittest.TestCase):
    def setUp(self):
        import test_publish_nightly as fixture
        self.case=fixture.PublisherTests('test_v6_initial_gate_refusal_prevents_any_ref_or_draft_mutation')
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.publisher=fixture.p;self.feed=fixture.n
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        ownership=patch.dict(os.environ,{'GITHUB_WORKSPACE':str(ROOT.resolve()),'RUNNER_TEMP':str(Path(self.temp.name).resolve())}); ownership.start(); self.addCleanup(ownership.stop)
        self.output=Path(self.temp.name).resolve()/'ownership'

    def run_cli(self, failure, *, before_publish=False, close_failure=False):
        import nightly_artifacts as bindings
        import nightly_policy as policy
        import nightly_signing as signing
        import nightly_ownership_receipt as ownership
        case=self.case;publisher=self.publisher
        original=case.api.request
        def request(method,path,data=None):
            if method=='GET' and path.startswith('/git/ref/tags/') and (case.api.published or before_publish):
                if failure=='mismatch':return {'ref':'wrong','object':{}}
                raise failure
            reply=original(method,path,data)
            if case.api.published and method=='PATCH' and failure=='timestamp':reply['published_at']='invalid timestamp'
            return reply
        case.api.request=request
        class OfflineTransport:
            def __new__(cls,*args,**kwargs):return case.api
        args=['publisher','--registry','registry','--app','photocraft','--identity','identity','--directory','FAKE_PACKET','--policy','policy','--config','config','--ownership-directory',str(self.output),'--signing-receipt-directory','FAKE_RECEIPT','--signing-receipt-sha256','a'*64,'--unsigned-sha256','b'*64,'--identity-sha256','c'*64,'--packet-sha256','d'*64,'--zip-sha256','e'*64,'--asset','synthetic-nightly.AppImage']
        def bounded(path):return {'registry':case.registry,'identity':case.prepared,'policy':{'fixture':'offline only'},'config':{}}[str(path)]
        actual_reserve=ownership.reserve
        @contextmanager
        def broken_close(directory):
            with actual_reserve(directory) as stream:
                try:yield stream
                finally:raise OSError('synthetic receipt close failure')
        output=io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(sys,'argv',args))
            stack.enter_context(patch.dict(os.environ,{**case.env,'TOOLING_REVISION':'b'*40}))
            for module,name,value in [(bindings,'verify_chain',None),(self.feed,'bounded_json',None),(policy,'validate_prepared',None),(policy,'publication_contract',None),(policy,'publication_gate',True),(policy,'summary',None),(signing,'verify_receipt',{}),(signing,'verify_package',None),(publisher,'prepare_packet',(case.ident,case.metadata))]:
                stack.enter_context(patch.object(module,name,side_effect=bounded) if name=='bounded_json' else patch.object(module,name,return_value=value))
            stack.enter_context(patch.object(publisher,'HTTPTransport',OfflineTransport))
            if close_failure:stack.enter_context(patch.object(ownership,'reserve',broken_close))
            stack.enter_context(redirect_stderr(output));stack.enter_context(redirect_stdout(output))
            with self.assertRaises(SystemExit) as result:publisher.main()
        self.assertFalse(any(call[0]=='DELETE' for call in case.api.calls))
        return result.exception.code,output.getvalue()

    def test_cli_unowned_receipt_path_refuses_before_publication(self):
        root=Path(self.temp.name);workspace=root/'workspace';workspace.mkdir();runner=root/'runner';runner.mkdir()
        self.output=root/'unowned-output'
        with patch.dict(os.environ,{'GITHUB_WORKSPACE':str(workspace),'RUNNER_TEMP':str(runner)}):
            code,message=self.run_cli(OSError('not reached'))
        self.assertEqual(code,1)
        self.assertIn('path outside owned job roots',message)
        self.assertFalse(self.case.api.published)
        self.assertFalse(self.output.exists())
        self.assertFalse(any(call[0] in ('POST','PATCH','DELETE') for call in self.case.api.calls))

    def assert_reconciliation(self, failure, **kwargs):
        code,message=self.run_cli(failure,**kwargs)
        self.assertTrue(self.case.api.published)
        self.assertEqual(code,2)
        self.assertIn('reconciliation',message)
        self.assertIn('do not recreate or retry publication',message)
        self.assertNotIn('nightly publication stopped:',message)
        self.assertEqual(sum(call[0]=='PATCH' for call in self.case.api.calls),1)
        self.assertEqual((self.output/'ownership.json').read_bytes(),b'')

    def test_final_get_oserror_preserves_public_state(self):self.assert_reconciliation(OSError('synthetic final GET failure'))
    def test_final_get_api_error_preserves_public_state(self):self.assert_reconciliation(self.publisher.APIError(503))
    def test_final_get_incomplete_read_preserves_public_state(self):self.assert_reconciliation(http.client.IncompleteRead(b'partial'))
    def test_final_get_bad_status_line_preserves_public_state(self):self.assert_reconciliation(http.client.BadStatusLine('synthetic broken status'))
    def test_pre_publication_protocol_failure_remains_refusal(self):
        code,message=self.run_cli(http.client.IncompleteRead(b'partial'),before_publish=True)
        self.assertEqual(code,1);self.assertFalse(self.case.api.published)
        self.assertIn('nightly publication stopped:',message)
        self.assertFalse(any(call[0]=='PATCH' for call in self.case.api.calls))
    def test_final_ref_mismatch_preserves_public_state(self):self.assert_reconciliation('mismatch')
    def test_invalid_publication_timestamp_preserves_public_state(self):self.assert_reconciliation('timestamp')
    def test_secondary_receipt_close_error_does_not_erase_public_state(self):self.assert_reconciliation(OSError('synthetic final GET failure'),close_failure=True)
    def test_pre_publication_failure_remains_refusal(self):
        code,message=self.run_cli(OSError('synthetic initial GET failure'),before_publish=True)
        self.assertEqual(code,1);self.assertFalse(self.case.api.published)
        self.assertIn('nightly publication stopped:',message)
        self.assertFalse(any(call[0]=='PATCH' for call in self.case.api.calls))

class CheckpointRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.destination=self.root/'checkpoint.json'
        self.previous={'synthetic':'exact plan'}
        self.destination.write_bytes(b'prior durable checkpoint')
    def verify_recoverable(self, operation):
        with patch.object(prune.os,operation,side_effect=OSError('synthetic '+operation+' failure')):
            with self.assertRaisesRegex(OSError,'synthetic'):prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(self.destination.read_bytes(),b'prior durable checkpoint')
        self.assertEqual(list(self.root.iterdir()),[self.destination])
        prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(self.destination.read_bytes(),prune.encode_plan(self.previous))
    def test_file_sync_failure_cleans_only_owned_temp_and_allows_retry(self):self.verify_recoverable('fsync')
    def test_replace_failure_preserves_old_checkpoint_and_allows_retry(self):self.verify_recoverable('replace')
    def test_partial_write_failure_cleans_temp_and_allows_retry(self):
        real=prune.tempfile.NamedTemporaryFile
        def broken(*args,**kwargs):
            stream=real(*args,**kwargs);stream.write=lambda *_: (_ for _ in ()).throw(OSError('synthetic write failure'));return stream
        with patch.object(prune.tempfile,'NamedTemporaryFile',side_effect=broken):
            with self.assertRaisesRegex(OSError,'synthetic write failure'):prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(list(self.root.iterdir()),[self.destination])
        self.assertEqual(self.destination.read_bytes(),b'prior durable checkpoint')
        prune.save_checkpoint(self.destination,self.previous)
    def test_directory_sync_failure_remains_fatal_after_atomic_replace(self):
        with patch.object(prune.os,'fsync',side_effect=[None,OSError('synthetic directory sync failure')]):
            with self.assertRaisesRegex(OSError,'directory sync'):prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(list(self.root.iterdir()),[self.destination])
        self.assertEqual(self.destination.read_bytes(),prune.encode_plan(self.previous))
    def test_foreign_fixed_new_file_is_neither_removed_nor_reused(self):
        foreign=self.root/'checkpoint.json.new';foreign.write_bytes(b'foreign work')
        prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(foreign.read_bytes(),b'foreign work')
        self.assertEqual(self.destination.read_bytes(),prune.encode_plan(self.previous))
    def test_cleanup_failure_does_not_mask_original_error_or_prevent_unique_retry(self):
        with patch.object(prune.os,'fsync',side_effect=OSError('primary sync failure')),patch.object(Path,'unlink',side_effect=OSError('cleanup failure')):
            with self.assertRaisesRegex(OSError,'primary sync failure'):prune.save_checkpoint(self.destination,self.previous)
        prune.save_checkpoint(self.destination,self.previous)
        self.assertEqual(self.destination.read_bytes(),prune.encode_plan(self.previous))

if __name__=='__main__':unittest.main()
