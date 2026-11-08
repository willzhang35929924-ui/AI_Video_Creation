"""CPU contracts for project-owned binding and caller constraints; no inference."""
from copy import deepcopy
from dataclasses import dataclass,field
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'scripts/comfy_nodes/VideoFactoryLocalVLMGuard'
if not SOURCE.is_dir():SOURCE=ROOT/'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
spec=importlib.util.spec_from_file_location('_revision_policy',SOURCE/'__init__.py',submodule_search_locations=[str(SOURCE)])
package=importlib.util.module_from_spec(spec);sys.modules[spec.name]=package;spec.loader.exec_module(package)
profile=__import__('importlib').import_module(spec.name+'.local_h3ir_backend_profile')
constraints=__import__('importlib').import_module(spec.name+'.revision_constraints')


@dataclass
class Subject:
    label:str
    descriptor:str
    sources:list
    kind:str='object'


@dataclass
class Plan:
    subjects:list=field(default_factory=list)
    manifest:list=field(default_factory=list)


def example():
    prompt='''subject_definitions:
<Subject 1> is the green tree toy in <Picture 1>.
<Subject 2> is the same green toy in <Picture 2>.
<Subject 3> is the operator in <Picture 2>.

summary:
A product demonstration.

retention_analysis:
<Subject 1>: fully_preserved - first product appearance
<Subject 2>: fully_preserved - second product appearance
<Subject 3>: partially_preserved - operator action

detailed_description:
The target video uses a photographic live-action style.
[Shot 1] The camera holds static. <Subject 1> is being squeezed by <Subject 3>.

overall_soundscape:
Quiet room tone.

non_diegetic_music:
N/A
'''
    plan=Plan([Subject('<Subject 1>','the green tree toy',['<Picture 1>']),
               Subject('<Subject 2>','the same green toy',['<Picture 2>']),
               # Reproduces the earlier card's incorrect object classification.
               Subject('<Subject 3>','the operator',['<Picture 2>'])],
              [SimpleNamespace(label='<Picture 1>'),SimpleNamespace(label='<Picture 2>')])
    # asdict requires every object in this lightweight Plan to be JSON-safe.
    plan.manifest=[]
    doc=SimpleNamespace(prompt=prompt,plan=plan,findings=[],provenance={},mode='reference',source='written',fallback_reason=None)
    group={'category':'product','group':'product-1','appearance_native':['<Picture 1>','<Picture 2>'],
           'motion_native':[],'native_sources':['<Picture 1>','<Picture 2>']}
    contract={'groups':[group],'required_native':[],
              'verified_product_descriptors_by_native':{'<Picture 1>':['the green tree toy'],
                                                       '<Picture 2>':['the same green toy']}}
    return doc,SimpleNamespace(dialogue=[],onscreen_text=[]),contract


class BindingIntegrityTests(unittest.TestCase):
    @staticmethod
    def repeated_product():
        doc,brief,contract=example()
        first='the green plush Christmas tree in <Picture 1>, with soft plush fabric, green color and a yellow star.'
        second='the green plush Christmas tree in <Picture 2>, with round body, embroidered eyes and small colored dots.'
        doc.prompt=doc.prompt.replace('the green tree toy in <Picture 1>.',first).replace(
            '<Subject 2> is the same green toy in <Picture 2>.','<Subject 1> is '+second)
        doc.prompt=doc.prompt.replace('<Subject 2>: fully_preserved - second product appearance\n','')
        doc.plan.subjects[0].descriptor='the green plush Christmas tree'
        doc.plan.subjects[1].descriptor='the green plush tree'
        contract['verified_product_descriptors_by_native']={
            '<Picture 1>':['the green plush Christmas tree'], '<Picture 2>':['the green plush tree']}
        return doc,brief,contract,first,second

    def test_repeated_same_label_verified_product_keeps_both_exact_bodies_and_plan(self):
        doc,brief,contract,first,second=self.repeated_product();before=deepcopy(doc)
        prompt,proof=profile.project_declared_bindings(doc,contract,brief)
        self.assertEqual(prompt.split('summary:')[0].count('<Subject 1> is '),1)
        self.assertIn(first,prompt);self.assertIn(second,prompt)
        self.assertIn('<Subject 3> is the operator',prompt)
        self.assertEqual(doc.prompt,before.prompt);self.assertEqual(doc.plan,before.plan)
        self.assertEqual(proof['alias_map'],{})
        self.assertEqual(proof['same_label_definition_merges'][0]['original_definition_count'],2)
        self.assertEqual(proof['same_label_definition_merges'][0]['group'],'product-1')
        self.assertTrue(all(profile.projection_invariants(prompt,proof).values()))

    def test_repeated_same_label_box_or_operator_cannot_borrow_verified_product_words(self):
        for replacement in ['the gift box in <Picture 2>, containing the green plush Christmas tree.',
                            'the operator in <Picture 2>, holding the green plush Christmas tree.']:
            with self.subTest(anchor=replacement):
                doc,brief,contract,_,second=self.repeated_product()
                doc.prompt=doc.prompt.replace(second,replacement)
                with self.assertRaisesRegex(profile.LocalProfileBlocked,'leading product anchor'):
                    profile.project_declared_bindings(doc,contract,brief)

    def test_repeated_same_label_cross_group_source_is_not_merged(self):
        doc,brief,contract,_,_=self.repeated_product()
        contract['groups'][0].update(appearance_native=['<Picture 1>'],native_sources=['<Picture 1>'])
        contract['groups'].append({'category':'product','group':'product-2',
            'appearance_native':['<Picture 2>'],'motion_native':[],'native_sources':['<Picture 2>']})
        with self.assertRaisesRegex(profile.LocalProfileBlocked,'one verified product group'):
            profile.project_declared_bindings(doc,contract,brief)

    def test_repeated_same_label_requires_verified_anchors_and_owned_sources(self):
        for change in ['missing_proof','unknown_anchor','unowned_source','unobserved_anchor']:
            with self.subTest(change=change):
                doc,brief,contract,_,second=self.repeated_product()
                if change=='missing_proof':contract.pop('verified_product_descriptors_by_native')
                elif change=='unknown_anchor':doc.prompt=doc.prompt.replace(second,'an unidentified green object in <Picture 2>.')
                elif change=='unowned_source':doc.prompt=doc.prompt.replace(second,second+' Also from <Picture 9>.')
                else:contract['verified_product_descriptors_by_native']={
                    '<Picture 1>':['the green plush Christmas tree'], '<Picture 2>':['the green plush Christmas tree']};doc.plan.subjects[0].descriptor='unverified package'
                with self.assertRaises(profile.LocalProfileBlocked):
                    profile.project_declared_bindings(doc,contract,brief)

    def test_only_verified_product_is_merged_and_operator_stays_independent_even_if_kind_is_object(self):
        doc,brief,contract=example();before=deepcopy(doc)
        prompt,proof=profile.project_declared_bindings(doc,contract,brief)
        self.assertEqual(proof['alias_map'],{'<Subject 2>':'<Subject 1>'})
        self.assertIn('<Subject 3> is the operator',prompt)
        self.assertIn('<Subject 1> is being squeezed by <Subject 3>',prompt)
        self.assertEqual(doc.prompt,before.prompt)
        self.assertEqual(doc.plan,before.plan)

    def test_self_squeeze_from_unverified_aliasing_is_blocked_before_model_loading(self):
        doc,brief,contract=example();contract.pop('verified_product_descriptors_by_native')
        doc.plan.subjects[2].sources=['<Picture 3>']
        doc.prompt=doc.prompt.replace('<Subject 3> is the operator in <Picture 2>',
                                      '<Subject 3> is the operator in <Picture 3>')
        contract['groups'][0]['appearance_native'].append('<Picture 3>')
        contract['groups'][0]['native_sources'].append('<Picture 3>')
        with self.assertRaisesRegex(profile.LocalProfileBlocked,'操作者'):
            profile.project_declared_bindings(doc,contract,brief)

    def test_required_visual_check_cannot_be_bypassed_by_a_declared_same_product_id(self):
        doc,brief,contract=example();contract['identity_validation_required']=True
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(profile.LocalProfileBlocked,'身份'):
                profile._audit_or_project(doc,brief,{'reference_contract':contract},Path(temp))

    def test_cloud_backend_and_node_use_byte_identical_constraint_rules(self):
        self.assertEqual((ROOT/'backend/app/services/revision_constraints.py').read_bytes(),
                         (SOURCE/'revision_constraints.py').read_bytes())


