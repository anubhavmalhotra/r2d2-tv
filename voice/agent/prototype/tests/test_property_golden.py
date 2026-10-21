"""Property test (10,000 adversarial outputs -> zero plans outside the
registry, zero URLs/app IDs not built from the tables) and the golden phrase set."""
import collections
import json
import random
import unittest

from agent.errors import Refused
from agent.fastpath import normalize
from agent.golden import CATEGORIES, GOLDEN, NO_ACTION, evaluate, expected_for, golden_model
from agent.models import ScriptedModel
from agent.parse import parse_tool_call
from agent.registry import APP_IDS, HOME_ID, MODEL_FACING, PHASES, REGISTRY, prompt_sha256
from agent.router import GOOGLE_PREFIX, SURFACES, YT_PREFIX, allowed_url, plan, site_urls
from tests.support import Case

HOSTILE_STRINGS = ['https://evil.example', 'http://192.0.2.20:3001', 'file:///etc/passwd', 'javascript:alert(1)',
                   'com.webos.app.settings', 'com.webos.app.tinybrowser', 'local.owner.applibrary',
                   'luna://com.webos.service.applicationmanager/launch', 'ssap://system/turnOff', 'rm -rf /',
                   '$(reboot)', '`id`', '; ls', '!g cats', '\\evil', '！bang', 'a‮b', '​', 'x\ny',
                   'netflix', 'youtube', 'duckduckgo', 'apple_tv', 'on', 'up', 'yes', 'toggle', 'title', '',
                   ' ', 'A' * 300, '\U0001F600' * 260, '%21g', '&kp=-1', '#frag', '\ud800']
TOOL_NAMES = list(REGISTRY) + ['shell.exec', 'browser.navigate', 'tv.launch', 'system.turnOff',
                               'Browser.open_site', 'tv.open_app ', '', None, 7]
KEYS = ['site', 'query', 'action', 'app', 'kind', 'text', 'direction', 'steps', 'state', 'answer', 'url', 'id',
        'params', 'cmd', 'contentTarget']


def random_value(rng, depth=0):
    roll = rng.random()
    if roll < 0.45:
        return rng.choice(HOSTILE_STRINGS)
    if roll < 0.6:
        return rng.choice([0, 1, 2, 3, 5, 6, -1, 10 ** 20, True, False, None, 2.0])
    if roll < 0.7 and depth < 3:
        return {rng.choice(KEYS): random_value(rng, depth + 1) for _ in range(rng.randint(0, 2))}
    if roll < 0.75 and depth < 3:
        return [random_value(rng, depth + 1)]
    return ''.join(rng.choice('abc !\\/:.%&#?=é中') for _ in range(rng.randint(0, 12)))


def random_output(rng):
    roll = rng.random()
    if roll < 0.1:   # raw garbage
        return ''.join(rng.choice('{}[]":,ab \\\n') for _ in range(rng.randint(0, 40)))
    tool = rng.choice(TOOL_NAMES)
    if roll < 0.55 and isinstance(tool, str) and tool in REGISTRY:   # well-formed keys, hostile values
        args = {name: (random_value(rng) if rng.random() < 0.5 else _valid(rng, spec))
                for name, spec in REGISTRY[tool].args}
    else:
        args = {rng.choice(KEYS): random_value(rng) for _ in range(rng.randint(0, 3))}
    value = {'tool': tool, 'args': args}
    if rng.random() < 0.1:
        value[rng.choice(KEYS)] = random_value(rng)
    text = json.dumps(value, ensure_ascii=rng.random() < 0.5)
    if rng.random() < 0.05:
        text = text + text
    return text


def _valid(rng, spec):
    if hasattr(spec, 'values'):
        return rng.choice(spec.values)
    if hasattr(spec, 'lo'):
        return rng.randint(spec.lo, spec.hi)
    return rng.choice(['space news', 'jazz & blues', '!g x', 'café', 'https://evil.example/?a=1#b'])


