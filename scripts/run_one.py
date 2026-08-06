"""run_one.py — the sweep CHILD: execute exactly one run and write one record
(grounding HARNESS_GROUNDING_4_SWEEP.md §2, §3).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0; Layers 1–3 binding.

Invoked by the parent as a FRESH OS process (§2):

    python -m scripts.run_one <phase> <mode> <condition> <case_id> <repeat>

The child builds its orchestrator/S0 world from scratch, executes the run, writes
one schema-checked raw record to
``results/raw/<phase>/<mode>/<cond>/<case>/r<k>.json`` (embedding the full
L2/L3 accounting, the injection marker + plan_sha256, the emitted-or-partial
trace, terminal status, and case wall-clock), and exits.

Dependency policy (§4): stdlib + the harness only — NO pandas/numpy. Label
isolation (§3): ``scorer`` is unreachable and ``labeler`` is not imported here
(``generator`` imports only ``rules``; the frozen ``validator``→``labeler``
type-edge is the ratified exception, as for the orchestrator). Oracle labels
enter only in the offline scoring pass (§4), never in this process.

Client selection: the real Anthropic client by default; for tests, a scripted
client is loaded from the JSON file named by env ``SWEEP_SCRIPTED_CLIENT`` — the
§8 "one real subprocess round-trip, scripted" seam. No other test seam exists.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import sys
import time
from pathlib import Path

# Harness (execution) + stdlib only. generator is label-safe (imports rules).
from scripts import sweep_common as sc
from src.harness import runlog
from src.harness.injection import make_controller
from src.harness.model_client import make_model_client, make_scripted_client
from src.harness.orchestrator import run_case
from src.harness.s0 import S0Knobs, run_s0
from src.oracle import generator

_DATA_ROOT = Path(__file__).resolve().parents[1]
# Phase 2/4 consume the tuned S0′ knobs versioned here by scripts/tune_s0prime.py
# (the §6.3 tuning loop). A S0prime_ condition with NO knobs file is a FATAL error,
# not a silent plain-S0 fallback — that silent fallback let phases 2/4 run
# degenerate (S0′ == plain S0) undetected (DEVLOG 2026-08-06, "third defect").
_S0PRIME_KNOBS_DIR = sc.RESULTS_DIR / "s0prime_knobs"


def _now_utc() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_case(case_id: str):
    ds = generator.generate_dataset(seed=42)
    for case in list(ds.eval_cases) + list(ds.dev_cases):
        if case.case_id == case_id:
            return case
    raise SystemExit(f"unknown case_id {case_id!r}")


def _build_client():
    scripted = os.environ.get("SWEEP_SCRIPTED_CLIENT")
    if scripted:  # §8 test seam: replay a scripted client from a JSON file
        script = json.loads(Path(scripted).read_text(encoding="utf-8"))
        return make_scripted_client(script)
    return make_model_client()


def _s0_knobs(condition: str) -> S0Knobs:
    if not condition.startswith("S0prime_"):
        return S0Knobs()  # plain S0
    path = _S0PRIME_KNOBS_DIR / f"{condition}.json"
    if not path.is_file():
        # Loud-fail (DEVLOG 2026-08-06): the silent plain-S0 fallback that ran
        # phases 2/4 degenerate is REMOVED. A S0′ control must not execute until
        # scripts/tune_s0prime.py has committed its tuned, matched-token knobs.
        raise SystemExit(
            f"FATAL: tuned S0′ knobs for {condition!r} not found at {path}. Run the "
            f"§6.3 tuning loop (python -m scripts.tune_s0prime --condition {condition}) "
            f"and commit results/s0prime_knobs/{condition}.json before this runs."
        )
    d = json.loads(path.read_text(encoding="utf-8"))
    return S0Knobs(
        extended_role=d.get("extended_role", ""),
        exemplars=tuple(d.get("exemplars", ())),
        scratchpad_instruction=d.get("scratchpad_instruction", ""),
    )


async def _execute(spec: sc.RunSpec, case, client, injection):
    if sc.is_s0_like(spec.condition):
        result = await run_s0(case, client, knobs=_s0_knobs(spec.condition), injection=injection)
        return result.status, result.emitted, result.run_record
    # C1–C4 through the orchestrator.
    result = await run_case(spec.condition, case, client, injection=injection)
    return result.status, result.emitted, result.run_record


def execute_and_write(spec: sc.RunSpec, out_root: Path = sc.RAW_DIR) -> Path:
    case = _load_case(spec.case_id)
    client = _build_client()
    injection = make_controller(spec.mode)

    start_utc = _now_utc()
    t0 = time.perf_counter()
    status, emitted, run_record = asyncio.run(_execute(spec, case, client, injection))
    duration_ms = (time.perf_counter() - t0) * 1000.0
    end_utc = _now_utc()

    # The inner L2/L3 run_record is authoritatively validated by runlog
    # (jsonschema) before we wrap it — fail loudly on any malformed record.
    inner = runlog.validate_run_record(run_record)
    if not inner.ok:
        raise SystemExit(f"inner run_record invalid: {inner.errors}")

    raw = sc.build_raw_record(
        spec, run_record, emitted, status, start_utc, end_utc, duration_ms
    )
    ok, errors = sc.validate_raw_record(raw)
    if not ok:
        raise SystemExit(f"raw record invalid: {errors}")

    path = sc.record_path(spec, out_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(raw, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("usage: run_one <phase> <mode> <condition> <case_id> <repeat>", file=sys.stderr)
        return 2
    phase_s, mode, condition, case_id, repeat_s = argv
    spec = sc.RunSpec(int(phase_s), mode, condition, case_id, int(repeat_s))
    if spec.mode not in sc.MODES:
        print(f"unknown mode {spec.mode!r}", file=sys.stderr)
        return 2
    if spec.condition not in sc.ALL_CONDITIONS:
        print(f"unknown condition {spec.condition!r}", file=sys.stderr)
        return 2
    # SWEEP_OUT_ROOT lets a test redirect output off the repo tree (§8 harness);
    # unset → the canonical results/raw root.
    out_root_env = os.environ.get("SWEEP_OUT_ROOT")
    out_root = Path(out_root_env) if out_root_env else sc.RAW_DIR
    path = execute_and_write(spec, out_root)
    print(str(path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
