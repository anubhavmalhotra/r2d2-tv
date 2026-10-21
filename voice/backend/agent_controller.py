"""14-tool Voice Agent adapter for VoiceIngress and SharedVoiceOwner.

Wraps the verified 14-tool Agent (browser.open_site, browser.web_search,
browser.youtube_search, browser.back, browser.close, tv.open_app,
tv.open_content, tv.home, tv.volume_up, tv.volume_down, tv.mute,
media.play_pause, status.whats_playing, meta.cancel / meta.unsupported)
behind the Controller(arm, cancel, execute) contract. Audit persistence is
disabled by default (persistent=False); no transcript or free text ever
reaches disk.
"""
from pathlib import Path
import sys
import threading
import time

if __package__:
    from .controller import Outcome, Refused
else:
    from controller import Outcome, Refused


def _load_agent_package():
    here = Path(__file__).resolve().parent
    if __package__ and (here / 'agent' / 'agent.py').is_file():
        from .agent.agent import Agent
        from .agent.audit import AuditLog
        from .agent.errors import Refused as AgentRefused
        from .agent.executors.browser import BrowserExecutor
        from .agent.executors.feedback import Feedback
        from .agent.executors.tv_gateway import TVExecutor
        from .agent.killswitch import KillSwitch
        from .agent.parse import legacy_to_call, parse_legacy_intent
        from .agent.registry import prompt_sha256, render_prompt
        return (Agent, AuditLog, AgentRefused, BrowserExecutor, Feedback,
                TVExecutor, KillSwitch, legacy_to_call, parse_legacy_intent,
                prompt_sha256, render_prompt)
    candidates = (
        here.parent / 'agent' / 'prototype',
        here.parents[1] / 'voice' / 'agent' / 'prototype',
    )
    for path in candidates:
        if (path / 'agent' / 'agent.py').is_file():
            entry = str(path)
            if entry not in sys.path:
                sys.path.insert(0, entry)
            break
    from agent.agent import Agent
    from agent.audit import AuditLog
    from agent.errors import Refused as AgentRefused
    from agent.executors.browser import BrowserExecutor
    from agent.executors.feedback import Feedback
    from agent.executors.tv_gateway import TVExecutor
    from agent.killswitch import KillSwitch
    from agent.parse import legacy_to_call, parse_legacy_intent
    from agent.registry import prompt_sha256, render_prompt
    return (Agent, AuditLog, AgentRefused, BrowserExecutor, Feedback,
            TVExecutor, KillSwitch, legacy_to_call, parse_legacy_intent,
            prompt_sha256, render_prompt)


class InMemoryKillSwitch:
    """Zero-disk kill switch for ephemeral sessions when no config_dir is supplied."""
    def __init__(self):
        self._lock = threading.Lock()
        self._paused = False
        self._tv_off = False
        self.last_reason = None

    def paused(self):
        with self._lock:
            return self._paused

    def tv_off(self):
        with self._lock:
            return self._tv_off

    def pause(self, reason='PAUSED'):
        with self._lock:
            self._paused = True
            self.last_reason = reason

    def set_tv_off(self, reason='TV_OFF'):
        with self._lock:
            self._tv_off = True
            self.last_reason = reason


