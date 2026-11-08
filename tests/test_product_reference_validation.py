"""CPU-only protocol fixtures; these are not model accuracy claims."""
from copy import deepcopy
import ast
import base64
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

import httpx
from h3ir.backend import Backend, digits_png
from h3ir.config import Config, LLMConfig
from h3ir.models import AssetCard, AssetKind, AssetRef, Role


SOURCE = Path(__file__).resolve().parents[1]/'scripts/comfy_nodes/VideoFactoryLocalVLMGuard/product_reference_validation.py'
if not SOURCE.is_file():
    SOURCE=Path(__file__).resolve().parents[1]/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard/product_reference_validation.py'
spec = importlib.util.spec_from_file_location('product_reference_validation_tested', SOURCE)
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


class ProductReferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.refs, self.cards = [], {}
        for index in (1, 2):
            path = self.root/f'image{index}.png'
            path.write_bytes(digits_png(str(index), scale=2, pad=2))
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            self.refs.append(AssetRef(AssetKind.IMAGE, Role.SUBJECT, sha, path=str(path)))
            self.cards[sha] = AssetCard(sha, AssetKind.IMAGE, subjects=[
                {'kind':'object', 'descriptor':f'the product in view {index}', 'attributes':['fixture']},
                # Deliberately mislabeled earlier card: the second model must
                # keep this operator separate without mutating the card.
                {'kind':'object', 'descriptor':'the person holding the product', 'attributes':['fixture']},
            ])
        self.contract = {'groups':[{'category':'product', 'group':'sku-green',
                                     'appearance_native':['<Picture 1>','<Picture 2>'],
                                     'target_description':'The caller-defined green tree toy.'}]}
        self.reply = {'groups':[{'group':'sku-green', 'target_match':'clear',
            'target_match_source':'derived_from_per_picture_checks', 'references':[
            {'native_label':f'<Picture {index}>', 'same_sku_as_anchor':'same', 'target_visible':True,
             'target_subject_indices':[0], 'independent_person_indices':[1],
             'conflicting_variant_indices':[], 'reason':'Synthetic protocol fixture; not an image description.'}
            for index in (1,2)]}]}
        self.requests = []

    def backend(self, reply=None, endpoint='http://127.0.0.1:8772/v1', model=validation.MODEL):
        def handler(request):
            self.requests.append(json.loads(request.content))
            content = self.reply if reply is None else reply
            if callable(content):
                content = content(self.requests[-1])
            if isinstance(content,dict) and isinstance(content.get('groups'),list):
                content = self._raw_reply(content)
            return httpx.Response(200, json={'model':model, 'choices':[{
                'message':{'content':json.dumps(content)}, 'finish_reason':'stop'}]})
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return Backend(Config(llm=LLMConfig(base_url=endpoint,model=model,guided_decoding=True)), client)

    def _raw_reply(self, reply):
        return {'groups':{group['group']:{
            'references':{row['native_label']:{key:value for key,value in row.items() if key!='native_label'}
                          for row in group['references']}} for group in reply['groups']}}

    def check(self, backend=None, **kwargs):
        return validation.verify_product_references(backend or self.backend(), self.refs, self.cards,
            self.contract, self.root/'cache', seed=42, receipt_root=self.root/'scope', **kwargs)

    def test_actual_images_sent_once_and_only_verified_product_descriptors_are_approved(self):
        original = deepcopy(self.cards)
        receipt = self.check()
        self.assertEqual(receipt['status'],'PASS')
        self.assertFalse(receipt['cache_hit'])
        self.assertFalse(receipt['human_approved'])
        self.assertEqual(receipt['approved_product_descriptors_by_native'], {
            '<Picture 1>':['the product in view 1'], '<Picture 2>':['the product in view 2']})
        self.assertEqual(receipt['independent_person_indices_by_native']['<Picture 1>'],[1])
        self.assertEqual(self.cards, original)
        self.assertEqual(len(self.requests),1)
        request = self.requests[0]
        self.assertEqual(request['max_tokens'],4096)
        self.assertEqual(request['seed'],42)
        self.assertFalse(request['chat_template_kwargs']['enable_thinking'])
        images = [part['image_url']['url'] for part in request['messages'][1]['content'] if part['type']=='image_url']
        self.assertEqual(len(images),2)
        for image,ref in zip(images,self.refs):
            self.assertEqual(base64.b64decode(image.split(',',1)[1]), Path(ref.path).read_bytes())
        evidence=json.loads((self.root/'scope/product-reference-identity.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['reply'],self.reply)
        self.assertEqual(evidence['reply_sha256'],validation._digest(self.reply))

    def test_pass_cache_is_revalidated_and_target_or_card_change_requires_new_call(self):
        backend=self.backend()
        first=self.check(backend)
        second=self.check(backend)
        self.assertTrue(second['cache_hit'])
        self.assertEqual(first['cache_key'],second['cache_key'])
        self.assertEqual(len(self.requests),1)
        self.contract['groups'][0]['target_description']='A changed explicit caller SKU description.'
        self.check(backend)
        self.assertEqual(len(self.requests),2)
        self.cards[self.refs[0].sha256].subjects[0]['attributes'].append('new observation')
        self.check(backend)
        self.assertEqual(len(self.requests),3)

    def test_corrupt_semantic_cache_never_grants_an_identity(self):
        backend=self.backend()
        receipt=self.check(backend)
        cache=self.root/'cache'/f"{receipt['cache_key']}.json"
        saved=json.loads(cache.read_text(encoding='utf-8'))
        saved['reply']['groups'][0]['references'][0]['target_subject_indices']=[999]
        saved['reply_sha256']=validation._digest(saved['reply'])
        cache.write_text(json.dumps(saved),encoding='utf-8')
        fresh=self.check(backend)
        self.assertFalse(fresh['cache_hit'])
        self.assertEqual(len(self.requests),2)
        self.assertEqual(fresh['approved_product_indices_by_native']['<Picture 1>'],[0])

    def test_variants_missing_targets_and_uncertain_results_block_before_cache_write(self):
        for field,value in [('same_sku_as_anchor','different'),('same_sku_as_anchor','uncertain'),
                            ('target_visible',False),('conflicting_variant_indices',[1])]:
            with self.subTest(field=field,value=value):
                reply=deepcopy(self.reply)
                reply['groups'][0]['references'][1][field]=value
                with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 2'):
                    self.check(self.backend(reply))
                self.assertEqual(list((self.root/'cache').glob('*.json')),[])
                evidence=json.loads((self.root/'scope/product-reference-identity.json').read_text(encoding='utf-8'))
                self.assertEqual(evidence['status'],'BLOCKED')

    def test_missing_duplicate_unknown_cross_group_and_invalid_subject_results_block(self):
        variants=[]
        reply=deepcopy(self.reply);reply['groups'][0]['references'].pop();variants.append(reply)
        reply=deepcopy(self.reply);reply['groups'][0]['references'][1]['native_label']='<Picture 1>';variants.append(reply)
        reply=deepcopy(self.reply);reply['groups'][0]['references'][1]['native_label']='<Picture 9>';variants.append(reply)
        for indices in ([],[0,1],[8],[True]):
            reply=deepcopy(self.reply);reply['groups'][0]['references'][0]['target_subject_indices']=indices;variants.append(reply)
        reply=deepcopy(self.reply);reply['groups'][0]['references'][0]['independent_person_indices']=[0];variants.append(reply)
        for reply in variants:
            with self.subTest(reply=reply):
                with self.assertRaises(validation.ProductReferenceConflict):self.check(self.backend(reply))

    def test_person_can_never_be_approved_as_product_and_ambiguous_descriptors_block(self):
        self.cards[self.refs[0].sha256].subjects[0]['kind']='person'
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'人物'):self.check()
        self.cards[self.refs[0].sha256].subjects[0]['kind']='object'
        self.cards[self.refs[0].sha256].subjects[1]['descriptor']='the product in view 1'
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'唯一'):self.check()

    def test_actual_file_changes_and_missing_cards_block_without_model_request(self):
        Path(self.refs[0].path).write_bytes(b'changed')
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'SHA256'):self.check()
        self.assertEqual(self.requests,[])
        self.refs[0].sha256=hashlib.sha256(b'changed').hexdigest()
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'图像分析'):self.check()
        self.assertEqual(self.requests,[])

    def test_cloud_or_wrong_local_model_is_not_used_and_unneeded_check_is_inert(self):
        for address,model in [('https://example.com/v1',validation.MODEL),
                              ('http://127.0.0.1:8772/v1','another-model')]:
            with self.assertRaisesRegex(validation.ProductReferenceConflict,'本机 Qwen'):
                self.check(self.backend(endpoint=address,model=model))
        self.assertEqual(self.requests,[])
        receipt=validation.verify_product_references(None,[],{}, {'groups':[]},self.root/'unused')
        self.assertEqual(receipt['status'],'NOT_REQUIRED')
        self.assertFalse((self.root/'unused').exists())

    def test_several_groups_are_checked_in_one_request_with_original_native_labels(self):
        self.contract['groups']=[
            {'category':'product','group':'sku-one','appearance_native':['<Picture 1>']},
            {'category':'product','group':'sku-two','appearance_native':['<Picture 2>']},
        ]
        self.reply={'groups':[
            {'group':'sku-one','target_match':'clear','references':[deepcopy(self.reply['groups'][0]['references'][0])]},
            {'group':'sku-two','target_match':'clear','references':[deepcopy(self.reply['groups'][0]['references'][1])]},
        ]}
        receipt=self.check()
        self.assertEqual(receipt['status'],'PASS')
        self.assertEqual(len(self.requests),1)
        self.assertEqual([row['group'] for row in receipt['group_results']],['sku-one','sku-two'])

    def _nine_images(self, group_lengths=(9,)):
        for index in range(3,10):
            path=self.root/f'image{index}.png'
            path.write_bytes(digits_png(str(index),scale=2,pad=2))
            sha=hashlib.sha256(path.read_bytes()).hexdigest()
            self.refs.append(AssetRef(AssetKind.IMAGE,Role.SUBJECT,sha,path=str(path)))
            self.cards[sha]=AssetCard(sha,AssetKind.IMAGE,subjects=[
                {'kind':'object','descriptor':f'the product in view {index}','attributes':['fixture']},
                {'kind':'object','descriptor':'the person holding the product','attributes':['fixture']},
            ])
        offset=0
        groups=[]
        for index,length in enumerate(group_lengths,1):
            groups.append({'category':'product','group':f'sku-{index}',
                           'appearance_native':[f'<Picture {i}>' for i in range(offset+1,offset+length+1)],
                           'target_description':'Synthetic identity test target.'})
            offset+=length
        self.contract={'groups':groups}

    def _batch_reply(self, request):
        text=next(part['text'] for part in request['messages'][1]['content'] if part['type']=='text')
        groups=json.loads(text.split('Caller product groups and target descriptions:\n',1)[1]
                          .split('\nExisting image observations,',1)[0])
        return {'groups':[{'group':group['group'],'target_match':'clear','references':[
            {'native_label':label,'same_sku_as_anchor':'same','target_visible':True,
             'target_subject_indices':[0],'independent_person_indices':[1],
             'conflicting_variant_indices':[],'reason':'Synthetic bounded-batch protocol fixture.'}
            for label in group['appearance_native']]} for group in groups]}

    def test_schema_is_accepted_by_the_actual_fixed_service_guided_schema_checker(self):
        project=Path(__file__).resolve().parents[1]
        source=project/'scripts/local_inference/bundle/serve_screenplay_qwen.py'
        if not source.is_file():
            source=project/'scripts/serve_screenplay_qwen.py'
        tree=ast.parse(source.read_text(encoding='utf-8-sig'),filename=str(source))
        function=next(node for node in tree.body if isinstance(node,ast.FunctionDef) and node.name=='_check_guided_schema')
        namespace={'re':re}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(source),'exec'),namespace)
        checker=namespace['_check_guided_schema']
        groups=[{'group':'fixture-one','appearance_native':[f'<Picture {i}>' for i in range(1,9)]}]
        checker(validation._schema(groups))
        checker(validation._schema([{'group':f'fixture-{i}','appearance_native':[f'<Picture {i}>']} for i in range(1,9)]))
        for forbidden in ('minimum','maximum','uniqueItems'):
            self.assertNotIn('"'+forbidden+'"',json.dumps(validation._schema(groups)))
        with self.assertRaisesRegex(ValueError,'Unsupported guided'):
            checker({'type':'integer','minimum':0})

    def test_nine_view_product_uses_eight_then_same_anchor_plus_ninth_without_dropping_images(self):
        self._nine_images()
        receipt=self.check(self.backend(self._batch_reply))
        self.assertEqual(receipt['status'],'PASS')
        self.assertEqual(receipt['actual_call_count'],2)
        self.assertEqual(len(self.requests),2)
        counts=[sum(part['type']=='image_url' for part in request['messages'][1]['content']) for request in self.requests]
        self.assertEqual(counts,[8,2])
        self.assertEqual(receipt['batches'][0]['input_labels'],[f'<Picture {i}>' for i in range(1,9)])
        self.assertEqual(receipt['batches'][1]['input_labels'],['<Picture 1>','<Picture 9>'])
        self.assertEqual(set(receipt['approved_product_indices_by_native']),{f'<Picture {i}>' for i in range(1,10)})
        self.assertEqual(len(receipt['group_results']),9)
        for index in (1,2):
            evidence=json.loads((self.root/f'scope/product-reference-identity.batch-{index:03}.json').read_text(encoding='utf-8'))
            self.assertEqual(evidence['status'],'PASS')
            self.assertEqual(evidence['reply_sha256'],validation._digest(evidence['reply']))

    def test_seven_plus_two_product_groups_stay_intact_and_use_only_two_calls(self):
        self._nine_images((7,2))
        receipt=self.check(self.backend(self._batch_reply))
        self.assertEqual(receipt['actual_call_count'],2)
        self.assertEqual([len(batch['input_labels']) for batch in receipt['batches']],[7,2])
        self.assertEqual([row['group'] for row in receipt['group_results']],['sku-1']*7+['sku-2']*2)
        self.assertEqual(receipt['batches'][1]['input_labels'],['<Picture 8>','<Picture 9>'])

    def test_conflicting_repeated_anchor_subject_or_person_indices_blocks_the_merged_identity(self):
        self._nine_images()
        for product_index,persons in [(1,[]),(0,[])]:
            def answer(request):
                result=self._batch_reply(request)
                if len([part for part in request['messages'][1]['content'] if part['type']=='image_url'])==2:
                    anchor=result['groups'][0]['references'][0]
                    anchor['target_subject_indices']=[product_index]
                    anchor['independent_person_indices']=persons
                return result
            with self.subTest(product_index=product_index,persons=persons):
                with self.assertRaisesRegex(validation.ProductReferenceConflict,'重复锚点'):
                    self.check(self.backend(answer))
                self.assertEqual(list((self.root/'cache').glob('*.json')),[])
        self.assertEqual(len(self.requests),4)

    def test_schema_integer_without_bounds_still_refuses_negative_large_boolean_and_person_indices(self):
        changes=[('target_subject_indices',[-1]),('target_subject_indices',[8]),
                 ('target_subject_indices',[100000]),('target_subject_indices',[True]),
                 ('independent_person_indices',[-1]),('independent_person_indices',[8]),
                 ('independent_person_indices',[1,1])]
        for field,value in changes:
            reply=deepcopy(self.reply)
            reply['groups'][0]['references'][0][field]=value
            with self.subTest(field=field,value=value):
                with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 1.*唯一定位商品'):
                    self.check(self.backend(reply))

    def test_two_batch_cache_revalidates_anchor_agreement_and_protocol_before_reuse(self):
        self._nine_images()
        backend=self.backend(self._batch_reply)
        first=self.check(backend)
        cached=self.check(backend)
        self.assertTrue(cached['cache_hit'])
        self.assertEqual(cached['actual_call_count'],0)
        self.assertEqual(len(self.requests),2)
        path=self.root/'cache'/f"{first['cache_key']}.json"
        data=json.loads(path.read_text(encoding='utf-8'))
        data['batches'][1]['reply']['groups'][0]['references'][0]['independent_person_indices']=[]
        data['batches'][1]['reply_sha256']=validation._digest(data['batches'][1]['reply'])
        path.write_text(json.dumps(data),encoding='utf-8')
        fresh=self.check(backend)
        self.assertFalse(fresh['cache_hit'])
        self.assertEqual(fresh['actual_call_count'],2)
        self.assertEqual(len(self.requests),4)
        data=json.loads(path.read_text(encoding='utf-8'))
        data['protocol']='product-reference-identity-1'
        path.write_text(json.dumps(data),encoding='utf-8')
        self.assertFalse(self.check(backend)['cache_hit'])
        self.assertEqual(len(self.requests),6)

    def test_dynamic_schema_requires_every_group_and_picture_and_observes_before_verdict(self):
        schema=validation._schema(self.contract['groups'])
        groups=schema['properties']['groups']
        self.assertEqual(groups['type'],'object')
        self.assertEqual(groups['required'],['sku-green'])
        self.assertFalse(groups['additionalProperties'])
        group=groups['properties']['sku-green']
        self.assertEqual(list(group['properties']),['references'])
        refs=group['properties']['references']
        self.assertEqual(refs['type'],'object')
        self.assertEqual(refs['required'],['<Picture 1>','<Picture 2>'])
        self.assertEqual(set(refs['properties']),set(refs['required']))
        self.assertFalse(refs['additionalProperties'])
        for picture in refs['properties'].values():
            self.assertEqual(next(iter(picture['properties'])),'reason')
            self.assertEqual(list(picture['properties'])[-1],'same_sku_as_anchor')
            self.assertEqual(set(picture['required']),set(picture['properties']))
            self.assertNotIn('native_label',picture['properties'])

    def test_missing_picture_raw_reply_blocks_and_preserves_exact_original_reply(self):
        raw=self._raw_reply(self.reply)
        del raw['groups']['sku-green']['references']['<Picture 2>']
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 2'):
            self.check(self.backend(raw))
        evidence=json.loads((self.root/'scope/product-reference-identity.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['batches'][0]['raw_reply'],raw)
        self.assertEqual(evidence['batches'][0]['raw_reply_sha256'],validation._digest(raw))
        self.assertNotIn('reply',evidence['batches'][0])
        self.assertEqual(len(self.requests),1)
        self.assertEqual(list((self.root/'cache').glob('*.json')),[])

    def test_unknown_or_disguised_duplicate_picture_keys_cannot_satisfy_named_answers(self):
        for unknown in ('<Picture 9>','picture1','<Picture 01>'):
            raw=self._raw_reply(self.reply)
            raw['groups']['sku-green']['references'][unknown]=deepcopy(raw['groups']['sku-green']['references']['<Picture 1>'])
            with self.subTest(unknown=unknown):
                with self.assertRaisesRegex(validation.ProductReferenceConflict,'全部指定图片'):
                    self.check(self.backend(raw))
        raw=self._raw_reply(self.reply)
        raw['groups']['sku-other']=deepcopy(raw['groups']['sku-green'])
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'所有指定商品'):
            self.check(self.backend(raw))

    def test_complete_named_uncertain_observations_are_rejected_not_rewritten_as_pass(self):
        raw=self._raw_reply(self.reply)
        raw['groups']['sku-green']['references']['<Picture 2>']['same_sku_as_anchor']='uncertain'
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 2'):
            self.check(self.backend(raw))
        evidence=json.loads((self.root/'scope/product-reference-identity.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['batches'][0]['raw_reply'],raw)
        self.assertEqual(evidence['batches'][0]['reply']['groups'][0]['target_match'],'uncertain')
        self.assertEqual(evidence['status'],'BLOCKED')
        self.assertEqual(list((self.root/'cache').glob('*.json')),[])

    def test_specific_conflicting_picture_is_reported_before_the_uncertain_group_summary(self):
        raw=self._raw_reply(self.reply)
        group=raw['groups']['sku-green']
        group['references']['<Picture 2>']['same_sku_as_anchor']='different'
        group['references']['<Picture 2>']['reason']='A different physical design is visible in this synthetic fixture.'
        with self.assertRaises(validation.ProductReferenceConflict) as caught:
            self.check(self.backend(raw))
        self.assertIn('<Picture 2> A different physical design',str(caught.exception))
        self.assertNotIn('<Picture 1>',str(caught.exception))

    def test_anchor_comparison_is_a_logical_const_but_untrusted_uncertain_is_never_overwritten(self):
        refs=validation._schema(self.contract['groups'])['properties']['groups']['properties']['sku-green']['properties']['references']['properties']
        self.assertEqual(refs['<Picture 1>']['properties']['same_sku_as_anchor'],{'type':'string','const':'same'})
        self.assertEqual(refs['<Picture 2>']['properties']['same_sku_as_anchor']['enum'],['same','different','uncertain'])
        raw=self._raw_reply(self.reply)
        raw['groups']['sku-green']['references']['<Picture 1>']['same_sku_as_anchor']='uncertain'
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 1'):
            self.check(self.backend(raw))
        evidence=json.loads((self.root/'scope/product-reference-identity.json').read_text(encoding='utf-8'))
        self.assertEqual(evidence['batches'][0]['raw_reply'],raw)
        self.assertEqual(evidence['batches'][0]['reply']['groups'][0]['target_match'],'uncertain')

    def test_conflicting_variants_require_real_disjoint_bounded_card_subject_indices(self):
        for indices in ([-1],[8],[True],[0],[1],[2,2]):
            raw=self._raw_reply(self.reply)
            raw['groups']['sku-green']['references']['<Picture 2>']['conflicting_variant_indices']=indices
            with self.subTest(indices=indices):
                with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 2.*唯一定位商品'):
                    self.check(self.backend(raw))
        self.cards[self.refs[1].sha256].subjects.append({'kind':'object','descriptor':'a distinct physical product variant'})
        raw=self._raw_reply(self.reply)
        raw['groups']['sku-green']['references']['<Picture 2>']['conflicting_variant_indices']=[2]
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'Picture 2.*实体商品款式.*2'):
            self.check(self.backend(raw))

    def test_checker_receives_only_index_descriptor_attributes_but_full_cards_remain_in_evidence_and_cache(self):
        card=self.cards[self.refs[0].sha256]
        card.summary='SUMMARY-NOISE-NEVER-SENT'
        card.environment='ENVIRONMENT-NOISE-NEVER-SENT'
        card.subjects[0]['pose']=['POSE-NOISE-NEVER-SENT']
        receipt=self.check()
        text=next(part['text'] for part in self.requests[0]['messages'][1]['content'] if part['type']=='text')
        for omitted in (card.summary,card.environment,card.subjects[0]['pose'][0]):
            self.assertNotIn(omitted,text)
        self.assertIn('the product in view 1',text)
        self.assertIn('"index": 0',text)
        self.assertIn('"attributes": ["fixture"]',text)
        self.assertEqual(receipt['original_observations']['<Picture 1>']['card']['summary'],card.summary)
        self.assertEqual(receipt['target_match_source'],'derived_from_per_picture_checks')
        cache=json.loads((self.root/'cache'/f"{receipt['cache_key']}.json").read_text(encoding='utf-8'))
        self.assertEqual(cache['original_observations'],receipt['original_observations'])

    def test_target_with_separate_package_and_person_can_pass_without_a_variant_boolean(self):
        self.cards[self.refs[1].sha256].subjects.append({'kind':'object','descriptor':'the same-product gift box',
            'attributes':['flat printed illustrations','packaging']})
        receipt=self.check()
        self.assertEqual(receipt['status'],'PASS')
        row=receipt['reply']['groups'][0]['references'][1]
        self.assertEqual(row['conflicting_variant_indices'],[])
        self.assertNotIn('other_variant_visible',row)
        self.assertEqual(row['target_subject_indices'],[0])
        self.assertEqual(row['independent_person_indices'],[1])

    def test_old_array_omitted_picture_regression_fixture_is_not_an_acceptable_raw_envelope(self):
        # Synthetic reproduction of a legally short v2 array; no private
        # workstation response or actual model accuracy is claimed by this test.
        old={'groups':[{'group':'sku-green','target_match':'uncertain','references':[
            {**deepcopy(self.reply['groups'][0]['references'][0]),
             'reason':'Synthetic combined Image 1 and Image 2 observation, with only one returned picture.'}]}]}
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'所有指定商品'):
            validation._normalise_reply(old,self.contract['groups'])
        with self.assertRaisesRegex(validation.ProductReferenceConflict,'观察与判断字段'):
            raw=self._raw_reply(self.reply)
            raw['groups']['sku-green']['references']['<Picture 2>']['native_label']='<Picture 1>'
            self.check(self.backend(raw))


if __name__ == '__main__':
    unittest.main()
