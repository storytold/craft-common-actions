"""Synthetic read-only contracts, NOT GitHub/native/cryptographic qualification."""
import copy
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import nightly_release_request as r


class API:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.hook = None

    def request(self, method, suffix, data=None):
        if method != 'GET' or data is not None: raise AssertionError('writes forbidden')
        self.calls.append((method, suffix))
        if self.hook: self.hook(suffix)
        if suffix.startswith('/actions/workflows/91/runs?'):
            return copy.deepcopy(self.rows['newest'])
        if suffix not in self.rows: raise ValueError('missing exact API fixture: '+suffix)
        value = self.rows[suffix]
        if isinstance(value, Exception): raise value
        return copy.deepcopy(value)


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.policy = {'schema': 'craft-nightly-release-policy/v1', 'repository': 'storytold/photocraft',
            'repository_id': 123, 'producer_workflow_id': 91,
            'producer_workflow_path': '.github/workflows/nightly.yml',
            'producer_tooling_revision': 'b'*40,
            'producer_workflow_ref': 'storytold/craft-common-actions/.github/workflows/nightly-unsigned.yml@'+'b'*40,
            'signer_workflow_path': '.github/workflows/nightly-sign.yml', 'signer_tooling_revision': 'c'*40,
            'release_branch': 'release', 'manual_actors': [], 'platform': 'windows-x86_64',
            'windows_publisher_subject': 'CN=Synthetic fixture only',
            'max_age_seconds': 86400, 'candidate_policy': {'schema': 'craft-nightly-candidate-policy/v3'},
            'unsigned_artifact_name': 'nightly-unsigned-windows-x86_64', 'max_artifact_bytes': 1000000}
        self.env = {'GITHUB_REPOSITORY': 'storytold/photocraft', 'GITHUB_REPOSITORY_ID': '123',
            'GITHUB_REF': 'refs/heads/release', 'GITHUB_REF_TYPE': 'branch',
            'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_RUN_ID': '200', 'GITHUB_RUN_ATTEMPT': '1',
            'GITHUB_SHA': 'd'*40, 'TOOLING_REVISION': 'c'*40,
            'GITHUB_WORKFLOW_REF': 'storytold/photocraft/.github/workflows/nightly-sign.yml@refs/heads/release',
            'GITHUB_TRIGGERING_ACTOR': 'github-actions[bot]'}
        repo = {'full_name': self.policy['repository'], 'id': 123}
        producer = {'id': 100, 'run_attempt': 1, 'run_number': 10, 'workflow_id': 91,
            'head_sha': 'a'*40, 'head_branch': 'main', 'repository': repo, 'head_repository': repo,
            'path': self.policy['producer_workflow_path'], 'event': 'schedule',
            'status': 'completed', 'conclusion': 'success', 'triggering_actor': {'login': 'github-actions[bot]'},
            'created_at': '2026-11-22T00:00:00Z', 'run_started_at': '2026-11-22T00:00:01Z',
            'referenced_workflows': [{'path': self.policy['producer_workflow_ref'], 'sha': 'b'*40}]}
        signer = {**copy.deepcopy(producer), 'id': 200, 'run_number': 20, 'workflow_id': 92,
            'head_sha': 'd'*40, 'head_branch': 'release', 'path': self.policy['signer_workflow_path'],
            'event': 'workflow_dispatch', 'status': 'in_progress', 'conclusion': None}
        self.item = {'id': 300, 'name': self.policy['unsigned_artifact_name']+'-100-1', 'expired': False,
            'size_in_bytes': 512, 'digest': 'sha256:'+'e'*64, 'created_at': '2026-11-22T00:01:00Z',
            'expires_at': '2026-11-29T00:00:00Z',
            'workflow_run': {'id': 100, 'repository_id': 123, 'head_repository_id': 123,
                'head_sha': 'a'*40, 'head_branch': 'main'}}
        rows = {'/actions/runs/100': producer, '/actions/runs/100/attempts/1': copy.deepcopy(producer),
            '/actions/runs/200': signer, '/actions/runs/200/attempts/1': copy.deepcopy(signer),
            '/actions/workflows/91': {'id': 91, 'path': producer['path'], 'state': 'active'},
            '/actions/workflows/92': {'id': 92, 'path': signer['path'], 'state': 'active'},
            '/actions/runs/100/artifacts?per_page=20&page=1': {'total_count': 1, 'artifacts': [self.item]},
            '/actions/artifacts/300': copy.deepcopy(self.item),
            'newest': {'workflow_runs': [copy.deepcopy(producer)]}}
        self.api = API(rows)
        self.now = r.p.timestamp('2026-11-22T01:00:00Z')

    def invoke(self):
        return r.validate_request(self.policy, self.env, '100', '1', self.api, now=self.now)

    def modify_run(self, which, key, value):
        for suffix in (f'/actions/runs/{which}', f'/actions/runs/{which}/attempts/1'):
            self.api.rows[suffix][key] = copy.deepcopy(value)

    def result(self):
        policy = self.policy; base = 'https://github.com/'+policy['repository']
        builder = 'https://github.com/'+policy['producer_workflow_ref']
        cert = {'issuer': 'https://token.actions.githubusercontent.com', 'buildSignerURI': builder,
            'buildSignerDigest': 'b'*40, 'sourceRepositoryURI': base, 'sourceRepositoryIdentifier': '123',
            'sourceRepositoryRef': 'refs/heads/main', 'sourceRepositoryDigest': 'a'*40,
            'buildConfigURI': base+'/'+policy['producer_workflow_path']+'@refs/heads/main'}
        return [{'verificationResult': {'signature': {'certificate': cert}, 'statement': {
            '_type': 'https://in-toto.io/Statement/v1', 'subject': [{'name': 'payload.zip', 'digest': {'sha256': 'f'*64}}],
            'predicateType': 'https://slsa.dev/provenance/v1', 'predicate': {'buildDefinition': {
                'buildType': 'https://actions.github.io/buildtypes/workflow/v1',
                'externalParameters': {'workflow': {'repository': base,
                    'path': policy['producer_workflow_path'], 'ref': 'refs/heads/main'}},
                'internalParameters': {'github': {'event_name': 'schedule', 'repository_id': '123'}},
                'resolvedDependencies': [{'uri': 'git+'+base+'@refs/heads/main', 'digest': {'gitCommit': 'a'*40}}]},
                'runDetails': {'builder': {'id': builder},
                    'metadata': {'invocationId': base+'/actions/runs/100/attempts/1'}}}}}}]

    def verify(self, results=None, payload='f'*64):
        return r.verify_unsigned_attestation(self.policy, self.invoke(),
            self.result() if results is None else results, payload)

    def test_valid_request_is_read_only_distinct_roles_and_no_download(self):
        request = self.invoke()
        self.assertEqual(request['producer']['source_commit'], 'a'*40)
        self.assertEqual(request['signer']['source_commit'], 'd'*40)
        self.assertEqual(request['artifact']['wrapper_sha256'], 'e'*64)
        self.assertTrue(all(method == 'GET' and not suffix.endswith('/zip') for method,suffix in self.api.calls))
        self.assertEqual(self.env['GITHUB_SHA'], 'd'*40)

    def test_actual_context_ref_tag_event_identity_and_pin_refuse_before_api(self):
        for key,value in [('GITHUB_REPOSITORY', 'other/photocraft'), ('GITHUB_REPOSITORY_ID', '124'),
            ('GITHUB_REF', 'refs/heads/main'), ('GITHUB_REF', 'refs/tags/release'),
            ('GITHUB_REF_TYPE', 'tag'), ('GITHUB_EVENT_NAME', 'workflow_run'),
            ('TOOLING_REVISION', 'a'*40), ('GITHUB_WORKFLOW_REF', self.env['GITHUB_WORKFLOW_REF']+'x')]:
            with self.subTest(key=key, value=value):
                env = {**self.env, key: value}; self.api.calls.clear()
                with self.assertRaises(ValueError): r.validate_request(self.policy, env, 100, 1, self.api, now=self.now)
                self.assertEqual(self.api.calls, [])

    def test_api_run_and_attempt_authority_cannot_be_claimed_by_inputs(self):
        for key,value in [('head_branch','release'), ('head_sha','0'*40), ('workflow_id',93),
            ('event','push'), ('conclusion','failure'), ('status','in_progress'),
            ('path','.github/workflows/caller.yml'), ('run_attempt',2),
            ('repository',{'id':124,'full_name':self.policy['repository']}),
            ('head_repository',{'id':123,'full_name':'other/photocraft'}),
            ('referenced_workflows',[]), ('referenced_workflows',[{'path':self.policy['producer_workflow_ref'],'sha':'c'*40}])]:
            with self.subTest(key=key):
                saved=copy.deepcopy(self.api.rows); self.modify_run(100,key,value)
                with self.assertRaises(ValueError): self.invoke()
                self.api.rows=saved
        self.api.rows['/actions/runs/100/attempts/1']['triggering_actor']={'login':'different'}
        with self.assertRaisesRegex(ValueError,'metadata changed'): self.invoke()

    def test_manual_producer_and_all_reruns_use_current_not_original_actor(self):
        self.modify_run(100,'event','workflow_dispatch'); self.modify_run(100,'actor',{'login':'approved'})
        self.modify_run(100,'triggering_actor',{'login':'unapproved'})
        self.policy['manual_actors']=['approved']
        with self.assertRaisesRegex(ValueError,'actor not authorized'): self.invoke()
        self.modify_run(100,'triggering_actor',{'login':'Approved'})
        self.assertEqual(self.invoke()['producer']['triggering_actor'],'Approved')
        for which in (100,200):
            saved=copy.deepcopy(self.api.rows); self.modify_run(which,'run_attempt',2)
            self.api.rows[f'/actions/runs/{which}/attempts/2']=copy.deepcopy(self.api.rows[f'/actions/runs/{which}'])
            self.modify_run(which,'triggering_actor',{'login':'unapproved'})
            if which == 100:
                with self.assertRaises(ValueError): r.validate_request(self.policy,self.env,100,2,self.api,now=self.now)
            else:
                env={**self.env,'GITHUB_RUN_ATTEMPT':'2','GITHUB_TRIGGERING_ACTOR':'unapproved'}
                with self.assertRaises(ValueError): r.validate_request(self.policy,env,100,1,self.api,now=self.now)
            self.api.rows=saved

    def test_missing_expired_substituted_oversized_cross_attempt_artifacts(self):
        for key,value in [('expired',True), ('expires_at','2026-11-22T00:30:00Z'),
            ('name','nightly-signed'), ('name',self.policy['unsigned_artifact_name']+'-100-2'),
            ('name',self.policy['unsigned_artifact_name']+'-101-1'), ('size_in_bytes',1000001), ('size_in_bytes',0),
            ('created_at','2026-11-21T00:00:00Z'), ('digest',None), ('digest','sha256:bad'),
            ('workflow_run',{'id':999})]:
            with self.subTest(key=key):
                old=copy.deepcopy(self.item); self.item[key]=value
                with self.assertRaises(ValueError): self.invoke()
                self.item.clear(); self.item.update(old)
        self.api.rows['/actions/artifacts/300']['digest']='sha256:'+'9'*64
        with self.assertRaisesRegex(ValueError,'substituted'): self.invoke()
        self.api.rows['/actions/artifacts/300']=ValueError('deleted')
        with self.assertRaisesRegex(ValueError,'deleted'): self.invoke()

    def test_artifact_list_ambiguity_duplicates_missing_and_incomplete_totals(self):
        key='/actions/runs/100/artifacts?per_page=20&page=1'
        for rows,total in [([],0),([self.item,self.item],2),([self.item],2),
            ([self.item,{**self.item,'id':301}],2),([self.item],401)]:
            self.api.rows[key]={'artifacts':rows,'total_count':total}
            with self.assertRaises(ValueError): self.invoke()

    def test_pause_age_newer_run_replay_and_changes_during_admission(self):
        for workflow in (91,92):
            self.api.rows[f'/actions/workflows/{workflow}']['state']='disabled_manually'
            with self.assertRaisesRegex(ValueError,'paused'): self.invoke()
            self.api.rows[f'/actions/workflows/{workflow}']['state']='active'
        self.now+=86401
        with self.assertRaisesRegex(ValueError,'stale'): self.invoke()
        self.now-=86401
        for newer in ({'id':101,'run_number':11}, {'run_attempt':2}):
            self.api.rows['newest']['workflow_runs'][0].update(newer)
            with self.assertRaises(ValueError): self.invoke()
        self.api.rows['newest']['workflow_runs']=[]
        with self.assertRaisesRegex(ValueError,'absent'): self.invoke()
        count=0
        def change(suffix):
            nonlocal count
            if suffix == '/actions/runs/100':
                count+=1
                if count == 2: self.api.rows[suffix]['run_attempt']=2
        self.api.hook=change
        with self.assertRaisesRegex(ValueError,'superseded'): self.invoke()

    def test_successful_claims_bind_real_payload_not_api_wrapper_digest(self):
        result=self.verify()
        self.assertEqual(result['payload_sha256'],'f'*64)
        self.assertTrue(result['invocation_id'].endswith('/100/attempts/1'))
        with self.assertRaisesRegex(ValueError,'payload digest'): self.verify(payload='e'*64)
        self.assertNotIn('verified',result)

    def test_verified_boolean_raw_bundle_empty_and_multiple_mixed_statements_refuse(self):
        for results in ([], {'verified':True}, [{'verified':True}], [{'attestation':{}}]):
            with self.subTest(results=results), self.assertRaises(ValueError): self.verify(results)
        results=self.result(); wrong=copy.deepcopy(results[0]); wrong['verificationResult']['statement']['subject'][0]['digest']['sha256']='0'*64
        with self.assertRaises(ValueError): self.verify(results+[wrong])

    def test_every_certificate_identity_source_ref_and_pinned_builder_is_required(self):
        for key in self.result()[0]['verificationResult']['signature']['certificate']:
            result=self.result(); result[0]['verificationResult']['signature']['certificate'][key]='wrong'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError,'certificate'): self.verify(result)
        result=self.result(); cert=result[0]['verificationResult']['signature']['certificate']
        cert['buildSignerURI']='https://github.com/'+self.policy['repository']+'/'+self.policy['producer_workflow_path']+'@refs/heads/main'
        with self.assertRaises(ValueError): self.verify(result)  # caller is not reusable builder

    def test_signed_invocation_attempt_repository_and_run_cannot_be_inferred_from_workflow_sha(self):
        for invocation in ('https://github.com/storytold/photocraft/actions/runs/100',
            'https://github.com/storytold/photocraft/actions/runs/100/attempts/2',
            'https://github.com/storytold/photocraft/actions/runs/101/attempts/1',
            'https://github.com/other/photocraft/actions/runs/100/attempts/1'):
            result=self.result(); result[0]['verificationResult']['statement']['predicate']['runDetails']['metadata']['invocationId']=invocation
            with self.subTest(invocation=invocation), self.assertRaisesRegex(ValueError,'exact invocation'): self.verify(result)

    def test_signed_predicate_source_event_builder_digest_and_type_mismatches_refuse(self):
        mutations = [(['predicateType'],'other'), (['_type'],'other'),
            (['subject',0,'digest'],{'sha256':'0'*64}),
            (['predicate','buildDefinition','buildType'],'other'),
            (['predicate','buildDefinition','externalParameters','workflow','ref'],'refs/tags/main'),
            (['predicate','buildDefinition','internalParameters','github','event_name'],'push'),
            (['predicate','buildDefinition','internalParameters','github','repository_id'],'124'),
            (['predicate','buildDefinition','resolvedDependencies',0,'digest','gitCommit'],'0'*40),
            (['predicate','runDetails','builder','id'],'caller')]
        for path,value in mutations:
            result=self.result(); node=result[0]['verificationResult']['statement']
            for part in path[:-1]: node=node[part]
            node[path[-1]]=value
            with self.subTest(path=path), self.assertRaises(ValueError): self.verify(result)

    def test_receipt_has_two_roles_three_distinct_digest_boundaries_and_no_boolean_proof(self):
        request=self.invoke(); receipt=r.release_receipt(self.policy,request,'f'*64,'1'*64,'CN=Synthetic fixture only')
        self.assertEqual(receipt['schema'],'craft-nightly-signing/v2')
        self.assertEqual(receipt['producer']['source_commit'],'a'*40)
        self.assertEqual(receipt['signer']['source_commit'],'d'*40)
        self.assertEqual(receipt['artifact']['wrapper_sha256'],'e'*64)
        self.assertEqual(receipt['unsigned_sha256'],'f'*64); self.assertEqual(receipt['final_sha256'],'1'*64)
        for signer in (True,False,{'verified':True},'', 'CN=bad\nsubject', 'CN=Other Publisher'):
            with self.assertRaises(ValueError): r.release_receipt(self.policy,request,'f'*64,'1'*64,signer)
        request['producer']['source_commit']='9'*40
        self.assertEqual(receipt['producer']['source_commit'],'a'*40)
        policy=copy.deepcopy(self.policy); policy['max_age_seconds']+=1
        with self.assertRaisesRegex(ValueError,'policy binding'): r.release_receipt(policy,request,'f'*64,'1'*64,'CN=fixture')

    def test_authorized_current_producer_and_signer_reruns_bind_attempt_two(self):
        self.policy['manual_actors']=['Approved']
        for which in (100,200):
            self.modify_run(which,'run_attempt',2)
            self.modify_run(which,'triggering_actor',{'login':'approved'})
            self.api.rows[f'/actions/runs/{which}/attempts/2']=copy.deepcopy(self.api.rows[f'/actions/runs/{which}'])
        self.env.update(GITHUB_RUN_ATTEMPT='2', GITHUB_TRIGGERING_ACTOR='approved')
        self.api.rows['newest']['workflow_runs']=[copy.deepcopy(self.api.rows['/actions/runs/100'])]
        rerun_item={**copy.deepcopy(self.item),'id':301,'name':self.policy['unsigned_artifact_name']+'-100-2'}
        self.api.rows['/actions/artifacts/301']=rerun_item
        self.api.rows['/actions/runs/100/artifacts?per_page=20&page=1']={'total_count':2,'artifacts':[self.item,rerun_item]}
        request=r.validate_request(self.policy,self.env,100,2,self.api,now=self.now)
        self.assertEqual(request['artifact']['id'],301)
        self.assertEqual(self.item['name'],self.policy['unsigned_artifact_name']+'-100-1')
        self.assertEqual(request['producer']['run_attempt'],2)
        self.assertEqual(request['signer']['run_attempt'],2)
        with self.assertRaisesRegex(ValueError,'exact invocation'):
            r.verify_unsigned_attestation(self.policy,request,self.result(),'f'*64)
        results=self.result()
        results[0]['verificationResult']['statement']['predicate']['runDetails']['metadata']['invocationId']=            'https://github.com/storytold/photocraft/actions/runs/100/attempts/2'
        r.verify_unsigned_attestation(self.policy,request,results,'f'*64)

    def test_missing_current_actor_signer_context_api_error_and_metadata_disagreement_refuse(self):
        for which in (100,200):
            saved=copy.deepcopy(self.api.rows)
            self.modify_run(which,'triggering_actor',None)
            with self.assertRaises(ValueError): self.invoke()
            self.api.rows=saved
        for key,value in [('GITHUB_SHA','a'*40),('GITHUB_TRIGGERING_ACTOR','other')]:
            env={**self.env,key:value}
            with self.assertRaisesRegex(ValueError,'signer context'):
                r.validate_request(self.policy,env,100,1,self.api,now=self.now)
        self.api.rows['/actions/runs/100']=ValueError('API unavailable')
        with self.assertRaisesRegex(ValueError,'API unavailable'): self.invoke()

    def test_malformed_nested_verifier_objects_never_fall_back(self):
        for path in (['signature'],['statement'],['statement','predicate'],
            ['statement','predicate','buildDefinition'],
            ['statement','predicate','buildDefinition','internalParameters'],
            ['statement','predicate','runDetails'],
            ['statement','predicate','runDetails','metadata']):
            result=self.result(); node=result[0]['verificationResult']
            for key in path[:-1]: node=node[key]
            node[path[-1]]=None
            with self.subTest(path=path), self.assertRaises(ValueError): self.verify(result)

    def test_snapshot_callback_follows_authentication_and_precedes_any_artifact_read(self):
        observed=[]
        def snapshot(producer):
            observed.append(copy.deepcopy(producer))
            self.assertTrue(any(path == '/actions/workflows/91' for _,path in self.api.calls))
            self.assertTrue(any(path == '/actions/workflows/92' for _,path in self.api.calls))
            self.assertFalse(any('/artifacts' in path for _,path in self.api.calls))
            producer['source_commit']='9'*40  # Callback cannot alter authenticated identity.
        request=r.validate_request(self.policy,self.env,100,1,self.api,now=self.now,before_artifacts=snapshot)
        self.assertEqual(len(observed),1)
        self.assertEqual(observed[0]['source_commit'],'a'*40)
        self.assertEqual(request['producer']['source_commit'],'a'*40)
        self.assertEqual(r.artifact_name(self.policy,request['producer']),self.item['name'])

    def test_failed_or_inadmissible_snapshot_never_reads_artifacts(self):
        def refuse(producer): raise ValueError('quality snapshot unavailable')
        with self.assertRaisesRegex(ValueError,'snapshot unavailable'):
            r.validate_request(self.policy,self.env,100,1,self.api,now=self.now,before_artifacts=refuse)
        self.assertFalse(any('/artifacts' in path for _,path in self.api.calls))
        self.api.calls.clear(); self.modify_run(100,'conclusion','failure')
        def forbidden(producer): raise AssertionError('callback before authentication')
        with self.assertRaises(ValueError):
            r.validate_request(self.policy,self.env,100,1,self.api,now=self.now,before_artifacts=forbidden)
        self.assertFalse(any('/artifacts' in path for _,path in self.api.calls))

    def test_protected_publisher_subject_has_strict_nonempty_bounded_literal_shape(self):
        for subject in (None, True, '', 'x'*2049, 'CN=bad\rsubject', 'CN=bad\nsubject', 'CN=bad\0subject'):
            with self.subTest(subject=subject):
                policy={**self.policy,'windows_publisher_subject':subject}
                with self.assertRaises(ValueError): r.validate_policy(policy)

    def test_old_schemas_unknown_fields_and_unpatched_verifier_never_fall_back(self):
        extra={**self.policy,'unreviewed':True}
        with self.assertRaises(ValueError): r.validate_policy(extra)
        for schema in ('craft-nightly-candidate-policy/v1','craft-nightly-candidate-policy/v2'):
            self.policy['candidate_policy']['schema']=schema
            with self.assertRaisesRegex(ValueError,'no fallback'): self.invoke()
        for output in ('gh version 2.101.9', 'gh version 2.102.0-pre', 'verified:true', True):
            with self.assertRaises(ValueError): r.require_gh_version(output)
        for output in ('gh version 2.102.0 (2026-01-01)\nurl', 'gh version 2.103.1', 'gh version 3.0.0'):
            r.require_gh_version(output)


if __name__ == '__main__': unittest.main()