class PinnedIntentModel:
    """Bridges LocalIntentAdapter / WorkerIntent to the Agent's pinned prompt contract.

    Configures prompt-aware providers (`LocalQwen`, `NativeQwenIntent`, `ModelWorker`,
    `WorkerIntent`, `LocalIntentAdapter`) with the phase's rendered 14-tool prompt
    before startup, and verifies `inner.prompt_sha256` rather than stamping a
    computed SHA over a mismatched prompt.
    """
    def __init__(self, inner, *, phase='P5', parse_legacy=None, to_call=None,
                 sha_fn=None, render_fn=None):
        if not callable(getattr(inner, 'interpret', None)):
            raise Refused('MODEL_CONFIG')
        if sha_fn is None or parse_legacy is None or to_call is None or render_fn is None:
            (_, _, _, _, _, _, _, to_call, parse_legacy, sha_fn, render_fn) = _load_agent_package()
        self.inner = inner
        self._parse_legacy = parse_legacy
        self._to_call = to_call
        expected_prompt = render_fn(phase)
        expected_sha = sha_fn(phase)
        configure = getattr(inner, 'configure_prompt', None)
        if callable(configure) and getattr(inner, 'prompt_sha256', None) != expected_sha:
            configure(expected_prompt, phase=phase)
        inner_sha = getattr(inner, 'prompt_sha256', None)
        if inner_sha is not None and inner_sha != expected_sha:
            raise Refused('MODEL_CONFIG')
        self.prompt_sha256 = inner_sha if inner_sha is not None else expected_sha

    def interpret(self, transcript, *, timeout_s):
        raw = self.inner.interpret(transcript, timeout_s=timeout_s)
        if type(raw) is str and '"action"' in raw and '"tool"' not in raw:
            try:
                return self._to_call(self._parse_legacy(raw)).as_json()
            except Exception:
                return raw
        return raw


