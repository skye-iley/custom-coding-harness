"""Tests for harness/scrub.py (Milestone 6 T1 — the scrub extracted from audit.py).

This file is the *new* coverage. The **oracle** for the move is
``test_audit.py``'s existing scrub cases, which still call ``audit.scrub`` and
must keep passing unedited — a rename or a behaviour change would have forced an
edit to the very test that proves the move was behaviour-preserving
(``milestone6_spec.md`` §1).

What is asserted here beyond that: the module is a genuine leaf (no sibling
harness import, so ``telemetry.py`` can depend on it without dragging
``harness.interrupt`` in — invariant 21), and ``audit`` re-exports the same
function objects rather than shadowing them with copies.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from _bootstrap import _load

scrub_mod = _load("harness.scrub")
it = _load("harness.interrupt")
audit = _load("harness.audit")

_HARNESS = Path(__file__).resolve().parent.parent / "harness"


def test_audit_reexports_the_same_objects():
    # Identity, not equality: a copy would drift, and every existing call site
    # (audit.scrub, hitl's audit writes) must reach the one implementation.
    assert audit.scrub is scrub_mod.scrub
    assert audit.scrub_deep is scrub_mod.scrub_deep


def test_scrub_redacts_env_secret_values():
    env = {"ANTHROPIC_API_KEY": "supersecretvalue123", "PATH": "/usr/bin"}
    out = scrub_mod.scrub("key is supersecretvalue123 here", env=env)
    assert "supersecretvalue123" not in out
    assert "REDACTED" in out


def test_scrub_redacts_key_shapes():
    out = scrub_mod.scrub("token sk-abcdefghijklmnopqrstuv leaked", env={})
    assert "sk-abcdefghijklmnopqrstuv" not in out
    assert "REDACTED" in out


def test_scrub_leaves_ordinary_text():
    assert scrub_mod.scrub("just a normal prompt", env={}) == "just a normal prompt"


def test_scrub_handles_empty_and_none():
    assert scrub_mod.scrub(None, env={}) is None
    assert scrub_mod.scrub("", env={}) == ""


def test_longer_secret_redacted_before_a_shorter_one_it_contains():
    # _secret_values sorts longest-first precisely so a short secret that is a
    # substring of a long one cannot chop the long one into a half-redacted mess.
    env = {"A_TOKEN": "abcdef123456", "B_TOKEN": "abcdef123456789extra"}
    out = scrub_mod.scrub("value abcdef123456789extra here", env=env)
    assert "abcdef123456" not in out


def test_scrub_deep_recurses_through_containers():
    env = {"MY_TOKEN": "abcdef123456xyz"}
    value = {"a": ["abcdef123456xyz", 7], "b": {"c": "abcdef123456xyz"}, "n": None}
    out = scrub_mod.scrub_deep(value, env)
    assert "abcdef123456xyz" not in json.dumps(out)
    assert out["a"][1] == 7  # non-strings pass through untouched
    assert out["n"] is None


def test_scrub_deep_leaves_numbers_and_bools():
    out = scrub_mod.scrub_deep({"i": 3, "f": 1.5, "b": True}, {})
    assert out == {"i": 3, "f": 1.5, "b": True}


def test_scrub_is_a_leaf_module():
    """It must import nothing from ``harness`` — that is the whole reason it was
    split out of ``audit`` rather than imported from it (invariant 21).

    Checked in a fresh subprocess so imports other tests leaked into *this*
    process cannot mask a real violation (same technique as test_import_isolation).
    """
    script = textwrap.dedent(
        f"""
        import importlib.util, sys, types
        from pathlib import Path
        harness_dir = Path(r{str(_HARNESS)!r})
        pkg = types.ModuleType("harness")
        pkg.__path__ = [str(harness_dir)]
        sys.modules["harness"] = pkg
        spec = importlib.util.spec_from_file_location("harness.scrub", harness_dir / "scrub.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules["harness.scrub"] = mod
        spec.loader.exec_module(mod)
        print("\\n".join(sorted(m for m in sys.modules if m.startswith("harness."))))
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    loaded = set(out.stdout.split())
    assert loaded == {"harness.scrub"}, f"harness.scrub pulled in siblings: {loaded}"
