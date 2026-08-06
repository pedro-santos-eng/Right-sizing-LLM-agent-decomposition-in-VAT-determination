"""sweep.py — the PARENT sweep runner (grounding HARNESS_GROUNDING_4_SWEEP.md
§1, §2, §7).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_4_SWEEP.md v1.0; Layers 1–3 binding.

Enumerates the §1 matrix for a phase and drives process-per-run execution (§2):
one fresh OS process per run via ``python -m scripts.run_one``, up to
``N_PARALLEL`` children across DIFFERENT cases. Resume = re-enumerate and skip
complete runs; a corrupt record is quarantined and re-executed; no record is
overwritten. Per-phase token/dollar caps (pinned price sheet, §5) and a global
kill file (``results/STOP``) abort gracefully, preserving all completed records
and writing an abort marker.

The parent NEVER imports a harness execution module (§2): it only spawns children
and reads their JSON records. Dependency policy (§4): stdlib only.

Phase-0 entry point (§7): ``python -m scripts.sweep --phase 0`` runs the L2 §11
dry run through this same isolation path (real API). Phase 0 is a live decision;
this module IMPLEMENTS it but the runner is not executed as part of the gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from scripts import sweep_common as sc


# ---------------------------------------------------------------------------
# Configuration (§2 budget guards; caps are set from the Phase-0 projection).
# ---------------------------------------------------------------------------


@dataclass
class SweepConfig:
    n_parallel: int = 4
    poll_interval_s: float = 0.05
    # Per-phase caps; a phase absent from a map means "no cap" for that resource.
    token_caps: dict[int, int] = field(default_factory=dict)
    dollar_caps: dict[int, float] = field(default_factory=dict)
    price_sheet: Optional[dict] = None

    def price(self) -> dict:
        if self.price_sheet is None:
            self.price_sheet = sc.load_price_sheet()
        return self.price_sheet


@dataclass
class PhaseResult:
    phase: int
    completed: int
    skipped: int
    quarantined: int
    failed: int
    aborted: bool
    abort_reason: Optional[str]
    tokens: int
    dollars: float


# ---------------------------------------------------------------------------
# Runner abstraction — makes resume/quarantine/budget/kill testable without
# spawning real processes. The default spawns ``run_one`` as a subprocess (§2);
# tests inject a synchronous fake.
# ---------------------------------------------------------------------------


class SubprocessRunner:
    """Spawns ``python -m scripts.run_one`` per run in a fresh OS process (§2)."""

    def __init__(self, cwd: Optional[Path] = None):
        self._cwd = str(cwd or sc._REPO_ROOT)

    def start(self, spec: sc.RunSpec):
        return subprocess.Popen(
            [sys.executable, "-m", "scripts.run_one",
             str(spec.phase), spec.mode, spec.condition, spec.case_id, str(spec.repeat)],
            cwd=self._cwd,
        )

    def poll(self, handle) -> bool:
        return handle.poll() is not None

    def returncode(self, handle) -> int:
        return handle.returncode


# ---------------------------------------------------------------------------
# Resume + quarantine (§2).
# ---------------------------------------------------------------------------


def _quarantine(spec: sc.RunSpec) -> None:
    src = sc.record_path(spec)
    dst = sc.quarantine_path(spec)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))


def _pending_worklist(
    phase: int, modes: Optional[frozenset[str]] = None
) -> tuple[list[sc.RunSpec], int, int]:
    """Return (worklist, skipped, quarantined): skip complete runs; quarantine a
    record that exists but does not validate, then include it for re-execution.
    ``modes`` (a subset of the phase's modes; None = all) restricts execution to
    those cells — the §1 enumeration is unchanged, only the runner's worklist is
    filtered (e.g. a timeout-only re-run of Phase 3)."""
    worklist: list[sc.RunSpec] = []
    skipped = quarantined = 0
    for spec in sc.enumerate_runs(phase):
        if modes is not None and spec.mode not in modes:
            continue
        path = sc.record_path(spec)
        if path.is_file():
            if sc.record_is_complete(spec):
                skipped += 1
                continue
            _quarantine(spec)          # corrupt → quarantine, then re-run
            quarantined += 1
        worklist.append(spec)
    return worklist, skipped, quarantined


# ---------------------------------------------------------------------------
# Budget accounting (§2, §5) from completed records.
# ---------------------------------------------------------------------------


def _record_tokens(spec: sc.RunSpec) -> tuple[int, int]:
    obj = json.loads(sc.record_path(spec).read_text(encoding="utf-8"))
    tc = obj.get("run_record", {}).get("accounting", {}).get("token_counts", {})
    return int(tc.get("input", 0)), int(tc.get("output", 0))


def _write_marker(phase: int, name: str, payload: dict) -> None:
    d = sc.RAW_DIR / sc.phase_dir_name(phase)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# run_phase — the driver.
# ---------------------------------------------------------------------------


def run_phase(
    phase: int,
    config: SweepConfig,
    runner=None,
    modes: Optional[frozenset[str]] = None,
) -> PhaseResult:
    runner = runner or SubprocessRunner()
    worklist, skipped, quarantined = _pending_worklist(phase, modes)
    token_cap = config.token_caps.get(phase)
    dollar_cap = config.dollar_caps.get(phase)
    price = config.price()

    completed = failed = 0
    tokens = 0
    dollars = 0.0
    aborted = False
    abort_reason: Optional[str] = None

    in_flight: dict[object, sc.RunSpec] = {}
    idx = 0

    def budget_breached() -> Optional[str]:
        if token_cap is not None and tokens > token_cap:
            return f"token cap exceeded ({tokens} > {token_cap})"
        if dollar_cap is not None and dollars > dollar_cap:
            return f"dollar cap exceeded ({dollars:.6f} > {dollar_cap})"
        return None

    while (idx < len(worklist) or in_flight) and not aborted:
        # Fill the pool — but never while STOP is present, and never two runs of
        # the same case concurrently (§2: parallelism is across different cases).
        while (
            not aborted
            and idx < len(worklist)
            and len(in_flight) < config.n_parallel
        ):
            if sc.STOP_FILE.exists():
                aborted, abort_reason = True, "kill file (results/STOP) present"
                break
            spec = worklist[idx]
            if any(s.case_id == spec.case_id for s in in_flight.values()):
                break  # would collide on a case already in flight; wait
            handle = runner.start(spec)
            in_flight[handle] = spec
            idx += 1

        # Reap finished children.
        done = [h for h in in_flight if runner.poll(h)]
        for h in done:
            spec = in_flight.pop(h)
            rc = runner.returncode(h)
            if rc == 0 and sc.record_is_complete(spec):
                completed += 1
                tin, tout = _record_tokens(spec)
                tokens += tin + tout
                dollars += sc.dollars(tin, tout, price)
            else:
                failed += 1
            reason = budget_breached()
            if reason and not aborted:
                aborted, abort_reason = True, reason
        if not done:
            time.sleep(config.poll_interval_s)

    # Drain any still-running children (graceful: let in-flight finish) so
    # completed records are preserved (§2).
    while in_flight:
        done = [h for h in in_flight if runner.poll(h)]
        for h in done:
            spec = in_flight.pop(h)
            if runner.returncode(h) == 0 and sc.record_is_complete(spec):
                completed += 1
        if not done:
            time.sleep(config.poll_interval_s)

    if aborted:
        _write_marker(phase, "ABORTED.json", {
            "phase": phase, "reason": abort_reason,
            "completed": completed, "skipped": skipped, "quarantined": quarantined,
            "tokens": tokens, "dollars": round(dollars, 6),
        })

    return PhaseResult(
        phase=phase, completed=completed, skipped=skipped, quarantined=quarantined,
        failed=failed, aborted=aborted, abort_reason=abort_reason,
        tokens=tokens, dollars=round(dollars, 6),
    )


def _parse_modes(phase: int, raw: Optional[str]) -> Optional[frozenset[str]]:
    """Parse ``--modes`` into a validated subset of the phase's modes, or None
    (all). Rejects unknown modes and modes not enumerated by the phase, so a
    typo can never silently run zero cells."""
    if raw is None:
        return None
    requested = [m.strip() for m in raw.split(",") if m.strip()]
    if not requested:
        raise SystemExit("--modes given but empty")
    allowed = set(sc.phase_modes(phase))
    unknown = [m for m in requested if m not in allowed]
    if unknown:
        raise SystemExit(
            f"--modes {unknown} not in phase {phase} modes {sorted(allowed)}"
        )
    return frozenset(requested)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Layer-4 sweep runner")
    parser.add_argument("--phase", type=int, required=True, choices=sc.PHASES)
    parser.add_argument("--n-parallel", type=int, default=4)
    parser.add_argument(
        "--modes", type=str, default=None,
        help="comma-separated subset of the phase's modes to execute "
             "(default: all). The §1 enumeration is unchanged; only the "
             "runner's worklist is filtered — e.g. --modes timeout for a "
             "timeout-only re-run of Phase 3.",
    )
    args = parser.parse_args(argv)
    modes = _parse_modes(args.phase, args.modes)
    config = SweepConfig(n_parallel=args.n_parallel)
    result = run_phase(args.phase, config, modes=modes)
    print(json.dumps(result.__dict__, sort_keys=True))
    return 1 if result.aborted else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
