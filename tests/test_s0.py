"""test_s0.py — S0 whole-trace repair loop and the S0′ matched-token knobs
(grounding HARNESS_GROUNDING_2_ORCHESTRATION §6, §8, §10).
"""

from __future__ import annotations

import asyncio
import copy
import json

from conftest import fence, turn
from src.harness.model_client import make_scripted_client
from src.harness.orchestrator import RunConfig
from src.harness.runlog import validate_run_record
from src.harness.s0 import (
    S0Knobs,
    assemble_s0_prompt,
    measure_s0_tokens,
    run_s0,
    s0_prompt_hash,
)

CFG = RunConfig(timeout_s=5)


def _run(case, script, **kw):
    return asyncio.run(run_s0(case, make_scripted_client(script), config=CFG, **kw))


class TestHappyPath:
    def test_full_trace_passes(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        res = _run(case, {"S0": [turn(fence(emitted), 200, 80)]})
        assert res.status == "ok" and res.gate_ok
        assert res.emitted["case_id"] == case.case_id

    def test_run_record_valid_no_incremental_verdicts(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        res = _run(case, {"S0": [turn(fence(emitted), 200, 80)]})
        rec = res.run_record
        assert validate_run_record(rec).ok, validate_run_record(rec).errors
        assert rec["identity"]["condition"] == "S0"
        # S0 has NO incremental per-record verdicts (§6) — only the whole-trace gate.
        assert rec["validation"]["record_verdicts"] == []
        assert rec["validation"]["final"]["ok"] is True


class TestWholeTraceRepair:
    def test_repair_within_budget(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        bad = copy.deepcopy(emitted)
        bad["lines"][0]["rat"]["decision"]["rate"] = 0.999
        res = _run(case, {"S0": [turn(fence(bad), 200, 80), turn(fence(emitted), 150, 60)]})
        assert res.status == "ok" and res.gate_ok
        assert res.run_record["workers"][0]["retries"] == 1

    def test_budget_exhaustion(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        bad = copy.deepcopy(emitted)
        bad["lines"][0]["rat"]["decision"]["rate"] = 0.999
        # initial + 3 repairs, all bad
        script = {"S0": [turn(fence(bad), 10, 5) for _ in range(4)]}
        res = _run(case, script)
        assert res.status == "validation_exhausted"
        assert res.gate_ok is False
        assert res.run_record["workers"][0]["retries"] == 3

    def test_extraction_failure_consumes_budget(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        script = {"S0": [turn("no json"), turn(fence(emitted), 100, 50)]}
        res = _run(case, script)
        assert res.status == "ok" and res.gate_ok
        assert res.run_record["workers"][0]["retries"] == 1


class TestS0PrimeKnobs:
    def test_knobs_default_plain(self):
        assert S0Knobs().is_plain() is True

    def test_each_knob_expands_prompt_and_changes_hash(self):
        base = assemble_s0_prompt(S0Knobs())
        base_h = s0_prompt_hash(S0Knobs())
        for knobs in (
            S0Knobs(extended_role="You are an expert VAT adjudicator with deep training."),
            S0Knobs(exemplars=("EXEMPLAR: dev_case format demonstration ...",)),
            S0Knobs(scratchpad_instruction="First think step by step in a scratchpad, then answer."),
        ):
            expanded = assemble_s0_prompt(knobs)
            assert len(expanded) > len(base)
            assert s0_prompt_hash(knobs) != base_h
            assert knobs.is_plain() is False

    def test_token_measurement(self, dataset, emitted_for):
        case = dataset.dev_cases[0]
        emitted = emitted_for(case)
        res = _run(case, {"S0": [turn(fence(emitted), 200, 80)]})
        assert measure_s0_tokens(res) == 280
        assert res.total_tokens == 280


class TestS0PromptStableAcrossProcesses:
    def test_plain_hash_stable(self):
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo = Path(__file__).resolve().parents[1]
        probe = "from src.harness.s0 import s0_prompt_hash; print(s0_prompt_hash())"
        env = dict(os.environ, PYTHONPATH=str(repo))
        out = subprocess.run([sys.executable, "-c", probe], cwd=str(repo), env=env,
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == s0_prompt_hash()
