"""Scoped local compatibility wrapper around the actual registered upstream Main."""
from copy import deepcopy
import inspect
import json
import re
from .revision_semantics import CALLER_CONTRACT_POLICY


# Audited Main calls compile_here before render_fields and every H3 loader.
# Refuse source drift before execute, rather than relying on seven outputs.
MAIN_EXECUTE_SHA256='b43758b3d0e89aec5ecb9b0d21028688ff15bcefea9ddf55a12d1c33e9498953'


def upstream_main():
    import nodes
    node=nodes.NODE_CLASS_MAPPINGS.get('OpenH3IRCompile')
    if node is None:raise RuntimeError('The actual OpenH3IRCompile node is not registered')
    return node


def reference_contract(upstream,media,groups='',required='',motion='',production_contract=''):
    """Resolve explicit human declarations through the official native ordering.

    A group is the caller's statement, never face recognition or a conclusion
    drawn from similar visual descriptors. No group is guessed for a video.
    """
    slots=[entry['slot'] for entry in (media or {}).get('slots',[])]
    execute=getattr(upstream.execute,'__func__',upstream.execute)
    tray=execute.__globals__['T']
    aliases={};counts={'Picture':0,'Video':0,'Audio':0}
    for slot,part in tray.asset_order(slots):
        kind={'picture':'Picture','video':'Video','sound':'Audio'}[slot.kind] if part=='file' else 'Audio'
        counts[kind]+=1
        if part=='file':aliases[slot.label]=f'<{kind} {counts[kind]}>'
    def labels(text):
        values=[item.strip() for item in text.split(',') if item.strip()]
        if len(set(values))!=len(values):raise ValueError('A reference declaration repeats a label')
        unknown=set(values)-aliases.keys()
        if unknown:raise ValueError('Reference contract names absent media: '+', '.join(sorted(unknown)))
        return values
    declared={};used=set()
    for raw in groups.splitlines():
        if not raw.strip():continue
        match=re.fullmatch(r'\s*(character|product)\s+([^:]+?)\s*:\s*(.+?)\s*',raw)
        if not match:raise ValueError('Use one group per line: character person-name: front, side (or product sku-name: front, side)')
        category,name,source=match.groups();members=labels(source)
        if not members or any(not aliases[x].startswith('<Picture ') for x in members):
            raise ValueError('Identity appearance groups require declared picture labels')
        key=(category,name)
        if key in declared or used.intersection(members):raise ValueError('A picture cannot belong to conflicting identity groups')
        used.update(members)
        declared[key]={'category':category,'group':name,'appearance_labels':members,'motion_labels':[]}
    for raw in motion.splitlines():
        if not raw.strip():continue
        match=re.fullmatch(r'\s*([^:]+?)\s*:\s*(.+?)\s*',raw)
        if not match or ('character',match[1]) not in declared:
            raise ValueError('A motion declaration must name an explicit character group: person-name: clip-label')
        members=labels(match[2])
        if any(not aliases[x].startswith('<Video ') for x in members) or used.intersection(members):
            raise ValueError('Motion sources must be distinct declared videos')
        used.update(members);declared[('character',match[1])]['motion_labels']+=members
    required_labels=labels(required)
    result={'version':3,'declaration_source':'explicit caller groups and must-use labels; no visual identity inference',
            'alias_to_native':aliases,'required_labels':required_labels,
            'required_native':[aliases[x] for x in required_labels],'groups':list(declared.values()),
            'raw_declarations':{'reference_groups':groups,'required_references':required,'motion_targets':motion}}
    for group in result['groups']:
        group['appearance_native']=[aliases[x] for x in group['appearance_labels']]
        group['motion_native']=[aliases[x] for x in group['motion_labels']]
        group['native_sources']=group['appearance_native']+group['motion_native']
    policy=json.loads(production_contract) if production_contract else {}
    if not isinstance(policy,dict) or (policy and policy.get('version')!=1):
        raise ValueError('Unsupported production constraint contract')
    product_groups={g['group']:g for g in result['groups'] if g['category']=='product'}
    descriptions=policy.get('product_groups',{})
    if not isinstance(descriptions,dict) or set(descriptions)-product_groups.keys():
        raise ValueError('Product identity policy names an absent product group')
    for name,description in descriptions.items():
        if not isinstance(description,dict) or not isinstance(description.get('description',''),str):
            raise ValueError('Product identity policy requires a frozen text description')
        product_groups[name]['target_description']=description.get('description','')
    targets=policy.get('constraint_target_groups',{})
    if (not isinstance(targets,dict) or any(not isinstance(k,str) or v not in product_groups for k,v in targets.items())):
        raise ValueError('Revision constraint target must name a declared product group')
    result.update(identity_validation_required=bool(product_groups),
                  revision_constraints=policy.get('revision_constraints') or {},
                  constraint_target_groups=targets,
                  scene_requirements=policy.get('scene_requirements') or [])
    requirements=result['scene_requirements']
    constraint_keys={item.get('target_key') for item in result['revision_constraints'].get('items',[])
                     if isinstance(item,dict) and isinstance(item.get('target_key'),str)}
    if (not isinstance(requirements,list) or any(not isinstance(row,dict)
            or row.get('target_key') not in constraint_keys or not isinstance(row.get('source_text'),str)
            or row.get('rule') not in {'target_first_frame_palm_support','target_hand_face_same_frame','target_raise_to_cheek'}
            for row in requirements)):
        raise ValueError('Unsupported caller scene requirement contract')
    return result


