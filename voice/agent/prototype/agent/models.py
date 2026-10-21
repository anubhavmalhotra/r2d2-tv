"""Model adapters. The agent depends on one method only, the same signature as
voice/backend/local_qwen.py (LocalQwen.interpret):

    interpret(transcript: str, *, timeout_s: float) -> str   # raw model text

It may raise anything; the agent maps every failure to refused/MODEL_UNAVAILABLE
before any dispatch. The returned text is untrusted data and is strictly parsed.

Two contracts exist:
  * 'registry' (P1b+): the model emits {"tool","args"} under render_prompt(phase).
  * 'legacy'   (P1a):  the unchanged 4-intent model (LocalQwen as shipped) whose
    output LegacyIntentAdapter converts with parse.legacy_to_call.
"""
import json

from .errors import Refused
from .fastpath import normalize
from .parse import legacy_to_call, parse_legacy_intent
from .registry import prompt_sha256


class FakeModel:
    """Deterministic test model: a lookup from normalized phrase to raw output.
    Unknown phrases give meta.unsupported. Counts calls; stores no transcript."""
    def __init__(self, table=None, *, default='{"tool":"meta.unsupported","args":{}}', phase=None):
        self.table = {normalize(k): v if isinstance(v, str) else json.dumps(v)
                      for k, v in (table or {}).items()}
        self.default = default
        self.calls = 0
        self.prompt_sha256 = prompt_sha256(phase) if phase else None

    def interpret(self, transcript, *, timeout_s):
        if type(timeout_s) not in (int, float) or not 0 < timeout_s <= 5:
            raise Refused('MODEL_CONFIG')
        self.calls += 1
        return self.table.get(normalize(transcript), self.default)


class ScriptedModel:
    """Returns one fixed raw string (or raises) regardless of input."""
    def __init__(self, raw=None, exc=None, prompt_sha=None):
        self.raw, self.exc, self.calls = raw, exc, 0
        self.prompt_sha256 = prompt_sha

    def interpret(self, transcript, *, timeout_s):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.raw


class LegacyIntentAdapter:
    """Drop-in for the unchanged 4-intent LocalQwen (P1a). The inner output is
    strictly parsed with the controller.parse_intent rules, mapped by legacy_to_call and
    re-serialized as {"tool","args"}, which the agent then re-parses again."""
    def __init__(self, inner):
        if not callable(getattr(inner, 'interpret', None)):
            raise Refused('MODEL_CONFIG')
        self.inner = inner
        self.prompt_sha256 = None  # legacy prompt is the inner model's own pinned SYSTEM

    def interpret(self, transcript, *, timeout_s):
        raw = self.inner.interpret(transcript, timeout_s=timeout_s)
        try:
            return legacy_to_call(parse_legacy_intent(raw)).as_json()
        finally:
            raw = None


def check_pinned(model, phase, *, legacy_allowed):
    """Refuse MODEL_CONFIG unless the model declares the prompt pinned for `phase`
    (registry contract) or is the legacy adapter where allowed (P1a)."""
    if isinstance(model, LegacyIntentAdapter):
        if not legacy_allowed:
            raise Refused('MODEL_CONFIG')
        return
    if getattr(model, 'prompt_sha256', None) != prompt_sha256(phase):
        raise Refused('MODEL_CONFIG')
