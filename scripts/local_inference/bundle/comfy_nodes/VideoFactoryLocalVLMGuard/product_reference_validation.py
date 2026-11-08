"""Local visual checks before caller-declared product identities are projected.

The checker compares actual files, not descriptor keywords. Its receipt only
authorizes specific observed card subjects; it never edits a card or silently
removes a caller reference. The surrounding local profile supplies HTTP audit,
guided-schema verification and the single-attempt/offline execution policy.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit
import uuid


PROTOCOL = 'product-reference-identity-4'
MODEL = 'Qwen/Qwen3-VL-4B-Instruct'
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_OUTPUT = 4096
MAX_IMAGES_PER_CALL = 8
MAX_CALLS = 2


class ProductReferenceConflict(RuntimeError):
    """A product identity is ambiguous or not supported by its actual pictures."""


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode('utf-8')).hexdigest()


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _blocked(message):
    raise ProductReferenceConflict(
        '商品参考身份不一致或无法确定：' + message +
        '。请仅将同款商品清晰图绑定为商品参考；人物或其他款式图片请移除该身份绑定或改为合适用途，原图库文件无需删除。')


def _schema(groups):
    """Every group and native picture is a required named answer, never a list.

    Put brief per-picture observation before classification. There is no second
    global model verdict: a group succeeds only when every picture passes its
    explicit checks. The anchor's comparison with itself is logically ``same``.
    """
    image = {'type': 'object', 'additionalProperties': False,
             'required': ['reason', 'target_subject_indices', 'independent_person_indices',
                          'conflicting_variant_indices', 'target_visible', 'same_sku_as_anchor'],
             'properties': {
                 'reason': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                 'target_subject_indices': {'type': 'array', 'maxItems': 1,
                                            'items': {'type': 'integer'}},
                 'independent_person_indices': {'type': 'array', 'maxItems': 8,
                                               'items': {'type': 'integer'}},
                 'conflicting_variant_indices': {'type': 'array', 'maxItems': 8,
                                                'items': {'type': 'integer'}},
                 'target_visible': {'type': 'boolean'},
                 'same_sku_as_anchor': {'type': 'string', 'enum': ['same', 'different', 'uncertain']}}}
    named_groups = {}
    for group in groups:
        named_groups[group['group']] = {
            'type': 'object', 'additionalProperties': False,
            'required': ['references'],
            'properties': {
                'references': {'type': 'object', 'additionalProperties': False,
                               'required': list(group['appearance_native']),
                               'properties': {label: {
                                   **image, 'properties': {**image['properties'],
                                       'same_sku_as_anchor': ({'type': 'string', 'const': 'same'} if index == 0
                                                              else image['properties']['same_sku_as_anchor'])}}
                                              for index,label in enumerate(group['appearance_native'])}}}}
    return {'title': 'ProductReferenceIdentity', 'type': 'object', 'additionalProperties': False,
            'required': ['groups'], 'properties': {'groups': {
                'type': 'object', 'additionalProperties': False,
                'required': list(named_groups), 'properties': named_groups}}}


def _normalise_reply(raw, groups):
    """Preserve the exact raw object and normalize only its keyed envelope."""
    expected = {group['group']: group for group in groups}
    if (not isinstance(raw, dict) or set(raw) != {'groups'}
            or not isinstance(raw['groups'], dict) or set(raw['groups']) != set(expected)):
        _blocked('校验回复未逐一回答所有指定商品分组')
    result = []
    fields = {'reason', 'target_subject_indices', 'independent_person_indices',
              'conflicting_variant_indices', 'target_visible', 'same_sku_as_anchor'}
    for name, group in expected.items():
        answer = raw['groups'][name]
        labels = group['appearance_native']
        if (not isinstance(answer, dict) or set(answer) != {'references'}
                or not isinstance(answer['references'], dict)
                or set(answer['references']) != set(labels)):
            _blocked(name + ' 未逐一回答全部指定图片（' + ', '.join(labels) + '）')
        references = []
        for label in labels:
            row = answer['references'][label]
            if not isinstance(row, dict) or set(row) != fields:
                _blocked(label + ' 的逐图观察与判断字段不完整')
            references.append({'native_label': label, **row})
        clear = all(row['target_visible'] is True and row['same_sku_as_anchor'] == 'same'
                    and isinstance(row['target_subject_indices'], list) and len(row['target_subject_indices']) == 1
                    and isinstance(row['conflicting_variant_indices'], list) and not row['conflicting_variant_indices']
                    for row in references)
        uncertain = any(row['same_sku_as_anchor'] == 'uncertain' for row in references)
        result.append({'group': name, 'target_match': 'clear' if clear else 'uncertain' if uncertain else 'mismatch',
                       'target_match_source': 'derived_from_per_picture_checks', 'references': references})
    return {'groups': result}


def _validate_reply(reply, groups, observations):
    if not isinstance(reply, dict) or set(reply) != {'groups'} or not isinstance(reply['groups'], list):
        _blocked('校验回复缺少完整分组结果')
    expected = {group['group']: group for group in groups}
    if len(reply['groups']) != len(expected):
        _blocked('校验回复的商品分组数量不完整')
    found = set()
    approved, indices, people, results = {}, {}, {}, []
    for answer in reply['groups']:
        if (not isinstance(answer, dict) or set(answer) != {'group', 'target_match', 'target_match_source', 'references'}
                or answer['target_match_source'] != 'derived_from_per_picture_checks'):
            _blocked('校验回复的商品分组结构无效')
        name = answer['group']
        if not isinstance(name, str) or name not in expected or name in found:
            _blocked('校验回复包含重复或未知商品分组')
        found.add(name)
        group = expected[name]
        labels = group['appearance_native']
        rows = answer['references']
        if not isinstance(rows, list) or len(rows) != len(labels):
            _blocked(name + ' 缺少部分实际图片的校验结果')
        seen = set()
        for row in rows:
            required = {'native_label', 'same_sku_as_anchor', 'target_visible',
                        'target_subject_indices', 'independent_person_indices',
                        'conflicting_variant_indices', 'reason'}
            if not isinstance(row, dict) or set(row) != required:
                _blocked(name + ' 的图片结果字段不完整')
            label = row['native_label']
            if not isinstance(label, str) or label not in labels or label in seen:
                _blocked(name + ' 的图片编号缺失、重复或跨组')
            seen.add(label)
            if row['same_sku_as_anchor'] != 'same' or row['target_visible'] is not True:
                _blocked(label + ' ' + str(row.get('reason') or '不是唯一、清楚的同款商品'))
            reason = row['reason']
            if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 160:
                _blocked(label + ' 未提供有效的视觉依据')
            target = row['target_subject_indices']
            persons = row['independent_person_indices']
            variants = row['conflicting_variant_indices']
            subjects = observations[label]['card']['subjects']
            if (not isinstance(target, list) or len(target) != 1
                    or not isinstance(persons, list) or len(persons) > 8
                    or not isinstance(variants, list) or len(variants) > 8
                    or any(type(x) is not int or not 0 <= x < len(subjects) for x in target + persons + variants)
                    or len(set(persons)) != len(persons) or len(set(variants)) != len(variants)
                    or set(target).intersection(persons) or set(variants).intersection(target + persons)):
                _blocked(label + ' 无法唯一定位商品及独立操作者')
            if variants:
                _blocked(label + ' 包含其他实体商品款式（主体索引 ' + ', '.join(map(str, variants)) + '）：' + reason)
            subject = subjects[target[0]]
            descriptor = subject.get('descriptor') if isinstance(subject, dict) else None
            if (not isinstance(descriptor, str) or not descriptor.strip()
                    or subject.get('kind') in {'person', 'animal', 'environment'}):
                _blocked(label + ' 把人物或非商品主体选作了商品')
            if sum(isinstance(s, dict) and s.get('descriptor') == descriptor for s in subjects) != 1:
                _blocked(label + ' 的商品描述不能唯一对应一个观察主体')
            if label in approved:
                _blocked(label + ' 同时属于不同商品身份')
            approved[label] = [descriptor]
            indices[label] = list(target)
            people[label] = list(persons)
            results.append({'group': name, **row, 'approved_descriptors': [descriptor]})
        if seen != set(labels):
            _blocked(name + ' 未覆盖全部图片')
        if answer['target_match'] != 'clear':
            _blocked(name + '（' + ', '.join(labels) + '）与声明目标不符或不清楚')
    return {'approved_product_descriptors_by_native': approved,
            'approved_product_indices_by_native': indices,
            'independent_person_indices_by_native': people, 'group_results': results}


def _request_batches(groups):
    """Keep complete groups together, except a nine-view group repeats its anchor.

    The workflow permits nine pictures while the verified local service permits
    eight per request. With at most nine distinct product pictures, this packing
    needs at most two bounded calls and never drops a picture or changes an anchor.
    """
    batches, current, count = [], [], 0
    for group in groups:
        members = group['appearance_native']
        if len(members) > MAX_IMAGES_PER_CALL:
            if current:
                batches.append(current)
                current, count = [], 0
            batches.append([{**group, 'appearance_native': list(members[:MAX_IMAGES_PER_CALL])}])
            batches.append([{**group, 'appearance_native': [members[0], *members[MAX_IMAGES_PER_CALL:]]}])
        elif count + len(members) > MAX_IMAGES_PER_CALL:
            batches.append(current)
            current, count = [group], len(members)
        else:
            current.append(group)
            count += len(members)
    if current:
        batches.append(current)
    if not 1 <= len(batches) <= MAX_CALLS:
        _blocked('商品参考图无法在两次有界本地检查内完整比较')
    return batches


def _merge_batch_replies(batch_records, batches, groups, observations):
    if not isinstance(batch_records, list) or len(batch_records) != len(batches):
        _blocked('跨图检查缺少部分批次回执')
    merged = {group['group']: {} for group in groups}
    for record, batch in zip(batch_records, batches):
        labels = [label for group in batch for label in group['appearance_native']]
        if (not isinstance(record, dict) or record.get('input_labels') != labels
                or record.get('groups') != batch
                or record.get('reply_sha256') != _digest(record.get('reply'))
                or record.get('raw_reply_sha256') != _digest(record.get('raw_reply'))):
            _blocked('跨图检查的批次输入或原始回复不完整')
        reply = record['reply']
        if _normalise_reply(record['raw_reply'], batch) != reply:
            _blocked('跨图检查的规范化结果与原始逐图回复不一致')
        _validate_reply(reply, batch, observations)
        for group in reply['groups']:
            target = merged[group['group']]
            for row in group['references']:
                label = row['native_label']
                if label in target:
                    # Natural-language reasons can differ between calls. The
                    # identity verdict, selected product and operator indices
                    # must agree exactly for the repeated physical anchor.
                    before = {k: v for k, v in target[label].items() if k != 'reason'}
                    after = {k: v for k, v in row.items() if k != 'reason'}
                    if before != after:
                        _blocked(label + ' 的重复锚点在两批检查中分类或主体索引不一致')
                else:
                    target[label] = row
    reply = {'groups': [
        {'group': group['group'], 'target_match': 'clear',
         'target_match_source': 'derived_from_per_picture_checks',
         'references': [merged[group['group']][label]
                        for label in group['appearance_native']
                        if label in merged[group['group']]]}
        for group in groups]}
    _validate_reply(reply, groups, observations)
    return reply


def verify_product_references(backend, refs, cards, contract, cache_root, seed=None, *, receipt_root=None):
    """Verify product groups in at most two actual-image calls before H3 loading.

    ``refs`` are the actual upstream Brief AssetRefs in their native order;
    ``cards`` are indexed by their exact file SHA256. ``target_description`` on
    a contract group is frozen caller metadata, not inferred identity.
    """
    groups = [g for g in (contract or {}).get('groups', []) if g.get('category') == 'product']
    if not groups:
        return {'protocol': PROTOCOL, 'status': 'NOT_REQUIRED',
                'approved_product_descriptors_by_native': {}, 'group_results': []}
    cfg = getattr(backend, 'cfg', None)
    endpoint = urlsplit(getattr(cfg, 'base_url', ''))
    if (getattr(cfg, 'model', None) != MODEL or endpoint.scheme != 'http'
            or endpoint.hostname not in {'127.0.0.1', 'localhost', '::1'}
            or endpoint.port != 8772 or endpoint.path.rstrip('/') != '/v1'
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        _blocked('跨图检查只允许现有本机 Qwen 服务，不会改用云端接口')
    image_refs = [ref for ref in refs if getattr(getattr(ref, 'kind', None), 'value', getattr(ref, 'kind', None)) == 'image']
    native = {f'<Picture {i}>': ref for i, ref in enumerate(image_refs, 1)}
    labels = []
    names = set()
    for group in groups:
        name, members = group.get('group'), group.get('appearance_native')
        if (not isinstance(name, str) or not name.strip() or name in names or not isinstance(members, list)
                or not members or len(set(members)) != len(members) or any(x not in native for x in members)):
            _blocked('声明的商品组或图片编号无效')
        names.add(name)
        labels += members
    if len(labels) > 9 or len(set(labels)) != len(labels):
        _blocked('商品图超过9张或同一图片跨商品身份绑定')
    observations, paths = {}, {}
    for label in labels:
        ref = native[label]
        path = Path(ref.path) if getattr(ref, 'path', None) else None
        if path is None or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            _blocked(label + ' 的真实图片文件不可用或过大')
        sha = hashlib.sha256()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                sha.update(block)
        if sha.hexdigest() != getattr(ref, 'sha256', None):
            _blocked(label + ' 的图片SHA256与冻结输入不一致')
        card = cards.get(ref.sha256)
        card = asdict(card) if is_dataclass(card) else card
        if (not isinstance(card, dict) or not isinstance(card.get('subjects'), list)
                or not 1 <= len(card['subjects']) <= 8):
            _blocked(label + ' 没有可明确对应的真实图像分析主体')
        observations[label] = {'sha256': ref.sha256, 'note': getattr(ref, 'note', None), 'card': card}
        paths[label] = path
    group_inputs = [{'group': g['group'], 'appearance_native': g['appearance_native'],
                     'target_description': g.get('target_description') or g.get('product_name') or g['group']}
                    for g in groups]
    batches = _request_batches(group_inputs)
    key_data = {'protocol': PROTOCOL, 'model': MODEL, 'groups': group_inputs, 'observations': observations}
    key = _digest(key_data)
    cache_path = Path(cache_root)/f'{key}.json'
    evidence_path = Path(receipt_root)/'product-reference-identity.json' if receipt_root else None
    cache_hit = False
    reply = None
    batch_records = []
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding='utf-8'))
            if (cached.get('protocol') == PROTOCOL and cached.get('cache_key') == key
                    and cached.get('status') == 'PASS' and cached.get('input_sha256') == _digest(key_data)
                    and cached.get('reply_sha256') == _digest(cached.get('reply'))):
                combined = _merge_batch_replies(cached.get('batches'), batches, group_inputs, observations)
                if combined != cached.get('reply'):
                    _blocked('缓存的合并回复与原始批次回复不一致')
                _validate_reply(cached.get('reply'), groups, observations)
                batch_records = cached['batches']
                reply, cache_hit = cached['reply'], True
        except (ValueError, OSError, ProductReferenceConflict, TypeError, AttributeError):
            # A corrupt/stale cache is never evidence; perform the one actual check.
            pass
    record = {'protocol': PROTOCOL, 'status': 'BLOCKED', 'model': MODEL, 'cache_key': key,
              'input_sha256': _digest(key_data), 'cache_hit': cache_hit,
              'evidence_source': 'local_vlm_cross_image', 'human_approved': False,
              'input_labels': labels, 'groups': group_inputs, 'batch_count': len(batches),
              'max_images_per_call': MAX_IMAGES_PER_CALL, 'max_calls': MAX_CALLS,
              'actual_call_count': 0, 'batches': batch_records,
              'target_match_source': 'derived_from_per_picture_checks',
              'original_observations': observations}
    try:
        if reply is None:
            from h3ir.backend import user_message
            for batch_index, batch in enumerate(batches, 1):
                batch_labels = [label for group in batch for label in group['appearance_native']]
                batch_observations = {label: {'candidate_subjects': [
                    {'index': index, 'descriptor': subject.get('descriptor'),
                     'attributes': subject.get('attributes', [])}
                    for index,subject in enumerate(observations[label]['card']['subjects'])]}
                    for label in batch_labels}
                text = ('Compare the ACTUAL attached images before allowing their target product identities to merge. '
                    'Image n corresponds to native labels in this ordered list: ' + json.dumps(batch_labels) + '.\n'
                    'Caller product groups and target descriptions:\n' + json.dumps(batch, ensure_ascii=False) + '\n'
                    'Existing image observations, indexed subjects are ZERO-based:\n' +
                    json.dumps(batch_observations, ensure_ascii=False) + '\n'
                    'Return the exact required named group and picture objects; every picture key must have its own answer. '
                    'Within each picture object first write reason as 8-16 words about ONLY that image\'s visible product, '
                    'then its subject indices and target visibility, and finally same_sku_as_anchor. '
                    'Do not discuss several images in one reason or write a long narrative. '
                    'Do not add a global group verdict. The first picture is that group\'s anchor; '
                    'its comparison with itself is same by definition, while target_visible still checks the caller target. '
                    'same_sku_as_anchor asks whether the VISIBLE product '
                    'design, shape, fixed features and color variant match the anchor; it does NOT ask whether the '
                    'pictures show the identical physical specimen, registered SKU, manufacturer, authenticity or provenance. '
                    'Pose, viewpoint, scale, packaging beside the product and background differences alone are NOT uncertainty. '
                    'A detail sheet and the same design next to its packaging are normal matching references. '
                    'A shared brand or family alone is insufficient when the visible shape, design or color variant differs. '
                    'target_visible is true ONLY when a physical object matching the caller appearance target is clearly visible. '
                    'If no such object can be identified use target_visible false and target_subject_indices []. '
                    'For non-anchor pictures use uncertain when visible design comparison cannot be settled. '
                    'Do not treat a depicted person, hand, '
                    'a printed illustration or the entire scene as the product. Select exactly one existing card subject index '
                    'for the target physical product; if it has no card subject use [] and mark target_visible false. '
                    'A person holding the SAME product may be present: put that person\'s index only in independent_person_indices, '
                    'even if the earlier card mislabeled that person as object. Keep the product and operator separate. '
                    'conflicting_variant_indices must list actual ZERO-based candidate subject indices for a different '
                    'PHYSICAL variant of this product type, never the target or an operator. Use [] if none is visible. '
                    'Ordinary objects, the product\'s package and flat printed illustrations are not another physical variant. '
                    'A gift box next to a toy is packaging, not a conflicting toy. '
                    'Distinct designs and color variants '
                    'must not be merged merely because they share a brand or product family. Caller scale overrides '
                    'are handled separately: printed dimensions alone do not reject an otherwise matching identity. '
                    'Give short factual visual reasons. Do not guess missing detail.')
                batch_record = {'index': batch_index, 'input_labels': batch_labels, 'groups': batch,
                                'status': 'BLOCKED'}
                batch_records.append(batch_record)
                record['actual_call_count'] += 1
                raw_reply = backend.json_call([
                    {'role': 'system', 'content': 'You verify visual product reference identity. Observe images; return only the required JSON.'},
                    user_message(text, [paths[label] for label in batch_labels])],
                    _schema(batch), required=('groups',),
                    max_tokens=MAX_OUTPUT, temperature=0.2, thinking=False, seed=seed, retries=0)
                batch_record.update(raw_reply=raw_reply, raw_reply_sha256=_digest(raw_reply))
                if receipt_root:
                    _write(Path(receipt_root)/f'product-reference-identity.batch-{batch_index:03}.json', batch_record)
                batch_reply = _normalise_reply(raw_reply, batch)
                batch_record.update(reply=batch_reply, reply_sha256=_digest(batch_reply))
                _validate_reply(batch_reply, batch, observations)
                batch_record['status'] = 'PASS'
                if receipt_root:
                    _write(Path(receipt_root)/f'product-reference-identity.batch-{batch_index:03}.json', batch_record)
            reply = _merge_batch_replies(batch_records, batches, group_inputs, observations)
        record.update(reply=reply, reply_sha256=_digest(reply))
        approved = _validate_reply(reply, groups, observations)
        record.update(status='PASS', **approved)
        if not cache_hit:
            _write(cache_path, record)
        return record
    except ProductReferenceConflict as exc:
        record['error'] = str(exc)
        raise
    except Exception as exc:
        record.update(error_type=type(exc).__name__, error=str(exc))
        raise ProductReferenceConflict('商品参考身份检查未完成，未开始 H3 采样；请检查本机 Qwen 服务并重试。' + str(exc)) from exc
    finally:
        if evidence_path:
            _write(evidence_path, record)
