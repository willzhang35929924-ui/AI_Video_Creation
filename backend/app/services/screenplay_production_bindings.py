"""Connect generated production prose to the existing versioned design contract.

Only unambiguous references are linked. New records contain the model's existing
name/description verbatim; this adapter does not create appearance, actions or
continuity states. Historical/unselected shots and declared designs stay intact.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _labels(record: dict) -> set[str]:
    spec = record.get('spec') if isinstance(record.get('spec'), dict) else {}
    return {value.strip() for value in (record.get('name'), record.get('description'),
                                       spec.get('description'), spec.get('name'))
            if _text(value)}


def bind_production_designs(ir: dict, namespace: str,
                            shot_keys: set[str] | None = None) -> dict:
    """Return a deep copy with selected production design references resolved.

    Existing explicit keys must exist and have the expected kind/owner. Without
    keys, a character's sole wardrobe or a unique exact text match is usable.
    Otherwise clear source prose may establish a new draft design, while an
    ambiguous choice fails visibly. ``shot_keys`` protects partial revisions.
    """
    if not isinstance(namespace, str) or not namespace:
        raise ValueError('制作设定绑定缺少稳定任务命名空间')
    result = copy.deepcopy(ir)
    designs = result.setdefault('designs', [])
    people = {person['key'] for person in result.get('characters', [])}
    shot_ids = {shot['key'] for shot in result.get('shots', [])}
    if shot_keys is not None and set(shot_keys) - shot_ids:
        raise ValueError('制作设定绑定包含不存在的分镜编号')
    by_key = {}
    for design in designs:
        key = design['key']
        if key in by_key:
            raise ValueError('制作设定编号重复，无法唯一绑定：' + key)
        if design.get('character_key') and design['character_key'] not in people:
            raise ValueError('制作设定引用未定义人物：' + key)
        by_key[key] = design

    def applicable(design: dict, shot: dict) -> bool:
        return design.get('scene_key') in {None, shot.get('scene_key')}

    def explicit(key: Any, kind: str, owner: str | None = None,
                 scene_key: str | None = None) -> dict:
        if not isinstance(key, str) or not key.strip() or key not in by_key:
            raise ValueError('明确引用的制作设定不存在：' + str(key))
        design = by_key[key]
        if design.get('kind') != kind:
            raise ValueError('制作设定类型与分镜引用不符：' + key)
        if owner and design.get('character_key') not in {None, owner}:
            label = '服装造型' if kind == 'wardrobe' else '道具设定'
            raise ValueError(label + '属于另一人物，未自动替换：' + key)
        if design.get('scene_key') not in {None, scene_key}:
            raise ValueError('制作设定属于另一场景，未自动替换：' + key)
        return design

    def create(kind: str, owner: str | None, state: dict, shot: dict) -> dict:
        description = _text(state.get('description'))
        name = _text(state.get('name')) or description
        if name is None:
            raise ValueError(('服装' if kind == 'wardrobe' else '道具') + '缺少明确名称或描述，无法建立制作设定')
        scene_key = shot.get('scene_key')
        material = json.dumps([namespace, kind, owner, scene_key, name.strip()],
                              ensure_ascii=False, separators=(',', ':'))
        key = 'generated_' + kind + '_' + hashlib.sha256(material.encode()).hexdigest()[:24]
        if key in by_key:
            # A generated key may be reused across shots, never repurposed.
            design = explicit(key, kind, owner, scene_key)
            if design['name'].strip() != name.strip():
                raise ValueError('稳定制作设定编号发生冲突：' + key)
            return design
        # A model's filenames/source_ids are not evidence of human review or
        # provenance. This record is solely a structural derivative of this
        # generation lineage's existing wording.
        source = ['generation:' + namespace]
        design = {'key': key, 'kind': kind, 'name': name,
                  'character_key': owner, 'scene_key': scene_key,
                  'spec': {'description': description or name},
                  'source': copy.deepcopy(source),
                  'provenance': {'description': {
                      'state': 'suggested', 'source_ids': copy.deepcopy(source),
                      'confidence': .5,
                      'reason': '仅从模型已有名称或描述建立待审核制作记录；未新增状态、外观或动作，内容未经人工批准',
                  }}}
        if len(designs) >= 1000:
            raise ValueError('制作设定数量超过结构化剧本上限')
        designs.append(design)
        by_key[key] = design
        return design

    for shot in result.get('shots', []):
        if shot_keys is not None and shot['key'] not in shot_keys:
            continue
        production = shot.get('production') or {}
        participants = {person['character_key'] for person in shot.get('participants', [])}
        scene_state_key = production.get('scene_state_key')
        if scene_state_key is not None:
            explicit(scene_state_key, 'scene_state', scene_key=shot.get('scene_key'))
        for owner, state in production.get('wardrobe_states', {}).items():
            if owner not in people or owner not in participants:
                raise ValueError('服装造型引用未在本镜出场的人物：' + owner)
            if state.get('wardrobe_key') is not None:
                explicit(state['wardrobe_key'], 'wardrobe', owner, shot.get('scene_key'))
                continue
            owned = [design for design in designs if design.get('kind') == 'wardrobe'
                     and design.get('character_key') == owner and applicable(design, shot)]
            labels = _labels(state)
            matches = [design for design in designs if design.get('kind') == 'wardrobe'
                       and design.get('character_key') in {None, owner}
                       and applicable(design, shot)
                       and labels & _labels(design)]
            if len(owned) == 1:
                chosen = owned[0]
            elif len(matches) == 1:
                chosen = matches[0]
            elif len(owned) > 1 or len(matches) > 1:
                raise ValueError('人物服装有多个制作设定，无法唯一对应；请填写wardrobe_key：' + owner)
            else:
                chosen = create('wardrobe', owner, state, shot)
            state['wardrobe_key'] = chosen['key']
        for state in production.get('prop_states', []):
            owner = state.get('character_key')
            if owner is not None and (owner not in people or owner not in participants):
                raise ValueError('道具设定引用未在本镜出场的人物：' + str(owner))
            if state.get('prop_key') is not None:
                explicit(state['prop_key'], 'prop', owner, shot.get('scene_key'))
                continue
            labels = _labels(state)
            matches = [design for design in designs if design.get('kind') == 'prop'
                       and (owner is None or design.get('character_key') in {None, owner})
                       and applicable(design, shot)
                       and labels & _labels(design)]
            if len(matches) > 1:
                raise ValueError('道具名称或描述对应多个制作设定，请填写prop_key')
            chosen = matches[0] if matches else create('prop', owner, state, shot)
            state['prop_key'] = chosen['key']
    return result
