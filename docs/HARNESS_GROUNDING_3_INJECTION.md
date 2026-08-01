# HARNESS_GROUNDING_3_INJECTION.md

**Version 1.0 — 2026-08-01.** Layer-3 source of truth for Part 2. The four
design decisions marked **[DECISION n]** were **ratified on 2026-08-01**
(§11). Order of
authority: (1) `ORACLE_GROUNDING.md` + frozen Part-1 code, (2) the paper —
§6.4 is the governing specification for everything in this document, with
§6.1, §6.7, §4.6 supporting, (3) `HARNESS_GROUNDING_1_SURFACE.md` v1.1,
(4) `HARNESS_GROUNDING_2_ORCHESTRATION.md` v1.1, (5) this document. Layer-1
and Layer-2 interfaces are binding; nothing here may redefine them.

---

## 0. Scope

**In scope:** the concrete `InjectionController` implementing the three seam
signatures Layer 1/2 froze (`worker_timeout(case_id, τ) -> bool`,
`hallucinate(case_id, τ, record) -> dict | None`,
`rate_outage(case_id) -> bool`); the deterministic injection plan artifact and
its offline generator; run-record injection markers; the
`TOOL_CAP_EXHAUSTED` marker (DEVLOG 2026-08-01 follow-up); tests; the §10
gate.

**Out of scope:** the sweep runner, repeats, aggregation, and the four §6.4
reported metrics (Layer 4 computes them from run records); executing the
five-case dry run (L2 doc §11, at the Layer-3/4 boundary, mode `none`); any
modification under `src/oracle/`, `src/schemas/`, `data/eval_cases/`,
`data/dev_cases/`.

## 1. Injection plan artifact **[DECISION 1]**

Injection content is **precomputed offline**, not generated at runtime.

- `scripts/generate_injection_plan.py` — offline generator. It **may** import
  `src.oracle.labeler` (guaranteeing oracle-incorrectness requires the oracle
  label at generation time). It is imported by no `src/harness/` module.
- `data/injection_plan.json` — the committed artifact:
  `{injection_seed, tau_by_case, hallucinated_record_by_case,
  outage_cases, generator_version, content_sha256}` over the 40 evaluation
  cases. Contains only oracle-*incorrect* content; no true label appears in
  the file.
- `src/harness/injection.py` — runtime controller. Loads the JSON; imports
  ⊆ {stdlib}. Implements the three seam methods plus per-run firing state.

