"""Conservative checks of caller requirements in a written native brief.

Only explicit product bindings and a small set of recognized statements are
checked. This module neither rewrites prose nor evaluates generated frames.
It uses only the standard library and is shared byte-for-byte with the node.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re


CALLER_CONTRACT_POLICY = 'revision_semantics_v1'

_TAG = re.compile(r'<(Subject|Picture)\s*(\d+)>', re.I)
_DEFINITION = re.compile(r'^\s*(<(?:Subject|Picture)\s*\d+>)\s*(?:is\b|shows?\b|:)', re.I | re.M)
_HEADING = re.compile(r'^\s*(subject_definitions|summary|retention_analysis|detailed_description|overall_soundscape|non_diegetic_music):\s*$', re.I | re.M)
_NUMBER = r'\d+(?:\.\d+)?'
_UNIT = r'(?:centimet(?:er|re)s?|millimet(?:er|re)s?|met(?:er|re)s?|厘米|公分|毫米|cm|mm|米|m)'
_AXIS = r'(?:height|width|length|depth|高度|宽度|长度|深度|高|宽|长|深)'
_AXIS_FIRST = re.compile(
    r'(?<![A-Za-z])(?P<axis>' + _AXIS + r')\s*'
    r'(?:of\s+(?P<owner><(?:Subject|Picture)\s*\d+>)\s*)?'
    r'(?:(?:is|equals?|measures?|about|approximately|约为|约|为|是|[:：=])\s*)?'
    r'(?P<value>' + _NUMBER + r')\s*(?P<unit>' + _UNIT + r')(?![A-Za-z])', re.I)
_VALUE_FIRST = re.compile(
    r'(?P<value>' + _NUMBER + r')\s*(?P<unit>' + _UNIT + r')\s*'
    r'(?P<axis>wide|tall|high|long|deep|in\s+(?:height|width|length|depth))(?![A-Za-z])', re.I)
_AXES = {'高': 'height', '高度': 'height', 'tall': 'height', 'high': 'height',
         '宽': 'width', '宽度': 'width', 'wide': 'width', '长': 'length',
         '长度': 'length', 'long': 'length', '深': 'depth', '深度': 'depth', 'deep': 'depth'}
_FRAME = re.compile(r'第一帧|首帧|开场画面|初始画面|\bfirst\s+frame\b|\bopening\s+frame\b|\binitial\s+frame\b|\bat\s+(?:the\s+)?start\b', re.I)
_PALM = re.compile(
    r'(?:掌心|手掌).{0,15}(?:托|承托|捧|支撑)|(?:托住|承托|捧住|托着).{0,15}(?:掌心|手掌)|'
    r'\b(?:support(?:ed|s|ing)?|held|hold(?:s|ing)?|cradl(?:ed|es|ing)|rests?|resting)\b.{0,65}\bpalm\b|'
    r'\bpalm\b.{0,40}\b(?:support(?:s|ing)?|hold(?:s|ing)?|cradl(?:es?|ing))\b|\b(?:on|in|across)\b.{0,35}\bpalm\b', re.I)
_HAND = re.compile(r'手|掌|\bhands?\b|\bpalm\b', re.I)
_FACE = re.compile(r'脸|面部|面颊|\bface\b', re.I)
_SAME_FRAME = re.compile(r'同框|同一画面|同一个画面|同时.{0,8}(?:可见|入镜)|\bsame\s+frame\b|\bboth\b.{0,40}\b(?:visible|in\s+frame)\b|\bshown\s+together\b|\bvisible\s+together\s+(?:in|within)\s+(?:(?:the|same)\s+)?frame\b', re.I)
_FRAMING = re.compile(r'\b(?:fram(?:es?|ing)|captur(?:es?|ing)|show(?:s|ing)?)\b', re.I)
_FULL_BODY = re.compile(r'\bfull\s+body\b|\b(?:entire|complete|whole)\b.{0,45}(?:<Subject\s*\d+>|\b(?:product|plush|toy|body)\b)|\bfully\s+visible\b', re.I)
_FRAME_EXCLUSION = re.compile(r'\b(?:outside|off[- ]screen|hidden|excluded|cropped)\b|\bout\s+of\b.{0,20}\b(?:frame|view)\b|\bnot\b.{0,20}\b(?:visible|in\s+frame|shown)\b', re.I)
_FRAME_LOCATION = re.compile(r'<Subject\s*\d+>\s+(?:(?:is|remains)\s+)?'
    r'(?:(?:being\s+)?(?:held|shown|positioned|placed)\s+)?(?:in|at)\s+'
    r'(?:(?:the|a)\s+)?(?:(?:center|centre|middle|left|right)\s+(?:of\s+)?)?(?:the\s+)?frame\b', re.I)
_FACE_VISIBLE = re.compile(r'\bface\b.{0,35}\b(?:visible|shown|in\s+(?:the\s+)?frame)\b|\bvisible\b.{0,20}\bface\b', re.I)
_CHEEK = re.compile(
    r'(?:抬|举|提|拿|移|贴近|靠近|贴).{0,28}(?:脸颊|面颊|脸侧|脸旁|脸)|'
    r'\b(?:rais(?:e|es|ed|ing)|lift(?:s|ed|ing)?|bring(?:s|ing)?|brought|mov(?:e|es|ed|ing))\b'
    r'.{0,85}\b(?:cheek|beside\s+(?:her|his|the)\s+face|near\s+(?:her|his|the)\s+face)\b', re.I)
_NEGATIVE = re.compile(r'不要|不许|不能|禁止|避免|无需|不用|\b(?:not|never|without|avoid)\b|\bdon[’\x27]t\b', re.I)
_REFERENCE = re.compile(r'\b(?:printed|print|reference|picture|photo|ocr|label|caption|ruler|annotation)\b|印刷|标注|参考图|照片', re.I)
_REFERENCE_SCALE = re.compile(
    r'\b(?:used?|using|retained?|preserved?|referenc(?:es?|ed|ing)|establish(?:es|ed)?|determine(?:s|d)?|govern(?:s|ed)?|follow(?:s|ed)?)\b'
    r'.{0,85}\b(?:physical\s+scale|scale|physical\s+size|actual\s+size|dimensions?)\b|'
    r'\b(?:dimensions?|measurements?)\b.{0,90}\b(?:as|for)\b.{0,25}\b(?:physical\s+)?scale\b|'
    r'(?:用于|作为|决定|确定|保留).{0,20}(?:实体尺度|实际大小|物理尺寸|尺度|比例)', re.I)
_NON_SCALE = re.compile(
    r'\b(?:not|never)\b.{0,45}\b(?:used?|using|retained?|preserved?|scale|size|dimensions?)\b|'
    r'\b(?:do\s+not|don[’\x27]t|ignore|disregard)\b.{0,65}\b(?:dimensions?|measurements?|scale|size)\b|'
    r'(?:不|不得|不要|忽略).{0,20}(?:用于|作为|尺度|尺寸|比例|测量)', re.I)


def _canonical(value):
    match = _TAG.fullmatch(value.strip()) if isinstance(value, str) else None
    return f'<{match[1].title()} {int(match[2])}>' if match else None


def _tags(text):
    return [(match.start(), f'<{match[1].title()} {int(match[2])}>') for match in _TAG.finditer(text)]


def _sentences(text, *, punctuation=False):
    # Decimal points are measurements, not sentence boundaries. The returned
    # strings are original substrings, including caller punctuation if asked.
    start = 0
    for match in re.finditer(r'(?<!\d)\.(?!\d)|[。！？!?；;\n]', text):
        end = match.end() if punctuation else match.start()
        value = text[start:end].strip()
        if value:
            yield value
        start = match.end()
    value = text[start:].strip()
    if value:
        yield value


def _positive(pattern, text):
    for match in pattern.finditer(text):
        prefix = re.split(r'[，,。；;.!?\n]', text[:match.start()])[-1][-45:]
        if not _NEGATIVE.search(prefix) and not _NEGATIVE.search(match[0]):
            return match
    return None


def _scale_items(constraints):
    if not isinstance(constraints, dict) or constraints.get('version') != 1:
        return []
    items = constraints.get('items')
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)
            and isinstance(item.get('target_key'), str) and item['target_key'].strip()
            and not item['target_key'].startswith('__')
            and (item.get('measurements') or item.get('relative_scale'))]


def extract_scene_requirements(instruction, constraints):
    """Extract only explicit recognized actions for one resolved scale target.

    Multiple physical targets are deliberately left to the caller; no action is
    distributed among products or inferred from the general scene description.
    """
    items = _scale_items(constraints)
    keys = {item['target_key'] for item in items}
    if not isinstance(instruction, str) or len(keys) != 1:
        return []
    key = next(iter(keys))
    requirements, found = [], set()
    for sentence in _sentences(instruction, punctuation=True):
        codes = re.findall(r'(?<![A-Za-z0-9_])P\d+(?![A-Za-z0-9_])', sentence, re.I)
        if any(code.casefold() != key.casefold() for code in codes):
            continue
        candidates = []
        palm = _positive(_PALM, sentence)
        if palm and _positive(_FRAME, sentence):
            candidates.append(('target_first_frame_palm_support', palm))
        same = _positive(_SAME_FRAME, sentence)
        if same and _HAND.search(sentence) and _FACE.search(sentence):
            candidates.append(('target_hand_face_same_frame', same))
        cheek = _positive(_CHEEK, sentence)
        if cheek:
            candidates.append(('target_raise_to_cheek', cheek))
        for rule, evidence in candidates:
            if rule in found:
                continue
            found.add(rule)
            requirement = {'rule': rule, 'target_key': key, 'source_text': sentence,
                           'evidence_text': evidence[0]}
            if rule == 'target_first_frame_palm_support':
                if re.search(r'左|\bleft\b', sentence, re.I):
                    requirement['hand'] = 'left'
                elif re.search(r'右|\bright\b', sentence, re.I):
                    requirement['hand'] = 'right'
            requirements.append(requirement)
    return requirements


def _sections(prompt):
    matches = list(_HEADING.finditer(prompt))
    return {match[1].lower(): prompt[match.end():matches[index + 1].start() if index + 1 < len(matches) else len(prompt)]
            for index, match in enumerate(matches)}


def _definitions(text):
    matches = list(_DEFINITION.finditer(text))
    return [(_canonical(match[1]), text[match.start():matches[index + 1].start() if index + 1 < len(matches) else len(text)].strip())
            for index, match in enumerate(matches)]


def _contains_key(text, key):
    return bool(re.search(r'(?<![A-Za-z0-9_])' + re.escape(key) + r'(?![A-Za-z0-9_])', text, re.I))


def _scope(contract, definitions, item):
    key = item['target_key']
    target_groups = contract.get('constraint_target_groups') or {}
    group = target_groups.get(key) if isinstance(target_groups, dict) else None
    groups = contract.get('groups') or []
    owners, pictures = {}, set()
    for candidate in groups if isinstance(groups, list) else []:
        if not isinstance(candidate, dict) or candidate.get('category') != 'product':
            continue
        for raw in candidate.get('appearance_native') or []:
            picture = _canonical(raw)
            if picture and picture.startswith('<Picture '):
                owners.setdefault(picture, set()).add(candidate.get('group'))
                if candidate.get('group') == group:
                    pictures.add(picture)
    # Two physical targets assigned to one group cannot be guessed apart.
    if group and sum(value == group for value in target_groups.values()) > 1:
        return set(), set()
    pictures = {picture for picture in pictures if owners[picture] == {group}}
    subjects = set()
    for label, text in definitions:
        if not label or not label.startswith('<Subject '):
            continue
        refs = {tag for _, tag in _tags(text) if tag.startswith('<Picture ')}
        foreign = {ref for ref in refs if ref in owners and owners[ref] != {group}}
        if not foreign and ((pictures and refs.intersection(pictures)) or _contains_key(text, key)):
            subjects.add(label)
    verified = contract.get('verified_target_subjects')
    if isinstance(verified, dict) and key in verified:
        selected = verified[key] if isinstance(verified[key], (list, tuple)) else []
        subjects.intersection_update(tag for value in selected
            if (tag := _canonical(value)) and tag.startswith('<Subject '))
    return subjects, pictures


def _centimeters(value, unit):
    unit = str(unit).strip().lower()
    if unit in {'mm', '毫米'} or unit.startswith('millimet'):
        factor = Decimal('0.1')
    elif unit in {'cm', '厘米', '公分'} or unit.startswith('centimet'):
        factor = Decimal(1)
    elif unit in {'m', '米', 'meter', 'meters', 'metre', 'metres'}:
        factor = Decimal(100)
    else:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number * factor if number.is_finite() and number > 0 else None


def _measurements(text):
    for pattern in (_AXIS_FIRST, _VALUE_FIRST):
        for match in pattern.finditer(text):
            axis = re.sub(r'^in\s+', '', match['axis'].lower())
            axis = _AXES.get(axis, axis)
            value = _centimeters(match['value'], match['unit'])
            if value is not None:
                yield match, axis, value


def _owner(text, match, implicit=None):
    explicit = match.groupdict().get('owner')
    if explicit:
        return _canonical(explicit)
    previous = [tag for position, tag in _tags(text) if position <= match.start()]
    return previous[-1] if previous else implicit


def _physical_measurement(text, match):
    prefix, suffix = text[max(0, match.start() - 60):match.start()], text[match.end():match.end() + 35]
    # An unrelated disclaimer later in the sentence cannot erase a positive
    # product measurement earlier in it. Limit local exclusions to the text
    # after the preceding native anchor.
    anchors = list(_TAG.finditer(prefix))
    local_prefix = prefix[anchors[-1].end():] if anchors else prefix
    other = r'(?:camera|lens|table|shelf|room|ceiling|person|actor|woman|man|child|girl|boy)'
    if re.search(r'\b' + other + r'\b[^<>]{0,24}\b(?:is|measures|measuring)\s*$', local_prefix, re.I):
        return False
    if re.search(r'\b' + other + r'\s*$', local_prefix, re.I):
        return False
    if re.search(r'(?:镜头|相机|桌子|桌面|展台|柜子|房间|人物|身高)\s*$', prefix):
        return False
    if re.search(r'\b(?:beside|behind|next\s+to|in\s+front\s+of)\s+(?:a|an|the)\s*$', prefix, re.I):
        return False
    if re.match(r'\s+(?:box|table|shelf|room|person|actor|woman|man|child|girl|boy)\b', suffix, re.I):
        return False
    if _NEGATIVE.search(re.split(r'[，,;；]', local_prefix)[-1]):
        return False
    # A printed label may be described as identity evidence without making its
    # text the product's physical size. Quoted/explicitly printed measurements
    # are checked separately only when the writer says to use them for scale.
    if re.search(r'\b(?:printed|label|text|ocr|annotation|ruler)\b|印刷|标注', local_prefix, re.I):
        return bool(_REFERENCE_SCALE.search(text)) and not _NON_SCALE.search(text)
    return True


def _floor_conflicts(body, subjects):
    found = []
    for subject in sorted(subjects):
        number = _TAG.fullmatch(subject)[2]
        tag = r'<Subject\s*' + number + r'>'
        placement = re.compile(tag + r'\s*(?:(?:is|remains|sits|rests|stands|lies|sitting|resting|standing|lying|placed|positioned|located)\s+){0,2}'
            r'(?:directly\s+)?(?:on|upon)\s+(?:(?:the|a|an)\s+)?(?:floor|rug|carpet)\b|'
            + tag + r'\s*(?:静置于|放置在|摆放在|放在|位于|置于|在|坐在|躺在)(?:地毯|地板|地面)', re.I)
        for sentence in _sentences(body):
            match = _positive(placement, sentence)
            if match:
                found.append(match[0])
    return found


def _caller_scale_prefix(contract, definitions):
    """Return only the exact metadata prefix emitted by the scale producer.

    Reuse its pure renderer so a creative sentence cannot be mistaken for
    metadata just because it starts with a similar heading.
    """
    from .revision_constraints import _description

    constraints = contract.get('revision_constraints') or {}
    if constraints.get('version') != 1 or not constraints.get('items'):
        return ''
    clauses = []
    try:
        for item in constraints['items']:
            subjects, _ = _scope(contract, definitions, item)
            if len(subjects) != 1:
                return ''
            clauses.append(_description(item, next(iter(subjects))))
    except (KeyError, TypeError, ValueError):
        return ''
    return 'Current caller-authored product scale: ' + ' '.join(clauses) + ' '


def _opening_palm_state(body, subjects, scale_prefix=''):
    """Recognize a present held state in the first numbered shot sentence.

    An initial composition can settle the opening without the literal words
    "first frame". A future support action or a later sentence cannot.
    """
    shot = re.search(r'\[Shot\s*1\]\s*', body, re.I)
    if not shot:
        return ''
    beginning = body[shot.end():]
    if scale_prefix and beginning.startswith(scale_prefix):
        beginning = beginning[len(scale_prefix):]
    opening = next(_sentences(beginning), '')
    if re.search(r'\b(?:will|later|after(?:ward)?s?|eventually|then|subsequently|soon|next)\b|随后|稍后|之后|将会|再将', opening, re.I):
        return ''
    state = re.search(r'\b(?:being\s+|(?:is|remains|stays)\s+(?:already\s+)?)'
        r'(?:held|supported|cradled)\b|\b(?:rests?|resting)\b.{0,35}\bpalm\b|'
        r'\bpalm\b.{0,35}\b(?:support(?:s|ing)?|hold(?:s|ing)?|cradl(?:es?|ing))\b', opening, re.I)
    return opening if state and _target_action(_PALM, opening, subjects) else ''


def _scene_clauses(body):
    for sentence in _sentences(body):
        for contrast in re.split(r'\b(?:while|whereas|but)\b', sentence, flags=re.I):
            # Introductory commas and lists do not end a single subject's
            # action: "At the first frame, <Subject 1> rests in the palm"
            # still explicitly establishes both timing and support. Keep
            # comma isolation when another written subject could own an
            # adjacent action; never borrow its hand/face/cheek statement.
            subjects = {tag for _, tag in _tags(contrast) if tag.startswith('<Subject ')}
            clauses = [contrast] if len(subjects) <= 1 else re.split(r'[,，]', contrast)
            for clause in clauses:
                if clause.strip():
                    yield clause.strip()


def _target_action(pattern, clause, subjects):
    match = _positive(pattern, clause)
    if not match:
        return False
    # A sentence can name two products, with only the other one being raised.
    # An explicit action's object takes precedence over a nearby target tag.
    inside = {tag for _, tag in _tags(match[0]) if tag.startswith('<Subject ')}
    if inside:
        return bool(inside.intersection(subjects))
    tags = [(position, tag) for position, tag in _tags(clause) if tag.startswith('<Subject ')]
    if not tags:
        return False
    closest = min(tags, key=lambda entry: min(abs(entry[0] - match.start()), abs(entry[0] - match.end())))
    return closest[1] in subjects


def _listed_frame_scope(text, subjects):
    """A camera composition explicitly lists face, hand and complete target.

    Merely mentioning a framing verb is insufficient. The visible composition
    must include all requested items without an exclusion or another subject
    owning the adjacent clause.
    """
    match = _positive(_FRAMING, text)
    if not match:
        return False
    composition = text[match.end():]
    return bool(_HAND.search(composition) and _FACE.search(composition)
        and _FULL_BODY.search(composition) and not _FRAME_EXCLUSION.search(composition)
        and any(tag in subjects for _, tag in _tags(composition)))


def _supported_frame_scope(text, subjects):
    # A target explicitly located in-frame and supported by the person's palm,
    # with their face visibly beside it in that same sentence, establishes a
    # common composition without requiring an exact "same frame" phrase.
    return bool(not _FRAME_EXCLUSION.search(text) and _positive(_FACE_VISIBLE, text)
        and _target_action(_FRAME_LOCATION, text, subjects)
        and _target_action(_PALM, text, subjects))


def revision_semantic_findings(prompt, contract):
    """Return explicit, target-scoped written contradictions and missing rules.

    Messages retain caller text and exact conflicting prose for the native
    writer's existing correction rounds. Unknown wording and unbound subjects
    are left alone; this function never edits either argument.
    """
    if not isinstance(prompt, str) or not isinstance(contract, dict):
        return []
    items = _scale_items(contract.get('revision_constraints'))
    if not items:
        return []
    sections = _sections(prompt)
    definitions = _definitions(sections.get('subject_definitions', prompt))
    scale_prefix = _caller_scale_prefix(contract, definitions)
    body = sections.get('detailed_description', '')
    retention = sections.get('retention_analysis', '')
    findings, seen = [], set()

    def report(rule, message):
        if (rule, message) not in seen:
            seen.add((rule, message))
            findings.append({'rule': rule, 'severity': 'ERROR', 'message': message})

    for item in items:
        subjects, pictures = _scope(contract, definitions, item)
        allowed = subjects | pictures
        if not allowed:
            continue
        key = item['target_key']
        label = key + ' (' + ', '.join(sorted(subjects) + sorted(pictures)) + ')'
        expected = {}
        for entry in item.get('measurements') or []:
            if not isinstance(entry, dict):
                continue
            axis = entry.get('axis')
            value = _centimeters(entry.get('value'), entry.get('unit'))
            if axis in {'height', 'width', 'length', 'depth'} and value is not None:
                expected[axis] = value
        caller = item.get('source_text') or key
        segments = []
        for native, definition in definitions:
            if native in allowed:
                segments.extend((sentence, native) for sentence in _sentences(definition))
        segments.extend((sentence, None) for sentence in _sentences(body))
        for line in retention.splitlines():
            tags = _tags(line)
            implicit = tags[0][1] if tags and not line[:tags[0][0]].strip() else None
            segments.extend((sentence, implicit) for sentence in _sentences(line))
        for sentence, implicit in segments:
            conflicts = []
            for match, axis, value in _measurements(sentence):
                owner = _owner(sentence, match, implicit)
                if owner not in allowed or not _physical_measurement(sentence, match):
                    continue
                if axis in expected and abs(expected[axis] - value) > Decimal('0.000001'):
                    conflicts.append(f'{axis}: caller {expected[axis]} cm, written {match[0]}')
                elif not expected and item.get('relative_scale'):
                    report('revision_reference_scale_used', f'Target {label}. Caller: «{caller}». '
                        f'Use the caller relative scale ({item["relative_scale"]}); do not restore numeric reference dimensions as physical scale. '
                        f'Conflicting written fragment: «{sentence}».')
            if conflicts:
                report('revision_target_scale_conflict', f'Target {label}. Caller: «{caller}». '
                    + '; '.join(conflicts) + f'. Conflicting written fragment: «{sentence}». '
                    'Keep appearance identity and replace only the conflicting physical scale with the caller requirements.')
            linked = implicit in allowed or any(tag in allowed for _, tag in _tags(sentence))
            if linked and _REFERENCE.search(sentence) and _REFERENCE_SCALE.search(sentence) and not _NON_SCALE.search(sentence):
                report('revision_reference_scale_used', f'Target {label}. Caller: «{caller}». '
                    f'Printed/reference measurements must not govern physical scale. Conflicting written fragment: «{sentence}». '
                    'Retain product appearance and use only the current caller scale.')

        # Missing scene actions need an actually associated written Subject.
        # Pictures alone do not establish which unbound product is being moved.
        if not subjects:
            continue
        scoped = [clause for clause in _scene_clauses(body)
                  if any(tag in subjects for _, tag in _tags(clause))]
        written = ' '.join(scoped[:3]) or 'No target-scoped action is written.'
        requirements = contract.get('scene_requirements') or []
        for requirement in requirements if isinstance(requirements, list) else []:
            if not isinstance(requirement, dict) or requirement.get('target_key') != key:
                continue
            rule = requirement.get('rule')
            source = requirement.get('source_text') or key
            evidence, valid = '', False
            if rule == 'target_first_frame_palm_support':
                hand = requirement.get('hand')
                matches = [text for text in scoped if _positive(_FRAME, text) and _target_action(_PALM, text, subjects)
                    and (not hand or re.search(r'左|\bleft\b' if hand == 'left' else r'右|\bright\b', text, re.I))]
                opening = _opening_palm_state(body, subjects, scale_prefix)
                if opening and (not hand or re.search(r'左|\bleft\b' if hand == 'left' else r'右|\bright\b', opening, re.I)):
                    matches.append(opening)
                floor = _floor_conflicts(body, subjects)
                valid = bool(matches) and not floor
                evidence = '; '.join(floor) or 'No recognized first-frame palm support. Existing target prose: ' + written
                action = f'At the first frame, the target {", ".join(sorted(subjects))} is supported by the open {hand + " " if hand else ""}palm; do not place that target on the floor, rug, or carpet.'
            elif rule == 'target_hand_face_same_frame':
                valid = any((_HAND.search(text) and _FACE.search(text) and _target_action(_SAME_FRAME, text, subjects))
                    or _listed_frame_scope(text, subjects) or _supported_frame_scope(text, subjects) for text in scoped)
                evidence = 'No recognized target-scoped hand-and-face same-frame statement. Existing target prose: ' + written
                action = f'Keep the supporting hand and the face visible in the same frame with the target {", ".join(sorted(subjects))}.'
            elif rule == 'target_raise_to_cheek':
                valid = any(_target_action(_CHEEK, text, subjects) for text in scoped)
                evidence = 'No recognized target-scoped lift-to-cheek action. Existing target prose: ' + written
                action = f'Then raise the target {", ".join(sorted(subjects))} to the cheek.'
            else:
                continue
            if not valid:
                report(rule, f'Target {label}. Caller: «{source}». {action} Written conflict or omission: «{evidence}».')
    return findings
