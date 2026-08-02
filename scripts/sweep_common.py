"""sweep_common.py — shared, STDLIB-ONLY matrix/record helpers for Layer 4
(grounding HARNESS_GROUNDING_4_SWEEP.md §1, §2).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0; Layers 1–3 binding.

This module is imported by BOTH the parent runner (scripts/sweep.py) and the
child (scripts/run_one.py) and by the tests. It holds only pure, stdlib-only
pieces so the parent never pulls in a harness execution module (§2) and the
child stays stdlib+harness (§4 dependency policy):

  - the five-phase run matrix (§1) and deterministic enumeration,
  - the append-only raw-record path convention (§2),
  - the Layer-4 raw-record shape + a stdlib structural validator (§2 "schema-
    checked at write"; the INNER L2/L3 run_record is validated by runlog in the
    child, which has jsonschema),
  - the pinned-price dollar derivation (§5).

No harness import here; no oracle import here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# §1 matrix constants.
# ---------------------------------------------------------------------------

MODES: tuple[str, ...] = ("none", "timeout", "hallucination", "outage")

# Orchestrated + monolith conditions, plus the two matched-token S0′ controls.
# Filesystem-safe ids (no apostrophes): S0prime_C2 / S0prime_Cstar.
BASE_CONDITIONS: tuple[str, ...] = ("S0", "C1", "C2", "C3", "C4")
S0PRIME_C2 = "S0prime_C2"
S0PRIME_CSTAR = "S0prime_Cstar"
ALL_CONDITIONS: tuple[str, ...] = BASE_CONDITIONS + (S0PRIME_C2, S0PRIME_CSTAR)

EVAL_CASES: tuple[str, ...] = tuple(f"eval_{i:03d}" for i in range(1, 41))
DEV5_CASES: tuple[str, ...] = tuple(f"dev_{i:03d}" for i in range(1, 6))

REPEATS_MAIN = 5  # R = 5 (§1, §6.3/§6.5)


@dataclass(frozen=True)
class RunSpec:
    phase: int
    mode: str
    condition: str
    case_id: str
    repeat: int


# The five phases (§1). S0′ variants are EXCLUDED from injection cells (they are
# RQ3 controls, not conditions — §1). Phase 3 is the only multi-mode phase.
_PHASES: dict[int, dict] = {
    0: {"modes": ("none",), "conditions": BASE_CONDITIONS, "cases": DEV5_CASES, "repeats": 1},
    1: {"modes": ("none",), "conditions": BASE_CONDITIONS, "cases": EVAL_CASES, "repeats": REPEATS_MAIN},
    2: {"modes": ("none",), "conditions": (S0PRIME_C2,), "cases": EVAL_CASES, "repeats": REPEATS_MAIN},
    3: {"modes": ("timeout", "hallucination", "outage"), "conditions": BASE_CONDITIONS,
        "cases": EVAL_CASES, "repeats": REPEATS_MAIN},
    4: {"modes": ("none",), "conditions": (S0PRIME_CSTAR,), "cases": EVAL_CASES, "repeats": REPEATS_MAIN},
}

PHASES: tuple[int, ...] = tuple(sorted(_PHASES))


def enumerate_runs(phase: int) -> list[RunSpec]:
    """Deterministic enumeration of a phase's runs, in a fixed nested order
    (mode, condition, case, repeat). Reproduces the §1 counts exactly."""
    if phase not in _PHASES:
        raise ValueError(f"unknown phase {phase!r}")
    p = _PHASES[phase]
    runs: list[RunSpec] = []
    for mode in p["modes"]:
        for cond in p["conditions"]:
            for case_id in p["cases"]:
                for k in range(p["repeats"]):
                    runs.append(RunSpec(phase, mode, cond, case_id, k))
    return runs


def phase_run_count(phase: int) -> int:
    p = _PHASES[phase]
    return len(p["modes"]) * len(p["conditions"]) * len(p["cases"]) * p["repeats"]


def is_s0_like(condition: str) -> bool:
    """S0 and the S0′ controls run through run_s0; C1–C4 through the orchestrator."""
    return condition == "S0" or condition.startswith("S0prime_")


# ---------------------------------------------------------------------------
# §2 record paths (append-only, one file per run).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = _REPO_ROOT / "results"
RAW_DIR = RESULTS_DIR / "raw"
QUARANTINE_DIR = RESULTS_DIR / "quarantine"
STOP_FILE = RESULTS_DIR / "STOP"


def phase_dir_name(phase: int) -> str:
    return f"phase{phase}"


def record_path(spec: RunSpec, root: Optional[Path] = None) -> Path:
    # root resolved at CALL time (default RAW_DIR) so tests may redirect RAW_DIR.
    base = RAW_DIR if root is None else root
    return (
        base / phase_dir_name(spec.phase) / spec.mode / spec.condition
        / spec.case_id / f"r{spec.repeat}.json"
    )


def quarantine_path(spec: RunSpec, root: Optional[Path] = None) -> Path:
    base = QUARANTINE_DIR if root is None else root
    return (
        base / phase_dir_name(spec.phase) / spec.mode / spec.condition
        / spec.case_id / f"r{spec.repeat}.json"
    )


# ---------------------------------------------------------------------------
# §2 Layer-4 raw record. Wraps the full L2/L3 run_record and embeds the emitted
# or partial trace, terminal status, injection marker + plan_sha256, and case
# wall-clock. Wall-clock lives ONLY here (a log artifact), never in trace/agent
# content (L1 §7.2).
# ---------------------------------------------------------------------------

RAW_SCHEMA_VERSION = "L4-1"


def build_raw_record(
    spec: RunSpec,
    run_record: dict,
    emitted_trace: Optional[dict],
    terminal_status: str,
    wall_start_utc: str,
    wall_end_utc: str,
    duration_ms: float,
) -> dict:
    injection = run_record.get("accounting", {}).get("injection", {})
    return {
        "schema_version": RAW_SCHEMA_VERSION,
        "sweep": {
            "phase": spec.phase,
            "mode": spec.mode,
            "condition": spec.condition,
            "case_id": spec.case_id,
            "repeat": spec.repeat,
        },
        "terminal_status": terminal_status,
        "emitted_trace": emitted_trace,
        "wall_clock": {
            "start_utc": wall_start_utc,
            "end_utc": wall_end_utc,
            "duration_ms": duration_ms,
        },
        "plan_sha256": injection.get("plan_sha256"),
        "run_record": run_record,
    }


_REQUIRED_TOP = ("schema_version", "sweep", "terminal_status", "emitted_trace",
                 "wall_clock", "run_record")
_REQUIRED_SWEEP = ("phase", "mode", "condition", "case_id", "repeat")


def validate_raw_record(obj) -> tuple[bool, list[str]]:
    """Stdlib structural validation (§2). Detects truncation/corruption for the
    parent's resume check without importing jsonschema/harness. The child ALSO
    validates the inner run_record with runlog (jsonschema) before writing."""
    errors: list[str] = []
    if not isinstance(obj, dict):
        return False, ["raw record is not an object"]
    for k in _REQUIRED_TOP:
        if k not in obj:
            errors.append(f"missing top-level key {k!r}")
    if obj.get("schema_version") != RAW_SCHEMA_VERSION:
        errors.append("schema_version mismatch")
    sweep = obj.get("sweep")
    if not isinstance(sweep, dict):
        errors.append("sweep is not an object")
    else:
        for k in _REQUIRED_SWEEP:
            if k not in sweep:
                errors.append(f"missing sweep.{k}")
        if sweep.get("mode") not in MODES:
            errors.append("sweep.mode not in MODES")
        if sweep.get("condition") not in ALL_CONDITIONS:
            errors.append("sweep.condition not recognised")
    wc = obj.get("wall_clock")
    if not isinstance(wc, dict) or "duration_ms" not in wc:
        errors.append("wall_clock missing/invalid")
    if not isinstance(obj.get("run_record"), dict):
        errors.append("run_record missing/invalid")
    return (not errors), errors


def record_is_complete(spec: RunSpec, root: Optional[Path] = None) -> bool:
    """Completion invariant (§2): the record file exists and validates."""
    path = record_path(spec, root)
    if not path.is_file():
        return False
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    ok, _ = validate_raw_record(obj)
    return ok


# ---------------------------------------------------------------------------
# §5 dollar derivation from the pinned price sheet.
# ---------------------------------------------------------------------------

DEFAULT_PRICE_SHEET = _REPO_ROOT / "data" / "price_sheet.json"


def load_price_sheet(path: Path = DEFAULT_PRICE_SHEET) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dollars(input_tokens: int, output_tokens: int, price_sheet: dict) -> float:
    """USD from token counts (§5, §6.1: dollars derived from token cost)."""
    pin = price_sheet["usd_per_1m_input_tokens"]
    pout = price_sheet["usd_per_1m_output_tokens"]
    return round(input_tokens / 1_000_000 * pin + output_tokens / 1_000_000 * pout, 6)


def iter_all_runs() -> Iterator[RunSpec]:
    for phase in PHASES:
        yield from enumerate_runs(phase)
