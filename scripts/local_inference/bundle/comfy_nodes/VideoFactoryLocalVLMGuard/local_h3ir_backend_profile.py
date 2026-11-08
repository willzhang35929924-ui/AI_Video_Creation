"""Opt-in, scoped compatibility policy for the fixed offline 4B OpenH3 compiler.

No weights, services or vendor modules are imported at module load. Wrappers are
installed once, but do nothing outside an explicit ContextVar scope. Policy
changes decoding budgets, declared-reference metadata and image sizing; caller
text/media and the original model reply remain intact in their audit records.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import asdict, is_dataclass
import datetime as dt
import difflib
import functools
import hashlib
import importlib.metadata
import inspect
import json
import os
import re
from pathlib import Path
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit
import uuid

VERSION='local-h3ir-qwen4b-v3'
POLICY_REVISION='revision-integrity-20261107-v1'
MODEL='Qwen/Qwen3-VL-4B-Instruct'
MAX_OUTPUT=4096
CARD_OUTPUT=3072
_ACTIVE=ContextVar('factory_local_h3ir_profile',default=None)
_STAGE=ContextVar('factory_local_h3ir_stage',default=None)
_CALL=ContextVar('factory_local_h3ir_call',default=None)
_INSTALL_LOCK=threading.RLock()
COMPILER_CALL_SHA256='114d98c174d3d34d020e63aacf6d2fbcc8afc28b4ab944abb794f9fc507122fa'
SERIALIZER_SHA256='e03828302d1134ca5cef228c2c0e08cad92631ef7dc182e803c2de16248dc414'
RENDER_FIELDS_SHA256='6e2900fadda2d5ca7362f01af771c4862127daba11874b5a6c536fc8c6d601c4'
CONDITION_SHA256='ad11fdeabba7c3f2a73ddc1c2821ea58e048c45dc30edf4a02fa932ef158883c'
STOCK_H3_SHA256='108ceff11e5a5c9a34a57bbc380447cf9941beb673a1bd421abc818de54ccd5c'


class LocalProfileFailure(RuntimeError):
    """Deliberately not BackendError: upstream must not hide this as draft fallback."""


class LocalProfileBlocked(LocalProfileFailure):
    """The compiler produced a document that cannot be handed to H3."""


def source_hash(function):
    return hashlib.sha256(inspect.getsource(function).encode('utf-8')).hexdigest()


def _product_subject_labels(doc,contract,sources):
    """Use model-verified object identities, never every object in a product photo."""
    verified=contract.get('verified_product_descriptors_by_native')
    if verified is None:return None  # Historical, explicitly recorded contracts.
    labels=set()
    for subject in doc.plan.subjects:
        descriptor=_normal(subject.descriptor).casefold()
        for source in set(subject.sources).intersection(sources):
            if descriptor in {_normal(value).casefold() for value in verified.get(source,[])}:
                labels.add(subject.label)
    return labels


def reference_binding(doc,contract):
    """Audit final written bindings, separately from candidate Plan sources.

    Upstream deliberately builds per-file SubjectPlan candidates, then permits
    the writer to merge them according to the request (MERGE_SUBJECTS). We do
    not claim that the candidate plan or wiring retention proves that merge.
    """
    from h3ir.compile import _split_written
    sections=_split_written(doc.prompt)
    definitions=sections.get('subject_definitions','')
    detail=sections.get('detailed_description','')
    pattern=r'(?ms)^\s*(<Subject\s+\d+>)\s+is\b(.*?)(?=^\s*<(?:Subject|Picture|Video|Audio)\s+\d+>\s*(?:is|:)|^\s*\[Shot\s+\d+\]|\Z)'
    actual={};errors=[]
    for label,text in re.findall(pattern,definitions):
        if label in actual:errors.append({'rule':'F2-duplicate-definition','message':label+' is defined more than once'})
        actual[label]=sorted(set(re.findall(r'<(?:Picture|Video|Audio)\s+\d+>',text)))
    native=sorted({m.label for m in getattr(doc.plan,'manifest',[])})
    plan_sources=[{'label':s.label,'kind':s.kind,'sources':list(s.sources),
                   'descriptor':s.descriptor} for s in getattr(doc.plan,'subjects',[])]
    candidate_sources={x for s in plan_sources for x in s['sources']}
    for finding in doc.findings:
        if finding.rule in ('L4-unused-media','R25-subject-defined-twice') or finding.severity=='ERROR':
            errors.append({'rule':finding.rule,'message':finding.msg})
    for label in contract.get('required_native',[]):
        if label not in native or label not in definitions:
            errors.append({'rule':'F3-required-reference','message':label+' must be an actual attached reference cited in subject_definitions; a wiring row or summary mention is insufficient'})
    results=[];targets={}
    for group in contract.get('groups',[]):
        wanted=set(group['native_sources'])
        eligible=_product_subject_labels(doc,contract,wanted) if group['category']=='product' else None
        matching=[label for label,refs in actual.items() if wanted.intersection(refs)
                  and (eligible is None or label in eligible)]
        target=matching[0] if len(matching)==1 else None
        complete=bool(target and wanted.issubset(actual[target]))
        cited=bool(target and target in detail)
        supported=wanted.issubset(candidate_sources) and wanted.issubset(native)
        passed=complete and cited and supported
        row={'category':group['category'],'group':group['group'],'required_sources':sorted(wanted),
             'appearance_sources':group['appearance_native'],'motion_sources':group['motion_native'],
             'subject':target,'intersecting_definitions':matching,'all_sources_in_one_definition':complete,
             'subject_cited_in_detailed_description':cited,'candidate_plan_sources_present':supported,
             'status':'PASS' if passed else 'BLOCKED'}
        if target and target in targets:
            passed=False;row['status']='BLOCKED'
            errors.append({'rule':'F4-conflicting-groups','message':target+' combines independently declared groups '+targets[target]+' and '+group['group']})
        if target:targets[target]=group['group']
        if not passed:
            errors.append({'rule':'F5-declared-group-binding','message':
                'Declared '+group['category']+' group '+group['group']+' requires exactly one Subject definition drawing on '
                +', '.join(sorted(wanted))+', that Subject cited in detailed_description, and no duplicate target definitions. '
                +'Appearance sources: '+', '.join(group['appearance_native'])+'. Explicit motion sources: '
                +(', '.join(group['motion_native']) or 'none; do not invent a motion/identity assignment')+'.'})
        results.append(row)
    return {'status':'BLOCKED' if errors else 'PASS','required_native':contract.get('required_native',[]),
            'actual_subject_definitions':actual,'group_results':results,'plan_subject_sources':plan_sources,
            'plan_sources_semantics':'per-file observed candidates; final same-subject binding is verified in written definitions',
            'native_manifest_labels':native,'errors':errors,'semantic_visual_quality':'NOT_REVIEWED'}


_DEFINITION_PATTERN=re.compile(r'(?ms)^\s*(<Subject\s+\d+>)\s+is\b(.*?)(?=^\s*<(?:Subject|Picture|Video|Audio)\s+\d+>\s*(?:is|:)|^\s*\[Shot\s+\d+\]|\Z)')


def _redirect(text,aliases):
    return re.sub(r'<Subject\s+\d+>',lambda m:aliases.get(m[0],m[0]),text)


def _normal(text):return ' '.join(text.split())


def project_declared_bindings(doc,contract,brief):
    """Compile only caller-declared relationships over existing written units.

    No subject is created, no Plan is merged, and no visual statement is
    generated. Ambiguous written anchors or source/group intersections refuse.
    Original alias descriptions survive verbatim under the canonical unit.
    """
    from h3ir.compile import _split_written
    sections=_split_written(doc.prompt)
    if set(sections)!=set(('subject_definitions','summary','retention_analysis','detailed_description','overall_soundscape','non_diegetic_music')):
        raise LocalProfileBlocked('Declared binding projection requires the actual six-section Ref2VA document')
    definitions=sections['subject_definitions'];records={};matches=list(_DEFINITION_PATTERN.finditer(definitions))
    for match in matches:
        if match[1] in records:raise LocalProfileBlocked('Duplicate written definition prevents a unique canonical anchor: '+match[1])
        records[match[1]]={'label':match[1],'body':match[2].strip(),
            'refs':set(re.findall(r'<(?:Picture|Video|Audio)\s+\d+>',match[2])),
            'start':match.start(),'end':match.end()}
    kinds={}
    for subject in doc.plan.subjects:
        if subject.label in kinds:raise LocalProfileBlocked('Plan repeats a Subject label')
        kinds[subject.label]=subject.kind
    groups=contract.get('groups',[]);source_owner={};aliases={};assignments=[];owned={}
    for group in groups:
        for source in group['native_sources']:
            if source in source_owner:raise LocalProfileBlocked('A native reference crosses declared groups: '+source)
            source_owner[source]=group['group']
    for group in groups:
        kind='person' if group['category']=='character' else 'object' if group['category']=='product' else None
        if not kind or not group['appearance_native']:raise LocalProfileBlocked('Declared group has no supported appearance anchor')
        wanted=set(group['native_sources']);first=group['appearance_native'][0]
        eligible=_product_subject_labels(doc,contract,wanted) if group['category']=='product' else None
        def qualified(label):
            return label in eligible if eligible is not None else kinds.get(label)==kind
        candidates=[label for label,item in records.items() if first in item['refs'] and qualified(label)]
        if len(candidates)!=1:
            raise LocalProfileBlocked('The first appearance source must select one existing written '+kind+' definition: '+first+'; candidates='+repr(candidates))
        canonical=candidates[0]
        members=[]
        for source in group['native_sources']:
            source_eligible=_product_subject_labels(doc,contract,{source}) if group['category']=='product' else None
            selected=[label for label,item in records.items() if source in item['refs'] and
                      (label in source_eligible if source_eligible is not None else kinds.get(label)==kind)]
            if len(selected)>1:raise LocalProfileBlocked('Declared source has several written target candidates: '+source)
            members.extend(selected)
        members=list(dict.fromkeys(members))
        for label in members:
            foreign=records[label]['refs'].intersection(source_owner)-wanted
            if foreign or label in owned:
                raise LocalProfileBlocked('A written Subject crosses declared groups: '+label)
            owned[label]=group['group']
            if label!=canonical:aliases[label]=canonical
        assignments.append({'category':group['category'],'group':group['group'],'first_appearance_source':first,
            'canonical_subject':canonical,'existing_group_subjects':members,
            'appearance_sources':list(group['appearance_native']),'motion_sources':list(group['motion_native'])})
    if any(any(label in line.text for label in aliases) for line in brief.dialogue):
        raise LocalProfileBlocked('A Subject alias occurs in locked caller dialogue; projection would change a verbatim input')
    if any(any(label in text for label in aliases) for text in brief.onscreen_text):
        raise LocalProfileBlocked('A Subject alias occurs in caller on-screen text')
    replacements={};removed=set(aliases)
    for assignment in assignments:
        canonical=assignment['canonical_subject'];body=records[canonical]['body']
        # These are metadata relationships supplied by the caller, not observed
        # attributes. Existing model bodies below are copied, never paraphrased.
        clause='Declared reference binding (caller): appearance sources '+', '.join(assignment['appearance_sources'])
        if assignment['motion_sources']:clause+='; motion content sources '+', '.join(assignment['motion_sources'])
        clause+='.'
        additions=['Additional original model description: '+records[label]['body']
                   for label in assignment['existing_group_subjects'] if label!=canonical]
        replacements[canonical]=canonical+' is '+body+' '+clause+(' '+' '.join(additions) if additions else '')+'\n'
    projected_definitions=definitions
    for match in reversed(matches):
        label=match[1]
        if label in replacements or label in removed:
            projected_definitions=projected_definitions[:match.start()]+replacements.get(label,'')+projected_definitions[match.end():]
    result={key:_redirect(value,aliases) for key,value in sections.items()}
    result['subject_definitions']=_redirect(projected_definitions,aliases)
    # Only the colliding retention entries need structural normalization. Every
    # original marker, scope and description is preserved as a source-specific
    # model note. The canonical marker is never applied to the motion note.
    retention=sections['retention_analysis'];entries=[]
    entry_pattern=re.compile(r'(?m)^\s*(<Subject\s+\d+>)([^\n]*)$')
    for match in entry_pattern.finditer(retention):
        parsed=re.fullmatch(r'\s*(\([^\n]*?\))?\s*:\s*([a-z_]+)\s*-\s*(.*)',match[2])
        if match[1] in owned and parsed is None:raise LocalProfileBlocked('An affected retention entry has an unsupported structure')
        entries.append((match,parsed))
    for assignment in assignments:
        canonical=assignment['canonical_subject'];members=set(assignment['existing_group_subjects'])
        affected=[(match,parsed) for match,parsed in entries if match[1] in members]
        if len([1 for match,_ in affected if match[1]==canonical])!=1 or len({m[1] for m,_ in affected})!=len(affected):
            raise LocalProfileBlocked('Canonical retention is missing or repeated: '+canonical)
    retention_replacements={};retention_removed=set()
    for assignment in assignments:
        canonical=assignment['canonical_subject'];members=set(assignment['existing_group_subjects'])
        affected=[(match,parsed) for match,parsed in entries if match[1] in members]
        canonical_match,canonical_data=next((m,p) for m,p in affected if m[1]==canonical)
        notes=[]
        for match,data in affected:
            if match[1]==canonical:continue
            notes.append(' Source-specific original model retention ('+data[2]+'; '+(data[1] or 'no scope')+'): '+data[3])
            retention_removed.add(match.start())
        retention_replacements[canonical_match.start()]=canonical_match[0]+''.join(notes)
    for match,_ in reversed(entries):
        if match.start() in retention_removed or match.start() in retention_replacements:
            retention=retention[:match.start()]+retention_replacements.get(match.start(),'')+retention[match.end():]
    result['retention_analysis']=_redirect(retention,aliases)
    for assignment in assignments:
        if assignment['category']!='product':continue
        tag=re.escape(assignment['canonical_subject'])
        # This checks an invalid relation introduced by alias projection, not
        # inferred visual identity or a general ban on self-directed actions.
        if re.search(tag+r'\s+(?:is\s+)?(?:being\s+)?(?:squeezed|held|carried|picked up)\s+by\s+'+tag,
                     result.get('detailed_description',''),re.I):
            raise LocalProfileBlocked('商品与操作者被合并为同一主体，已停止采样；请分开商品身份图与人物／动作参考。')
    prompt='\n\n'.join(key+':\n'+value.strip() for key,value in result.items())+'\n'
    components={'definitions':[{'label':label,'text':_redirect(item['body'],aliases)} for label,item in records.items()],
        'retention':[{'label':match[1],'scope':parsed[1] or '', 'marker':parsed[2],
                      'text':_redirect(parsed[3],aliases)} for match,parsed in entries if parsed is not None],
        'creative_sections':{key:_redirect(value,aliases) for key,value in sections.items()
                             if key not in ('subject_definitions','retention_analysis')}}
    residual=definitions
    for match in reversed(matches):residual=residual[:match.start()]+residual[match.end():]
    components['definition_residual']=_redirect(residual,aliases)
    residual=sections['retention_analysis']
    for match,_ in reversed(entries):residual=residual[:match.start()]+residual[match.end():]
    components['retention_residual']=_redirect(residual,aliases)
    evidence={'status':'PROJECTED_PENDING_VALIDATION','creative_text_source':'model_written',
        'binding_source':'declared_contract_projection','visual_facts_added':False,
        'alias_map':aliases,'group_assignments':assignments,'preserved_model_components':components,
        'plan_before_sha256':digest(asdict(doc.plan)),
        'input_prompt_sha256':hashlib.sha256(doc.prompt.encode('utf-8')).hexdigest()}
    return prompt,evidence


def projection_invariants(prompt,evidence):
    from h3ir.compile import _split_written
    sections=_split_written(prompt);components=evidence['preserved_model_components']
    checks={'creative_sections_label_redirection_only':all(_normal(sections.get(key,''))==_normal(value)
        for key,value in components['creative_sections'].items()),
        'model_definition_bodies_preserved':all(_normal(item['text']) in _normal(sections.get('subject_definitions','')) for item in components['definitions']),
        'retention_descriptions_preserved':all(_normal(item['text']) in _normal(sections.get('retention_analysis','')) for item in components['retention']),
        'retention_markers_and_scopes_preserved':all(item['marker'] in sections.get('retention_analysis','') and
            (not item['scope'] or item['scope'] in sections.get('retention_analysis','')) for item in components['retention'])}
    residual=sections.get('subject_definitions','')
    for match in reversed(list(_DEFINITION_PATTERN.finditer(residual))):residual=residual[:match.start()]+residual[match.end():]
    checks['nondefinition_residual_label_redirection_only']=_normal(residual)==_normal(components['definition_residual'])
    residual=sections.get('retention_analysis','')
    for match in reversed(list(re.finditer(r'(?m)^\s*(<Subject\s+\d+>)([^\n]*)$',residual))):
        residual=residual[:match.start()]+residual[match.end():]
    checks['retention_residual_label_redirection_only']=_normal(residual)==_normal(components['retention_residual'])
    return checks


def _audit_or_project(doc,brief,scope,folder):
    contract=scope.get('reference_contract') or {}
    if contract.get('identity_validation_required') and any(g['category']=='product' for g in contract.get('groups',[])):
        proof=scope.get('product_identity_verification') or {}
        if proof.get('status')!='PASS':
            raise LocalProfileBlocked('商品参考身份尚未通过本地实图一致性校验，未加载H3模型。')
        contract={**contract,'verified_product_descriptors_by_native':proof['approved_product_descriptors_by_native']}
    before=reference_binding(doc,contract)
    write_json(folder/'reference-binding.before.json',before)
    final=before;result=doc
    if contract.get('groups'):
        scope['binding_projections']+=1
        if scope['binding_projections']>1 or 'cards' not in scope:
            raise LocalProfileBlocked('Declared binding projection requires complete captured cards and a single projection budget')
        projection={'status':'BLOCKED','creative_text_source':'model_written',
                    'binding_source':'declared_contract_projection','visual_facts_added':False}
        try:
            prompt,projection=project_declared_bindings(doc,contract,brief)
            (folder/'projection.before.txt').write_text(doc.prompt,encoding='utf-8')
            (folder/'projection.after.txt').write_text(prompt,encoding='utf-8')
            from h3ir.compile import (_written_context,_split_written,repair_brief,token_count,validate,wiring_findings)
            from h3ir.licence import resolve_licence
            from h3ir.style import resolve_style
            from h3ir.prose import _definition_lines
            cards=scope['cards'];labels=tuple(m.label for m in doc.plan.manifest)
            licence=resolve_licence(brief,cards);style=resolve_style(brief,cards,licence)
            repaired=repair_brief(prompt,target=doc.plan.target,mode=doc.mode,labels=labels,
                dialogue=tuple(d.text for d in brief.dialogue),style_phrase=style.phrase,
                definitions=tuple(_definition_lines(doc.plan.subjects,cards,labels)),task_types=tuple(doc.plan.task_types))
            result=deepcopy(doc);result.prompt=repaired.text;result.sections=_split_written(repaired.text)
            result.prompt_tokens=token_count(repaired.text)
            result.findings=list(wiring_findings(result.plan,brief))+validate(repaired.text,
                _written_context(brief,doc.mode,doc.plan,cards,result.prompt_tokens,style,licence))
            checks=projection_invariants(result.prompt,projection)
            checks['official_repair_only_whitespace']=_normal(result.prompt)==_normal(prompt)
            checks['plan_unchanged']=digest(asdict(result.plan))==projection['plan_before_sha256']
            projection.update(status='PASS' if all(checks.values()) else 'BLOCKED',invariants=checks,
                plan_after_sha256=digest(asdict(result.plan)),repair_actions=repaired.repairs,
                output_prompt_sha256=hashlib.sha256(result.prompt.encode('utf-8')).hexdigest(),
                additional_llm_calls=0,visual_analysis_repeated=False)
            result.provenance.update(creative_text_source='model_written',binding_source='declared_contract_projection',
                                     visual_facts_added=False,factory_binding_projection=projection)
            write_json(folder/'document.projected.json',_document_evidence(result))
            (folder/'binding-projection.diff.txt').write_text(''.join(difflib.unified_diff(
                doc.prompt.splitlines(keepends=True),result.prompt.splitlines(keepends=True),
                fromfile='model_written',tofile='declared_contract_projection')),encoding='utf-8')
            if not all(checks.values()):raise LocalProfileBlocked('Declared projection or upstream repair changed protected model text or Plan')
            final=reference_binding(result,contract)
            projection['status']=final['status']
        except Exception as error:
            projection.update(status='BLOCKED',error_type=type(error).__name__,error=str(error));raise
        finally:write_json(folder/'binding-projection.json',projection)
    final.update(correction_attempts=0,binding_projections=scope['binding_projections'],
        creative_text_source='model_written',binding_source='declared_contract_projection' if scope['binding_projections'] else 'model_written',
        visual_facts_added=False)
    write_json(folder/'reference-binding.final.json',final)
    if final['status']!='PASS':
        raise LocalProfileBlocked('Reference binding BLOCKED before H3 loading; no additional model rewrite was attempted; evidence: '+str(folder))
    scope['final_reference_binding']=final
    return result


def _apply_revision_constraints(doc,brief,scope,folder):
    contract=scope.get('reference_contract') or {}
    constraints=contract.get('revision_constraints') or {}
    if not constraints.get('items'):return doc
    from .revision_constraints import enforce_revision_constraints
    from h3ir.compile import _split_written,_written_context,token_count,validate,wiring_findings
    from h3ir.licence import resolve_licence
    from h3ir.style import resolve_style
    sections=_split_written(doc.prompt)
    bindings={row['group']:row['subject'] for row in scope.get('final_reference_binding',{}).get('group_results',[])
              if row.get('status')=='PASS'}
    subjects={};receipt={'status':'BLOCKED','source':'caller_revision_constraints'}
    try:
        for item in constraints['items']:
            key=item['target_key'];group=contract.get('constraint_target_groups',{}).get(key)
            if group:
                subject=bindings.get(group)
                if not subject:raise ValueError('The revised product has no validated written Subject: '+key)
            else:
                # A text-only caller label must occur in exactly one real
                # definition. Never apply a size to every visible object.
                names=[str(key),str(item.get('target_name') or '')]
                matching=[m[1] for m in _DEFINITION_PATTERN.finditer(sections.get('subject_definitions',''))
                          if any(name and re.search(r'(?<![\w])'+re.escape(name)+r'(?![\w])',m[2],re.I) for name in names)]
                if len(matching)!=1:raise ValueError('Cannot uniquely bind revised target '+key+' to a written Subject')
                subject=matching[0]
            subjects[key]=subject
        prompt,receipt=enforce_revision_constraints(doc.prompt,constraints,subjects)
        result=deepcopy(doc);result.prompt=prompt;result.sections=_split_written(prompt);result.prompt_tokens=token_count(prompt)
        cards=scope['cards'];licence=resolve_licence(brief,cards);style=resolve_style(brief,cards,licence)
        result.findings=list(wiring_findings(result.plan,brief))+validate(prompt,
            _written_context(brief,result.mode,result.plan,cards,result.prompt_tokens,style,licence))
        errors=[{'rule':f.rule,'message':f.msg} for f in result.findings if f.severity=='ERROR']
        if errors:raise ValueError('Caller constraints failed native document validation: '+str(errors))
        # A second application must be byte-identical: retries do not accrue
        # conflicting or repeated scale clauses in the written shot body.
        if enforce_revision_constraints(prompt,constraints,subjects)[0]!=prompt:
            raise ValueError('Revision constraint application is not idempotent')
        receipt.update(status='PASS',plan_unchanged=digest(asdict(doc.plan))==digest(asdict(result.plan)),
                       visual_facts_added=False,caller_authored_constraints_added=True)
        result.provenance.update(factory_revision_constraints=receipt,caller_constraints_source='explicit_revision',
                                 creative_text_source='model_written',caller_authored_constraints_added=True)
        write_json(folder/'document.constrained.json',_document_evidence(result))
        (folder/'constraints.before.txt').write_text(doc.prompt,encoding='utf-8')
        (folder/'constraints.after.txt').write_text(prompt,encoding='utf-8')
        return result
    except Exception as error:
        receipt.update(status='BLOCKED',error=str(error))
        raise LocalProfileBlocked('本次产品尺寸约束未能唯一绑定并写入最终镜头，未加载H3模型：'+str(error)) from error
    finally:write_json(folder/'revision-constraints.json',receipt)


def _document_evidence(doc):
    return {'source':getattr(doc,'source',None),'fallback_reason':getattr(doc,'fallback_reason',None),
            'prompt':getattr(doc,'prompt',None),'provenance':getattr(doc,'provenance',None),
            'plan':asdict(doc.plan) if is_dataclass(getattr(doc,'plan',None)) else None,
            'findings':[{'rule':f.rule,'severity':f.severity,'message':f.msg}
                        for f in getattr(doc,'findings',())]}


def _install_document_gate(namespace):
    """Wrap the serializer's real namespace, never a replacement Main/output."""
    with _INSTALL_LOCK:
        original=namespace['_as_the_service_answers']
        previous=getattr(original,'_factory_source_gate_version',None)
        if previous==VERSION:return
        if previous:raise LocalProfileFailure('Another compiler source gate is installed')

        @functools.wraps(original)
        def checked_document(doc,brief):
            scope=_ACTIVE.get()
            if scope is None:return original(doc,brief)
            scope['compiler_documents']+=1
            folder=scope['directory']/('compiler-'+str(scope['compiler_documents']).zfill(3))
            folder.mkdir()
            source=getattr(doc,'source',None)
            record={'profile':VERSION,'source':source,
                    'fallback_reason':getattr(doc,'fallback_reason',None),
                    'gate':'actual IRDocument.source before Main loads H3 weights',
                    'required_source':'written','status':'BLOCKED'}
            # These are the actual compiler facts, including rejected prose and
            # rules when present; do not infer quality from HTTP or output count.
            write_json(folder/'document.json',_document_evidence(doc))
            if is_dataclass(brief):write_json(folder/'brief.json',asdict(brief))
            try:
                if source!='written' or getattr(doc,'fallback_reason',None):
                    raise LocalProfileBlocked('Compiler source gate BLOCKED '+repr(source)
                        +'; requires actual source=written without fallback; evidence: '+str(folder))
                if scope.get('reference_contract') is not None:
                    try:doc=_audit_or_project(doc,brief,scope,folder)
                    except Exception as error:
                        path=folder/'reference-binding.final.json'
                        if not path.exists():
                            baseline=json.loads((folder/'reference-binding.before.json').read_text(encoding='utf-8')) if (folder/'reference-binding.before.json').exists() else {}
                            write_json(path,{**baseline,'status':'BLOCKED',
                                'correction_attempts':scope['reference_corrections'],
                                'binding_projections':scope['binding_projections'],
                                'correction_error_type':type(error).__name__,'correction_error':str(error)})
                        raise LocalProfileBlocked('Declared reference projection BLOCKED before H3 loading: '
                            +type(error).__name__+': '+str(error)+'; evidence: '+str(folder)) from error
                doc=_apply_revision_constraints(doc,brief,scope,folder)
                body=original(doc,brief)
                write_json(folder/'body.json',body)
                if not isinstance(body,dict) or body.get('degraded') is not False or body.get('fallback_reason'):
                    raise LocalProfileBlocked('Compiler body contradicts source=written; evidence: '+str(folder))
                record.update(status='PASS',body_sha256=digest(body))
                provenance=getattr(doc,'provenance',{}) or {}
                record.update(creative_text_source='model_written',
                    binding_source=provenance.get('binding_source','model_written'),
                    visual_facts_added=False,
                    caller_authored_constraints_added=bool(provenance.get('caller_authored_constraints_added')))
                binding_path=folder/'reference-binding.final.json'
                if binding_path.exists():
                    binding=json.loads(binding_path.read_text(encoding='utf-8'))
                    binding.update(body_sha256=digest(body),
                        final_document_filename='document.projected.json' if (folder/'document.projected.json').exists() else 'document.json')
                    write_json(binding_path,binding)
                return body
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error));raise
            finally:write_json(folder/'source-gate.json',record)

        checked_document._factory_source_gate_version=VERSION
        namespace['_as_the_service_answers']=checked_document


