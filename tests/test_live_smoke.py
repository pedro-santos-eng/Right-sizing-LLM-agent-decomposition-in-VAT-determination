"""test_live_smoke.py — the single live-API gate item (grounding
HARNESS_GROUNDING_2_ORCHESTRATION §10, last box).

SKIP-IF-UNCONFIGURED: this is the ONLY test that may touch the real API, and it
is skipped unless ANTHROPIC_API_KEY is set (§0, §8). It runs dev_001 under C1 and
S0 against the real model and asserts a validated trace with populated usage.

No other test in the suite requires the network or an API key.
"""

from __future__ import annotations

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="live smoke skipped: ANTHROPIC_API_KEY not set (grounding §10, skip-if-unconfigured)",
)


def _dev_001():
    from src.oracle import generator

    ds = generator.generate_dataset(seed=42)
    for c in ds.dev_cases:
        if c.case_id == "dev_001":
            return c
    return ds.dev_cases[0]


def _best_of_two(run_once):
    """Plumbing-gate semantics (ratified 2026-08-02): the smoke certifies the
    live pipeline (auth, client, schema round-trip), not a single stochastic
    draw of an ~measured-reliability process. One stochastic failure triggers
    exactly one retry; two consecutive failures fail the gate."""
    first_error = None
    for _ in range(2):
        try:
            run_once()
            return
        except AssertionError as exc:  # stochastic validation failure
            first_error = exc
    raise first_error


def test_live_c1_produces_validated_trace():
    from src.harness.model_client import make_model_client
    from src.harness.orchestrator import run_case
    from src.oracle import validator

    case = _dev_001()

    def once():
        res = asyncio.run(run_case("C1", case, make_model_client()))
        assert res.emitted is not None
        assert validator.validate_trace(res.emitted).ok is True
        assert res.run_record["accounting"]["token_counts"]["total"] > 0
        assert res.run_record["accounting"]["latency_ms"] is not None

    _best_of_two(once)


def test_live_s0_produces_validated_trace():
    from src.harness.model_client import make_model_client
    from src.harness.s0 import run_s0
    from src.oracle import validator

    case = _dev_001()

    def once():
        res = asyncio.run(run_s0(case, make_model_client()))
        assert res.emitted is not None
        assert validator.validate_trace(res.emitted).ok is True
        assert res.total_tokens > 0

    _best_of_two(once)
