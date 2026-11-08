"""CPU checks of the pinned native verification hook and final H3 admission.

The prompts are authored fixtures. No model, service, GPU, or generated frame is
used; the native Context builder and text validator are the installed compiler.
"""
from copy import deepcopy
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import h3ir.compile as compiler
from h3ir.models import (AssetKind, Brief, Finding, IRDocument, ManifestEntry, Mode,
                         Plan, Role, SubjectPlan, Target)
from h3ir.validate import Context


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'scripts/comfy_nodes/VideoFactoryLocalVLMGuard'
if not SOURCE.is_dir():
    SOURCE = ROOT / 'scripts/local_inference/bundle/comfy_nodes/VideoFactoryLocalVLMGuard'
spec = importlib.util.spec_from_file_location('_revision_semantic_gate', SOURCE / '__init__.py',
                                             submodule_search_locations=[str(SOURCE)])
package = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = package
spec.loader.exec_module(package)
profile = importlib.import_module(spec.name + '.local_h3ir_backend_profile')
constraints = importlib.import_module(spec.name + '.revision_constraints')
semantics = importlib.import_module(spec.name + '.revision_semantics')
node = importlib.import_module(spec.name + '.compile_node')

INSTRUCTION = ('P02高10cm，宽8cm。第一帧P02由左手掌心托住，'
               '手与脸在同一画面；随后把P02抬起贴近脸颊。')
CUSTOM_RULES = {'revision_target_scale_conflict', 'revision_reference_scale_used',
                'target_first_frame_palm_support', 'target_hand_face_same_frame',
                'target_raise_to_cheek'}


def contract():
    scale = constraints.extract_revision_constraints(INSTRUCTION)
    return {'groups': [{'group': 'product-1', 'category': 'product',
                        'appearance_native': ['<Picture 1>', '<Picture 2>']}],
            'revision_constraints': scale, 'constraint_target_groups': {'P02': 'product-1'},
            'scene_requirements': semantics.extract_scene_requirements(INSTRUCTION, scale)}


def prompt(*, repaired=False, text_only=False):
    definitions = ('<Subject 1> is P02, the green plush.' if text_only else
                   '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.')
    retention = '<Subject 1>: fully_preserved - green plush and yellow star.'
    body = 'The camera holds static. <Subject 1> sits on the carpet beside the child.'
    if repaired:
        body = ('The camera holds static. At the first frame <Subject 1> is supported by the open left palm. '
                'The left hand and the child\'s face remain visible in the same frame with <Subject 1>. '
                'The height of <Subject 1> is 10cm and its width is 8cm. '
                'The child then raises <Subject 1> to her cheek.')
    elif not text_only:
        definitions = ('<Picture 1> shows a green plush with a yellow star.\n'
                       '<Picture 2> shows the same plush, 15cm wide and 12.5cm tall.\n' + definitions)
        retention = ('<Subject 1>: fully_preserved - Printed dimensions from <Picture 2> '
                     'are used to establish its physical scale.')
    return (f'subject_definitions:\n{definitions}\n\nsummary:\nA product demonstration.\n\n'
            f'retention_analysis:\n{retention}\n\ndetailed_description:\n'
            f'The target video uses a photographic live-action style.\n[Shot 1] {body}\n\n'
            'overall_soundscape:\nQuiet room tone.\n\nnon_diegetic_music:\nN/A\n')


