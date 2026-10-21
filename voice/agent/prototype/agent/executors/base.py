"""Executor contract shared by the browser and TV executors.

    executor.run(plan, *, gate, deadline) -> ExecResult

  * `plan` is a router.Plan; the executor re-validates it against its own table.
  * `gate()` must be called immediately before every mutation. It returns None
    to proceed, or a refusal code (AGENT_PAUSED, REQUEST_EXPIRED,
    TV_CHANNEL_UNAVAILABLE); the executor then must not dispatch and returns
    refused/<code> (or unknown if an earlier step of the same plan was sent).
  * `deadline` is a clock() value; no call may outlive it.
  * A mutation is never retried. Once anything was dispatched, the result may
    not be 'refused'. Failures after dispatch are 'unknown'.
  * `display` carries enumerated values for the TV surface only (never audited).

The agent validates every ExecResult against RESULTS below; anything else,
including an exception, becomes unknown/EXECUTION_UNCONFIRMED (never success).
"""
from dataclasses import dataclass

from ..errors import STATES


@dataclass(frozen=True)
class ExecResult:
    state: str
    code: str
    dispatched: bool
    display: tuple = ()


_PRE = ('AGENT_PAUSED', 'REQUEST_EXPIRED', 'TOOL_SCHEMA')  # TOOL_SCHEMA: executor's own plan re-check failed
_TV_PRE = _PRE + ('TV_CHANNEL_UNAVAILABLE', 'TV_PERMISSION_DENIED', 'TV_TOOL_UNAVAILABLE', 'RATE_LIMITED')
RESULTS = {
    ('browser', 'navigate'): {'accepted': {'BROWSER_NAVIGATION_ACCEPTED'},
                              'rejected': {'NAVIGATION_REJECTED'},
                              'refused': {'BROWSER_NOT_OPENED', *_PRE},
                              'unknown': {'NAVIGATION_UNCONFIRMED'}},
    ('browser', 'history'): {'accepted': {'BROWSER_HISTORY_ACCEPTED'},
                             'rejected': {'HISTORY_REJECTED'},
                             'refused': {'BROWSER_NOT_OPENED', *_PRE},
                             'unknown': {'HISTORY_UNCONFIRMED'}},
    ('browser', 'media'): {'verified': {'BROWSER_MEDIA_VERIFIED'},
                           'rejected': {'MEDIA_REJECTED'},
                           'refused': {'BROWSER_NOT_OPENED', 'BROWSER_MEDIA_NO_VIDEO', *_PRE},
                           'unknown': {'MEDIA_UNCONFIRMED'}},
    ('browser', 'end'): {'verified': {'BROWSER_SESSION_ENDED'},
                         'accepted': {'BROWSER_SESSION_ENDED'},
                         'rejected': {'END_REJECTED'},
                         'refused': {'BROWSER_NOT_OPENED', *_PRE},
                         'unknown': {'END_UNCONFIRMED'}},
    ('browser', 'observe'): {'accepted': {'BROWSER_STATUS_SHOWN'},
                             'refused': {'BROWSER_NOT_OPENED'}},
    ('tv', 'launch_app'): {'verified': {'TV_FOREGROUND_VERIFIED'},
                           'rejected': {'TV_LAUNCH_REJECTED'},
                           'refused': {'TV_APP_NOT_INSTALLED', *_TV_PRE},
                           'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'home'): {'verified': {'TV_FOREGROUND_VERIFIED'},
                     'rejected': {'TV_LAUNCH_REJECTED'},
                     'refused': set(_TV_PRE),
                     'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'open_content'): {'verified': {'TV_FOREGROUND_VERIFIED'},
                             'rejected': {'TV_LAUNCH_REJECTED'},
                             'refused': {'DEEPLINK_UNQUALIFIED', 'TV_APP_NOT_INSTALLED', *_TV_PRE},
                             'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'volume'): {'verified': {'TV_VOLUME_VERIFIED'},
                       'accepted': {'TV_VOLUME_ACCEPTED'},
                       'rejected': {'TV_REQUEST_REJECTED'},
                       'refused': set(_TV_PRE),
                       'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'mute'): {'verified': {'TV_MUTE_VERIFIED'},
                     'rejected': {'TV_REQUEST_REJECTED'},
                     'refused': set(_TV_PRE),
                     'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'media'): {'accepted': {'TV_MEDIA_ACCEPTED'},
                      'rejected': {'TV_REQUEST_REJECTED'},
                      'refused': set(_TV_PRE),
                      'unknown': {'TV_UNCONFIRMED'}},
    ('tv', 'foreground'): {'accepted': {'TV_STATUS_SHOWN'},
                           'refused': set(_TV_PRE)},
}
assert all(set(table) <= set(STATES) for table in RESULTS.values())


def checked(plan, result):
    """-> (state, code, dispatched, display) or None when the result is not a
    consistent ExecResult for this plan (caller maps None to unknown)."""
    if type(result) is not ExecResult or type(result.dispatched) is not bool:
        return None
    allowed = RESULTS.get((plan.executor, plan.action), {})
    if result.code not in allowed.get(result.state, ()):
        return None
    if result.state == 'refused' and result.dispatched:
        return None
    if plan.action == 'end' and result.state == 'verified' and plan.param('reason') != 'close':
        return None  # tv.home via browser cannot verify Home foreground (no TV read in P1b)
    if type(result.display) is not tuple:
        return None
    return result.state, result.code, result.dispatched, result.display