class VideoFactoryLocalH3IRCompile:
    @classmethod
    def INPUT_TYPES(cls):
        inputs=deepcopy(upstream_main().INPUT_TYPES())
        extra=inputs.setdefault('optional',{})
        for name,tip in (
            ('reference_groups','Optional declared identity groups, one per line: character person-name: front, side or product sku-name: front, side. Use media tray labels; no identity is inferred.'),
            ('required_references','Optional must-use media tray labels separated by commas. The compiled definitions must actually cite them.'),
            ('motion_targets','Optional explicit motion source for a character group, one per line: person-name: clip-label. This is content reference, with no pure-motion isolation guarantee.'),
            ('production_contract','Frozen product identity and caller revision constraints as JSON. Validated before H3 loading.')):
            extra[name]=('STRING',{'default':'','multiline':True,'advanced':True,'tooltip':tip})
        extra['caller_contract_policy']=('STRING',{'default':CALLER_CONTRACT_POLICY,'advanced':True,
            'tooltip':'Required project caller-contract verification policy; cannot disable validation.'})
        return inputs

    RETURN_TYPES=('MODEL','CONDITIONING','LATENT','VAE','VAE','STRING','STRING')
    RETURN_NAMES=('model','positive','latent','vae','audio_vae','prompt','report')
    FUNCTION='compile'
    CATEGORY='VideoFactory/local H3'
    DESCRIPTION='Calls the real official Main with scoped local 4B decoding, declared-reference compilation and image sizing; all gates precede H3 weights.'

    @classmethod
    def IS_CHANGED(cls,**kwargs):return float('nan')

    def compile(self,reference_groups='',required_references='',motion_targets='',production_contract='',
                caller_contract_policy=CALLER_CONTRACT_POLICY,**kwargs):
        import folder_paths
        from pathlib import Path
        from .guard import validate_setup
        from .local_h3ir_backend_profile import profile_scope, source_hash, write_json
        if caller_contract_policy!=CALLER_CONTRACT_POLICY:
            raise ValueError('Unsupported caller contract policy; update the project nodes before H3 loading')
        validate_setup(kwargs.get('setup'))
        upstream=upstream_main()
        if tuple(upstream.RETURN_TYPES)!=self.RETURN_TYPES or inspect.iscoroutinefunction(upstream.execute):
            raise RuntimeError('The actual upstream Main contract changed; no substitute result was generated')
        if source_hash(upstream.execute)!=MAIN_EXECUTE_SHA256:
            raise RuntimeError('The audited upstream Main preload order changed; no H3 model was loaded')
        execute=getattr(upstream.execute,'__func__',upstream.execute)
        compiler_function=execute.__globals__.get('compile_here')
        if not callable(compiler_function):
            raise RuntimeError('The actual Main no longer exposes its audited local compiler')
        contract=reference_contract(upstream,kwargs.get('media'),reference_groups,required_references,motion_targets,production_contract)
        with profile_scope(Path(folder_paths.get_output_directory())/'h3_local_backend_profile_v3',
                           kwargs['setup'],kwargs.get('seed',20261104),
                           compiler_function=compiler_function,main_class=upstream,
                           reference_contract=contract,sizing=kwargs.get('sizing','max')) as receipt:
            write_json(receipt.parent/'main-contract.json',{
                'execute_sha256':MAIN_EXECUTE_SHA256,
                'caller_contract_policy':caller_contract_policy,
                'input_types':upstream.INPUT_TYPES(),
                'return_types':list(upstream.RETURN_TYPES),
                'return_names':list(upstream.RETURN_NAMES),
                'execution':'actual registered Main.execute(**kwargs); all inputs forwarded unchanged',
                'source_gate':'actual IRDocument.source=written before H3 model/clip/VAE loaders'})
            result=upstream.execute(**kwargs)
            if getattr(result,'expand',None) or getattr(result,'block_execution',None):
                raise RuntimeError('Unexpected upstream Main expansion/block; no substitute result was generated')
            outputs=result.result
            if not isinstance(outputs,tuple) or len(outputs)!=7:
                raise RuntimeError('The real upstream Main did not return its seven outputs')
        ui=getattr(result,'ui',None)
        if ui is not None and not isinstance(ui,dict):ui=ui.as_dict()
        return {'result':outputs,'ui':{**(ui or {}),'local_backend_profile_receipt':[str(receipt)]}}
