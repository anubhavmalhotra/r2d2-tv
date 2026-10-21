"""Validator bypass attempts: Unicode / whitespace / case tricks, URL parser
confusion at the third (allowlist) layer, and a read-only cross-check against the
shipped channel v1 allowlist (voice/backend/browser_channel.py)."""
import random
import unicodedata
import unittest
from urllib.parse import quote

from agent.errors import Refused
from agent.fastpath import match, negated
from agent.parse import check_call
from agent.registry import APP_IDS, REGISTRY, Enum, Int, Text
from agent.router import DDG_PREFIX, allowed_url, plan
from agent.validate import normalize_text
from tests_adversarial.adv_support import Case, real_channel_allowed_url

WEB = Text(256, 1024, '!\\')

# Characters whose NFKC form contains '!' or '\' (all must be stripped).
BANG_FORMS = ['!', '！', '﹗', '‼', '⁉', '⁈', '\\', '＼', '﹨']
_CF = ['​', '‌', '‍', '⁠', '‮', '⁦', '﻿', '­', '\U000e0041']
_MARKS = ['́', '̈', '̧', '̣', '̂']


class TextNormalization(Case):
    def test_normalize_text_is_idempotent_and_returns_nfkc(self):
        """Cf removal can create a new canonical composition (e + ZWJ + U+0301 ->
        e + U+0301), so one pass of NFKC-then-remove is not a fixed point. The P1
        re-parse compares the sender's normalized args with the agent's: a
        non-idempotent normalizer turns a benign query into PARSE_MISMATCH + Paused."""
        rng = random.Random(7)
        pool = list('aeiouAEIOU nc') + _CF + _MARKS + BANG_FORMS + ['　', ' ', 'ﬁ']
        samples = ['e‍́', 'cafe⁠́ menu', 'a​̈', 'n­̃o']
        samples += [''.join(rng.choice(pool) for _ in range(rng.randint(1, 24))) for _ in range(3000)]
        for value in samples:
            try:
                once = WEB.check(value)
            except Refused:
                continue
            with self.subTest(value=ascii(value)):
                self.assertEqual(WEB.check(once), once)
                self.assertTrue(unicodedata.is_normalized('NFKC', once))
                self.assertFalse(set(once) & set(_CF))
                self.assertNotIn('!', once)
                self.assertNotIn('\\', once)

    def test_every_bang_and_backslash_form_is_stripped(self):
        for form in BANG_FORMS:
            for template in ('{}g cats', 'cats {}g', '{}', '{}{}{}', 'x{}​y'):
                value = template.format(form, form, form)
                with self.subTest(value=ascii(value)):
                    try:
                        out = WEB.check(value)
                    except Refused as error:
                        self.assertEqual(error.code, 'TOOL_SCHEMA')
                        continue
                    self.assertNotIn('!', out)
                    self.assertNotIn('\\', out)

    def test_refused_categories_cannot_be_smuggled_by_nfkc(self):
        for value in ('x\u0000', 'x\u001b', 'x\u0085', 'x ', 'x ', 'x', 'x\U000f0000',
                      'x͸', 'x\ud800', ' '):
            with self.subTest(value=ascii(value)):
                with self.assertRaises(Refused):
                    WEB.check(value)

    def test_whitespace_tricks_collapse_or_refuse(self):
        self.assertEqual(WEB.check('a 　 b'), 'a b')
        self.assertEqual(WEB.check('​ a ​'), 'a')
        for value in ('\t', 'a\tb', 'a\nb', 'a\rb', 'a\x0bb', 'a\x0cb'):
            with self.assertRaises(Refused):
                WEB.check(value)

    def test_nfkc_expansion_cannot_exceed_bounds(self):
        with self.assertRaises(Refused):
            WEB.check('ﷺ' * 20)           # 18 chars each after NFKC
        with self.assertRaises(Refused):
            Text(160, 640).check('\U0001f600' * 161)
        self.assertEqual(len(Text(256, 1024).check('\U0001f600' * 256)), 256)
        with self.assertRaises(Refused):
            Text(256, 1024).check('\U0001f600' * 257)

    def test_enums_and_ints_do_no_folding(self):
        site = REGISTRY['browser.open_site'].spec['site']
        app = REGISTRY['tv.open_app'].spec['app']
        for spec, good in ((site, 'youtube'), (app, 'netflix')):
            for bad in (good.upper(), good + ' ', ' ' + good, good + '​', unicodedata.normalize('NFKD', good),
                        ''.join(chr(ord(c) + 0xFEE0) for c in good), good.replace('o', 'о')):
                if bad == good:
                    continue
                with self.subTest(bad=ascii(bad)):
                    with self.assertRaises(Refused):
                        spec.check(bad)
        steps = Int(1, 5)
        for bad in (True, False, 1.0, '1', None, 0, 6, -0, 2 ** 64, [1]):
            with self.assertRaises(Refused):
                steps.check(bad)


