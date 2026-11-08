"""Caller-authored physical-scale constraints shared with the offline compiler.

This module intentionally imports only the standard library. The node package
ships an identical copy: no web framework, database or model is required here.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re


VERSION = 1
_NUMBER = r'(?:\d+(?:\.\d+)?)'
_UNIT = r'(?:centimet(?:er|re)s?|millimet(?:er|re)s?|meters?|metres?|厘米|公分|毫米|cm|mm|米|m)'
_AXES = {'高': 'height', '高度': 'height', 'height': 'height', '宽': 'width', '宽度': 'width',
         'width': 'width', '长': 'length', '长度': 'length', 'length': 'length',
         '深': 'depth', '深度': 'depth', 'depth': 'depth'}
_MEASURE = re.compile(r'(?P<axis>高度|宽度|长度|深度|height|width|length|depth|高|宽|长|深)'
                      r'\s*(?:约为|约|只有|仅为|为|是|[:：=]|is|of|about)?\s*'
                      r'(?P<value>' + _NUMBER + r')\s*(?P<unit>' + _UNIT + r')(?![A-Za-z])', re.I)
_PAIR = re.compile(r'(?P<a>' + _NUMBER + r')\s*(?P<unit_a>' + _UNIT + r')?\s*[×xX*]\s*'
                   r'(?P<b>' + _NUMBER + r')\s*(?P<unit_b>' + _UNIT + r')', re.I)
_CODE = re.compile(r'(?<![A-Za-z0-9_])P\d{1,6}(?![A-Za-z0-9_])', re.I)
_DEPTH = re.compile(r'(?P<verb>凹陷|压下|压入|压缩|compress(?:es|ed)?(?:\s+by)?|indent(?:s|ed)?(?:\s+by)?)'
                    r'\s*(?P<value>' + _NUMBER + r')\s*(?P<unit>' + _UNIT + r')', re.I)
_RELATIVE = (
    (re.compile(r'(?:两个|两只|2\s*(?:个|只)?)\s*手掌(?:的)?(?:大小|尺寸|大)|双掌(?:大小|尺寸)|'
                r'size\s+of\s+(?:two|2)\s+(?:human\s+)?palms?|'
                r'(?:two|2)[ -](?:human[ -])?palm[ -]sized|(?:two|2)\s+(?:human\s+)?palms?\s+in\s+size', re.I), 'two_palms'),
    (re.compile(r'(?:一个|一只|1\s*(?:个|只)?)\s*手掌(?:的)?(?:大小|尺寸|大)|单掌(?:大小|尺寸)|'
                r'size\s+of\s+(?:one|a|single|1)\s+(?:human\s+)?palm|'
                r'(?:one|single|1)[ -](?:human[ -])?palm[ -]sized|(?<![A-Za-z0-9-])palm[ -]sized|'
                r'(?:one|a|single|1)\s+(?:human\s+)?palm\s+in\s+size', re.I), 'one_palm'),
    (re.compile(r'(?:一个|一只|普通|标准)?\s*水杯(?:的)?(?:大小|尺寸|大)|'
                r'(?:the\s+)?size\s+of\s+(?:a\s+)?(?:drinking\s+)?(?:cup|mug)|'
                r'(?:drinking\s+)?(?:cup|mug)[ -]sized', re.I), 'drinking_cup'),
)
_RELATIVE_ENGLISH = {'two_palms': 'the size of two human palms', 'one_palm': 'the size of one human palm',
                     'drinking_cup': 'the size of a drinking cup'}


def _affirmative(segment, match):
    prefix = re.split(r'[,，;；。]', segment[:match.start()])[-1]
    return not re.search(r'不(?!变)|禁止|小于|大于|至多|至少|[<>≤≥]|'
                         r'\b(?:not|avoid|under|over|less than|greater than|at most|at least|up to)\b', prefix, re.I)


def _product_measurement(segment, match):
    prefix = re.split(r'[,，;；。]', segment[:match.start()])[-1]
    other = list(re.finditer(r'桌面|桌子|展台|台面|柜子|房间|壁炉|背景|身高|机位|镜头|相机|'
                            r'\b(?:table|shelf|room|fireplace|platform|camera|person|actor)\b', prefix, re.I))
    if not other:
        return True
    # "P02 is on a table whose height is 100cm" describes the table, not P02.
    # A later explicit product-size phrase can restore the target relationship.
    return bool(re.search(r'产品尺寸|商品尺寸|玩具尺寸|product\s+(?:size|dimensions)|toy\s+size',
                          prefix[other[-1].end():], re.I))


def _unit(unit):
    unit = unit.lower()
    if unit in ('厘米', '公分', 'cm') or unit.startswith('centimet'):
        return 'cm'
    if unit in ('毫米', 'mm') or unit.startswith('millimet'):
        return 'mm'
    return 'm'


def _number(value):
    return format(float(value), 'g')


def _target_rows(targets):
    return [dict(row) for row in targets if isinstance(row, dict) and row.get('kind', 'prop') == 'prop']


def _aliases(row):
    return [str(row[k]) for k in ('logical_id', 'id', 'name') if row.get(k)]


def _alias_pattern(alias):
    if re.fullmatch(r'[A-Za-z0-9_:-]+', alias):
        return r'(?<![A-Za-z0-9_])' + re.escape(alias) + r'(?![A-Za-z0-9_])'
    return re.escape(alias)


def _segments(text, targets):
    # Speech numbers are content, not physical-scale directions. Preserve the
    # original instruction elsewhere; only mask them during extraction.
    text = re.sub(r'(?:台词|对白|朗读|字幕|dialogue|narration|say)\s*(?:改为|改成|为|是|[:：])?\s*'
                  r'(?:“[^”]*”|"[^"]*"|‘[^’]*’|\x27[^\x27]*\x27)', ' ', text, flags=re.I)
    aliases = sorted({a for row in targets for a in _aliases(row)}, key=len, reverse=True)
    boundary = '|'.join([_alias_pattern(a) for a in aliases] + [_CODE.pattern])
    for sentence in re.split(r'[\n。！？;；!?]+', text):
        if not sentence.strip() or re.match(r'^\s*(?:对白|台词|字幕|dialogue|narration)\s*[:：]', sentence, re.I):
            continue
        mentions = list(re.finditer(boundary, sentence, re.I))
        if not mentions:
            yield sentence, None
            continue
        # Repeated target names in one clause do not split an axis pair.
        active = mentions[0]
        for match in mentions[1:]:
            if match[0].casefold() == active[0].casefold():
                continue
            yield sentence[active.start():match.start()], active[0]
            active = match
        yield sentence[active.start():], active[0]


def extract_revision_constraints(instruction=None, *, prompt_override=None, targets=()):
    """Extract explicit object dimensions/scale, never arbitrary prompt numbers.

    The latest instruction overrides the editable prompt for the same target
    and axis. Unknown/ambiguous targets remain explicit and must be resolved
    uniquely by the provider; this function never assigns them to all objects.
    """
    targets = _target_rows(targets)
    items = {}
    for source, text in (('prompt_override', prompt_override), ('instruction', instruction)):
        if not isinstance(text, str) or not text.strip():
            continue
        active_key = None
        for segment, mention in _segments(text, targets):
            measurements = [{'axis': _AXES[match['axis'].lower()], 'value': _number(match['value']),
                             'unit': _unit(match['unit'])} for match in _MEASURE.finditer(segment)
                            if _affirmative(segment, match) and _product_measurement(segment, match)]
            pair = _PAIR.search(segment)
            if pair and _affirmative(segment, pair) and not measurements and (mention or re.search(r'尺寸|大小|规格|size|dimension', segment, re.I)):
                measurements = [{'axis': axis, 'value': _number(pair[key]),
                                 'unit': _unit(pair['unit_a'] or pair['unit_b'])}
                                for axis, key in (('height', 'a'), ('width', 'b'))]
            relatives = [(match.start(), value) for pattern, value in _RELATIVE
                         for match in pattern.finditer(segment) if _affirmative(segment, match)]
            relative = max(relatives)[1] if relatives else None
            if not measurements and not relative:
                if mention:
                    current = next((row for row in targets if mention in _aliases(row)), None)
                    owner = (current.get('logical_id') or current.get('id') or current.get('name')) if current else mention
                    active_key = owner if owner in items else None
                depth = _DEPTH.search(segment)
                if depth and not mention and active_key in items:
                    items[active_key]['deformation_depth'] = {'value': _number(depth['value']), 'unit': _unit(depth['unit'])}
                    items[active_key]['deformation_source_text'] = segment.strip()
                continue
            matching = [row for row in targets if mention and any(mention.casefold() == a.casefold() for a in _aliases(row))]
            target = matching[0] if len(matching) == 1 else None
            if (not mention and len(targets) == 1 and (
                    re.search(r'产品|商品|道具|玩具|尺寸|大小|product|prop|toy|size|dimension', segment, re.I)
                    or not re.search(r'镜头|机位|人物|身高|相机|摄像机|camera|lens|person|actor', segment, re.I))):
                target = targets[0]
            key = (target.get('logical_id') or target.get('id') or target.get('name')) if target else mention
            if not key:
                if not re.search(r'产品|商品|道具|玩具|product|prop|toy|object', segment, re.I):
                    continue
                # No subject identity was stated. Preserve it as unresolved,
                # rather than silently applying a dimension to every subject.
                key = '__unresolved_product__'
            active_key = key
            item = items.setdefault(key, {'target_key': key, 'kind': 'physical_scale', 'measurements': []})
            supersedes_prompt = source == 'instruction' and item.get('source') == 'prompt_override'
            by_axis = {} if relative and supersedes_prompt else {entry['axis']: entry for entry in item['measurements']}
            if supersedes_prompt:
                item.pop('deformation_depth', None)
            by_axis.update({entry['axis']: entry for entry in measurements})
            item.update(source=source, source_text=segment.strip(), measurements=list(by_axis.values()))
            if relative:
                item['relative_scale'] = relative
            elif measurements and source == 'instruction':
                item.pop('relative_scale', None)  # Old prompt scale must not contradict new dimensions.
            depth = _DEPTH.search(segment)
            if depth:
                item['deformation_depth'] = {'value': _number(depth['value']), 'unit': _unit(depth['unit'])}
            if target:
                item['target_name'] = target.get('name') or key
                if target.get('id'):
                    item['target_entity_id'] = target['id']
                if target.get('product_id'):
                    item['product_id'] = target['product_id']
    return {'version': VERSION, 'items': list(items.values())}


def _description(item, subject):
    clauses = [f"the physical {entry['axis']} of {subject} is {entry['value']} {entry['unit']}"
               for entry in item.get('measurements', [])]
    if item.get('relative_scale'):
        clauses.append(f"{subject} is {_RELATIVE_ENGLISH[item['relative_scale']]}")
    if item.get('deformation_depth'):
        depth = item['deformation_depth']
        clauses.append(f"the pressing action indents {subject} by {depth['value']} {depth['unit']}")
    return '; '.join(clauses) + '.'


def revision_constraints_text(constraints):
    if not constraints.get('items'):
        return ''
    return 'Current caller-authored product scale (overrides older size descriptions):\n' + '\n'.join(
        _description(item, str(item['target_key'])) for item in constraints['items'])


def _overlay_dimensions(before, item):
    if item.get('relative_scale') or not isinstance(before, str):
        return _description(item, item['target_key'])
    values = {entry['axis']: entry for entry in item.get('measurements', [])}
    seen = set()
    def replace(match):
        axis = _AXES[match['axis'].lower()]
        if axis not in values:
            return match[0]
        seen.add(axis)
        entry = values[axis]
        return match['axis'] + ' ' + entry['value'] + entry['unit']
    updated = _MEASURE.sub(replace, before)
    if not seen and _PAIR.search(before):
        # An unlabeled pair uses the same explicit height×width convention as
        # extraction. Preserve the axis the caller did not change.
        pair = _PAIR.search(before)
        previous = [{'axis':axis,'value':_number(pair[key]),'unit':_unit(pair['unit_a'] or pair['unit_b'])}
                    for axis,key in (('height','a'),('width','b'))]
        merged = {entry['axis']:entry for entry in previous}
        merged.update(values)
        return _description({**item,'measurements':list(merged.values())}, item['target_key'])
    missing = [entry for axis,entry in values.items() if axis not in seen]
    if missing:
        updated += '; ' + _description({**item,'measurements':missing}, item['target_key'])
    return updated


def apply_revision_overrides(action, entity_specs, constraints):
    """Change only generation copies; retain overwritten facts in the audit."""
    rows = copy.deepcopy(entity_specs)
    action = str(action or '')
    audit = {'entity_fields': [], 'action_before': action, 'action_changed': False}
    for item in constraints.get('items', []):
        key = item['target_key']
        for row in rows:
            matches_id = bool(item.get('target_entity_id') and item['target_entity_id'] == row.get('id'))
            if key not in _aliases(row) and not matches_id:
                continue
            spec = row.get('spec')
            if not isinstance(spec, dict):
                continue
            for field in ('dimensions', 'size', '尺寸', '规格'):
                if field in spec:
                    audit['entity_fields'].append({'target_key': key, 'entity_id': row.get('id'),
                                                  'field': field, 'before': spec[field]})
                    spec[field] = _overlay_dimensions(spec[field], item)
            if item.get('relative_scale'):
                for axis in ('height','width','length','depth'):
                    for field in (axis,axis+'_cm',axis+'_mm',axis+'_m'):
                        if field in spec:
                            audit['entity_fields'].append({'target_key':key,'entity_id':row.get('id'),
                                                          'field':field,'before':spec.pop(field)})
                spec['physical_scale_override'] = _description(item, key)
            for entry in item.get('measurements', []):
                for field in (entry['axis'], entry['axis'] + '_cm', entry['axis'] + '_mm'):
                    if field in spec:
                        audit['entity_fields'].append({'target_key': key, 'entity_id': row.get('id'),
                                                      'field': field, 'before': spec[field]})
                        if field.endswith(('_cm','_mm')):
                            meters = float(entry['value']) * {'m':1,'cm':.01,'mm':.001}[entry['unit']]
                            spec[field] = meters * (1000 if field.endswith('_mm') else 100)
                        else:
                            spec[field] = entry['value'] + ' ' + entry['unit']
        # Remove an obsolete pressing-depth number only in a clause uniquely
        # assigned to the resized product. Other objects' actions stay intact.
        pieces = []
        for segment, mention in _segments(action, _target_rows(entity_specs)):
            target = next((r for r in entity_specs if mention in _aliases(r)), None)
            matching = mention == key or bool(target and item.get('target_entity_id') and target.get('id') == item['target_entity_id'])
            if matching:
                dimensions = {entry['axis']:entry for entry in item.get('measurements', [])}
                def replace_dimension(match):
                    axis = _AXES[match['axis'].lower()]
                    if item.get('relative_scale'):
                        return ''
                    if axis not in dimensions:
                        return match[0]
                    entry = dimensions[axis]
                    return match['axis'] + ' ' + entry['value'] + entry['unit']
                segment = _MEASURE.sub(replace_dimension, segment)
                depth = item.get('deformation_depth')
                segment = _DEPTH.sub(lambda m: m['verb'] + (' ' + depth['value'] + depth['unit'] if depth else ''), segment)
            pieces.append(segment)
        # Preserve punctuation and all untouched bytes. Replacing known spans
        # avoids rebuilding the original action from sentence fragments.
        for before, after in zip(_segments(action, _target_rows(entity_specs)), pieces):
            if before[0] != after:
                action = action.replace(before[0], after, 1)
    audit['action_changed'] = action != audit['action_before']
    return action, rows, audit


def enforce_revision_constraints(prompt, constraints, subject_map):
    """Insert exact caller constraints into the first real numbered shot.

    A separate receipt identifies these clauses as caller-authored, not model
    output. Missing or ambiguous subject bindings fail before H3 sampling.
    """
    items = constraints.get('items', [])
    if constraints.get('version') != VERSION:
        raise ValueError('Unsupported revision constraint version')
    if not items:
        return prompt, {'version': VERSION, 'source': 'caller_revision_constraints', 'clauses': []}
    values = [subject_map.get(item['target_key']) for item in items]
    if any(not isinstance(value, str) or not re.fullmatch(r'<Subject\s+\d+>', value) for value in values):
        raise ValueError('Each revision scale target requires one explicit Subject binding')
    if len(set(values)) != len(values):
        raise ValueError('Independent revision scale targets cannot share one Subject')
    match = re.search(r'(?m)^detailed_description:\s*\n', prompt)
    if not match:
        raise ValueError('Revision constraints require the detailed_description section')
    for value in values:
        if len(re.findall(r'(?m)^\s*' + re.escape(value) + r'\s+is\b', prompt[:match.start()])) != 1:
            raise ValueError('Revision scale target must have one actual written Subject definition')
    tail = prompt[match.end():]
    end = re.search(r'(?m)^(?:overall_soundscape|non_diegetic_music):\s*$', tail)
    detail = tail[:end.start()] if end else tail
    shot = re.search(r'\[Shot\s+1\]', detail)
    if not shot:
        raise ValueError('Revision constraints require an actual first numbered shot')
    clauses = [_description(item, subject_map[item['target_key']]) for item in items]
    insertion = ' Current caller-authored product scale: ' + ' '.join(clauses) + ' '
    marker_end = match.end() + shot.end()
    if prompt[marker_end:].startswith(' Current caller-authored product scale:') and not prompt[marker_end:].startswith(insertion):
        raise ValueError('A different caller scale overlay already exists; compile a fresh revision document')
    updated = prompt if prompt[marker_end:].startswith(insertion) else prompt[:marker_end] + insertion + prompt[marker_end:]
    return updated, {'version': VERSION, 'source': 'caller_revision_constraints',
                     'clauses': [{'target_key': item['target_key'], 'subject': subject_map[item['target_key']],
                                  'text': clause, 'source': item['source'], 'source_text': item['source_text']}
                                 for item, clause in zip(items, clauses)],
                     'before_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                     'after_sha256': hashlib.sha256(updated.encode()).hexdigest()}