class Property(Case):
    def test_ten_thousand_adversarial_outputs(self):
        rng = random.Random(20261015)
        allowed_ids = set(APP_IDS.values()) | {HOME_ID}
        surfaces = sorted(SURFACES)
        stats = collections.Counter()
        for _ in range(10000):
            raw = random_output(rng)
            try:
                parsed = parse_tool_call(raw)
            except Refused as error:
                self.assertEqual(error.code, 'TOOL_SCHEMA')
                stats['refused_parse'] += 1
                continue
            self.assertIn(parsed.tool, MODEL_FACING)
            try:
                result = plan(parsed, rng.choice(surfaces), 'ab' * 16, rng.choice(PHASES))
            except Refused as error:
                self.assertIn(error.code, ('TOOL_DISABLED', 'TV_TOOL_UNAVAILABLE', 'UNSUPPORTED_COMMAND',
                                           'TOOL_SCHEMA'))
                stats['refused_plan'] += 1
                continue
            stats['plans'] += 1
            self.assertIn(result.executor, ('browser', 'tv', 'agent'))
            self.assertTrue(result.op.startswith('ab' * 16 + ':'))
            params = dict(result.params)
            if 'url' in params:
                url = params['url']
                self.assertTrue(allowed_url(url), url)
                self.assertTrue(url in site_urls() or url.startswith((GOOGLE_PREFIX, YT_PREFIX)), url)
            if 'app_id' in params:
                self.assertIn(params['app_id'], allowed_ids)
            for value in params.values():
                if isinstance(value, str) and result.action != 'open_content':
                    self.assertNotIn('evil', value.split('?q=')[0].split('search_query=')[0])
        self.assertGreater(stats['plans'], 500)          # the generator really reaches valid plans
        self.assertGreater(stats['refused_parse'], 3000)


class Golden(Case):
    def test_set_size_categories_and_uniqueness(self):
        self.assertGreaterEqual(len(GOLDEN), 200)
        counts = collections.Counter(c for c, _, _ in GOLDEN)
        minimums = {'negation': 15, 'injection': 15, 'compound': 10, 'unknown_app': 8, 'bang': 5, 'volume': 8,
                    'ambiguous': 5}
        for category, minimum in minimums.items():
            self.assertGreaterEqual(counts[category], minimum, category)
        keys = [normalize(p) for _, p, _ in GOLDEN]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertTrue(set(NO_ACTION) <= set(CATEGORIES))

    def test_expected_outputs_are_valid_and_carry_no_ids_or_urls(self):
        for entry in GOLDEN:
            for phase in PHASES:
                expected = expected_for(entry, phase)
                for value in expected.args.values():
                    if isinstance(value, str):
                        self.assertNotIn('://', value)
                        self.assertNotIn(value, set(APP_IDS.values()) - set(APP_IDS))
            if entry[0] in NO_ACTION:
                self.assertEqual(expected_for(entry, 'P5').tool, 'meta.unsupported', entry[1])

    def test_ideal_model_meets_p1b_bar_in_every_phase(self):
        for phase in PHASES:
            stats = evaluate(golden_model(phase), phase)
            self.assertTrue(stats['passes_p1b_bar'], (phase, stats))
            self.assertEqual(stats['exact'], len(GOLDEN))

    def test_harness_detects_a_bad_model(self):
        hostile = ScriptedModel(json.dumps({'tool': 'browser.open_site', 'args': {'site': 'https://evil.example'}}),
                                prompt_sha=prompt_sha256('P5'))
        stats = evaluate(hostile, 'P5')
        self.assertFalse(stats['passes_p1b_bar'])
        self.assertEqual(stats['url_or_app_id_in_args'], len(GOLDEN))
        self.assertEqual(stats['injection_unsupported'], stats['injection_total'])  # refused by schema
        lazy = ScriptedModel(json.dumps({'tool': 'tv.open_app', 'args': {'app': 'netflix'}}),
                             prompt_sha=prompt_sha256('P1b'))
        stats = evaluate(lazy, 'P1b')
        self.assertEqual(stats['outside_registry'], len(GOLDEN))   # not enabled in P1b
        self.assertFalse(stats['passes_p1b_bar'])


if __name__ == '__main__':
    unittest.main()
