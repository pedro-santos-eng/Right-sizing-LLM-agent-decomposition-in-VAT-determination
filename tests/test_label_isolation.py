"""test_label_isolation.py — import-graph isolation (grounding §1.2, §10).

The single most damaging Part-2 error is leaking oracle labels into agent
context. Enforcement: src.oracle.labeler and src.oracle.scorer must be
UNREACHABLE from any module that constructs agent context (surface.py, tools.py).
Each is imported in a FRESH interpreter; neither label source may appear in
sys.modules. rules.py and validator.py are legitimately importable elsewhere and
are not checked here.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Modules that construct agent context (grounding §1.2). Layer 2 adds its agent /
# prompt modules to this list.
AGENT_CONTEXT_MODULES = ["src.harness.surface", "src.harness.tools"]

_LABEL_SOURCES = ("src.oracle.labeler", "src.oracle.scorer")


def _import_in_fresh_interpreter(module: str) -> set[str]:
    """Import ``module`` in a subprocess and return the loaded oracle sub-modules."""
    probe = (
        "import importlib, sys, json;"
        f"importlib.import_module({module!r});"
        "print(json.dumps([m for m in sys.modules if m.startswith('src.oracle')]))"
    )
    env = dict(os.environ, PYTHONPATH=str(_REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(_REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    import json

    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


@pytest.mark.parametrize("module", AGENT_CONTEXT_MODULES)
def test_label_sources_unreachable_from_agent_context(module):
    loaded = _import_in_fresh_interpreter(module)
    for label_source in _LABEL_SOURCES:
        assert label_source not in loaded, (
            f"{module} transitively imports {label_source} — label isolation broken"
        )


@pytest.mark.parametrize("module", AGENT_CONTEXT_MODULES)
def test_agent_context_module_imports_cleanly(module):
    # sanity: the module imports at all in isolation (no hidden global state)
    loaded = _import_in_fresh_interpreter(module)
    # rules.py is the allowed oracle dependency (tables); it may be present.
    assert loaded <= {"src.oracle", "src.oracle.rules"}
