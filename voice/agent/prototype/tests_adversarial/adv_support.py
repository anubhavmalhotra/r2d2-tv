"""Helpers for the independent adversarial suite (tests_adversarial/).

Reuses tests.support (socket.socket is patched to raise in every test; all writes
go under a private temp dir). Nothing here opens a socket, contacts Spark or the
TV, loads a model, or writes outside the test's temp tree.
"""
import ast
import hashlib
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from tests.support import Case, call, scripted  # noqa: F401  (re-exported)
from tests.test_agent import AppleBuilder, mutations, writes  # noqa: F401

# An identifier-safe sentinel: it passes audit._SAFE_STR, so only the
# free-text-as-length rule (audit_args) can keep it off disk.
TOKEN = 'zebraquartzmarmalade'

# Read-only cross-check against the shipped channel v2 allowlist.
REPO_CHANNEL = Path(__file__).resolve().parents[3] / 'backend' / 'browser_channel.py'
# Pinned: a changed browser_channel.py must be re-checked before the pin moves.
REPO_CHANNEL_SHA256 = '9bae8418c52701258f5e4e0470f634c1845504dd193f3f46cca52a9116d74833'


def real_channel_allowed_url():
    """Compile only `allowed_url` from the repo's browser_channel.py without
    importing the module (it imports controller/ingress).
    Returns None when the file is absent or its hash differs."""
    try:
        source = REPO_CHANNEL.read_bytes()
    except OSError:
        return None
    if hashlib.sha256(source).hexdigest() != REPO_CHANNEL_SHA256:
        return None
    tree = ast.parse(source)
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'allowed_url']
    if len(nodes) != 1:
        return None
    namespace = {'re': re, 'urlsplit': urlsplit}
    exec(compile(ast.Module(nodes, []), str(REPO_CHANNEL) + ':allowed_url', 'exec'), namespace)
    return namespace['allowed_url']


def tree_bytes(root):
    """Every regular file written under `root` (audit rows, flags, key files)."""
    data = []
    for dirpath, _, files in os.walk(root):
        for name in files:
            path = Path(dirpath) / name
            if path.is_file() and not path.is_symlink():
                data.append(path.read_bytes())
    return b'\n'.join(data)


def leak_forms(text):
    """Byte forms of `text` that must never appear on disk."""
    from urllib.parse import quote, quote_plus
    forms = {text, text.upper(), text.lower(), quote(text, safe=''), quote_plus(text)}
    return [f.encode('utf-8') for f in forms if f]