def install_compiler_gate(compile_here):
    """Pin the audited path: compile_here serializes before Main's loaders.

    The vendor body deliberately drops source, so checking its degraded flag
    alone cannot distinguish every draft. Inspect the real Document first.
    """
    if source_hash(compile_here)!=COMPILER_CALL_SHA256:
        raise LocalProfileFailure('The installed compile_here contract changed; source gate was not guessed')
    namespace=compile_here.__globals__
    serializer=namespace.get('_as_the_service_answers')
    if not callable(serializer) or source_hash(inspect.unwrap(serializer))!=SERIALIZER_SHA256:
        raise LocalProfileFailure('The installed document serializer changed; source gate was not guessed')
    _install_document_gate(namespace)
    _install_analysis_capture()
    return {'compile_here_sha256':COMPILER_CALL_SHA256,'serializer_sha256':SERIALIZER_SHA256,
            'source_field':'actual h3ir.models.IRDocument.source',
            'required_source':'written','location':'before vendor serializer returns body to Main; before H3 loaders'}


def _install_analysis_capture():
    import h3ir.compile as compiler
    with _INSTALL_LOCK:
        original=compiler.analyse_all
        if getattr(original,'_factory_card_capture_version',None)!=VERSION:
            @functools.wraps(original)
            def captured(*args,**kwargs):
                cards=original(*args,**kwargs)
                scope=_ACTIVE.get()
                if scope is not None:
                    scope['cards']=cards
                    write_json(scope['directory']/'analysis-cards.json',
                               {key:asdict(card) for key,card in cards.items()})
                    contract=scope.get('reference_contract') or {}
                    if contract.get('identity_validation_required'):
                        from .product_reference_validation import verify_product_references,ProductReferenceConflict
                        bound=inspect.signature(original).bind(*args,**kwargs)
                        try:
                            scope['product_identity_verification']=verify_product_references(
                                bound.arguments['backend'],bound.arguments['refs'],cards,contract,
                                scope['directory'].parent/'product-identity-cache',scope['seed'],
                                receipt_root=scope['directory'])
                        except ProductReferenceConflict as error:
                            raise LocalProfileBlocked(str(error)) from error
                return cards
            captured._factory_card_capture_version=VERSION
            compiler.analyse_all=captured


