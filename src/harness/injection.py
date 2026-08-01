"""injection.py — the Layer-3 runtime injection controller (grounding
HARNESS_GROUNDING_3_INJECTION.md §3, §6, §9).

SOURCE OF TRUTH: docs/HARNESS_GROUNDING_3_INJECTION.md v1.0; Layer-1/2 interfaces
binding.

Loads the precomputed ``data/injection_plan.json`` and implements the three
frozen seam signatures (`worker_timeout`, `hallucinate`, `rate_outage`) with
per-run firing state (§3.1–§3.3): fire once, repairs never re-forced, outage on
the first `rate_table_lookup` per case only then recovery. Exactly one mode per
run (`none | timeout | hallucination | outage`); never combined (§9).

**Imports ⊆ stdlib** (§1, §8). It does NOT import ``src.harness.tools`` (so the
label-isolation test on this module is trivially satisfied — no oracle module is
reachable). It is a structural drop-in for the seam interface used by the
orchestrator / S0 / ``tools.ToolContext`` (duck-typed), and additionally exposes
run-record marker accessors (§6): ``mode``, ``plan_sha256``, ``tau_for``,
``did_fire``, ``marker_details``.

Guardrail (§9): no controller state ever enters agent-visible content — the
injected record returned by ``hallucinate`` is the sole, intended exception. The
plan file is read-only here; regeneration (scripts/generate_injection_plan.py) is
the only write path.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Optional

MODE_NONE = "none"
MODE_TIMEOUT = "timeout"
MODE_HALLUCINATION = "hallucination"
MODE_OUTAGE = "outage"
MODES = (MODE_NONE, MODE_TIMEOUT, MODE_HALLUCINATION, MODE_OUTAGE)

DEFAULT_PLAN_PATH = Path(__file__).resolve().parents[2] / "data" / "injection_plan.json"


def load_plan(path: Path = DEFAULT_PLAN_PATH) -> dict:
    """Read the committed injection plan (read-only)."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


class InjectionController:
    """Runtime controller for one run under exactly one injection mode.

    Structural drop-in for the Layer-1/2 no-op ``tools.InjectionController``
    (same three seam methods), extended with the §6 marker accessors. A fresh
    instance is used per run so the firing state is per-run (§3)."""

    def __init__(
        self,
        mode: str = MODE_NONE,
        *,
        plan: Optional[dict] = None,
        plan_path: Path = DEFAULT_PLAN_PATH,
    ):
        if mode not in MODES:
            raise ValueError(f"unknown injection mode {mode!r}")
        self.mode = mode
        # mode 'none' needs no plan; other modes load it (or accept an in-memory
        # plan for targeted tests — tests never rewrite the committed file, §9).
        if mode == MODE_NONE:
            self._plan = plan  # may be None
        else:
            self._plan = plan if plan is not None else load_plan(plan_path)
        self.plan_sha256: Optional[str] = (
            self._plan.get("content_sha256") if self._plan else None
        )
        # Per-run fire-once state (§3.1–§3.3).
        self._timeout_fired: set[str] = set()
        self._halluc_fired: set[str] = set()
        self._outage_fired: set[str] = set()

    # -- plan accessors -----------------------------------------------------

    def _tau(self, case_id: str) -> Optional[str]:
        if not self._plan:
            return None
        return self._plan.get("tau_by_case", {}).get(case_id)

    def _outage_cases(self) -> frozenset[str]:
        if not self._plan:
            return frozenset()
        return frozenset(self._plan.get("outage_cases", []))

    # -- seam 1: worker_timeout (§3.1) --------------------------------------

    def worker_timeout(self, case_id: str, subtask: str) -> bool:
        """Fire iff mode=timeout, case in plan, τ matches, and this is the first
        (initial) consult for the case — repairs are never re-forced (§3.1)."""
        if self.mode != MODE_TIMEOUT or not self._plan:
            return False
        if case_id in self._timeout_fired:
            return False
        if self._tau(case_id) == subtask:
            self._timeout_fired.add(case_id)
            return True
        return False

    # -- seam 2: hallucinate (§3.2) -----------------------------------------

    def hallucinate(self, case_id: str, subtask: str, record: dict) -> Optional[dict]:
        """Return the plan's precomputed record for (case, τ, first line),
        replacing the emitted one; fires once per case on the τ-owning
        invocation's initial payload (§3.2). The first-line target is realized by
        firing on the FIRST consult for (case, τ): the orchestrator/S0 iterate
        line records in case order, so the first consult is the first line."""
        if self.mode != MODE_HALLUCINATION or not self._plan:
            return None
        if case_id in self._halluc_fired:
            return None
        if self._tau(case_id) == subtask:
            self._halluc_fired.add(case_id)
            return copy.deepcopy(self._plan["hallucinated_record_by_case"][case_id])
        return None

    # -- seam 3: rate_outage (§3.3) -----------------------------------------

    def rate_outage(self, case_id: Optional[str]) -> bool:
        """Fire iff mode=outage, case ∈ outage_cases, and this is the FIRST
        rate_table_lookup for the case in this run; recovers thereafter (§3.3)."""
        if self.mode != MODE_OUTAGE or not self._plan or case_id is None:
            return False
        if case_id not in self._outage_cases():
            return False
        if case_id in self._outage_fired:
            return False
        self._outage_fired.add(case_id)
        return True

    # -- §6 run-record marker accessors -------------------------------------

    def tau_for(self, case_id: str) -> Optional[str]:
        return self._tau(case_id)

    def did_fire(self, case_id: str) -> bool:
        return (
            case_id in self._timeout_fired
            or case_id in self._halluc_fired
            or case_id in self._outage_fired
        )

    def marker_details(self, case_id: str) -> dict:
        """Mode-specific details for the run-record injection marker (§6)."""
        if self.mode == MODE_NONE or not self._plan:
            return {}
        if self.mode == MODE_TIMEOUT:
            return {"forced_subtask": self._tau(case_id)}
        if self.mode == MODE_HALLUCINATION:
            return {"target_subtask": self._tau(case_id), "target_line": "first"}
        if self.mode == MODE_OUTAGE:
            return {"is_outage_case": case_id in self._outage_cases()}
        return {}


def make_controller(
    mode: str = MODE_NONE,
    *,
    plan: Optional[dict] = None,
    plan_path: Path = DEFAULT_PLAN_PATH,
) -> InjectionController:
    return InjectionController(mode, plan=plan, plan_path=plan_path)


# The §6 run-record marker ({mode, tau, fired, plan_sha256, details}) is built by
# the run-record writers (orchestrator.py / s0.py) from these accessors — see
# orchestrator._injection_marker. Keeping the builder there (Layer 2) rather than
# here avoids a Layer-2 → Layer-3 import; this module stays stdlib-only (§1, §8).
