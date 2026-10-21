"""Strict JSON and text rules shared by every hop.

bounded_text is byte-for-byte the rule in voice/controller.py. normalize_text adds
[R] NFKC, Cf removal, then strip characters.
"""
import json
import math
import unicodedata

from .errors import Refused


def bounded_text(value, limit=160, byte_limit=640):
    return (type(value) is str and 0 < len(value) <= limit and
            len(value.encode('utf-8', errors='surrogatepass')) <= byte_limit and
            value == value.strip() and
            all(ord(c) >= 32 and not 127 <= ord(c) <= 159 and
                not 0xD800 <= ord(c) <= 0xDFFF for c in value))


# Categories refused outright (never silently rewritten). Cc is checked before
# whitespace collapse so that "x\nexec" is refused, not turned into "x exec".
_REFUSED_CATEGORIES = frozenset(('Cc', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'))


def normalize_text(value, max_chars, max_bytes, strip=''):
    """Text(): NFKC -> refuse controls -> drop Cf -> drop `strip`
    -> collapse whitespace -> bounded_text. Raises Refused('TOOL_SCHEMA')."""
    if type(value) is not str or not 0 < len(value) <= 4 * max_chars:
        raise Refused('TOOL_SCHEMA')
    text = unicodedata.normalize('NFKC', value)
    if any(unicodedata.category(c) in _REFUSED_CATEGORIES for c in text):
        raise Refused('TOOL_SCHEMA')
    text = ''.join(c for c in text if unicodedata.category(c) != 'Cf' and c not in strip)
    # Removing a Cf character can create a new canonical
    # composition (e + ZWJ + U+0301 -> e + U+0301), so normalize again; NFKC is
    # idempotent, so the result is a fixed point and re-parses compare equal.
    text = unicodedata.normalize('NFKC', text)
    if any(c in strip or unicodedata.category(c) in _REFUSED_CATEGORIES or
           unicodedata.category(c) == 'Cf' for c in text):
        raise Refused('TOOL_SCHEMA')
    text = ' '.join(text.split())
    if not bounded_text(text, max_chars, max_bytes):
        raise Refused('TOOL_SCHEMA')
    return text


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate')
        result[key] = value
    return result


def _no_constant(_):
    raise ValueError('constant')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('float')
    return value


def strict_loads(raw, max_bytes, code='TOOL_SCHEMA'):
    """One JSON value, UTF-8 <= max_bytes, no duplicate keys, no NaN/Infinity."""
    if type(raw) is bytes:
        if len(raw) > max_bytes:
            raise Refused(code)
        try:
            raw = raw.decode('utf-8')
        except UnicodeDecodeError:
            raise Refused(code) from None
    elif type(raw) is str:
        if len(raw.encode('utf-8', errors='surrogatepass')) > max_bytes:
            raise Refused(code)
    else:
        raise Refused(code)
    try:
        return json.loads(raw, object_pairs_hook=_unique, parse_constant=_no_constant,
                          parse_float=_finite_float)
    except (ValueError, TypeError, RecursionError):
        raise Refused(code) from None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')