def install_render_policy(main_class):
    execute=getattr(main_class.execute,'__func__',main_class.execute)
    namespace=execute.__globals__;original=namespace['render_fields']
    if source_hash(inspect.unwrap(original))!=RENDER_FIELDS_SHA256 or source_hash(main_class._condition)!=CONDITION_SHA256:
        raise LocalProfileFailure('The audited image sizing/conditioning contract changed')
    stock=Path(inspect.getfile(main_class)).resolve().parents[2]/'comfy_extras/nodes_minimax_h3.py'
    if not stock.is_file() or hashlib.sha256(stock.read_bytes()).hexdigest()!=STOCK_H3_SHA256:
        raise LocalProfileFailure('The audited stock H3 image/video sizing implementation changed')
    with _INSTALL_LOCK:
        if getattr(original,'_factory_image_sizing_version',None)==VERSION:return
        @functools.wraps(original)
        def render_fields(body):
            scope=_ACTIVE.get()
            if scope is None:return original(body)
            record={'profile':VERSION,'status':'BLOCKED','body_sha256':digest(body),
                    'requested_image_sizing':scope['sizing'],
                    'render_fields_source_sha256':RENDER_FIELDS_SHA256,
                    'execute_source_sha256':source_hash(main_class.execute),
                    'condition_source_sha256':CONDITION_SHA256,'stock_h3_file_sha256':STOCK_H3_SHA256,
                    'call_site':'actual Main.execute -> scoped render_fields -> actual Main._condition(ref_image_size=effective_tuple[4])',
                    'video_sizing_modified':False,'wiring_modified':False,'memory_stability':'NOT_VERIFIED'}
            try:
                fields=original(body)
                record['original_tuple']=list(fields)
                images=[w for w in body.get('wiring',[]) if w.get('kind')=='image' and str(w.get('wiring','')).startswith('ref_image_')]
                videos=[w for w in body.get('wiring',[]) if w.get('kind')=='video']
                record.update(image_wiring=images,video_wiring=videos)
                sizings={w.get('sizing') for w in images}
                if images and (len(sizings)!=1 or next(iter(sizings))!=scope['sizing']):
                    raise LocalProfileBlocked('Image wiring disagrees with the declared image sizing before H3 loading')
                effective=fields[:4]+(scope['sizing'] if images else fields[4],)
                record.update(status='PASS',effective_tuple=list(effective),
                              sizing_rule='image wiring only; stock video encoding independently uses adapt_canvas')
                return effective
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error))
                if isinstance(error,LocalProfileBlocked):
                    raise LocalProfileBlocked('Image sizing BLOCKED before H3 loading: '+str(error)
                        +'; evidence: '+str(scope['directory'])) from error
                raise
            finally:write_json(scope['directory']/'render-fields.json',record)
        render_fields._factory_image_sizing_version=VERSION
        namespace['render_fields']=render_fields


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(encoded(value)).hexdigest()


