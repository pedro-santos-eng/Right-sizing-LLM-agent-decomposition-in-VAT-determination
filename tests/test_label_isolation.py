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

# Modules that construct agent context (grounding §1.2). Layer 2 adds its true
# agent-context modules (prompt/message/string assembly). These must reach NO
# label source and not even the validator: loaded oracle submodules ⊆
# {src.oracle, src.oracle.rules}.
AGENT_CONTEXT_MODULES = [
    "src.harness.surface",
    "src.harness.tools",
    "src.harness.prompts",
    "src.harness.agents",
    "src.harness.model_client",
    # Layer-3 runtime controller: stdlib-only, loads the precomputed plan; must
    # reach no oracle module at all (grounding L3 §1, §8).
    "src.harness.injection",
]

# Control + validation modules (grounding §9). Like the Layer-1 validation.py,
# these legitimately import the frozen ``validator`` (which type-imports
# ``labeler`` for the CaseTrace annotation — an unavoidable frozen edge, see
# HARNESS_GROUNDING_1_SURFACE §1.2 "validator.py is importable"). We therefore
# assert the achievable, meaningful guarantees for them: ``scorer`` — the pure
# label-scoring source — is NEVER reachable, and neither module imports
# ``labeler``/``scorer`` DIRECTLY. All agent-context string assembly is delegated
# to the strictly-isolated modules above, so no label value can reach a prompt.
# (Flagged bounded interpretation of §9's "extend the test"; see DEVLOG.)
CONTROL_MODULES = ["src.harness.orchestrator", "src.harness.s0", "scripts.run_one"]

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
    # No label source AND no validator may be reachable from these (§1.2, §9).
    assert loaded <= {"src.oracle", "src.oracle.rules"}


def _source(module: str) -> str:
    rel = module.replace(".", "/") + ".py"
    return (_REPO_ROOT / rel).read_text(encoding="utf-8")


@pytest.mark.parametrize("module", CONTROL_MODULES)
def test_control_modules_never_reach_scorer(module):
    # scorer is the pure label-scoring source; it must never be reachable from
    # any Layer-2 module (grounding §9).
    loaded = _import_in_fresh_interpreter(module)
    assert "src.oracle.scorer" not in loaded, f"{module} reaches scorer — isolation broken"


@pytest.mark.parametrize("module", CONTROL_MODULES)
def test_control_modules_no_direct_label_import(module):
    # §9: do not import labeler or scorer FROM these modules. (labeler enters only
    # transitively via the frozen validator's CaseTrace type-import.)
    src = _source(module)
    assert "import labeler" not in src and "labeler import" not in src
    assert "import scorer" not in src and "scorer import" not in src