Rationale: the Layer-2 close recorded the orchestrator's import graph as
validated; runtime generation would route `labeler` into it. Precomputation
keeps runtime isolation intact by construction, makes §6.7's released
artifacts ("failure-injection seeds, target-subtask assignments,
affected-case lists") literal committed files, and lets determinism be tested
as byte-equality of regeneration.

## 2. Target-subtask sampling **[DECISION 2]**

Per paper §6.4: one target τ per case, deterministic from a fixed seed, the
same τ across all conditions and repeats. The paper leaves the distribution
and two mechanics open; proposed:

- **Distribution:** uniform over `T = {CLS, JUR, RAT, EXM, RCH}`, independent
  per case.
- **Seed:** `injection_seed = 20260801` (date-derived, self-documenting;
  deliberately distinct from the dataset seed 42 so the two artifacts are
  uncoupled).
- **Line selection:** CLS/RAT/EXM/RCH are per-line records; the paper's "the
  structured record for subtask τ is replaced" is singular. The injected
  record targets the **first line in case order** (lowest `line_id`).
  Timeout and outage are invocation- and tool-level and need no line choice.
- `tau_by_case` and the affected-line ids are released per §6.7.

## 3. Seam behaviors

All three fire only when the run's injection mode selects them; exactly one
mode per run: `none | timeout | hallucination | outage`.

### 3.1 `worker_timeout(case_id, τ)` — mode `timeout`

Fires iff `case_id` is in the plan, τ equals the plan's τ for that case, and
this is the **initial** invocation of the responsible worker for that case.
Repairs are never re-forced: the paper specifies a forced timeout at the
fixed threshold followed by the condition's *natural* repair mechanisms
within its budgets. For S0, the affected unit is the trace-generation call.
Controller keeps a fired-set per run; logged as `SEAM_WORKER_TIMEOUT`
(already wired in Layer 2).

### 3.2 `hallucinate(case_id, τ, record)` — mode `hallucination`

At the existing interception point (between payload extraction and
validation), returns the plan's precomputed record for (case, τ, first line),
replacing the emitted one; fires once per case, on the τ-owning invocation's
initial payload (S0: the τ slot of the emitted trace). Logged as
`SEAM_HALLUCINATED_OUTPUT`. Validation then proceeds normally — see §4 for
the consistency requirement that makes this the silent-error probe.

### 3.3 `rate_outage(case_id)` — mode `outage`

Fires iff `case_id ∈ outage_cases` and this is the **first**
`rate_table_lookup` call for that case in this run; the tool returns its
structured transient error and recovers on every subsequent call. First-call
state lives in the controller keyed per (run, case); the Layer-1
`ToolContext` already passes the active case id through.

## 4. Hallucinated-record construction **[DECISION 3]**

The operational meaning of the paper's "schema-conforming … plausible-but-
wrong rule citation": the injected record must pass **record-level**
validation (`validate_record`: schema, citation presence, citation–decision
consistency, RAT-vs-table) so that no subtask retry fires at the interception
point. Whether it then survives the assembly-time `validate_trace` gate or a
downstream worker's context is precisely the measured phenomenon — the
validator is input-blind by design, which is why an internally consistent
wrong record can validate. Per-subtask recipe (wrong decision + *that*
decision's canonical citation + internally consistent fields):

| τ | Perturbation (deterministic from the case) | Citation emitted |
|---|---|---|
| CLS | next category in fixed vocabulary order that differs from oracle | `CLS.ASSIGNED` |
| JUR | the alternate (jur_path, jurisdiction) pair: domestic ↔ intra_community_b2b with jurisdiction switched supplier ↔ customer country; b2c → domestic-at-supplier | the switched path's key |
| RAT | flip band standard ↔ reduced (exempt-slot oracle → standard) with the **flipped band's table rate** for the decided jurisdiction | the flipped band's key |
| EXM | negate `exempt` | `EXM.NONE` ↔ `EXM.EXEMPT_SUPPLY` |
| RCH | next outcome in {standard_charge, reverse_charge, exempt} differing from oracle, with that outcome's full consistent field set (`reverse_charge`, `liable_party` — `none` for exempt per amended §2.1 —, `non_charging_reason`, `vat_amount` arithmetically consistent) | the outcome's key |

The generator asserts, offline, both properties for every record:
`validate_record` passes AND the record differs from the oracle label.

## 5. Outage block structure

The 40 evaluation cases form 4 scenario families × 10; each family splits
into 2 blocks of 5 in case-id order → 8 blocks; the seed selects exactly one
case per block → 8 affected cases (20%), `m = 1` fixed. Selection is
independent of scheduling; the list is committed in the plan and released
per §6.7.

## 6. Run-record markers

Every run record echoes
`injection: {mode, tau, fired, plan_sha256, details}` into the progress
ledger's existing injection-marker slot (L2 §3.1). Additionally, the worker
tool loop tags cap exhaustion distinctly: when `MAX_TOOL_ITERATIONS` is
reached while the last response still requests tools, the turn's
`extraction_error` is `"TOOL_CAP_EXHAUSTED"` (DEVLOG 2026-08-01 follow-up),
so Layer-4 analysis can attribute these terminals separately from ordinary
no-fenced-block failures.

## 7. Pairing semantics **[DECISION 4]**

The paper's "separate copy of the 40-case set" is realized as a **run mode
over the same frozen files**, not a second dataset on disk: injected and
un-injected runs are paired by case identity. The plan artifact's
`content_sha256` is recorded inside the file and echoed into every injected
run record; `scripts/freeze_dataset.py` and the Part-1 manifest are **not**
touched. The plan is immutable once the dry run begins.

## 8. Module and test layout

```
scripts/generate_injection_plan.py     (offline; may import oracle)
data/injection_plan.json               (committed artifact)
src/harness/injection.py               (runtime controller; stdlib only)
tests/test_injection.py
```

`tests/test_injection.py` covers: regeneration determinism (byte-equal to the
committed plan); record-level validation pass + oracle-incorrectness for
every hallucinated record (this test module may import `labeler` — it is not
an agent-context module); timeout fires once on the initial responsible
invocation then natural repair proceeds (scripted client); outage fires on
exactly the 8 planned cases, first RAT lookup only, with observed recovery;
S0 τ-slot substitution; markers present in run records for all four modes;
`TOOL_CAP_EXHAUSTED` emitted at cap exhaustion. `test_label_isolation.py`
extends to `injection.py` (fresh interpreter; `labeler`/`scorer` absent).

## 9. Guardrails

All Layer-1 §11 and Layer-2 §9 rules remain in force. Additionally: no
controller state enters agent-visible content (the injected record itself is
the sole, intended exception); injection never alters the frozen case files;
the plan file is never edited by hand — regeneration is the only write path;
one mode per run, never combined.

## 10. Readiness gate — Layer-3 definition of done

Layer 4 does not start until:

- [ ] Plan artifact committed; regeneration is byte-identical
- [ ] Every hallucinated record passes `validate_record` AND differs from its
      oracle label (asserted offline in the generator and in tests)
- [ ] Timeout: initial responsible invocation forced at the §4.6 threshold;
      repairs proceed naturally; statuses and logs correct (scripted)
- [ ] Outage: exactly the 8 planned cases; first RAT lookup only; recovery
      observed; logged
- [ ] S0 τ-slot substitution covered
- [ ] Injection markers echoed in run records for all four modes
- [ ] `TOOL_CAP_EXHAUSTED` distinct and tested
- [ ] Label-isolation test extended to `injection.py`, green
- [ ] Whole suite green alongside untouched Part-1 (27) and Layers 1–2
- [ ] Zero diffs under `src/oracle/`, `src/schemas/`, `data/eval_cases/`,
      `data/dev_cases/`

## 11. Decisions — all four ratified 2026-08-01

1. **[DECISION 1]** Precomputed plan artifact + offline generator (labeler
   allowed offline only) vs runtime generation. Ratified: precomputed.
2. **[DECISION 2]** Sampling mechanics: uniform over T; `injection_seed =
   20260801`; first-line targeting for per-line subtasks.
3. **[DECISION 3]** The per-subtask perturbation table in §4, and the
   record-level reading of "satisfies 𝒱" at the interception point.
4. **[DECISION 4]** Pairing as a run mode over the frozen files (no second
   dataset); plan hash echoed in run records; Part-1 freeze untouched.