def write_json(path,value):
    Path(path).write_bytes(json.dumps(value,ensure_ascii=False,indent=2).encode('utf-8')+b'\n')


def fixed_endpoint(address):
    try:
        p=urlsplit(str(address))
        return (p.scheme=='http' and p.hostname in ('127.0.0.1','localhost') and p.port==8772
                and p.path.rstrip('/')=='/v1' and not any((p.username,p.password,p.query,p.fragment)))
    except ValueError:
        return False


def require_environment():
    expected={'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','H3IR_GUIDED_DECODING':'1',
              'H3IR_LLM_THINKING':'0','H3IR_LLM_MAX_TOKENS':'4096'}
    wrong=[key for key,value in expected.items() if os.environ.get(key)!=value]
    if wrong:
        raise LocalProfileFailure('Local 4B profile requires explicit offline/compiler environment: '+', '.join(wrong))
    return expected


def bounded_card_schema(schema):
    """Preserve vocabulary/required keys; bound only previously unbounded prose."""
    result=deepcopy(schema)
    changes=[]
    array_limits={'subjects':8,'attributes':8,'pose':6,'visible_text':8,'palette':8}
    string_limits={'summary':512,'descriptor':256,'attributes':160,'pose':160,
                   'environment':512,'lighting':256,'framing':256,'style':256,
                   'visible_text':128,'palette':64,'camera':256}
    def walk(node,path=(),field=''):
        if not isinstance(node,dict):return
        if node.get('type')=='string' and 'enum' not in node and 'const' not in node and 'maxLength' not in node:
            node['maxLength']=string_limits.get(field,512)
            changes.append({'path':'/'.join(path),'added_maxLength':node['maxLength']})
        if node.get('type')=='array' and 'maxItems' not in node:
            node['maxItems']=array_limits.get(field,8)
            changes.append({'path':'/'.join(path),'added_maxItems':node['maxItems']})
        for key,child in node.get('properties',{}).items():walk(child,path+('properties',key),key)
        if isinstance(node.get('items'),dict):walk(node['items'],path+('items',),field)
        for key in ('anyOf','oneOf','allOf'):
            for i,child in enumerate(node.get(key,[])):walk(child,path+(key,str(i)),field)
        for key in ('$defs','definitions'):
            for name,child in node.get(key,{}).items():walk(child,path+(key,name),name)
    walk(result)
    return result,changes


