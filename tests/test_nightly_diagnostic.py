"""No API/socket/policy for known unadmitted apps or separate Photo diagnostics."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_diagnostic as d

class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.cargo=self.root/'Cargo.toml'
        self.cargo.write_text('[workspace.package]\nversion="99.2.3-beta+dev"\n')
        self.registry=json.loads((ROOT/'nightly-apps.json').read_text())
    def env(self,app):
        return {'GITHUB_EVENT_NAME':'schedule','GITHUB_REF':'refs/heads/main','GITHUB_REPOSITORY':'storytold/'+app,'GITHUB_SHA':'a'*40,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}
    def test_every_known_app_including_all_unadmitted_apps_needs_no_policy_or_transport(self):
        for app in self.registry['apps']:
            with self.subTest(app=app),patch.object(socket,'socket',side_effect=AssertionError('diagnostics never scan APIs')):
                result=d.diagnostic(self.registry,app,self.cargo,self.env(app))
                self.assertFalse(result['ready']); self.assertFalse(result['installable'])
                self.assertEqual(result['schema'],'craft-nightly-diagnostic/v1')
                self.assertTrue(result['version'].startswith('99.2.3-nightly.diagnostic.123.2.'))
                self.assertNotIn('tag',result); self.assertNotIn('artifacts',result)
    def test_untrusted_event_repository_or_unknown_app_refuses(self):
        for field,value in [('GITHUB_REF','refs/heads/other'),('GITHUB_EVENT_NAME','pull_request'),('GITHUB_REPOSITORY','fork/photocraft')]:
            with self.subTest(field=field),self.assertRaises(ValueError): d.diagnostic(self.registry,'photocraft',self.cargo,{**self.env('photocraft'),field:value})
        with self.assertRaises(ValueError): d.diagnostic(self.registry,'unknown',self.cargo,self.env('unknown'))
    def test_missing_or_oversized_diagnostic_version_refuses(self):
        for contents in ('[workspace]\n', 'x'*(d.n.MAX_JSON_BYTES+1)):
            self.cargo.write_text(contents)
            with self.assertRaises(ValueError): d.diagnostic(self.registry,'photocraft',self.cargo,self.env('photocraft'))
if __name__=='__main__': unittest.main()