class FastPathTricks(Case):
    def test_confusables_never_confirm(self):
        for value in ('yеs', 'ye s', 'yes yes', 'yes, open netflix', 'yes please open netflix', 'yess'):
            with self.subTest(value=ascii(value)):
                self.assertIsNone(match(value))

    def test_negation_guard_covers_spacing_and_apostrophes(self):
        for value in ("Don't open Netflix", 'don’t open netflix', 'DO NOT open netflix', 'dont open netflix',
                      'Please, do not open Netflix!', 'never open netflix', "no, don't open netflix"):
            self.assertTrue(negated(value), value)

    def test_negation_guard_residual_is_documented(self):
        # [Residual] homoglyph or split forms are left to the model.
        self.assertFalse(negated('dоn\'t open netflix'))
        self.assertFalse(negated('open netflix, actually don\'t'))


class AllowlistLayer3(Case):
    """allowed_url is the third check: it must refuse anything the
    router would never build, independently of the router."""

    def test_url_parser_confusion_refused(self):
        bad = [
            'https://duckduckgo.com/?q=a#frag', 'https://duckduckgo.com/?q=a&kp=-1', 'https://duckduckgo.com/?q=a;b',
            'https://duckduckgo.com.evil.example/?q=a', 'https://duckduckgo.com@evil.example/?q=a',
            'https://evil.example@duckduckgo.com/?q=a', 'https://duckduckgo.com:443/?q=a',
            'https://DUCKDUCKGO.com/?q=a', 'HTTPS://duckduckgo.com/?q=a', 'http://duckduckgo.com/?q=a',
            'https://duckduckgo.com/?Q=a', 'https://duckduckgo.com//?q=a', 'https://duckduckgo.com/html/?q=a',
            'https://duckduckgo.com/?q=', 'https://duckduckgo.com/?q=a b', 'https://duckduckgo.com/?q=a\tb',
            'https://duckduckgo.com/?q=a\nb', 'https://duckduckgo.com/?q=!g', 'https://duckduckgo.com/?q=%21g',
            'https://duckduckgo.com/?q=%5Cg', 'https://duckduckgo.com/?q=%EF%BC%81g',
            'https://duckduckgo.com/?q=%E2%80%BCg',
            'https://www.youtube.com/', 'https://youtube.com', 'https://www.youtube.com.', 'https://www.youtube.com/watch',
            'https://www.youtube.com/results?search_query=a&sp=x', 'https://www.youtube.com/results?search_query=a#x',
            'https://www.youtube.com/results?search_query=', 'https://www.youtube.com/results?q=a',
            'https://www.youtube.com/results?search_query=á', 'https://duckduckgo.com/?q=café',
            'https://duckduckgo.com', 'javascript:alert(1)', 'data:text/html,x', 'about:blank', 'file:///etc/passwd',
            'https://duckduckgo.com/?q=' + 'a' * 1501, None, b'https://www.youtube.com', 1,
        ]
        for url in bad:
            if url == 'https://duckduckgo.com':
                continue  # the shipped sites.json root is allowed by design
            with self.subTest(url=ascii(url)):
                self.assertFalse(allowed_url(url))

    def test_encoding_level_tricks_refused(self):
        """Malformed escapes, encoded controls and multiply-encoded bangs are never
        built by the router (quote() emits only %XX upper-hex of valid UTF-8 for
        already-validated text), so layer 3 should refuse them too."""
        for tail in ('%', '%%', '%2', '%G1', '%zz', '%0A', '%00', '%7F', '%C2%85', '%E2%80%A8', '%0D%0A',
                     '%2521g', '%252521g', '%255Cg', 'a%2'):
            with self.subTest(tail=tail):
                self.assertFalse(allowed_url(DDG_PREFIX + tail))
        for tail in ('%0A', '%%', '%00', 'a%2'):
            self.assertFalse(allowed_url('https://www.youtube.com/results?search_query=' + tail))

    def test_router_urls_stay_accepted(self):
        for query in ('weather in springfield', '100% juice', 'café', '中文', 'C++ & C#',
                      'x' * 256, '\U0001f600 party', '50%off', '%zz'):
            for tool in ('browser.web_search', 'browser.youtube_search'):
                with self.subTest(query=ascii(query), tool=tool):
                    p = plan(check_call({'tool': tool, 'args': {'query': query}}), 'browser_other', 'ab' * 16, 'P5')
                    self.assertTrue(allowed_url(p.param('url')))

    def test_literal_percent_escaped_bang_text_retains_default_search_guard(self):
        """[Deliberate] A query whose literal text is an escaped bang ('%21g') is
        still refused by the default web-search guard after changing its provider;
        YouTube has no bang syntax and keeps it."""
        for query in ('%21g cats', 'a%5Cb', '%2521g'):
            with self.assertRaises(Refused):
                plan(check_call({'tool': 'browser.web_search', 'args': {'query': query}}), 'browser_other',
                     'ab' * 16, 'P5')
            p = plan(check_call({'tool': 'browser.youtube_search', 'args': {'query': query}}), 'browser_other',
                     'ab' * 16, 'P5')
            self.assertTrue(allowed_url(p.param('url')))