def concise_card_messages(messages):
    result=deepcopy(messages)
    instruction=('Local 4B analysis budget: return concise complete factual phrases. '
        'Use at most 8 reusable subjects, 8 short attributes and 6 short pose observations per subject; '
        'at most 8 clearly legible text entries, without repeating text. '
        'Do not fill the budget with guessed objects, words, materials or identity claims. '
        'Summarize observed evidence within the supplied field lengths; do not copy this instruction or a schema. '
        'Unknown or unobservable details should be stated as uncertain, without inventing an answer.')
    for message in reversed(result):
        if message.get('role')=='user':
            content=message.get('content')
            if isinstance(content,list):message['content']=content+[{'type':'text','text':instruction}]
            elif isinstance(content,str):message['content']=content+'\n\n'+instruction
            else:raise LocalProfileFailure('Unsupported user message; no input was coerced or truncated')
            return result
    raise LocalProfileFailure('Analysis call has no user message; no replacement request was invented')


def active_for(backend):
    scope=_ACTIVE.get()
    cfg=getattr(backend,'cfg',None)
    if scope is None or cfg is None:return None
    if not fixed_endpoint(getattr(cfg,'base_url','')) or getattr(cfg,'model',None)!=MODEL:
        raise LocalProfileFailure('An active local compiler scope refused a different endpoint or model')
    if getattr(cfg,'api_key','not-needed') not in ('','not-needed',None):
        raise LocalProfileFailure('This fixed offline profile does not accept or persist API credentials')
    return scope


