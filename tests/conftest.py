"""conftest.py — shared Layer-2 test fixtures/helpers.

These helpers build scripted worker payloads from the ORACLE trace (tests may
import ``labeler``/``scorer`` freely — they are not agent-context modules). The
scripted client then replays them, so every Layer-2 control/repair/accounting/
assembly test runs WITHOUT API access (grounding §8).
"""

from __future__ import annotations

import json

import pytest

from src.harness.prompts import PER_LINE_SUBTASKS, ordered_assigned, worker_id
from src.harness.surface import PARTITIONS
from src.oracle import generator, labeler, validator


@pytest.fixture(scope="session")
def dataset():
    return generator.generate_dataset(seed=42)


@pytest.fixture(scope="session")
def emitted_for():
    """case -> the oracle emitted trace (full final_trace dict)."""
    def _make(case):
        return validator.trace_to_emitted(labeler.label_case(case))
    return _make


def bundle_payload(assigned, emitted):
    """The bundle a worker owning ``assigned`` would emit (no case_id;
    per-line records under 'lines', 'jur'/'final' at top level)."""
    p: dict = {}
    per_line = [t for t in ordered_assigned(assigned) if t in PER_LINE_SUBTASKS]
    if per_line:
        p["lines"] = [
            {"line_id": ln["line_id"], **{t.lower(): ln[t.lower()] for t in per_line}}
            for ln in emitted["lines"]
        ]
    if "JUR" in assigned:
        p["jur"] = emitted["jur"]
    if "RCH" in assigned:
        p["final"] = emitted["final"]
    return p


def fence(payload) -> str:
    return "Here is my result:\n```json\n" + json.dumps(payload) + "\n```"


def turn(text, input_tokens=1, output_tokens=1, **extra):
    d = {"text": text, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens}}
    d.update(extra)
    return d


def happy_script(condition, emitted, in_tok=10, out_tok=5):
    """One good turn per worker for a partition condition (C1-C4)."""
    script = {}
    for grp in PARTITIONS[condition]:
        wid = worker_id(condition, grp)
        script[wid] = [turn(fence(bundle_payload(grp, emitted)), in_tok, out_tok)]
    return script


@pytest.fixture
def helpers():
    """Bundle-building helpers as a namespace for tests."""
    class _H:
        bundle_payload = staticmethod(bundle_payload)
        fence = staticmethod(fence)
        turn = staticmethod(turn)
        happy_script = staticmethod(happy_script)
    return _H