class TextOnlySceneContractTests(unittest.TestCase):
    @staticmethod
    def policy():
        scale = constraints.extract_revision_constraints(INSTRUCTION)
        return {'version': 1, 'revision_constraints': scale,
                'scene_requirements': semantics.extract_scene_requirements(INSTRUCTION, scale)}

    @staticmethod
    def resolve(policy):
        def execute():
            pass
        tray = SimpleNamespace(asset_order=lambda slots: [(slot, 'file') for slot in slots])
        with patch.dict(execute.__globals__, {'T': tray}):
            return node.reference_contract(SimpleNamespace(execute=execute), {'slots': []},
                                           production_contract=json.dumps(policy))

    def test_text_only_literal_target_preserves_and_enforces_scene_requirements_without_a_picture_group(self):
        policy = self.policy()
        before = deepcopy(policy)
        resolved = self.resolve(policy)
        self.assertEqual(policy, before)
        self.assertEqual(resolved['groups'], [])
        self.assertEqual(resolved['constraint_target_groups'], {})
        self.assertFalse(resolved['identity_validation_required'])
        self.assertEqual(resolved['scene_requirements'], policy['scene_requirements'])
        scene_rules = {'target_first_frame_palm_support', 'target_hand_face_same_frame',
                       'target_raise_to_cheek'}
        self.assertEqual({row['rule'] for row in resolved['scene_requirements']}, scene_rules)
        findings = semantics.revision_semantic_findings(prompt(text_only=True), resolved)
        self.assertEqual({row['rule'] for row in findings}, scene_rules)
        self.assertTrue(all(row['severity'] == 'ERROR' and 'P02' in row['message'] for row in findings))
        repaired = prompt(repaired=True, text_only=True)
        self.assertEqual(semantics.revision_semantic_findings(repaired, resolved), [])

    def test_scene_requirement_cannot_name_a_foreign_target_absent_from_revision_constraints(self):
        policy = self.policy()
        policy['scene_requirements'][0]['target_key'] = 'P99'
        with self.assertRaisesRegex(ValueError, 'Unsupported caller scene requirement contract'):
            self.resolve(policy)


class NativeRevisionSemanticGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        # Other profile suites may already have installed scoped wrappers. Start
        # from native functions and restore exactly the prior objects afterward.
        for name in ('_written_context', 'validate'):
            native = inspect.unwrap(getattr(compiler, name))
            replacement = patch.object(compiler, name, native)
            replacement.start()
            self.addCleanup(replacement.stop)
        token = profile._ACTIVE.set(None)
        self.addCleanup(profile._ACTIVE.reset, token)
        self.brief = Brief(intent=INSTRUCTION, seconds=124 / 24, shots=1, silent=True)
        self.plan = SimpleNamespace(manifest=[], subjects=[], loras=[], task_types=[],
                                    target=SimpleNamespace(effective_seconds=124 / 24),
                                    label_counts=lambda: {'Picture': 2, 'Video': 0, 'Audio': 0})
        self.scope = {'directory': self.directory, 'reference_contract': contract(),
                      'compiler_documents': 0, 'reference_corrections': 0, 'binding_projections': 0}

    def activate(self, scope=None):
        token = profile._ACTIVE.set(self.scope if scope is None else scope)
        self.addCleanup(profile._ACTIVE.reset, token)

    def native_context(self):
        return compiler._written_context(self.brief, Mode.REF2VA, self.plan, {}, 400, None)

    def relabelled_product_document(self):
        self.plan = Plan(mode=Mode.REF2VA, target=Target(124 / 24, 124, (672, 384)),
            manifest=[ManifestEntry(i, f'<Picture {i}>', AssetKind.IMAGE, str(i) * 64,
                                    f'ref_image_{i}', Role.SUBJECT) for i in (1, 2)],
            subjects=[SubjectPlan('<Subject 1>', 'object', ['<Picture 1>'], 'close-up of embroidered face'),
                      SubjectPlan('<Subject 2>', 'object', ['<Picture 2>'], 'plush Christmas tree'),
                      SubjectPlan('<Subject 3>', 'object', ['<Picture 2>'], 'gift box')],
            speakers=[], shots=[], task_types=[])
        approved = {'<Picture 1>': ['close-up of embroidered face'],
                    '<Picture 2>': ['plush Christmas tree']}
        self.scope['reference_contract']['groups'][0].update(
            native_sources=['<Picture 1>', '<Picture 2>'], motion_native=[])
        self.scope['product_identity_verification'] = {
            'status': 'PASS', 'approved_product_descriptors_by_native': approved}
        written = prompt(repaired=True).replace(
            '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.',
            '<Subject 1> is the plush Christmas tree from <Picture 1> and <Picture 2>.\n'
            '<Subject 2> is the gift box behind the plush Christmas tree in <Picture 2>.').replace(
            '\n\noverall_soundscape:',
            ' <Subject 2> is 30cm wide and 50cm tall. <Subject 2> sits on the carpet.\n\noverall_soundscape:')
        return IRDocument(ir_version='fixture', profile='fixture', mode=Mode.REF2VA,
                          prompt=written, plan=self.plan, sections={}, source='written')

    @staticmethod
    def custom(findings):
        return [finding for finding in findings if finding.rule in CUSTOM_RULES]

    def test_installer_pins_the_actual_native_verification_and_two_round_correction_path(self):
        self.assertEqual(profile.source_hash(compiler._written_context), profile.WRITTEN_CONTEXT_SHA256)
        self.assertEqual(profile.source_hash(compiler.validate), profile.WRITTEN_VALIDATE_SHA256)
        self.assertEqual(profile.source_hash(compiler.compile_brief), profile.COMPILE_BRIEF_SHA256)
        self.assertEqual(compiler.MAX_FIX_ROUNDS, 2)
        profile._install_revision_validation()
        installed = (compiler._written_context, compiler.validate)
        profile._install_revision_validation()
        self.assertEqual((compiler._written_context, compiler.validate), installed)

    def test_installer_rejects_changes_to_each_pinned_native_entry(self):
        def changed_native_entry(*args, **kwargs):
            return None
        for name in ('_written_context', 'validate', 'compile_brief'):
            with self.subTest(entry=name), patch.object(compiler, name, changed_native_entry):
                with self.assertRaisesRegex(profile.LocalProfileFailure, 'verification/correction path changed'):
                    profile._install_revision_validation()

    def test_non_active_validation_is_an_exact_native_passthrough(self):
        native_validate = compiler.validate
        profile._install_revision_validation()
        context = self.native_context()
        self.assertEqual(compiler.validate(prompt(), context), native_validate(prompt(), context))
        self.assertEqual(self.custom(compiler.validate(prompt(), context)), [])
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_equal_draft_context_is_not_a_registered_written_candidate(self):
        profile._install_revision_validation()
        self.activate()
        written = self.native_context()
        draft = Context(**vars(written))
        self.assertIsInstance(draft, Context)
        self.assertEqual(draft, written)
        self.assertIsNot(draft, written)
        self.assertEqual(self.custom(compiler.validate(prompt(), draft)), [])
        self.assertNotIn('revision_validation_passes', self.scope)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_actual_written_context_adds_native_errors_and_accepts_corrected_prose(self):
        native_validate = compiler.validate
        profile._install_revision_validation()
        self.activate()
        context = self.native_context()
        found = compiler.validate(prompt(), context)
        custom = self.custom(found)
        self.assertEqual({finding.rule for finding in custom}, CUSTOM_RULES)
        self.assertTrue(all(isinstance(finding, Finding) and finding.severity == 'ERROR' for finding in custom))
        self.assertEqual([finding for finding in found if finding.rule not in CUSTOM_RULES],
                         native_validate(prompt(), context))
        corrected = compiler.validate(prompt(repaired=True), self.native_context())
        self.assertEqual(self.custom(corrected), [])
        self.assertEqual(self.scope['revision_validation_passes'], 2)
        blocked = json.loads((self.directory / 'revision-verify-001.json').read_text(encoding='utf-8'))
        passed = json.loads((self.directory / 'revision-verify-002.json').read_text(encoding='utf-8'))
        self.assertEqual((blocked['status'], passed['status']), ('BLOCKED', 'PASS'))
        self.assertEqual(blocked['stage'], 'native_written_verification')
        self.assertFalse(blocked['rewrites_creative_text'])
        self.assertNotEqual(blocked['prompt_sha256'], passed['prompt_sha256'])
        self.assertEqual((self.directory / 'revision-verify-001.prompt.txt').read_text(encoding='utf-8'), prompt())

    def test_actual_plan_and_identity_proof_keep_gift_box_from_the_same_picture_outside_product_checks(self):
        self.plan.subjects = [
            SubjectPlan('<Subject 1>', 'object', ['<Picture 1>', '<Picture 2>'], 'the green plush'),
            SubjectPlan('<Subject 2>', 'object', ['<Picture 2>'], 'the gift box')]
        self.scope['reference_contract']['groups'][0]['native_sources'] = ['<Picture 1>', '<Picture 2>']
        self.scope['product_identity_verification'] = {
            'status': 'PASS', 'approved_product_descriptors_by_native': {
                '<Picture 1>': ['the green plush'], '<Picture 2>': ['the green plush']}}
        original_contract = deepcopy(self.scope['reference_contract'])
        definition = '<Subject 1> is the green plush from <Picture 1> and <Picture 2>.'
        good = prompt(repaired=True).replace(definition, definition +
            '\n<Subject 2> is the gift box from <Picture 2>.').replace('\n\noverall_soundscape:',
            ' <Subject 2> is 30cm wide and 50cm tall. <Subject 2> sits on the carpet.\n\noverall_soundscape:')
        bad = good.replace('The height of <Subject 1> is 10cm and its width is 8cm.',
                           'The height of <Subject 1> is 12.5cm and its width is 15cm.')
        profile._install_revision_validation()
        self.activate()
        context = self.native_context()
        self.assertTrue(any(item is context and plan is self.plan
                            for item, plan in self.scope['written_plans']))
        self.assertEqual(self.custom(compiler.validate(good, context)), [])
        found = self.custom(compiler.validate(bad, self.native_context()))
        self.assertEqual({finding.rule for finding in found}, {'revision_target_scale_conflict'})
        self.assertTrue(all(isinstance(finding, Finding) and finding.severity == 'ERROR' for finding in found))
        self.assertTrue(all('<Subject 1>' in finding.msg and '<Subject 2>' not in finding.msg for finding in found))
        self.assertTrue(any('12.5cm' in finding.msg and '15cm' in finding.msg for finding in found))
        for index, status in ((1, 'PASS'), (2, 'BLOCKED')):
            record = json.loads((self.directory / f'revision-verify-{index:03d}.json').read_text(encoding='utf-8'))
            self.assertEqual(record['verified_target_subjects'], {'P02': ['<Subject 1>']})
            self.assertEqual(record['status'], status)
            self.assertEqual(record['stage'], 'native_written_verification')

        calls = []
        def serializer(doc, brief):
            calls.append(doc.prompt)
            return {'prompt': doc.prompt, 'degraded': False, 'fallback_reason': None}
        namespace = {'_as_the_service_answers': serializer}
        profile._install_document_gate(namespace)
        doc = IRDocument(ir_version='fixture', profile='fixture', mode=Mode.REF2VA,
                         prompt=good, plan=self.plan, sections={}, source='written')
        # Binding/projection are separate checks; keep final semantic admission
        # and its use of the actual document plan under test here.
        with patch.object(profile, '_audit_or_project', side_effect=lambda doc, *_: doc), \
             patch.object(profile, '_apply_revision_constraints', side_effect=lambda doc, *_: doc):
            self.assertEqual(namespace['_as_the_service_answers'](doc, self.brief)['prompt'], good)
            doc.prompt = bad
            with self.assertRaises(profile.LocalProfileBlocked):
                namespace['_as_the_service_answers'](doc, self.brief)
        self.assertEqual(calls, [good])
        for index, status in ((1, 'PASS'), (2, 'BLOCKED')):
            final = json.loads((self.directory / f'compiler-{index:03d}/revision-semantics.final.json').read_text(encoding='utf-8'))
            self.assertEqual(final['verified_target_subjects'], {'P02': ['<Subject 1>']})
            self.assertEqual(final['status'], status)
            self.assertEqual(final['stage'], 'final_serialization')
        self.assertEqual(self.scope['reference_contract'], original_contract)

    def test_writer_relabelled_product_uses_written_identity_in_native_and_final_semantic_receipts(self):
        doc = self.relabelled_product_document()
        before_plan = deepcopy(doc.plan)
        profile._install_revision_validation()
        self.activate()
        self.assertEqual(self.custom(compiler.validate(doc.prompt, self.native_context())), [])
        record = json.loads((self.directory / 'revision-verify-001.json').read_text(encoding='utf-8'))
        self.assertEqual(record['verified_target_subjects'], {'P02': ['<Subject 1>']})
        self.assertEqual(record['status'], 'PASS')
        final_folder = self.directory / 'final-written-identity'
        final_folder.mkdir()
        self.assertEqual(profile._revision_semantic_findings(
            doc.prompt, self.scope, plan=doc.plan, final_folder=final_folder), [])
        final = json.loads((final_folder / 'revision-semantics.final.json').read_text(encoding='utf-8'))
        self.assertEqual(final['verified_target_subjects'], {'P02': ['<Subject 1>']})
        self.assertEqual(final['status'], 'PASS')
        self.assertEqual(final['stage'], 'final_serialization')
        bad = doc.prompt.replace('The height of <Subject 1> is 10cm and its width is 8cm.',
                                 'The height of <Subject 1> is 12.5cm and its width is 15cm.')
        findings = self.custom(compiler.validate(bad, self.native_context()))
        self.assertEqual({finding.rule for finding in findings}, {'revision_target_scale_conflict'})
        self.assertTrue(all('<Subject 1>' in finding.msg and '<Subject 2>' not in finding.msg for finding in findings))
        self.assertEqual(doc.plan, before_plan)

    def test_binding_projection_anchors_the_written_product_despite_remapped_native_plan_labels(self):
        doc = self.relabelled_product_document()
        doc.prompt = doc.prompt.replace(
            '<Subject 1> is the plush Christmas tree from <Picture 1> and <Picture 2>.',
            '<Subject 1> is the plush Christmas tree from <Picture 1>.')
        before = deepcopy(doc)
        binding_contract = {**self.scope['reference_contract'], 'verified_product_descriptors_by_native':
                            self.scope['product_identity_verification']['approved_product_descriptors_by_native']}
        projected, proof = profile.project_declared_bindings(doc, binding_contract, self.brief)
        self.assertEqual(proof['alias_map'], {})
        self.assertEqual(proof['group_assignments'][0]['canonical_subject'], '<Subject 1>')
        self.assertEqual(proof['group_assignments'][0]['existing_group_subjects'], ['<Subject 1>'])
        self.assertIn('<Subject 2> is the gift box behind the plush Christmas tree in <Picture 2>.', projected)
        self.assertTrue(all(profile.projection_invariants(projected, proof).values()))
        projected_doc = deepcopy(doc)
        projected_doc.prompt = projected
        binding = profile.reference_binding(projected_doc, binding_contract)
        self.assertEqual(binding['status'], 'PASS')
        self.assertEqual(binding['group_results'][0]['subject'], '<Subject 1>')
        self.assertEqual(doc, before)
        self.assertEqual(projected_doc.plan, before.plan)

    def test_context_from_a_previous_scope_does_not_receive_another_callers_findings(self):
        profile._install_revision_validation()
        self.activate()
        old = self.native_context()
        other = dict(self.scope)
        other.pop('written_contexts')
        self.activate(other)
        self.assertEqual(self.custom(compiler.validate(prompt(), old)), [])
        self.assertNotIn('revision_validation_passes', other)

    def test_final_serializer_blocks_unfixed_prose_and_records_explicit_receipt(self):
        calls = []
        def serializer(doc, brief):
            calls.append(doc.prompt)
            return {'prompt': doc.prompt, 'degraded': False, 'fallback_reason': None}
        namespace = {'_as_the_service_answers': serializer}
        profile._install_document_gate(namespace)
        self.activate()
        doc = IRDocument(ir_version='fixture', profile='fixture', mode=Mode.REF2VA,
                         prompt=prompt(), plan=self.plan, sections={}, source='written')
        # Isolate this last admission step after binding/constraint work. The
        # semantic checker and serializer gate are real; no text is rewritten.
        with patch.object(profile, '_audit_or_project', side_effect=lambda doc, *_: doc), \
             patch.object(profile, '_apply_revision_constraints', side_effect=lambda doc, *_: doc):
            with self.assertRaisesRegex(profile.LocalProfileBlocked, 'before H3 loading'):
                namespace['_as_the_service_answers'](doc, self.brief)
            self.assertEqual(calls, [])
            folder = self.directory / 'compiler-001'
            final = json.loads((folder / 'revision-semantics.final.json').read_text(encoding='utf-8'))
            gate = json.loads((folder / 'source-gate.json').read_text(encoding='utf-8'))
            self.assertEqual(final['status'], 'BLOCKED')
            self.assertEqual(final['stage'], 'final_serialization')
            self.assertEqual({finding['rule'] for finding in final['findings']}, CUSTOM_RULES)
            self.assertEqual(gate['status'], 'BLOCKED')
            self.assertEqual(gate['error_type'], 'LocalProfileBlocked')
            self.assertFalse((folder / 'body.json').exists())
            doc.prompt = prompt(repaired=True)
            self.assertEqual(namespace['_as_the_service_answers'](doc, self.brief)['prompt'], doc.prompt)
            self.assertEqual(calls, [doc.prompt])
            passed = json.loads((self.directory / 'compiler-002/revision-semantics.final.json').read_text(encoding='utf-8'))
            self.assertEqual(passed['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
