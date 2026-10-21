"""Mutation checks (every validator mutant must be caught).

Copies agent/, tests/ and (when present) tests_adversarial/ to a scratch directory,
applies one source mutation at a time, runs both suites there and requires a
non-zero exit from at least one. The prototype tree
itself is never modified. Writes mutation-results.json beside this file.

    python3 -B mutation_check.py [--workdir DIR]
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent

# (name, file, old, new, expected occurrences); old/new/count may be parallel lists
MUTANTS = [
    ('int-accepts-bool', 'agent/registry.py',
     'if type(value) is int and self.lo <= value <= self.hi:',
     'if isinstance(value, int) and self.lo <= value <= self.hi:', 1),
    ('extra-top-level-keys-allowed', 'agent/parse.py',
     "if type(value) is not dict or set(value) != {'tool', 'args'}:",
     "if type(value) is not dict or not {'tool', 'args'} <= set(value):", 1),
    ('cf-characters-kept', 'agent/validate.py',
     "if unicodedata.category(c) != 'Cf' and c not in strip", 'if c not in strip', 1),
    ('controls-collapsed-not-refused', 'agent/validate.py',
     "_REFUSED_CATEGORIES = frozenset(('Cc', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'))",
     "_REFUSED_CATEGORIES = frozenset(('Cs',))", 1),
    ('bang-not-stripped', 'agent/registry.py', "Text(256, 1024, '!\\\\')", 'Text(256, 1024)', 1),
    ('ddg-allowlist-ignores-bangs', 'agent/router.py', ' and _no_bang(url)', '', 1),
    ('youtube-query-unencoded', 'agent/router.py', "YT_PREFIX + quote_plus(args['query'])",
     "YT_PREFIX + args['query'].replace(' ', '+')", 1),
    ('tv-route-over-handoff', 'agent/router.py', "if route.executor == 'tv' and not tv_routes:", 'if False:', 1),
    ('inconsistent-result-trusted', 'agent/executors/base.py',
     "    if result.state == 'refused' and result.dispatched:\n        return None\n", '', 1),
    ('unvalidated-result-is-success', 'agent/agent.py',
     "        if valid is None:\n            return Outcome('unknown', 'EXECUTION_UNCONFIRMED')",
     "        if valid is None:\n            return Outcome('accepted', 'BROWSER_NAVIGATION_ACCEPTED')", 1),
    ('lease-identity-unchecked', 'agent/agent.py',
     'if (self.busy or type(lease) is not Lease or self.active is not lease or',
     'if (self.busy or type(lease) is not Lease or', 1),
    ('lease-expiry-unchecked', 'agent/agent.py',
     'if lease.cancelled or self.clock() >= lease.expires:', 'if lease.cancelled:', 1),
    ('lease-replayable', 'agent/agent.py',
     ['            lease.consumed = True\n            self.busy = True',
      '                if self.active is lease:\n                    self.active = None\n'],
     ['            self.busy = True', ''], [1, 1]),
    ('gate-ignored', 'agent/executors/browser.py', 'blocked = gate()', 'blocked = None', 3),
    ('nav-ack-counted-as-success', 'agent/executors/browser.py',
     "lambda o: o['ready'] and o['matched'])", "lambda o: o['ready'])", 1),
    ('audit-records-free-text', 'agent/audit.py',
     "result[name] = {'kind': 'text', 'chars': len(value)} if spec.free_text else value",
     'result[name] = value', 1),
    ('killswitch-fail-open', 'agent/killswitch.py',
     '        if not self._dir_ok():\n            return True', '        if not self._dir_ok():\n            return False', 1),
    ('unknown-streak-never-trips', 'agent/policy.py', 'UNKNOWN_STREAK_TRIP = 3', 'UNKNOWN_STREAK_TRIP = 99', 1),
    ('rate-limits-off', 'agent/policy.py', 'if self._used(scope, window, now) + cost > limit:', 'if False:', 1),
    ('open-content-not-confirmed', 'agent/registry.py',
     "('text', Text(160, 640))), 'always',", "('text', Text(160, 640))), 'never',", 1),
    ('confirm-digest-skipped', 'agent/agent.py', 'if pending.plan.digest() != pending.digest:', 'if False:', 1),
    ('negation-guard-off', 'agent/fastpath.py',
     'return any(words[:len(p)] == p for p in _NEGATION_PREFIXES)', 'return False', 1),
    ('handoff-window-reusable', 'agent/bridge.py',
     '            self.armed = None\n            self.seen.add(attempt)', '            self.seen.add(attempt)', 1),
    ('handoff-stale-accepted', 'agent/bridge.py', 'not now - FRESH_S <= parsed <= now)', 'False)', 1),
    ('gateway-connects-without-key', 'agent/executors/tv_gateway.py',
     'key_doc = private_json(self.key_path)',
     "key_doc = private_json(self.key_path) if os.path.exists(self.key_path) else {'client-key': 'a' * 32}", 1),
    ('gateway-tolerates-pairing-prompt', 'agent/executors/tv_gateway.py',
     "if type(reply) is not dict or 'pairingType' in reply or reply.get('client-key') != key:",
     "if type(reply) is not dict or reply.get('returnValue') is False:", 1),
    ('launch-skips-installed-check', 'agent/executors/tv_gateway.py', 'if key not in installed:', 'if False:', 1),
    ('volume-continues-after-unknown', 'agent/executors/tv_gateway.py',
     "            if status != 'accepted':\n                return ExecResult('unknown', 'TV_UNCONFIRMED', True)  # stop: never send more\n",
     '', 1),
    # ---- adversarial mutants (caught by tests_adversarial/) ----
    ('nfkc-not-reapplied-after-cf-removal', 'agent/validate.py',
     "    text = unicodedata.normalize('NFKC', text)\n    if any(c in strip", '    if any(c in strip', 1),
    ('url-escapes-not-well-formed', 'agent/router.py',
     'if not _ESCAPES.fullmatch(tail):\n        return False', 'if False:\n        return False', 1),
    ('url-single-decode-only', 'agent/router.py',
     '        decoded = unquote(text)\n        if decoded == text:\n            return True', '        return True', 1),
    ('url-decoded-format-chars-allowed', 'agent/router.py',
     'if any(unicodedata.category(c) in _REFUSED_DECODED for c in text):\n        return False',
     'if False:\n        return False', 1),
    ('nav-generation-unchecked', 'agent/executors/browser.py',
     "return obs if obs is not None and obs['generation'] > before['generation'] else None", 'return obs', 1),
    ('arm-published-before-audit', 'agent/bridge.py',
     '            self.arming = True\n',
     '            self.arming = True\n            self.armed = (attempt, opened)\n', 1),
    ('attempt-rearmable-after-expiry', 'agent/bridge.py',
     '            self.seen.add(attempt)\n            opened = self.clock()',
     '            opened = self.clock()', 1),
    ('attempt-unvalidated', 'agent/agent.py',
     'attempt_ok = attempt is None or (type(attempt) is str and _ATTEMPT.fullmatch(attempt) is not None)',
     'attempt_ok = True', 1),
    ('pause-keeps-pending-confirmation', 'agent/agent.py',
     "                self.confirm.take()  # a pause also drops any pending plan\n"
     "                raise Refused('AGENT_PAUSED')", "                raise Refused('AGENT_PAUSED')", 1),
    ('deeplink-params-unbounded', 'agent/executors/tv_gateway.py',
     '_flat_params(params) and builder.validate(params) is True)', 'builder.validate(params) is True)', 1),
    ('open-content-text-unchecked', 'agent/executors/tv_gateway.py',
     "params['kind'] in ('search', 'title') and bounded_text(params['text'], 160, 640))", 'True)', 1),
    # ---- TV-aware warm policy (agent/warm.py; caught by tests/test_warm.py) ----
    ('warm-unload-under-inflight', 'agent/warm.py',
     '        if self.inflight:\n            return  # never unload under an utterance: finish it first\n', '', 1),
    ('warm-over-unverified-unload', 'agent/warm.py',
     ["        if self.state == 'warming' or not self.clean:\n",
      "        if not self.clean:\n            self.clean = self._unload()\n            if not self.clean:\n"
      "                self._fail(now, 'UNLOAD_UNVERIFIED')\n                return\n"],
     ["        if self.state == 'warming':\n", ''], [1, 1]),
    ('warm-backoff-never-escalates', 'agent/warm.py',
     'delay = BACKOFF_S[min(self.failures, len(BACKOFF_S)) - 1]', 'delay = BACKOFF_S[0]', 1),
    ('warm-starts-on-unknown', 'agent/warm.py',
     "return self.unknown_since is None and self.known == 'active'", "return self.known == 'active'", 1),
    ('warm-stale-reading-trusted', 'agent/warm.py', ' or at > now or now - at > STALE_S:', ' or at > now:', 1),
    ('warm-unknown-hold-never-expires', 'agent/warm.py',
     'if now - self.unknown_since >= UNKNOWN_HOLD_S:', 'if False:', 1),
    ('warm-clock-backwards-accepted', 'agent/warm.py',
     '(self.high_water is not None and now < self.high_water)', 'False', 1),
    ('warm-press-admitted-while-warming', 'agent/warm.py',
     'if self.state in _LOADED:', "if self.state in _LOADED + ('warming',):", 1),
    ('warm-grace-counted-from-warm-done', 'agent/warm.py',
     "                self.grace_until = self.inactive_since + GRACE_S\n                self._row('grace', 'WARM_DONE')",
     "                self.grace_until = now + GRACE_S\n                self._row('grace', 'WARM_DONE')", 1),
    ('agent-warm-not-consulted', 'agent/agent.py',
     '                    ticket = self.warm.admit()\n', '                    ticket = None\n', 1),
    ('agent-cancel-releases-running-utterance', 'agent/agent.py',
     '                if not self.active.consumed:\n                    self._release(self.active)  # a running',
     '                if True:\n                    self._release(self.active)  # a running', 1),
    ('agent-ticket-never-released', 'agent/agent.py',
     '                self._release(lease)\n                if self.active is lease:',
     '                if self.active is lease:', 1),
    # ---- warm-policy races (caught by tests_adversarial/test_adv_warm.py) ----
    ('warm-huge-timestamp-crashes-tick', 'agent/warm.py',
     '        at = _seconds(at)\n        if at is None or at > now',
     '        if type(at) not in (int, float) or not math.isfinite(at) or at > now', 1),
    ('warm-huge-clock-crashes-tick', 'agent/warm.py',
     '        now = _seconds(self.clock())\n        if now is None or (',
     '        now = self.clock()\n        if type(now) not in (int, float) or not math.isfinite(now) or (', 1),
    ('warm-pool-call-under-the-press-lock', 'agent/warm.py',
     '        self.lock.release()\n        try:\n            return getattr(self.pool, name)()\n'
     '        finally:\n            self.lock.acquire()',
     '        return getattr(self.pool, name)()', 1),
    ('warm-ticks-not-serialized', 'agent/warm.py', 'with self.ticking, self.lock:', 'with self.lock:', 1),
    ('tv-final-send-gate-removed', 'agent/executors/tv_gateway.py',
     '            self._check_gate(gate)\n            if kind == \'write\':',
     '            if kind == \'write\':', 1),
    ('tv-connect-register-gates-removed', 'agent/executors/tv_gateway.py',
     '            self._check_gate(gate, close_on_refusal=True)\n', '', 2),
    ('tv-launch-current-gate-not-forwarded', 'agent/executors/tv_gateway.py',
     "return self._send('launch', {'id': APP_IDS[key]}, op_id, gate=gate)",
     "return self._send('launch', {'id': APP_IDS[key]}, op_id)", 1),
    ('tv-execution-deadline-unchecked', 'agent/executors/tv_gateway.py',
     "return owner_gate() or ('REQUEST_EXPIRED' if self.clock() >= deadline else None)",
     'return owner_gate()', 1),
    ('browser-end-postread-gate-removed', 'agent/executors/browser.py',
     "        blocked = gate() or ('REQUEST_EXPIRED' if self.clock() >= deadline else None)\n"
     "        if blocked is not None:\n            return ExecResult('refused', blocked, False)\n",
     '', 1),
    ('unshown-confirmation-retained', 'agent/agent.py',
     "            if outcome.code == 'CONFIRMATION_PENDING':\n", '            if False:\n', 1),
    ('arm-audit-renews-window', 'agent/bridge.py',
     '                if self.clock() - opened >= ARM_WINDOW_S:', '                if False:', 1),
    ('audit-persists-by-default', 'agent/audit.py',
     'retention_days=30, persistent=False):', 'retention_days=30, persistent=True):', 1),
]
SUITES = ('tests', 'tests_adversarial')


def run(workdir):
    results = []
    for name, rel, olds, news, counts in MUTANTS:
        if isinstance(olds, str):
            olds, news, counts = [olds], [news], [counts]
        source = (HERE / rel).read_text()
        found = [source.count(old) for old in olds]
        if found != list(counts):
            results.append({'mutant': name, 'applied': False, 'occurrences': found, 'rejected': False})
            continue
        for old, new in zip(olds, news):
            source = source.replace(old, new)
        target = Path(tempfile.mkdtemp(prefix=name + '-', dir=workdir))
        shutil.copytree(HERE / 'agent', target / 'agent')
        suites = [suite for suite in SUITES if (HERE / suite).is_dir()]
        for suite in suites:
            shutil.copytree(HERE / suite, target / suite)
        (target / rel).write_text(source)
        env = dict(os.environ, R2D2_CONTROLLER_DIR=str(HERE.parents[1] / 'backend'))
        exits, tails = {}, {}
        for suite in suites:
            proc = subprocess.run([sys.executable, '-B', '-m', 'unittest', 'discover', '-s', suite, '-t', '.'],
                                  cwd=target, capture_output=True, text=True, timeout=300, env=env)
            exits[suite] = proc.returncode
            tails[suite] = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else ''
        results.append({'mutant': name, 'applied': True, 'exit': exits,
                        'rejected': any(code != 0 for code in exits.values()), 'summary': tails})
        shutil.rmtree(target, ignore_errors=True)
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workdir', default=None)
    args = parser.parse_args()
    workdir = args.workdir or tempfile.mkdtemp(prefix='r2d2-agent-mutants-')
    Path(workdir).mkdir(parents=True, exist_ok=True)
    results = run(workdir)
    (HERE / 'mutation-results.json').write_text(json.dumps(results, indent=2) + '\n')
    caught = sum(r['rejected'] for r in results)
    print('mutants caught: %d/%d' % (caught, len(results)))
    for r in results:
        if not r['rejected']:
            print('NOT CAUGHT:', r['mutant'], r)
    return 0 if caught == len(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
