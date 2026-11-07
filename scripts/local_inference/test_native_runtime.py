"""CPU unit tests for native installer/proxy safety; these do not claim model acceptance."""
from __future__ import annotations
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
import sys
from unittest.mock import patch
import uuid
from urllib.error import HTTPError,URLError

import videofactory_native as cli
import native_supervisor as supervisor

class NativeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='video-factory-unit-')
        self.root=Path(self.temp.name).resolve()
        self.config={'runtime_root':str(self.root),'output_root':str(self.root/'output'),
                     'input_root':str(self.root/'input'),'comfy_origin':'http://127.0.0.1:8188'}
        (self.root/'jobs').mkdir()

    def tearDown(self):self.temp.cleanup()

    def runtime(self):
        runtime=object.__new__(supervisor.Runtime)
        runtime.config=self.config;runtime.active={};runtime.admit=threading.Lock()
        runtime.status=lambda:{'ready':True}
        return runtime

    def graph(self):return {'4':{'class_type':'VideoFactoryLocalH3IRCompile','inputs':{}}}

    def test_local_origins_no_cloud_paths_or_credentials(self):
        self.assertEqual(cli.local_url('http://127.0.0.1:8188/'),'http://127.0.0.1:8188')
        for origin in ('https://cloud.invalid','http://user:secret@localhost:8188','http://localhost:8188/v1',
                       'http://localhost:8188?key=secret','http://192.168.1.1:8188'):
            with self.subTest(origin=origin),self.assertRaises(ValueError):cli.local_url(origin)

    def test_model_discovery_refuses_duplicate_files(self):
        for name in ('one','two'):
            path=self.root/name/'model.safetensors';path.parent.mkdir();path.write_bytes(b'unit fixture')
        with self.assertRaisesRegex(ValueError,'Several'):cli.discovered(self.root,'model.safetensors')

    def test_model_cache_cannot_accept_modified_bytes(self):
        path=self.root/'weights.safetensors';path.write_bytes(b'first')
        item={'name':path.name,'bytes':5,'sha256':hashlib.sha256(b'first').hexdigest()}
        config=dict(self.config,model_paths={path.name:str(path)},qwen_model=str(self.root))
        manifest={'models':[item],'qwen_files':[]}
        with patch.object(cli,'load',side_effect=lambda p:manifest if p==cli.MANIFEST else json.loads(Path(p).read_text(encoding='utf-8'))):
            self.assertFalse(cli.inspect_models(config)[0]['ok'])
            self.assertTrue(cli.inspect_models(config,True)[0]['ok'])
            self.assertTrue(cli.inspect_models(config)[0]['ok'])
            path.write_bytes(b'other')
            self.assertFalse(cli.inspect_models(config)[0]['ok'])

    def test_evidence_is_native_namespace_bounded(self):
        allowed=self.root/'output/h3_local_backend_profile_v3/scope/compiler-001/source-gate.json'
        allowed.parent.mkdir(parents=True);allowed.write_text('{"source":"written"}')
        self.assertEqual(supervisor.safe_evidence(self.config,str(allowed)),allowed)
        elsewhere=self.root/'secret.json';elsewhere.write_text('{}')
        for name in (str(elsewhere),str(allowed.parent/'../source-gate.json'),'relative.json',str(self.root/'output/user.json')):
            with self.subTest(name=name),self.assertRaises(ValueError):supervisor.safe_evidence(self.config,name)

    def test_exclusive_prompt_requires_exact_graph_and_no_pending(self):
        graph=self.graph();pid=str(uuid.uuid4());queue={'queue_running':[[1,pid,graph]],'queue_pending':[]}
        self.assertTrue(supervisor.exclusive(queue,pid,graph))
        self.assertFalse(supervisor.exclusive(queue,pid,{}))
        queue['queue_pending']=[['new job']]
        self.assertFalse(supervisor.exclusive(queue,pid,graph))

    def test_qwen_requires_actual_idle_cleanup_and_identity(self):
        health={'service':'video-factory-h3ir-qwen4b-v1','profile':'h3ir-qwen4b-32k',
                'model_id':cli.QWEN_ID,'script_sha256':'a'*64,'offline':True,'busy':False,'loaded':False,
                'inference_phase':'idle','gpu_release_status':'confirmed','last_gpu_cleanup':{'released':True}}
        self.assertTrue(supervisor.idle_qwen(health,'a'*64))
        for key,value in [('busy',True),('loaded',True),('offline',False),('gpu_release_status','unconfirmed'),
                          ('script_sha256','b'*64),('last_gpu_cleanup',{'released':False})]:
            with self.subTest(key=key):self.assertFalse(supervisor.idle_qwen(dict(health,**{key:value}),'a'*64))

    def test_unknown_submit_is_sent_once_and_future_submit_blocked(self):
        runtime=self.runtime();calls=[]
        def fail(route,body,timeout):calls.append((route,body));raise TimeoutError('unit test unknown outcome')
        runtime.comfy=fail;body={'client_id':'job_'+'a'*32,'prompt':self.graph()}
        manifest={'allowed_graph_nodes':['VideoFactoryLocalH3IRCompile']}
        original=cli.load
        with patch.object(supervisor,'load',side_effect=lambda p:manifest if p==cli.MANIFEST else original(p)):
            with self.assertRaisesRegex(supervisor.SubmissionUnknown,'outcome unknown'):runtime.submit(body)
            with self.assertRaisesRegex(ValueError,'already recorded'):runtime.submit(body)
        self.assertEqual(len(calls),1)
        self.assertTrue((self.root/'jobs'/body['client_id']/'submission-unknown.json').is_file())

    def test_foreign_graph_never_submitted(self):
        runtime=self.runtime();calls=[];runtime.comfy=lambda *a,**k:calls.append(a)
        body={'client_id':'job_'+'a'*32,'prompt':{'4':{'class_type':'UnreviewedPythonNode','inputs':{}}}}
        with self.assertRaisesRegex(ValueError,'audited Context'):runtime.submit(body)
        self.assertEqual(calls,[])

    def test_audited_400_validation_rejection_is_terminal_without_queue_admission(self):
        runtime=self.runtime();calls=[]
        payload={'error':{'type':'prompt_outputs_failed_validation','message':'Invalid node inputs'},'node_errors':{'4':{}}}
        def reject(route,body=None,timeout=5):
            if route=='/queue':return {'queue_running':[],'queue_pending':[]}
            calls.append(route)
            raise HTTPError('http://127.0.0.1:8188/prompt',400,'Bad Request',None,io.BytesIO(json.dumps(payload).encode()))
        runtime.comfy=reject;body={'client_id':'job_'+'a'*32,'prompt':self.graph()}
        original=cli.load
        with patch.object(supervisor,'load',side_effect=lambda p:{'allowed_graph_nodes':['VideoFactoryLocalH3IRCompile']} if p==cli.MANIFEST else original(p)):
            with self.assertRaises(supervisor.PromptRejected):runtime.submit(body)
        self.assertEqual(calls,['/prompt']);self.assertEqual(runtime.active,{})
        receipt=cli.load(self.root/'jobs'/body['client_id']/'supervisor-result.json')
        self.assertTrue(receipt['terminal_verified']);self.assertTrue(receipt['rejected_before_queue'])

    def test_400_with_possible_queue_admission_remains_unknown(self):
        runtime=self.runtime();body={'client_id':'job_'+'a'*32,'prompt':self.graph()};calls=[]
        payload={'error':{'type':'prompt_outputs_failed_validation','message':'Invalid'},'node_errors':{}}
        def ambiguous(route,body_arg=None,timeout=5):
            if route=='/queue':return {'queue_running':[[1,str(uuid.uuid4()),body['prompt'],{'client_id':body['client_id']}]],'queue_pending':[]}
            calls.append(route)
            raise HTTPError('http://127.0.0.1:8188/prompt',400,'Bad Request',None,io.BytesIO(json.dumps(payload).encode()))
        runtime.comfy=ambiguous;original=cli.load
        with patch.object(supervisor,'load',side_effect=lambda p:{'allowed_graph_nodes':['VideoFactoryLocalH3IRCompile']} if p==cli.MANIFEST else original(p)):
            with self.assertRaises(supervisor.SubmissionUnknown):runtime.submit(body)
        self.assertEqual(calls,['/prompt']);self.assertIn('unknown:'+body['client_id'],runtime.active)

    def test_http_failure_or_timeout_never_means_absent_comfy(self):
        self.assertFalse(cli.connection_refused(HTTPError('http://127.0.0.1:8188/queue',500,'Failure',None,None)))
        self.assertFalse(cli.connection_refused(URLError(TimeoutError('unknown'))))
        self.assertTrue(cli.connection_refused(URLError(ConnectionRefusedError('no listener'))))
        core=self.root/'comfy';core.mkdir();(core/'main.py').write_text('# unit fixture',encoding='utf-8')
        config=dict(self.config,comfy_root=str(core))
        with patch.object(cli,'request_json',side_effect=HTTPError('http://127.0.0.1:8188/queue',404,'Unknown',None,None)):
            with self.assertRaisesRegex(ValueError,'no node files copied'):cli.install(config)
        self.assertFalse((core/'custom_nodes').exists())

    def test_remote_listener_requires_exact_private_client_allowlist(self):
        supervisor.validate_listener({'listen':'127.0.0.1'})
        with self.assertRaises(ValueError):supervisor.validate_listener({'listen':'0.0.0.0'})
        config={'listen':'0.0.0.0','allowed_clients':['127.0.0.1','192.168.1.30']}
        supervisor.validate_listener(config)
        self.assertTrue(supervisor.client_permitted(config,'192.168.1.30'))
        for address in ('192.168.1.31','8.8.8.8','169.254.1.2'):
            self.assertFalse(supervisor.client_permitted(config,address))
        for address in ('0.0.0.0/0','192.168.1.0/24','8.8.8.8','169.254.1.2'):
            with self.assertRaises(ValueError):supervisor.validate_listener({'listen':'0.0.0.0','allowed_clients':[address]})

    def test_source_gate_draft_never_admits_handoff(self):
        runtime=self.runtime();name='20261106-123456-123456Z-aabbccdd'
        scope=self.root/'output/h3_local_backend_profile_v3'/name;compiler=scope/'compiler-001'
        compiler.mkdir(parents=True)
        for path,body in [(compiler/'source-gate.json',{'status':'PASS','source':'draft','body_sha256':'a'*64}),
                          (compiler/'reference-binding.final.json',{}),(scope/'reference-contract.json',{}),
                          (compiler/'brief.json',{}),(scope/'profile.json',{})]:cli.write(path,body)
        self.assertIsNone(runtime.matching_scope(self.root/'jobs/job_test',self.graph(),set()))

    def test_written_scope_must_bind_exact_seed_declarations_and_cleanup(self):
        runtime=self.runtime();name='20261106-123456-123456Z-aabbccdd'
        scope=self.root/'output/h3_local_backend_profile_v3'/name;compiler=scope/'compiler-001'
        compiler.mkdir(parents=True)
        inputs={'reference_groups':'','required_references':'','motion_targets':'','seed':17}
        graph={'4':{'inputs':inputs},'2':{'inputs':{'tray':'[]'}}}
        records={compiler/'source-gate.json':{'status':'PASS','source':'written','body_sha256':'a'*64},
                 compiler/'reference-binding.final.json':{'status':'PASS','body_sha256':'a'*64},
                 scope/'reference-contract.json':{'raw_declarations':{k:inputs[k] for k in ('reference_groups','required_references','motion_targets')}},
                 compiler/'brief.json':{'assets':[]},scope/'profile.json':{'seed':17},
                 scope/'request-001/receipt.json':{'factory_receipt':{'gpu_cleanup':{'released':True}}}}
        for path,body in records.items():cli.write(path,body)
        case=self.root/'jobs'/('job_'+'a'*32)
        self.assertEqual(runtime.matching_scope(case,graph,set()),scope)
        cli.write(scope/'profile.json',{'seed':18})
        self.assertIsNone(runtime.matching_scope(case,graph,set()))
        cli.write(scope/'profile.json',{'seed':17})
        cli.write(scope/'request-001/receipt.json',{'factory_receipt':{'gpu_cleanup':{'released':False}}})
        self.assertIsNone(runtime.matching_scope(case,graph,set()))

    def test_unowned_qwen_stop_refuses_before_any_termination(self):
        owner=supervisor.QwenOwner(self.config)
        health={'service':'video-factory-h3ir-qwen4b-v1','profile':'h3ir-qwen4b-32k',
                'model_id':cli.QWEN_ID,'script_sha256':owner.expected_sha,'offline':True,
                'busy':False,'loaded':False,'inference_phase':'idle','gpu_release_status':'not_used'}
        with patch.object(owner,'health',return_value=health),patch.object(owner,'owned_process',side_effect=ValueError('foreign process')):
            with self.assertRaisesRegex(ValueError,'foreign process'):owner.stop_idle()

    def test_manifest_has_real_recipe_and_no_weights_bundled(self):
        manifest=cli.load(cli.MANIFEST)
        self.assertGreaterEqual(len(manifest['core_pins']),15)
        self.assertIn('server.py',manifest['core_pins'])
        self.assertFalse(manifest['weights_included'])
        smd=next(x for x in manifest['models'] if x['category']=='loras')
        self.assertEqual(smd['sha256'],'9c6f61221a1043ee709af0d32ae083c17efeb36950b4b08a945296b5b9c39353')
        self.assertIn('VideoFactorySMDSamplingAudit',manifest['required_nodes'])
        self.assertGreaterEqual(manifest['qwen_profile']['context_length'],32768)
        for pack in manifest['project_nodes']:
            for relative,expected in pack['files'].items():
                self.assertEqual(cli.sha(cli.HERE/'bundle/comfy_nodes'/pack['name']/relative),expected)
        self.assertEqual(list(cli.HERE.rglob('*.safetensors')),[])

    def test_matching_package_versions_cannot_hide_missing_cuda_device(self):
        manifest=cli.load(cli.MANIFEST)
        config=dict(self.config,comfy_python='comfy-python',qwen_python='qwen-python')
        def packages(python,names):
            expected=manifest['comfy_packages'] if python=='comfy-python' else manifest['qwen_packages']
            return {'python':manifest['python_versions']['comfy_python' if python=='comfy-python' else 'qwen_python'],'packages':expected}
        hardware={'ok':False,'cuda_available':False,'devices':[],'models_loaded':False,
                  'diagnostics':[{'code':'cuda_device_or_driver_unavailable'}]}
        with patch.object(cli,'inspect_sources',return_value=[]),patch.object(cli,'inspect_models',return_value=[]),\
             patch.object(cli,'probe_python',side_effect=packages),patch.object(cli,'probe_hardware',return_value=hardware),\
             patch.object(cli,'probe_sampler_sources',return_value={'kind':'native_sampler_decorator_source','ok':True}),\
             patch.object(cli,'inspect_media_tool',side_effect=lambda config,name:{'kind':name,'ok':True}):
            result=cli.check(config)
        self.assertFalse(result['ready'])
        device_checks=[x for x in result['checks'] if x['kind'].endswith('_hardware')]
        self.assertEqual(len(device_checks),2)
        self.assertTrue(all(x['ok'] is False for x in device_checks))

    @unittest.skipUnless(sys.platform=='win32','Windows native telemetry')
    def test_windows_commit_structure_reads_real_platform_metrics(self):
        metrics=supervisor.windows_commit()
        self.assertIsInstance(metrics,dict)
        self.assertGreater(metrics['CommitLimit'],0)
        self.assertGreaterEqual(metrics['CommitTotal'],0)
        self.assertGreaterEqual(metrics['CommitPeak'],metrics['CommitTotal'])

if __name__=='__main__':unittest.main()
