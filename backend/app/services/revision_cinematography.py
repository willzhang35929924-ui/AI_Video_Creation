"""Apply explicit caller framing to generation copies, never approved source rows."""
from __future__ import annotations

from copy import deepcopy
import re


_SIZES = {
    'macro': ('微距特写', 'Macro Shot'),
    'extreme_close_up': ('超级特写', 'Extreme Close-Up'),
    'close_up': ('特写', 'Close-Up'),
    'medium_close_up': ('中近景', 'Medium Close-Up'),
    'medium': ('中景', 'Medium Shot'),
    'wide': ('远景', 'Wide Shot'),
    'full': ('全景', 'Full Shot'),
}
_PATTERNS = (
    ('macro', r'微距(?:超级)?特写|微距|\bmacro(?:\s+shot)?\b'),
    ('extreme_close_up', r'超级特写|大特写|\bextreme[ -]+close[ -]?up\b'),
    ('medium_close_up', r'中近景|\bmedium[ -]+close[ -]?up\b'),
    ('medium', r'中景|\bmedium\s+shot\b'),
    ('close_up', r'特写|近景|\bclose[ -]?up\b'),
    ('wide', r'远景|\bwide\s+shot\b|\blong\s+shot\b'),
    ('full', r'全景|\bfull\s+shot\b'),
)
_SIZE = re.compile('|'.join(f'(?P<{key}>{pattern})' for key, pattern in _PATTERNS), re.I)
_NEGATIVE = re.compile(r'不要|不许|不能|不得|禁止|避免|无需|不用|并非|不是|不(?:采用|使用|改|切换|换|固定)|非固定|\b(?:not|never|without|avoid)\b|don[’\x27]t', re.I)
_CHOICE = re.compile(r'或者|还是|或|任选|二选|\b(?:or|either|versus)\b|[/／]', re.I)
_REPLACE = re.compile(r'改为|改成|换成|切换为|\b(?:change|switch)\b.{0,80}\bto\b', re.I)
_CAMERA_CONTEXT = re.compile(r'镜头|景别|机位|摄影|拍摄|构图|采用|使用|改为|改成|换成|切换为|固定|\b(?:shot|camera|framing|use|change|switch)\b', re.I)
_STATIC = re.compile(r'固定(?:机位|镜头|中近景|中景|近景|全景|拍摄)|(?:摄像机|相机|机位|镜头).{0,6}固定|\bcamera\s+(?:holds?\s+static|is\s+static)\b|\bstatic\s+camera\b', re.I)


def _clauses(text):
    # A spoken or displayed line is content, not a camera instruction.
    text = re.sub(r'(?:台词|对白|字幕|dialogue|narration|say)\s*(?:改为|改成|为|是|[:：])?\s*'
                  r'(?:“[^”]*”|"[^"]*"|‘[^’]*’|\x27[^\x27]*\x27)', ' ', text, flags=re.I)
    return [part.strip() for part in re.split(r'(?<!\d)\.(?!\d)|[\n。！？;；!?]+', text) if part.strip()]


def _positive(clause, match):
    prefix = re.split(r'[，,]', clause[:match.start()])[-1][-35:]
    suffix = clause[match.end():match.end() + 10]
    return not _NEGATIVE.search(prefix + match[0]) and not re.match(r'\s*(?:不要|不用|不采用|不使用)', suffix)


def explicit_cinematography(instruction):
    """Recognize only affirmative, unambiguous shot-size/camera directions."""
    if not isinstance(instruction, str) or not instruction.strip():
        return {}
    sizes, static, source = [], False, []
    for original in _clauses(instruction):
        clause = original
        replacements = [match for match in _REPLACE.finditer(clause) if _positive(clause, match)]
        if replacements:
            # In "from close-up to medium shot", the stated replacement wins;
            # a bare choice such as "medium or close-up" remains unresolved.
            clause = clause[replacements[-1].end():]
        candidates = [match for match in _SIZE.finditer(clause) if _positive(clause, match)]
        if candidates:
            if _CHOICE.search(clause):
                return {}
            relevant = []
            for match in candidates:
                local = re.split(r'[，,]', clause[:match.start()])[-1]
                if not local.strip() or _CAMERA_CONTEXT.search(local) or replacements:
                    relevant.append(match.lastgroup)
            if relevant:
                sizes.extend(relevant)
                source.append(original)
        moves = [match for match in _STATIC.finditer(original) if _positive(original, match)]
        if moves:
            if _CHOICE.search(original):
                return {}
            static = True
            source.append(original)
    if len(set(sizes)) > 1:
        return {}
    result = {'source': 'instruction', 'source_text': list(dict.fromkeys(source))}
    if sizes:
        key = sizes[0]
        result.update(shot_size=key, shot_size_text=f'{_SIZES[key][0]}（{_SIZES[key][1]}）')
    if static:
        result['camera_move'] = '固定（holds static）'
    return result if sizes or static else {}


def _replace_framing(value, size):
    if not isinstance(value, str):
        return value
    chinese, english = _SIZES[size]
    return _SIZE.sub(lambda match: chinese if re.search(r'[\u3400-\u9fff]', match[0]) else english, value)


def apply_cinematography_override(camera_move, photography, instruction=None):
    """Return effective copies and an audit; leave all caller objects intact."""
    before = {'camera_move': deepcopy(camera_move), 'photography': deepcopy(photography)}
    effective = deepcopy(before)
    request = explicit_cinematography(instruction)
    if request.get('shot_size'):
        size = request['shot_size']
        photo = effective['photography'] if isinstance(effective['photography'], dict) else {}
        photo['shot_size'] = request['shot_size_text']
        for field in ('framing', '景别', 'camera_position', '机位'):
            if field in photo:
                photo[field] = _replace_framing(photo[field], size)
        effective['photography'] = photo
        effective['camera_move'] = _replace_framing(effective['camera_move'], size)
    if request.get('camera_move'):
        effective['camera_move'] = request['camera_move']
    audit = {'applied': bool(request), 'changed': effective != before, **request,
             'before': before, 'after': deepcopy(effective)}
    return effective['camera_move'], effective['photography'], audit