class ChannelV1CrossCheck(Case):
    """Read-only comparison with the shipped channel allowlist ([V] SHA-256 pinned).
    Channel v2 must keep accepting every router URL and must add the checks below."""

    def setUp(self):
        super().setUp()
        self.real = real_channel_allowed_url()
        if self.real is None:
            self.skipTest('repo clone absent or changed')

    def test_every_router_ddg_url_passes_the_shipped_channel(self):
        rng = random.Random(3)
        pool = list('abc XYZ 019 -_.~+%&#?/=;:@') + ['é', '中', '\U0001f600', '！', '!']
        for _ in range(2000):
            query = ''.join(rng.choice(pool) for _ in range(rng.randint(1, 30)))
            try:
                p = plan(check_call({'tool': 'browser.web_search', 'args': {'query': query}}),
                         'browser_other', 'ab' * 16, 'P5')
            except Refused:
                continue
            self.assertTrue(self.real(p.param('url')), p.param('url'))

    def test_channel_v1_gaps_that_v2_must_close(self):
        """[V] Channel v2 closes the v1 gaps: refuses DuckDuckGo bangs and backslashes,
        and admits the site roots and YouTube results URLs that the agent builds."""
        self.assertFalse(self.real('https://duckduckgo.com/?q=!g'))
        self.assertFalse(self.real('https://duckduckgo.com/?q=%21g'))
        self.assertFalse(self.real('https://duckduckgo.com/?q=%5Cg'))
        self.assertTrue(self.real('https://duckduckgo.com'))
        self.assertTrue(self.real('https://www.wikipedia.org'))
        self.assertTrue(self.real('https://news.ycombinator.com'))
        self.assertTrue(self.real('https://www.youtube.com/results?search_query=cats'))
        for url in ('https://duckduckgo.com/?q=!g', 'https://duckduckgo.com/?q=%21g'):
            self.assertFalse(allowed_url(url))


if __name__ == '__main__':
    unittest.main()