class AgentController:
    """Drop-in Controller replacement backed by the 14-tool Agent."""
    def __init__(self, model, runtime=None, *, phase='P5', tv_gateway=None,
                 config_dir=None, audit_dir=None, overlay=None,
                 clock=time.monotonic, wait=time.sleep, auto_open=True,
                 feedback=None, voice_status=None):
        (Agent, AuditLog, AgentRefused, BrowserExecutor, Feedback,
         TVExecutor, KillSwitch, to_call, parse_legacy, sha_fn, render_fn) = _load_agent_package()
        self._AgentRefused = AgentRefused
        self.runtime = runtime
        self.clock, self.wait = clock, wait
        self.auto_open = bool(auto_open)
        gateway_ks = getattr(tv_gateway, 'killswitch', None) if tv_gateway is not None else None
        if config_dir is not None:
            self.killswitch = KillSwitch(config_dir)
        elif gateway_ks is not None:
            self.killswitch = gateway_ks
        else:
            self.killswitch = InMemoryKillSwitch()
        if tv_gateway is not None and gateway_ks is not self.killswitch:
            if gateway_ks is None:
                tv_gateway.killswitch = self.killswitch
            else:
                prev_trip = getattr(tv_gateway, 'on_trip', None)
                def _forward_trip(reason, _prev=prev_trip, _ks=self.killswitch):
                    _ks.set_tv_off(reason)
                    if reason in ('PAIRING_PROMPT', 'CERT_CHANGED'):
                        _ks.pause(reason)
                    if callable(_prev):
                        _prev(reason)
                tv_gateway.on_trip = _forward_trip
        self.audit = AuditLog(audit_dir or Path('/nonexistent/ephemeral-audit'), persistent=False)
        self.pinned_model = PinnedIntentModel(
            model, phase=phase, parse_legacy=parse_legacy, to_call=to_call,
            sha_fn=sha_fn, render_fn=render_fn)
        self.browser = (BrowserExecutor(runtime, clock=clock, wait=wait)
                        if runtime is not None else None)
        if self.browser is not None:
            base_owner_open = self.browser._owner_open
            def _owner_open_with_auto_launch(deadline, _base=base_owner_open):
                nonce = getattr(self, '_current_nonce', None)
                is_current = getattr(self, '_current_check', None)
                exec_deadline = getattr(self, '_current_exec_deadline', None)
                if type(exec_deadline) is list and exec_deadline:
                    exec_deadline[0] = max(exec_deadline[0], deadline)
                if self.auto_open and self.runtime is not None and hasattr(self.runtime, 'arm_auto_open') and nonce:
                    self.runtime.arm_auto_open(nonce + ':start', is_current=is_current, nonce=nonce)
                try:
                    return _base(deadline)
                finally:
                    if self.runtime is not None and hasattr(self.runtime, 'clear_auto_open'):
                        self.runtime.clear_auto_open(nonce, keep_invocation=True)
            self.browser._owner_open = _owner_open_with_auto_launch
        self.tv = (TVExecutor(tv_gateway, clock=clock, wait=wait)
                   if tv_gateway is not None else None)
        # A supplied feedback (the resident StatusFeedback) replaces the SSAP toast.
        self.feedback = feedback if feedback is not None else Feedback(
            phase, overlay=overlay, toast=self.tv.toast if self.tv is not None else None)
        self.voice_status = voice_status
        self._event_lease = None
        self.agent = Agent(
            phase=phase, audit=self.audit, killswitch=self.killswitch,
            model=self.pinned_model, browser=self.browser, tv=self.tv,
            feedback=self.feedback, clock=clock,
            events=self._agent_event if voice_status is not None else None)
        self._current_nonce = None
        self._current_check = None
        self._current_exec_deadline = None

    def arm(self):
        try:
            lease = self.agent.arm()
        except self._AgentRefused as error:
            if error.code == 'AGENT_BUSY':
                raise Refused('CONTROLLER_BUSY') from None
            raise Refused(error.code) from None
        self._event_lease = lease
        return lease

    def _agent_event(self, lease, kind, fields):
        # Only the latest armed lease reaches the UI; a cancelled one never shows "doing".
        status = self.voice_status
        if status is None or lease is None or lease is not self._event_lease:
            return
        if kind == 'doing' and not getattr(lease, 'cancelled', False):
            status.doing(fields.get('tool'), fields.get('slot'))
        elif kind == 'outcome':
            status.agent_outcome(fields.get('state'), fields.get('code'), fields.get('tool'),
                                 fields.get('slot'), fields.get('rule'), fields.get('ttl'))

    def cancel(self, lease=None):
        cancelled = self.agent.cancel(lease)
        if cancelled and self.runtime is not None and hasattr(self.runtime, 'clear_auto_open'):
            nonce = getattr(lease, 'nonce', None)
            self.runtime.clear_auto_open(nonce, keep_invocation=True)
        return cancelled

    def execute(self, lease, transcript):
        if type(transcript) is not str or not (0 < len(transcript) <= 1024 and len(transcript.encode('utf-8', errors='surrogatepass')) <= 4096 and transcript == transcript.strip()):
            self.cancel(lease)
            return Outcome('refused', 'TRANSCRIPT_SCOPE')
        if not getattr(self.runtime, 'browser_enabled', False) and self.tv is None:
            self.cancel(lease)
            return Outcome('refused', 'BROWSER_BRIDGE_UNQUALIFIED')
        nonce = getattr(lease, 'nonce', None)
        # Agent._steps enforces the 30s admission lease before and after model inference;
        # once admitted into executor.run(), browser startup + navigation permits up to
        # OWNER_OPEN_WINDOW_S (140s) + EXEC_SLACK_S (15s) = 155s while still honoring cancel/pause.
        exec_deadline = [max(getattr(lease, 'expires', 0), self.clock() + 155.0)]
        is_current = lambda: (
            not getattr(lease, 'cancelled', False)
            and not self.killswitch.paused()
            and self._current_nonce == nonce
            and self.clock() <= exec_deadline[0]
        )
        self._current_nonce = nonce
        self._current_check = is_current
        self._current_exec_deadline = exec_deadline
        if self.runtime is not None and hasattr(self.runtime, 'bind_invocation') and nonce:
            self.runtime.bind_invocation(nonce, is_current=is_current)
        try:
            result = self.agent.handle_utterance(lease, transcript)
            if getattr(lease, 'cancelled', False):
                dispatched = (self.runtime is not None and hasattr(self.runtime, 'was_dispatched') and
                              self.runtime.was_dispatched(nonce))
                if dispatched:
                    return Outcome('unknown', 'EXECUTION_UNCONFIRMED')
                if result.state != 'refused':
                    return Outcome('refused', 'REQUEST_EXPIRED')
            return Outcome(result.state, result.code)
        finally:
            if self._current_nonce == nonce:
                self._current_nonce = None
                self._current_check = None
                self._current_exec_deadline = None
            if self.runtime is not None and hasattr(self.runtime, 'clear_auto_open'):
                self.runtime.clear_auto_open(nonce)
