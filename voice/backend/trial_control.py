"""Main-thread bridge to the private browser channel; no listener or I/O on import.

The channel thread can only queue the ordinary navigation operation. CDP remains
on Trial's main thread. Browser ownership and selected URL observations cannot
prove TV launch, decoded frames, or physical presentation.
"""
import time


def service_trial_control(host, trial, page, *, clock=time.monotonic):
    check_close = getattr(trial, 'check_agent_close', None)
    if callable(check_close):
        check_close()

    def ready():
        observed = trial.owner_verified_at
        return (trial.page_ready is True and not trial.stop.is_set() and
                not getattr(trial, 'agent_close_requested', False) and
                trial.navigation_pending is None and not trial.navigation_active and
                type(observed) in (int, float) and 0 <= clock() - observed <= 1 and
                trial.sandbox is not None and trial.sandbox.get('passed') is True)

    with trial.lock:
        admitted, epoch = ready(), trial.input_epoch

    def check(target):
        # Only the caller's already-owned selected CDP page can supply this bit.
        # A concurrent remote navigation invalidates the observation before it
        # reaches the channel; no URL or page content is added to trial reports.
        try:
            matched = page.voice_target_matches(target) is True
        except Exception:
            # A partial CDP exchange cannot be retried as if the channel were
            # clean. End this capability and let Trial's existing finally
            # perform its ordinary owned-session cleanup.
            host.close()
            raise
        with trial.lock:
            return matched and ready() and trial.input_epoch == epoch

    def probe():
        fn = getattr(page, 'voice_surface_probe', None)
        if not callable(fn):
            return {'surfaceClass': 'browser_other', 'video': {'count': 0, 'paused': None}}
        try:
            value = fn()
        except Exception:
            host.close()
            raise
        with trial.lock:
            if not (ready() and trial.input_epoch == epoch):
                return {'surfaceClass': 'browser_other', 'video': {'count': 0, 'paused': None}}
        return value

    host.publish(check, ready=admitted, probe=probe)
