"""Deterministic grammar that runs before the model.

Exact whole-utterance matches only. Available from P2 on [R]: before P2 the
transcript never reaches the agent.
"""
import unicodedata


def normalize(transcript):
    text = unicodedata.normalize('NFKC', transcript).casefold()
    text = ''.join(' ' if unicodedata.category(c)[0] in 'PSZC' else c for c in text
                   if c not in "'’")  # "don't" -> "dont"
    return ' '.join(text.split())


_CANCEL = frozenset(normalize(p) for p in ('cancel', 'stop', 'never mind', 'nevermind'))
_YES = frozenset(normalize(p) for p in ('yes', 'yes please', 'confirm', 'do it', 'ok', 'okay'))
_NO = frozenset(normalize(p) for p in ('no', 'no thanks', "don't"))


def match(transcript):
    """-> ('cancel', None) | ('confirm', 'yes'|'no') | None"""
    key = normalize(transcript)
    if key in _CANCEL:
        return ('cancel', None)
    if key in _YES:
        return ('confirm', 'yes')
    if key in _NO:
        return ('confirm', 'no')
    return None


# Leading-negation guard. Mapping negated commands to meta.unsupported is otherwise
# left to the model; this deterministic guard refuses the unambiguous leading forms
# before the model is asked, so a model that drops the negation cannot turn
# "don't open Netflix" into a launch.
_NEGATION_PREFIXES = tuple(tuple(normalize(p).split()) for p in (
    "don't", 'do not', 'dont', 'never', 'please do not', "please don't",
    "no don't", 'no do not', "i don't want", 'i do not want', "let's not", 'stop opening',
))


def negated(transcript):
    words = tuple(normalize(transcript).split())
    return any(words[:len(p)] == p for p in _NEGATION_PREFIXES)
