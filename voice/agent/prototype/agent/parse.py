"""Strict tool-call parsing (validation layer 1) and the legacy 4-intent map."""
from dataclasses import dataclass
import json
from types import MappingProxyType

from .errors import Refused
from .registry import REGISTRY
from .validate import bounded_text, strict_loads

MAX_CALL_BYTES = 2048


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: MappingProxyType  # normalized, validated values only

    def as_json(self):
        return json.dumps({'tool': self.tool, 'args': dict(self.args)},
                          ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def check_call(value, *, allow_internal=False):
    """Validate an already-decoded {tool, args} object against the registry."""
    if type(value) is not dict or set(value) != {'tool', 'args'}:
        raise Refused('TOOL_SCHEMA')
    name, args = value['tool'], value['args']
    tool = REGISTRY.get(name) if type(name) is str else None
    if tool is None or (not tool.model_facing and not allow_internal):
        raise Refused('TOOL_SCHEMA')
    spec = tool.spec
    if type(args) is not dict or set(args) != set(spec):
        raise Refused('TOOL_SCHEMA')
    return ToolCall(name, MappingProxyType({k: spec[k].check(args[k]) for k in spec}))


def parse_tool_call(raw):
    """Model or handoff output -> ToolCall. Every failure is TOOL_SCHEMA.

    Phase gating is separate (TOOL_DISABLED), so a known-but-disabled tool is
    distinguishable from an invented one.
    """
    if type(raw) is not str:  # model text only (local_qwen returns str)
        raise Refused('TOOL_SCHEMA')
    return check_call(strict_loads(raw, MAX_CALL_BYTES))


# --------------------------------------------------------------------- legacy
def parse_legacy_intent(raw):
    """Same accept set as voice/controller.py parse_intent."""
    if type(raw) is not str:
        raise Refused('TOOL_SCHEMA')
    try:
        value = strict_loads(raw, MAX_CALL_BYTES)
    except Refused:
        raise Refused('TOOL_SCHEMA') from None
    if type(value) is not dict:
        raise Refused('TOOL_SCHEMA')
    action = value.get('action')
    if action == 'browser_open' and set(value) == {'action', 'destination'}:
        if value['destination'] == 'youtube':
            return value
    elif action == 'tv_content_open' and set(value) == {'action', 'provider', 'title'}:
        if value['provider'] == 'apple_tv' and bounded_text(value['title']):
            return value
    elif action == 'web_search' and set(value) == {'action', 'query'}:
        if bounded_text(value['query'], 256, 1024):
            return value
    elif action == 'unsupported' and set(value) == {'action'}:
        return value
    raise Refused('TOOL_SCHEMA')


def legacy_to_call(intent):
    """Legacy intent -> tool call. Output is re-validated by check_call."""
    action = intent['action']
    if action == 'browser_open':
        value = {'tool': 'browser.open_site', 'args': {'site': 'youtube'}}
    elif action == 'web_search':
        value = {'tool': 'browser.web_search', 'args': {'query': intent['query']}}
    elif action == 'tv_content_open':
        value = {'tool': 'tv.open_content',
                 'args': {'app': 'apple_tv', 'kind': 'title', 'text': intent['title']}}
    else:
        value = {'tool': 'meta.unsupported', 'args': {}}
    return check_call(value)
