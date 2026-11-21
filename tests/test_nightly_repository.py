"""Generic API context binding is not multi-app installable admission. No network."""
import contextlib
import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import nightly_policy as p
import nightly_attestation as a
import nightly_artifacts as bindings
import publish_nightly as publisher
import test_nightly_attestation as fixture

class NoToken(dict):
    def get(self,key,default=None):
        if key=='GH_TOKEN': raise AssertionError('unsupported entry must not read a token')
        return super().get(key,default)
    def __getitem__(self,key):
        if key=='GH_TOKEN': raise AssertionError('unsupported entry must not read a token')
        return super().__getitem__(key)

class RepositoryBindingTests(unittest.TestCase):
    def setUp(self):
        self.local=fixture.AttestationTests('test_exact_staged_digest_and_source_with_no_transport'); self.local.setUp()
        self.addCleanup(self.local.doCleanups)
        self.case=self.local.case; self.registry=copy.deepcopy(self.case.registry)
        self.app='gridcraft'; self.repository=self.registry['apps'][self.app]['repository']
        self.assertEqual(self.repository,'storytold/gridcraft')
        self.env={**self.case.env,'GITHUB_REPOSITORY':self.repository,'GITHUB_SHA':'c'*40,
            'GITHUB_WORKFLOW_REF':self.repository+'/.github/workflows/nightly.yml@refs/heads/main'}
        run={**self.case.policy['run'],'repository':self.repository,'source_commit':'c'*40}
        ident,version=p.n.identity(self.registry,self.app,self.repository,run['source_commit'],self.case.policy['stable_base']['version'],run['id'],run['attempt'],p.timestamp(run['created_at']))
        self.prepared={**ident,'version':version,'ready':False}
        self.policy={**self.case.policy,'identity':self.prepared,'run':run}
        self.identity=self.local.root/'second-identity'; self.identity.mkdir()
        (self.identity/'nightly-identity.json').write_text(json.dumps(self.prepared))
        (self.identity/'nightly-producer-policy.json').write_text(json.dumps(self.policy))
        self.registry_path=self.local.root/'registry.json'; self.registry_path.write_text(json.dumps(self.registry))
        self.ih=bindings.fingerprint(self.identity,'identity')
    def common_args(self):
        return ['--registry',str(self.registry_path),'--identity',str(self.identity/'nightly-identity.json'),
            '--policy',str(self.identity/'nightly-producer-policy.json'),'--directory',str(self.local.packet),
            '--config',str(self.local.config),'--identity-sha256',self.ih,
            '--packet-sha256',self.local.binding['packet_sha256'],'--zip-sha256',self.local.binding['zip_sha256'],
            '--asset',self.local.filename]
    def refused_without_credentials(self,module,argv):
        with patch.object(sys,'argv',['fixture',*argv]),patch.object(module.os,'environ',NoToken(self.env)),patch.object(publisher,'HTTPTransport',side_effect=AssertionError('unsupported transport forbidden')),contextlib.redirect_stdout(io.StringIO()),self.assertRaises(SystemExit) as result:
            module.main()
        self.assertEqual(result.exception.code,1)
    def test_real_second_catalog_identity_routes_real_transport_read_prefix_to_its_own_repository(self):
        # Real catalog, real n.identity and real trusted_context; only the final
        # HTTP exchange is replaced. This does not call a non-Photo installable CLI.
        repository=p.trusted_repository(self.registry,self.prepared['app'],self.env)
        self.assertEqual(repository,self.prepared['repository'])
        transport=publisher.HTTPTransport(repository,'synthetic-token-not-a-credential')
        with patch.object(transport,'exchange',return_value={'state':'active'}) as exchange:
            self.assertEqual(transport.request('GET','/actions/workflows/91'),{'state':'active'})
        self.assertEqual(transport.prefix,'/repos/'+self.repository)
        exchange.assert_called_once_with('api.github.com','GET','/repos/'+self.repository+'/actions/workflows/91',data=None)
    def test_real_catalog_context_mismatch_unknown_app_and_bad_source_refuse(self):
        for change in ({'GITHUB_REPOSITORY':'storytold/photocraft'},{'GITHUB_REF':'refs/heads/other'},{'GITHUB_SHA':'invalid'},{'GITHUB_EVENT_NAME':'pull_request'}):
            with self.subTest(change=change),self.assertRaises(ValueError): p.trusted_repository(self.registry,self.app,{**self.env,**change})
        with self.assertRaises(ValueError): p.trusted_repository(self.registry,'not-in-catalog',self.env)
        bad=copy.deepcopy(self.registry); bad['apps'][self.app]['repository']='storytold/photocraft'
        with self.assertRaises(ValueError): p.trusted_repository(bad,self.app,self.env)
    def test_real_second_identity_does_not_pass_photo_installable_validation(self):
        with self.assertRaises(ValueError): p.validate_prepared(self.registry,self.prepared,self.policy,self.env,self.case.config)
        self.assertEqual(self.registry['apps'][self.app]['targets'],[])
    def test_current_nonphoto_prepare_cli_still_refuses_before_token_or_transport(self):
        self.refused_without_credentials(p,['prepare','--registry',str(self.registry_path),'--config',str(self.local.config),'--app',self.app,'--identity','unused','--policy','unused'])
    def test_current_nonphoto_attestation_cli_still_refuses_before_token_or_transport(self):
        self.refused_without_credentials(a,self.common_args())
    def test_current_nonphoto_publisher_cli_still_refuses_before_token_or_transport(self):
        self.refused_without_credentials(publisher,[*self.common_args(),'--app',self.app])
    def test_photo_prepare_keeps_real_validation_and_uses_its_validated_repository(self):
        argv=['fixture','prepare','--registry',str(ROOT/'nightly-apps.json'),'--config',str(self.local.config),'--app','photocraft',
            '--identity',str(self.local.root/'prepared.json'),'--policy',str(self.local.root/'policy.json')]
        with patch.object(sys,'argv',argv),patch.dict(p.os.environ,{**self.case.env,'GH_TOKEN':'synthetic-photo-token'},clear=True),patch.object(publisher,'HTTPTransport',return_value=self.case.api) as factory,contextlib.redirect_stdout(io.StringIO()): p.main()
        factory.assert_called_once_with(self.registry['apps']['photocraft']['repository'],'synthetic-photo-token')
        self.assertEqual(json.loads((self.local.root/'prepared.json').read_text()),self.case.prepared)
    def test_photo_attestation_keeps_real_validation_and_uses_its_validated_repository(self):
        # Reuse the existing fully bound canonical fixture/CLI with real validators.
        with contextlib.redirect_stdout(io.StringIO()): output=self.local.cli()
        self.assertIn('source_commit='+self.case.prepared['source_commit'],output.read_text())
        self.assertEqual(self.case.api.calls,[('GET','/actions/workflows/91')]*2)

if __name__=='__main__': unittest.main()
