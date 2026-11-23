"""Synthetic read-only contracts, NOT GitHub/native/cryptographic qualification."""
import copy
from pathlib import Path
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'scripts'))
import nightly_release_request as r
WINDOWS_KIND = 'nightly-unsigned-windows-x86_64'
MAC_KIND = 'nightly-unsigned-macos-aarch64'
LINUX_KIND = 'nightly-unsigned-linux-x86_64'


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
        self.policy = {'schema': 'craft-nightly-release-policy/v3', 'repository': 'storytold/photocraft',
            'repository_id': 123, 'producer_workflow_id': 91,
            'producer_workflow_path': '.github/workflows/nightly.yml',
            'producer_tooling_revision': 'b'*40,
            'producer_workflow_ref': 'storytold/craft-common-actions/.github/workflows/nightly-unsigned.yml@'+'b'*40,
            'signer_workflow_path': '.github/workflows/nightly-sign.yml', 'signer_tooling_revision': 'c'*40,
            # Synthetic dispatcher identity is explicitly reviewed, never implicitly trusted.
            'release_branch': 'release', 'manual_actors': [],
            'automatic_signer_actors': ['github-actions[bot]'],
            'max_age_seconds': 86400, 'candidate_policy': {'schema': 'craft-nightly-candidate-policy/v3'},
            'max_artifact_bytes': 1000000,
            # One release for every admitted platform; synthetic trust values only.
            'platforms': {
                'windows-x86_64': {'unsigned_artifact_name': 'nightly-unsigned-windows-x86_64',
                    'trust': {'publisher_subject': 'CN=Synthetic fixture only'}},
                'macos-aarch64': {'unsigned_artifact_name': 'nightly-unsigned-macos-aarch64',
                    'trust': {'apple_team_id': 'SYNTH12345'}},
                'linux-x86_64': {'unsigned_artifact_name': 'nightly-unsigned-linux-x86_64',
                    'trust': {'fingerprint': '0123456789ABCDEF0123456789ABCDEF01234567'}}}}
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
        self.item = {'id': 300, 'name': WINDOWS_KIND+'-100-1', 'expired': False,
            'size_in_bytes': 512, 'digest': 'sha256:'+'e'*64, 'created_at': '2026-11-22T00:01:00Z',
            'expires_at': '2026-11-29T00:00:00Z',
            'workflow_run': {'id': 100, 'repository_id': 123, 'head_repository_id': 123,
                'head_sha': 'a'*40, 'head_branch': 'main'}}
        self.mac_item = {**copy.deepcopy(self.item), 'id': 302, 'name': MAC_KIND+'-100-1', 'digest': 'sha256:'+'c'*64}
        self.linux_item = {**copy.deepcopy(self.item), 'id': 304, 'name': LINUX_KIND+'-100-1', 'digest': 'sha256:'+'4'*64}
        rows = {'/actions/runs/100': producer, '/actions/runs/100/attempts/1': copy.deepcopy(producer),
            '/actions/runs/200': signer, '/actions/runs/200/attempts/1': copy.deepcopy(signer),
            '/actions/workflows/91': {'id': 91, 'path': producer['path'], 'state': 'active'},
            '/actions/workflows/92': {'id': 92, 'path': signer['path'], 'state': 'active'},
            '/actions/runs/100/artifacts?per_page=20&page=1': {'total_count': 3, 'artifacts': [self.item, self.mac_item, self.linux_item]},
            '/actions/artifacts/300': copy.deepcopy(self.item),
            '/actions/artifacts/302': copy.deepcopy(self.mac_item),
            '/actions/artifacts/304': copy.deepcopy(self.linux_item),
            'newest': {'workflow_runs': [copy.deepcopy(producer)]}}
        self.api = API(rows)
        self.now = r.p.timestamp('2026-11-22T01:00:00Z')

    def invoke(self):
        return r.validate_request(self.policy, self.env, '100', '1', self.api, now=self.now)

    def legacy_policy(self):
        """Frozen v2 (Windows-only) shape: history only, never live admission."""
        old = {key: copy.deepcopy(value) for key, value in self.policy.items() if key != 'platforms'}
        old.update(schema='craft-nightly-release-policy/v2', platform='windows-x86_64',
            windows_publisher_subject='CN=Synthetic fixture only', unsigned_artifact_name=WINDOWS_KIND)
        return old

    def legacy_request(self, policy=None):
        """A v1 request bound to the legacy policy, built from a live v3 request."""
        policy = policy or self.legacy_policy(); request = copy.deepcopy(self.invoke())
        request['schema'] = 'craft-nightly-release-request/v1'; request['policy_sha256'] = r.digest(policy)
        request['artifact'] = request.pop('artifacts')['windows-x86_64']
        return request

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
        self.assertEqual(request['schema'], 'craft-nightly-release-request/v2')
        self.assertEqual(request['artifacts']['windows-x86_64']['wrapper_sha256'], 'e'*64)
        self.assertEqual(request['artifacts']['macos-aarch64']['wrapper_sha256'], 'c'*64)
        self.assertEqual(request['artifacts']['linux-x86_64']['wrapper_sha256'], '4'*64)
        self.assertEqual(sorted(request['artifacts']), ['linux-x86_64', 'macos-aarch64', 'windows-x86_64'])
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
        self.policy['automatic_signer_actors']=[]
        self.modify_run(200,'triggering_actor',{'login':'approved'})
        self.env['GITHUB_TRIGGERING_ACTOR']='approved'
        with self.assertRaisesRegex(ValueError,'actor not authorized'): self.invoke()
        self.modify_run(100,'triggering_actor',{'login':'Approved'})
        self.assertEqual(self.invoke()['producer']['triggering_actor'],'Approved')
        for which in (100,200):
            saved=copy.deepcopy(self.api.rows); self.modify_run(which,'run_attempt',2)
            self.modify_run(which,'actor',{'login':'approved'})
            self.modify_run(which,'triggering_actor',{'login':'unapproved'})
            self.api.rows[f'/actions/runs/{which}/attempts/2']=copy.deepcopy(self.api.rows[f'/actions/runs/{which}'])
            self.assertEqual(self.api.rows[f'/actions/runs/{which}']['triggering_actor'],
                self.api.rows[f'/actions/runs/{which}/attempts/2']['triggering_actor'])
            if which == 100:
                with self.assertRaisesRegex(ValueError,'current manual/re-run actor not authorized'):
                    r.validate_request(self.policy,self.env,100,2,self.api,now=self.now)
            else:
                env={**self.env,'GITHUB_RUN_ATTEMPT':'2','GITHUB_TRIGGERING_ACTOR':'unapproved'}
                with self.assertRaisesRegex(ValueError,'current manual/re-run actor not authorized'):
                    r.validate_request(self.policy,env,100,1,self.api,now=self.now)
            self.api.rows=saved

    def test_signer_dispatch_attempt_one_requires_an_explicit_reviewed_actor(self):
        for actors,actor,authorized in (([], 'github-actions[bot]', False),
            (['github-actions[bot]'], 'github-actions[bot]', True),
            (['github-actions[bot]'], 'unapproved', False), (['approved'], 'Approved', True)):
            with self.subTest(actors=actors,actor=actor):
                self.policy['manual_actors']=actors
                self.policy['automatic_signer_actors']=[]
                self.modify_run(200,'triggering_actor',{'login':actor})
                self.env['GITHUB_TRIGGERING_ACTOR']=actor
                self.api.calls.clear()
                if authorized:
                    request=self.invoke()
                    self.assertEqual(request['signer']['triggering_actor'],actor)
                    r.request_contract(self.policy,request)
                else:
                    with self.assertRaisesRegex(ValueError,'current manual/re-run actor not authorized'):
                        self.invoke()
                    self.assertFalse(any('/artifacts' in path for _,path in self.api.calls))

    def test_only_initial_scheduled_producer_is_exempt_from_reviewed_actor_check(self):
        self.modify_run(100,'triggering_actor',{'login':'unlisted-scheduler'})
        request=self.invoke()
        self.assertEqual(request['producer']['triggering_actor'],'unlisted-scheduler')
        r.request_contract(self.policy,request)
        self.modify_run(100,'event','workflow_dispatch')
        with self.assertRaisesRegex(ValueError,'current manual/re-run actor not authorized'):
            self.invoke()

    def test_retained_request_rechecks_each_manual_and_rerun_role_actor(self):
        self.policy['manual_actors']=['approved']
        self.policy['automatic_signer_actors']=[]
        for which in (100,200): self.modify_run(which,'triggering_actor',{'login':'approved'})
        self.env['GITHUB_TRIGGERING_ACTOR']='approved'
        original=self.invoke()
        for name in ('producer','signer'):
            for attempt in (1,2):
                with self.subTest(role=name,attempt=attempt):
                    retained=copy.deepcopy(original)
                    retained[name].update(event='workflow_dispatch',run_attempt=attempt)
                    for platform in retained['artifacts']:
                        retained['artifacts'][platform]['name']=r.artifact_name(self.policy,retained['producer'],platform)
                    r.request_contract(self.policy,retained)  # Reviewed manual actor still passes.
                    retained[name]['triggering_actor']='unapproved'
                    with self.assertRaisesRegex(ValueError,'role current actor not authorized'):
                        r.request_contract(self.policy,retained)

    def test_live_retained_request_revalidation_checks_current_first_dispatch_actor(self):
        retained=self.invoke()
        self.modify_run(200,'triggering_actor',{'login':'unapproved'})
        self.env['GITHUB_TRIGGERING_ACTOR']='unapproved'
        with self.assertRaisesRegex(ValueError,'current manual/re-run actor not authorized'):
            r.validate_request(self.policy,self.env,retained['producer']['run_id'],
                retained['producer']['run_attempt'],self.api,now=self.now)

    def test_v2_complete_role_actor_admission_table(self):
        self.policy['manual_actors']=['approved']
        self.policy['automatic_signer_actors']=['dispatcher']
        scheduled={'event':'schedule','run_attempt':1}
        manual={'event':'workflow_dispatch','run_attempt':1}
        rerun={'event':'schedule','run_attempt':2}
        cases=[(False,'schedule',1,'unlisted',None,True),
            (False,'schedule',2,'dispatcher',None,False),
            (False,'workflow_dispatch',1,'dispatcher',None,False),
            (False,'workflow_dispatch',2,'dispatcher',None,False),
            (False,'schedule',2,'Approved',None,True),
            (False,'workflow_dispatch',1,'Approved',None,True),
            (False,'workflow_dispatch',2,'Approved',None,True),
            (True,'workflow_dispatch',1,'dispatcher',scheduled,True),
            (True,'workflow_dispatch',1,'dispatcher',manual,False),
            (True,'workflow_dispatch',1,'dispatcher',rerun,False),
            (True,'workflow_dispatch',1,'dispatcher',None,False),
            (True,'workflow_dispatch',2,'dispatcher',scheduled,False),
            (True,'workflow_dispatch',1,'Approved',manual,True),
            (True,'workflow_dispatch',2,'Approved',scheduled,True),
            (True,'schedule',1,'Approved',scheduled,False)]
        for signer,event,attempt,actor,producer,allowed in cases:
            with self.subTest(signer=signer,event=event,attempt=attempt,actor=actor,producer=producer):
                if allowed:
                    self.assertIsNone(r.require_actor(self.policy,event,attempt,actor,signer=signer,producer=producer))
                else:
                    with self.assertRaises(ValueError):
                        r.require_actor(self.policy,event,attempt,actor,signer=signer,producer=producer)

    def test_automatic_only_actor_cannot_dispatch_or_rerun_producer_or_rerun_signer(self):
        original=copy.deepcopy(self.api.rows)
        for event,attempt in (('workflow_dispatch',1),('schedule',2),('workflow_dispatch',2)):
            self.api.rows=copy.deepcopy(original)
            self.modify_run(100,'event',event); self.modify_run(100,'run_attempt',attempt)
            if attempt == 2:
                self.api.rows['/actions/runs/100/attempts/2']=copy.deepcopy(self.api.rows['/actions/runs/100'])
            with self.subTest(event=event,attempt=attempt),self.assertRaisesRegex(ValueError,'actor not authorized'):
                r.validate_request(self.policy,self.env,100,attempt,self.api,now=self.now)
        self.api.rows=copy.deepcopy(original)
        self.modify_run(200,'run_attempt',2)
        self.api.rows['/actions/runs/200/attempts/2']=copy.deepcopy(self.api.rows['/actions/runs/200'])
        env={**self.env,'GITHUB_RUN_ATTEMPT':'2'}
        with self.assertRaisesRegex(ValueError,'actor not authorized'):
            r.validate_request(self.policy,env,100,1,self.api,now=self.now)

    def test_automatic_signer_requires_authenticated_initial_schedule_producer(self):
        self.policy['manual_actors']=['approved']
        original=copy.deepcopy(self.api.rows)
        for event,attempt in (('workflow_dispatch',1),('schedule',2)):
            self.api.rows=copy.deepcopy(original)
            self.modify_run(100,'event',event); self.modify_run(100,'run_attempt',attempt)
            self.modify_run(100,'triggering_actor',{'login':'approved'})
            if attempt == 2:
                self.api.rows['/actions/runs/100/attempts/2']=copy.deepcopy(self.api.rows['/actions/runs/100'])
            with self.subTest(event=event,attempt=attempt),self.assertRaisesRegex(ValueError,'actor not authorized'):
                r.validate_request(self.policy,self.env,100,attempt,self.api,now=self.now)
            self.assertFalse(any('/artifacts' in path for _,path in self.api.calls))
        self.api.rows=original
        request=self.invoke()
        self.assertEqual(self.api.calls[0][1],'/actions/runs/100')
        r.request_contract(self.policy,request)
        request['producer'].update(event='workflow_dispatch',triggering_actor='approved')
        with self.assertRaisesRegex(ValueError,'role current actor not authorized'):
            r.request_contract(self.policy,request)
        request['producer'].update(event='schedule',run_attempt=2)
        for platform in request['artifacts']:
            request['artifacts'][platform]['name']=r.artifact_name(self.policy,request['producer'],platform)
        with self.assertRaisesRegex(ValueError,'role current actor not authorized'):
            r.request_contract(self.policy,request)

    def test_automatic_signer_without_producer_proof_is_denied_and_final_reread_repeats_it(self):
        row=self.api.rows['/actions/runs/200']
        with self.assertRaisesRegex(ValueError,'actor not authorized'):
            r.role(self.policy,row,200,1,signer=True)
        def changed(suffix):
            if suffix == '/actions/runs/200/attempts/1' and sum(path == suffix for _,path in self.api.calls) == 2:
                self.api.rows[suffix]['triggering_actor']={'login':'unapproved'}
        self.api.hook=changed
        with self.assertRaisesRegex(ValueError,'actor not authorized'): self.invoke()

    def test_v2_actor_lists_are_required_explicit_bounded_disjoint_and_deny_by_default(self):
        missing=copy.deepcopy(self.policy); del missing['automatic_signer_actors']
        with self.assertRaises(ValueError): r.validate_policy(missing)
        for actors in (None,{},['invalid_login'],['same','SAME'],['actor']*65):
            policy={**self.policy,'automatic_signer_actors':actors}
            with self.subTest(actors=actors),self.assertRaises(ValueError): r.validate_policy(policy)
        overlap={**self.policy,'manual_actors':['GITHUB-ACTIONS[bot]']}
        with self.assertRaisesRegex(ValueError,'overlap'): r.validate_policy(overlap)
        self.policy['automatic_signer_actors']=[]
        with self.assertRaisesRegex(ValueError,'actor not authorized'): self.invoke()

    def test_frozen_v1_historical_contract_is_explicit_and_never_a_live_fallback(self):
        old=self.legacy_policy(); old['schema']='craft-nightly-release-policy/v1'
        del old['automatic_signer_actors']; old['manual_actors']=['github-actions[bot]']
        request=self.legacy_request(old)
        r.validate_policy(old); r.request_contract(old,request)
        receipt=r.release_receipt(old,request,'f'*64,'1'*64,old['windows_publisher_subject'])
        self.assertEqual(receipt['policy_sha256'],r.digest(old))
        request['producer']['event']='workflow_dispatch'
        r.request_contract(old,request)  # Explicit historical v1 broad manual grant stays broad.
        self.api.calls.clear()
        for history in (old, self.legacy_policy()):
            with self.assertRaisesRegex(ValueError,'live admission requires.*v3'):
                r.validate_request(history,self.env,100,1,self.api,now=self.now)
        self.assertEqual(self.api.calls,[])
        with self.assertRaises(ValueError): r.validate_policy({**old,'automatic_signer_actors':[]})
        old['manual_actors']=[]; request['policy_sha256']=r.digest(old)
        with self.assertRaisesRegex(ValueError,'actor not authorized'): r.request_contract(old,request)
        with self.assertRaises(ValueError): r.validate_policy({**self.policy,'schema':'unknown'})

    def test_missing_expired_substituted_oversized_cross_attempt_artifacts(self):
        for key,value in [('expired',True), ('expires_at','2026-11-22T00:30:00Z'),
            ('name','nightly-signed'), ('name',WINDOWS_KIND+'-100-2'),
            ('name',WINDOWS_KIND+'-101-1'), ('name',MAC_KIND+'-100-1'), ('size_in_bytes',1000001), ('size_in_bytes',0),
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
        for rows,total in [([],0),([self.item,self.item],2),([self.item],2),([self.item],1),([self.mac_item],1),
            ([self.item,self.mac_item,{**self.item,'id':301}],3),([self.item,self.mac_item],401)]:
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
        original=copy.deepcopy(self.api.rows['newest']['workflow_runs'][0])
        for newer,reason in (({'id':101,'run_number':11},'newer trusted workflow ordinal'),
            ({'run_attempt':2},'same ordinal has another run or a newer attempt')):
            self.api.rows['newest']['workflow_runs']=[{**copy.deepcopy(original),**newer}]
            with self.subTest(newer=newer), self.assertRaisesRegex(ValueError,reason):
                self.invoke()
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
        legacy=self.legacy_policy(); request=self.legacy_request(legacy); self.policy=legacy
        receipt=r.release_receipt(self.policy,request,'f'*64,'1'*64,'CN=Synthetic fixture only')
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
        rerun_item={**copy.deepcopy(self.item),'id':301,'name':WINDOWS_KIND+'-100-2'}
        rerun_mac={**copy.deepcopy(self.mac_item),'id':303,'name':MAC_KIND+'-100-2'}
        rerun_linux={**copy.deepcopy(self.linux_item),'id':305,'name':LINUX_KIND+'-100-2'}
        self.api.rows['/actions/artifacts/301']=rerun_item
        self.api.rows['/actions/artifacts/303']=rerun_mac
        self.api.rows['/actions/artifacts/305']=rerun_linux
        self.api.rows['/actions/runs/100/artifacts?per_page=20&page=1']={'total_count':6,'artifacts':[self.item,rerun_item,self.mac_item,rerun_mac,self.linux_item,rerun_linux]}
        request=r.validate_request(self.policy,self.env,100,2,self.api,now=self.now)
        self.assertEqual(request['artifacts']['windows-x86_64']['id'],301)
        self.assertEqual(request['artifacts']['macos-aarch64']['id'],303)
        self.assertEqual(request['artifacts']['linux-x86_64']['id'],305)
        self.assertEqual(self.item['name'],WINDOWS_KIND+'-100-1')
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
                policy=copy.deepcopy(self.policy); policy['platforms']['windows-x86_64']['trust']['publisher_subject']=subject
                with self.assertRaises(ValueError): r.validate_policy(policy)
                legacy={**self.legacy_policy(),'windows_publisher_subject':subject}
                with self.assertRaises(ValueError): r.validate_policy(legacy)

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

    def test_ref_qualified_role_paths_are_canonical_only_for_their_own_branch(self):
        # GitHub's workflow-run schema documents `path@branch`; the request keeps bare paths.
        self.modify_run(100, 'path', self.policy['producer_workflow_path']+'@main')
        self.modify_run(200, 'path', self.policy['signer_workflow_path']+'@release')
        request = self.invoke()
        self.assertEqual(request['producer']['workflow_path'], self.policy['producer_workflow_path'])
        self.assertEqual(request['signer']['workflow_path'], self.policy['signer_workflow_path'])
        r.request_contract(self.policy, request)
        for which, path in ((100, self.policy['producer_workflow_path']+'@release'),
                            (200, self.policy['signer_workflow_path']+'@main'),
                            (100, self.policy['producer_workflow_path']+'@refs/heads/main'),
                            (200, self.policy['producer_workflow_path']+'@release')):
            with self.subTest(which=which, path=path):
                saved = copy.deepcopy(self.api.rows); self.modify_run(which, 'path', path)
                with self.assertRaises(ValueError): self.invoke()
                self.api.rows = saved

    def test_complete_artifact_inventory_on_the_last_allowed_full_page_is_accepted(self):
        limit = r.p.MAX_PAGES*r.p.PAGE_SIZE
        rows = [{**copy.deepcopy(self.item), 'id': 1000+index, 'name': 'other-'+str(index)} for index in range(limit-1)]
        rows.insert(limit//2, copy.deepcopy(self.item)); rows[limit//3] = copy.deepcopy(self.linux_item); rows[-1] = copy.deepcopy(self.mac_item)
        for page in range(1, r.p.MAX_PAGES+1):
            self.api.rows[f'/actions/runs/100/artifacts?per_page={r.p.PAGE_SIZE}&page={page}'] = {'total_count': limit,
                'artifacts': rows[(page-1)*r.p.PAGE_SIZE:page*r.p.PAGE_SIZE]}
        self.assertEqual(self.invoke()['artifacts']['windows-x86_64']['id'], 300)
        self.assertFalse(any(path.endswith(f'&page={r.p.MAX_PAGES+1}') for _, path in self.api.calls))
        # A full page that still leaves the reported total unread keeps refusing.
        last = f'/actions/runs/100/artifacts?per_page={r.p.PAGE_SIZE}&page={r.p.MAX_PAGES}'
        self.api.rows[last]['artifacts'] = self.api.rows[last]['artifacts'][:-1]
        with self.assertRaisesRegex(ValueError, 'incomplete'): self.invoke()


    def test_v3_policy_requires_exact_platform_map_kinds_and_single_trust_values(self):
        r.validate_policy(self.policy)
        self.assertEqual(r.policy_platforms(self.policy), ['linux-x86_64', 'macos-aarch64', 'windows-x86_64'])
        mutations = [
            lambda pol: pol['platforms'].clear(),
            lambda pol: pol['platforms'].update({'freebsd-x86_64': copy.deepcopy(pol['platforms']['windows-x86_64'])}),
            lambda pol: pol['platforms']['macos-aarch64'].update(unsigned_artifact_name=WINDOWS_KIND),
            lambda pol: pol['platforms']['macos-aarch64'].update(unsigned_artifact_name='nightly-unsigned'),
            lambda pol: pol['platforms']['macos-aarch64'].update(extra=True),
            lambda pol: pol['platforms']['macos-aarch64']['trust'].update(apple_team_id='short'),
            lambda pol: pol['platforms']['macos-aarch64']['trust'].update(apple_team_id='synth12345'),
            lambda pol: pol['platforms']['macos-aarch64']['trust'].update(apple_team_id='SYNTH12345\n'),
            lambda pol: pol['platforms']['macos-aarch64']['trust'].update(publisher_subject='CN=x'),
            lambda pol: pol['platforms']['macos-aarch64'].update(trust={'publisher_subject': 'CN=x'}),
            lambda pol: pol['platforms']['windows-x86_64'].update(trust={'apple_team_id': 'SYNTH12345'}),
            lambda pol: pol.update(platform='windows-x86_64'),
            lambda pol: pol.update(windows_publisher_subject='CN=x'),
            lambda pol: pol.update(unsigned_artifact_name=WINDOWS_KIND),
            lambda pol: pol.pop('platforms'),
        ]
        for index, mutate in enumerate(mutations):
            policy = copy.deepcopy(self.policy); mutate(policy)
            with self.subTest(index=index), self.assertRaises(ValueError): r.validate_policy(policy)

    def test_each_admitted_platform_needs_its_own_exact_artifact_and_live_reread(self):
        key = '/actions/runs/100/artifacts?per_page=20&page=1'
        self.api.rows[key] = {'total_count': 1, 'artifacts': [self.item]}
        with self.assertRaisesRegex(ValueError, 'admitted platform'): self.invoke()
        self.api.rows[key] = {'total_count': 2, 'artifacts': [self.item, self.mac_item]}
        with self.assertRaisesRegex(ValueError, 'admitted platform'): self.invoke()  # Linux artifact missing.
        self.api.rows[key] = {'total_count': 3, 'artifacts': [self.item, self.mac_item, self.linux_item]}
        self.api.rows['/actions/artifacts/302']['digest'] = 'sha256:'+'9'*64
        with self.assertRaisesRegex(ValueError, 'substituted'): self.invoke()
        self.api.rows['/actions/artifacts/302'] = copy.deepcopy(self.mac_item)
        request = self.invoke()
        swapped = copy.deepcopy(request)
        swapped['artifacts']['windows-x86_64'], swapped['artifacts']['macos-aarch64'] = (
            swapped['artifacts']['macos-aarch64'], swapped['artifacts']['windows-x86_64'])
        with self.assertRaisesRegex(ValueError, 'artifact request binding'): r.request_contract(self.policy, swapped)
        missing = copy.deepcopy(request); del missing['artifacts']['macos-aarch64']
        with self.assertRaisesRegex(ValueError, 'platform set'): r.request_contract(self.policy, missing)
        legacy = copy.deepcopy(request); legacy['schema'] = 'craft-nightly-release-request/v1'
        with self.assertRaises(ValueError): r.request_contract(self.policy, legacy)

    def test_v3_receipt_binds_platform_trust_method_support_and_artifact(self):
        request = self.invoke()
        mac = r.release_receipt_v3(self.policy, request, 'macos-aarch64', 'f'*64, '1'*64, 'SYNTH12345', {})
        self.assertEqual((mac['schema'], mac['platform'], mac['method']), ('craft-nightly-signing/v3', 'macos-aarch64', 'developer-id-notarized'))
        self.assertEqual(mac['artifact'], request['artifacts']['macos-aarch64'])
        win = r.release_receipt_v3(self.policy, request, 'windows-x86_64', 'f'*64, '1'*64, 'CN=Synthetic fixture only', {})
        self.assertEqual(win['method'], 'authenticode')
        for platform, signer in (('macos-aarch64', 'OTHER12345'), ('macos-aarch64', 'CN=Synthetic fixture only'),
                                 ('windows-x86_64', 'SYNTH12345'), ('macos-aarch64', None), ('linux-x86_64', 'SYNTH12345')):
            with self.subTest(platform=platform, signer=signer), self.assertRaises(ValueError):
                r.release_receipt_v3(self.policy, request, platform, 'f'*64, '1'*64, signer, {})
        # Windows and macOS signing must change bytes: no unsigned fallback.
        with self.assertRaisesRegex(ValueError, 'unsigned fallback'):
            r.release_receipt_v3(self.policy, request, 'macos-aarch64', 'f'*64, 'f'*64, 'SYNTH12345', {})
        for support in ({'../x': {'size': 1, 'sha256': '1'*64}}, {'x.asc': {'size': 0, 'sha256': '1'*64}},
                        {'x.asc': {'size': 1, 'sha256': 'bad'}}, {'x.asc': {'size': 1}}, []):
            with self.subTest(support=support), self.assertRaises(ValueError):
                r.release_receipt_v3(self.policy, request, 'macos-aarch64', 'f'*64, '1'*64, 'SYNTH12345', support)
        with self.assertRaisesRegex(ValueError, 'policy v3'):
            r.release_receipt_v3(self.legacy_policy(), self.legacy_request(), 'windows-x86_64', 'f'*64, '1'*64, 'CN=Synthetic fixture only', {})


if __name__ == '__main__': unittest.main()