class RecordingClient:
    def __init__(self,client,folder,record):
        self.client,self.folder,self.record=client,folder,record

    def __getattr__(self,name):return getattr(self.client,name)

    def post(self,*args,**kwargs):
        try:
            response=self.client.post(*args,**kwargs)
        except Exception as error:
            request=getattr(error,'request',None)
            if request is not None:
                try:self.save_request(request.content)
                except Exception:pass
            raise
        self.record['http_status']=response.status_code
        raw=response.content
        (self.folder/'response.raw.bin').write_bytes(raw)
        self.record['response_sha256']=hashlib.sha256(raw).hexdigest()
        self.record['response_bytes']=len(raw)
        request=getattr(response,'request',None)
        if request is not None:self.save_request(request.content)
        try:
            self.record['_response']=response.json()
            write_json(self.folder/'response.json',self.record['_response'])
        except ValueError:pass
        return response

    def save_request(self,raw):
        (self.folder/'request.wire.json').write_bytes(raw)
        self.record['wire_request_sha256']=hashlib.sha256(raw).hexdigest()


def install(backend_class=None):
    """Install inert wrappers once; only profile_scope can activate them."""
    if backend_class is None:
        if importlib.metadata.version('open-h3-ir')!='0.4.1':
            raise LocalProfileFailure('This compatibility profile is verified only against open-h3-ir 0.4.1')
        from h3ir.backend import Backend
        backend_class=Backend
    with _INSTALL_LOCK:
        previous=getattr(backend_class,'_factory_local_profile_version',None)
        if previous==VERSION:return
        if previous:raise LocalProfileFailure('Another local compiler profile is already installed: '+str(previous))
        original_json=backend_class.json_call
        original_chat=backend_class.chat
        original_once=backend_class._once
        original_http=backend_class._http

        @functools.wraps(original_http)
        def http(self):
            if active_for(self) is None:return original_http(self)
            if self._client is None:
                import httpx
                self._client=httpx.Client(timeout=self.cfg.timeout_s,trust_env=False,follow_redirects=False)
            return self._client

        @functools.wraps(original_json)
        def json_call(self,messages,schema,**kwargs):
            scope=active_for(self)
            if scope is None:return original_json(self,messages,schema,**kwargs)
            is_card=schema.get('title') in ('ImageCard','VideoCard')
            effective,changes=bounded_card_schema(schema) if is_card else (deepcopy(schema),[])
            requested={'max_tokens':kwargs.get('max_tokens') or self.cfg.max_tokens,
                       'temperature':kwargs.get('temperature',0.0),'thinking':kwargs.get('thinking'),
                       'seed':kwargs.get('seed'),'retries':kwargs.get('retries',2),**kwargs}
            stage={'kind':'asset_card' if is_card else 'structured',
                   'original_schema':deepcopy(schema),'effective_schema':effective,'schema_changes':changes,
                   'original_messages':deepcopy(messages),'requested_parameters':deepcopy(requested)}
            token=_STAGE.set(stage)
            effective_kwargs={**kwargs,'retries':0,'temperature':0.2,'thinking':False,'seed':scope['seed']}
            effective_kwargs['max_tokens']=min(kwargs.get('max_tokens') or self.cfg.max_tokens,CARD_OUTPUT if is_card else MAX_OUTPUT)
            try:
                value=original_json(self,concise_card_messages(messages) if is_card else messages,effective,**effective_kwargs)
                # The fixed local service executes jsonschema post-validation;
                # _once verifies its effective-schema SHA and receipt. Do not
                # add a missing second dependency to the Comfy environment.
                return value
            except LocalProfileFailure:raise
            except Exception as error:
                raise LocalProfileFailure('Local compiler structured stage failed without retry/fallback: '
                    +type(error).__name__+': '+str(error)+'; profile receipts: '+str(scope['directory'])) from error
            finally:_STAGE.reset(token)

        @functools.wraps(original_chat)
        def chat(self,messages,**kwargs):
            scope=active_for(self)
            if scope is None:return original_chat(self,messages,**kwargs)
            stage=_STAGE.get()
            limit=CARD_OUTPUT if stage and stage['kind']=='asset_card' else MAX_OUTPUT
            effective={**kwargs,'max_tokens':min(kwargs.get('max_tokens') or self.cfg.max_tokens,limit),
                       'temperature':0.2,'thinking':False,'seed':scope['seed'],'retries':0}
            requested={'max_tokens':kwargs.get('max_tokens') or self.cfg.max_tokens,
                       'temperature':kwargs.get('temperature',0.7),'thinking':kwargs.get('thinking'),
                       'seed':kwargs.get('seed'),'retries':kwargs.get('retries',2),**kwargs}
            call={'stage':deepcopy(stage),'original_messages':deepcopy(stage['original_messages'] if stage else messages),
                  'requested_parameters':deepcopy(requested),'effective_parameters':deepcopy(effective)}
            token=_CALL.set(call)
            try:return original_chat(self,messages,**effective)
            except LocalProfileFailure:raise
            except Exception as error:
                raise LocalProfileFailure('Local compiler chat failed without retry/fallback: '
                    +type(error).__name__+': '+str(error)+'; profile receipts: '+str(scope['directory'])) from error
            finally:_CALL.reset(token)

        @functools.wraps(original_once)
        def once(self,body):
            scope=active_for(self)
            if scope is None:return original_once(self,body)
            scope['requests']+=1
            folder=scope['directory']/('request-'+str(scope['requests']).zfill(3))
            folder.mkdir()
            call=_CALL.get() or {}
            stage=_STAGE.get()
            limit=CARD_OUTPUT if stage and stage['kind']=='asset_card' else MAX_OUTPUT
            effective=deepcopy(body)
            effective.update(max_tokens=min(body.get('max_tokens') or self.cfg.max_tokens,limit),
                             temperature=0.2,seed=scope['seed'],chat_template_kwargs={'enable_thinking':False})
            requested=deepcopy(body)
            requested['messages']=deepcopy(call.get('original_messages',body.get('messages')))
            original_parameters=(stage or {}).get('requested_parameters',call.get('requested_parameters',{}))
            for key in ('max_tokens','temperature','seed'):
                if key in original_parameters:requested[key]=original_parameters[key]
            requested_thinking=original_parameters.get('thinking')
            if requested_thinking is None:requested_thinking=self.cfg.default_thinking
            if requested_thinking:requested.pop('chat_template_kwargs',None)
            if stage and 'response_format' in requested:
                requested['response_format']['json_schema']['schema']=deepcopy(stage['original_schema'])
            write_json(folder/'request.original.json',requested)
            write_json(folder/'request.effective.json',effective)
            if stage:
                write_json(folder/'schema.original.json',stage['original_schema'])
                write_json(folder/'schema.effective.json',stage['effective_schema'])
            record={'version':VERSION,'stage':(stage or {}).get('kind','prose'),
                    'original_request_sha256':digest(requested),'effective_request_sha256':digest(effective),
                    'requested_parameters':original_parameters,'effective_parameters':call.get('effective_parameters',{}),
                    'schema_changes':(stage or {}).get('schema_changes',[]),'automatic_retries':0,
                    'schema_validation_location':'fixed offline VLM service; verified receipt and schema SHA' if stage else None,
                    'input_text_or_media_truncated':False,'status':'FAIL'}
            if stage:record.update(original_schema_sha256=digest(stage['original_schema']),effective_schema_sha256=digest(stage['effective_schema']))
            started=time.monotonic()
            real_client=self._http()
            self._client=RecordingClient(real_client,folder,record)
            try:
                reply=original_once(self,effective)
                if reply.finish_reason!='stop':
                    raise LocalProfileFailure('Local compiler reply did not finish normally: '+str(reply.finish_reason))
                response=record.get('_response')
                if not isinstance(response,dict):raise LocalProfileFailure('No actual local HTTP response was captured')
                receipt=response.get('factory_receipt',{})
                if (receipt.get('offline') is not True or receipt.get('model_id')!=MODEL
                        or receipt.get('gpu_cleanup',{}).get('released') is not True):
                    raise LocalProfileFailure('Actual reply did not prove fixed offline model and confirmed GPU release')
                if stage:
                    control=receipt.get('response_format_control',{})
                    if (control.get('guided_decoding') is not True or control.get('post_validation') is not True
                            or control.get('schema_sha256')!=digest(stage['effective_schema'])):
                        raise LocalProfileFailure('Actual reply did not prove enforcement of the effective schema')
                    # Upstream extracts JSON substrings. This profile requires
                    # the entire real reply to be one complete JSON object;
                    # schema validation remains at the fixed VLM service.
                    def invalid_constant(value):
                        raise ValueError('Non-JSON numeric constant: '+value)
                    parsed=json.loads(reply.content,parse_constant=invalid_constant)
                    if not isinstance(parsed,dict):
                        raise LocalProfileFailure('Structured reply must be one complete JSON object')
                    record.update(complete_json_parse=True,parsed_json_sha256=digest(parsed))
                record.update(status='PASS',finish_reason=reply.finish_reason,
                              usage=response.get('usage'),factory_receipt=receipt)
                return reply
            except Exception as error:
                record.update(error_type=type(error).__name__,error=str(error))
                raise LocalProfileFailure('Local compiler request failed without retry/draft fallback: '
                    +type(error).__name__+': '+str(error)+'; evidence: '+str(folder)) from error
            finally:
                self._client=real_client
                record.pop('_response',None)
                record['elapsed_seconds']=round(time.monotonic()-started,3)
                write_json(folder/'receipt.json',record)

        backend_class.json_call=json_call
        backend_class.chat=chat
        backend_class._once=once
        backend_class._http=http
        backend_class._factory_local_profile_version=VERSION