class CallerConstraintTests(unittest.TestCase):
    def apply(self,doc,contract,folder):
        scope={'reference_contract':contract,'cards':{},'final_reference_binding':{'group_results':[
            {'group':'product-1','subject':'<Subject 1>','status':'PASS'}]}}
        # These are boundary unit tests, not simulated model/quality acceptance.
        # The existing native suite separately checks actual Main serialization.
        with patch('h3ir.compile.token_count',return_value=200),patch('h3ir.compile.validate',return_value=[]), \
             patch('h3ir.compile.wiring_findings',return_value=[]),patch('h3ir.compile._written_context'), \
             patch('h3ir.licence.resolve_licence'),patch('h3ir.style.resolve_style'):
            return profile._apply_revision_constraints(doc,SimpleNamespace(),scope,folder)

    def test_five_real_instruction_forms_reach_actual_shot_body_with_explicit_source_receipt(self):
        for instruction in ['P02高12.5cm，宽15cm','P02高3.5cm，宽5cm','P02只有两个手掌大小',
                            'P02只有一个手掌大小','P02只有水杯大小']:
            with self.subTest(instruction=instruction),tempfile.TemporaryDirectory() as temp:
                doc,_,contract=example();contract.update(revision_constraints=constraints.extract_revision_constraints(instruction),
                    constraint_target_groups={'P02':'product-1'})
                result=self.apply(doc,contract,Path(temp))
                self.assertIn('[Shot 1] Current caller-authored product scale:',result.prompt)
                receipt=json.loads((Path(temp)/'revision-constraints.json').read_text(encoding='utf-8'))
                self.assertEqual(receipt['status'],'PASS')
                self.assertEqual(receipt['source'],'caller_revision_constraints')
                self.assertEqual(receipt['clauses'][0]['subject'],'<Subject 1>')
                self.assertTrue(result.provenance['caller_authored_constraints_added'])
                self.assertNotEqual(doc.prompt,result.prompt)
                self.assertEqual(self.apply(result,contract,Path(temp)).prompt,result.prompt)

    def test_text_target_requires_a_unique_actual_definition_not_a_summary_mention(self):
        for changed in [False,True]:
            with self.subTest(ambiguous=changed),tempfile.TemporaryDirectory() as temp:
                doc,_,contract=example();contract.update(revision_constraints=constraints.extract_revision_constraints('P02高3.5cm'),
                    constraint_target_groups={})
                if changed:
                    doc.prompt=doc.prompt.replace('the green tree toy','P02 toy').replace('the same green toy','P02 toy')
                else:doc.prompt=doc.prompt.replace('A product demonstration.','A P02 product demonstration.')
                with self.assertRaises(profile.LocalProfileBlocked):self.apply(doc,contract,Path(temp))
                receipt=json.loads((Path(temp)/'revision-constraints.json').read_text(encoding='utf-8'))
                self.assertEqual(receipt['status'],'BLOCKED')


if __name__=='__main__':unittest.main()