@contextmanager
def profile_scope(receipt_root,setup,seed=20261104,backend_class=None,compiler_function=None,
                  main_class=None,reference_contract=None,sizing='max'):
    if (not isinstance(setup,dict) or setup.get('server') or setup.get('llm_model')!=MODEL
            or not fixed_endpoint(setup.get('llm_url',''))):
        raise LocalProfileFailure('This explicit compiler scope only supports the fixed local 4B Setup')
    if type(seed) is not int or not 0<=seed<2**63:
        raise LocalProfileFailure('The fixed local VLM requires the real integer workflow seed in 0..2^63-1')
    environment=require_environment()
    if urlsplit(setup['llm_url']).hostname=='localhost':
        resolved=socket.getaddrinfo('localhost',8772,type=socket.SOCK_STREAM)
        if not resolved or not all(ipaddress.ip_address(entry[4][0]).is_loopback for entry in resolved):
            raise LocalProfileFailure('localhost must resolve exclusively to loopback addresses')
    install(backend_class)
    compiler_contract=install_compiler_gate(compiler_function) if compiler_function else None
    if sizing not in ('max','match'):raise LocalProfileFailure('Unsupported declared image sizing')
    if main_class is not None:install_render_policy(main_class)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%fZ')
    directory=Path(receipt_root)/(stamp+'-'+uuid.uuid4().hex[:8]);directory.mkdir(parents=True)
    scope={'directory':directory,'seed':seed,'requests':0,'compiler_documents':0,
           'reference_corrections':0,'binding_projections':0,'reference_contract':reference_contract,'setup':deepcopy(setup),'sizing':sizing}
    receipt={'profile':VERSION,'policy_revision':POLICY_REVISION,'model':MODEL,'base_url':setup['llm_url'],'environment':environment,
             'max_output_tokens':MAX_OUTPUT,'card_max_output_tokens':CARD_OUTPUT,'seed':seed,
             'temperature':0.2,'retries':0,'status':'FAIL','semantic_status':'NOT_REVIEWED',
             'source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
             'scope':'explicit project wrapper Main only; all user/media inputs preserved'}
    receipt['compiler_source_gate']=compiler_contract
    receipt.update(card_cache_policy='reuse unchanged vendor model/analyzer/schema cache keys and existing STATE_DIR',max_reference_corrections=0,
                   max_binding_projections=1,
                   reference_contract_required=reference_contract is not None,
                   requested_image_sizing=sizing)
    if reference_contract is not None:write_json(directory/'reference-contract.json',reference_contract)
    write_json(directory/'profile.json',receipt)
    token=_ACTIVE.set(scope)
    try:
        yield directory/'profile.json'
        if compiler_function and scope['compiler_documents']!=1:
            raise LocalProfileBlocked('Main did not produce exactly one audited compiler document')
        receipt['status']='PROFILE_SCOPE_COMPLETE'
    except Exception as error:
        receipt.update(status='BLOCKED' if isinstance(error,LocalProfileBlocked) else 'FAIL',
                       error_type=type(error).__name__,error=str(error));raise
    finally:
        _ACTIVE.reset(token)
        receipt['requests']=scope['requests']
        receipt['compiler_documents']=scope['compiler_documents']
        receipt['reference_corrections']=scope['reference_corrections']
        receipt['binding_projections']=scope['binding_projections']
        write_json(directory/'profile.json',receipt)
