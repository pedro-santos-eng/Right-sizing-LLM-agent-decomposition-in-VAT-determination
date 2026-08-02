# HARNESS_GROUNDING_4_SWEEP.md

**Version 1.0 — 2026-08-02.** Layer-4 source of truth for Part 2. The four
design decisions marked **[DECISION n]** were **ratified on 2026-08-02**
(§9). Order of
authority: (1) `ORACLE_GROUNDING.md` + frozen Part-1 code, (2) the paper —
§6.1 (metrics), §6.3 (matched-token), §6.4 (injection cells), §6.5
(statistics), §6.6 (falsification), §6.7 (isolation + artifacts) govern this
layer, (3) grounding docs 1–3 (v1.1 / v1.1 / v1.0), (4) this document.
Layers 1–3 interfaces are binding; the L2 §11 dry-run protocol is executed
through this layer, unchanged.

---

## 0. Scope

**In scope:** the sweep runner (matrix enumeration, per-run process
isolation, resume, budget guards); the offline scoring pass (raw traces →
scored table, implementing every §6.1 rule); the offline analysis pass
(scored table → all reported numbers, implementing §6.5–§6.6); the pinned
price sheet; the dry-run entry point (L2 §11). **Out of scope:** executing
the dry run or any live phase (separate go/no-go decisions); §7/§8 writing;
VM provisioning; any change to Layers 1–3 or the frozen trees.

## 1. Run matrix and phases **[DECISION 1]**

A *run* is one (phase, mode, condition, case, repeat) execution producing one
raw record. Repeats: **R = 5** (§6.3/§6.5); the repeat index is bookkeeping
only — the API has no sampling seed, so repeats are honest stochastic
samples under the pinned decoding constants.

| Phase | Mode | Conditions | Cases | Runs |
|---|---|---|---|---|
| 0 — dry run (L2 §11) | none | S0, C1–C4 | dev_001..005, R=1 | 25 (dev; never reported) |
| 1 — main sweep | none | S0, C1–C4 | 40 eval, R=5 | 1,000 |
| 2 — matched-token confirmatory | none | S0′_C2 | 40 eval, R=5 | 200 (after B̄_C2 from Phase 1; tuning on dev only, §6.3) |
| 3 — injections | timeout, hallucination, outage | S0, C1–C4 | 40 eval, R=5 | 3,000 |
| 4 — post-selection sensitivity | none | S0′_C⋆ | 40 eval, R=5 | 200 (after C⋆ identified) |

Total: 4,400 evaluation runs + 25 dev. **S0′ variants are excluded from
injection cells** — §6.4 injects "into each condition"; the S0′ variants are
RQ3 controls, not conditions, and appear in no §6.4 cell. Phases are strictly
ordered; a phase starts only on explicit go-ahead recorded in the DEVLOG
(Phase 1 additionally requires the three L2 §11 dry-run gates green).

## 2. Isolation, records, resume **[DECISION 2]**

Per §6.7, each run executes in a **fresh OS process** with no shared mutable
state: the parent runner enumerates the matrix and spawns
`python -m scripts.run_one <phase> <mode> <cond> <case> <repeat>` per run;
the child builds its orchestrator/S0 world from scratch, executes, writes
its record, exits. The parent never imports harness execution modules.

- **Record path:** `results/raw/<phase>/<mode>/<cond>/<case>/r<k>.json` —
  append-only, one file per run, schema-checked at write. The record embeds
  the full L2/L3 accounting (per-call usage, latency, retries, seams,
  `injection` marker, `plan_sha256`), the emitted or partial trace, terminal
  status, and case wall-clock start/end.
- **Completion invariant:** a run is complete iff its record file exists and
  validates. **Resume = re-enumerate and skip complete runs.** No run is
  ever overwritten; a corrupt record is moved to `results/quarantine/` and
  the run re-executed.
- **Budget guards:** per-phase caps on total tokens and total dollars
  (pinned price sheet, §5) plus a global kill file
  (`results/STOP` → finish in-flight children, exit). Cap breach aborts the
  phase, preserves all completed records, and writes an abort marker; caps
  are configuration, set from the Phase-0 cost projection (L2 §11 gate 3).
- **Concurrency:** the parent may run up to `N_PARALLEL` children (default
  4) across *different* cases; within-case concurrency remains the L2
  orchestrator's own (C4 cap 2). Runs of the same (case, mode) across
  conditions need not be serialized — pairing is by identity, not timing.

## 3. Label isolation at process level

The run child imports the harness only; **`labeler`/`scorer` are absent from
the child's import graph** (extend the import-graph test to
`scripts/run_one.py`: `scorer` unreachable, no direct `labeler` import —
the frozen `validator` type-edge excepted, as ratified for the
orchestrator). Oracle labels enter only in §4's offline pass, after the raw
records exist.

## 4. Offline scoring pass **[DECISION 3]**

`scripts/score_runs.py` (offline; imports `scorer`/`labeler` freely) reads
`results/raw/**`, scores every record against the oracle, and writes ONE
tidy table `results/scored.csv` — a row per run with: identifiers (phase,
mode, condition, case, repeat); final-answer accuracy; the six §3.1
field-level indicators; per-τ step accuracy (missing → recorded missing,
counted incorrect end-to-end, §6.1); trace consistency; error type
(*earliest failing subtask*; same-layer ties all recorded in an auxiliary
column, single label by the fixed order CLS, JUR, RAT, EXM, RCH); terminal
flag (terminal ⇒ final-answer incorrect + trace-inconsistent, cost/latency
in full); prompt/completion tokens, tool calls, retry counts, dollar cost
(derived, §5); wall-clock latency; substitution-success indicator (injected
phases). Scoring is deterministic and re-runnable; the CSV is derived data —
raw records remain the artifact of record (§6.7). Dependency policy: runner
and `run_one` are stdlib-only; scoring/analysis may use pandas/numpy.

## 5. Pinned price sheet

`data/price_sheet.json` — committed: model string, USD per 1M input tokens,
USD per 1M output tokens, retrieval date. Dollar cost is derived from token
counts at scoring time (§6.1: "derived from token cost rather than an
independent metric"). Any price change during the study is a new file
version, recorded in the DEVLOG; reported dollars use one pinned sheet.

## 6. Offline analysis pass **[DECISION 4]**

`scripts/analyze.py` (offline) reads `results/scored.csv` and emits every
reported number to `results/analysis/` as CSV tables, implementing §6.5
exactly:

- Aggregate R=5 to case level: mean accuracy, mean token cost, median
  latency.
- **Case-clustered paired bootstrap**, 1,000 resamples at case level, 95%
  percentile CIs on paired differences; `bootstrap_seed = 20260805`.
- **Paired permutation tests**, 1,000 permutations of within-case labels,
  for the pre-specified family; `permutation_seed = 20260806`.
- **Holm–Bonferroni across exactly the four headline tests** (S0–C1, C1–C4,
  C2–C1, S0′_C2–C2); everything else descriptive, outside the family.
- **Cohen's d_z** = mean(paired diffs) / sd(paired diffs, ddof=1) over
  case-level summaries; sign follows the named contrast; the §6.6
  materiality rule (CI excludes zero positively AND d_z ≥ 0.2) computed and
  emitted per contrast.
- Mann–Whitney U on case-level aggregates, descriptive column only.
- §6.4 cell metrics per (injection, configuration): substitution success
  rate, all-case accuracy under injection, validated-trace accuracy
  (secondary), and cost penalty vs. the paired un-injected baseline.
- The three §6.6 falsification criteria evaluated mechanically and emitted
  as a yes/no table with the supporting numbers.

Latency inference reports medians with bootstrap CIs only — **no p95/p99**
(§6.1). All seeds, resample counts, and the family definition are constants
at the top of the script; changing any of them is a DEVLOG-recorded event.

## 7. Dry-run entry point

`python -m scripts.sweep --phase 0` executes L2 §11 verbatim through the
same runner/isolation path (dev_001..005, S0+C1–C4, R=1, mode none, real
API). Its three gates (C1 accuracy 40–90%; extraction/parse failure < 10%;
cost projection within budget) are evaluated by `score_runs.py` +
`analyze.py --phase 0` and the projection is recorded in the DEVLOG before
any Phase-1 go-ahead. Dry-run records live under `results/raw/phase0/` and
never enter reported results.

## 8. Tests and readiness gate

`tests/test_sweep.py` against a scripted ModelClient (no API): matrix
enumeration reproduces the §1 counts exactly (25 / 1,000 / 200 / 3,000 /
200); resume skips complete runs and quarantines a corrupted record;
child-process isolation smoke (one real subprocess round-trip, scripted);
records schema-valid with injection markers in injected phases; budget-cap
abort preserves completed records; kill-file honored.
`tests/test_scoring.py`: hand-built synthetic records reproduce every §6.1
rule — terminal scoring, missing-step handling, earliest-failing-subtask
with a same-layer tie, dollar derivation. `tests/test_analysis.py`: a tiny
fixture with hand-computed bootstrap/permutation/d_z under the pinned seeds
reproduces the expected numbers; Holm ordering verified on a constructed
p-value set; §6.6 criteria evaluated on constructed outcomes.
Import-graph test extended to `run_one`. Live smoke stays
skip-if-unconfigured.

**Gate:** all of the above green alongside the untouched 167+27; zero diffs
under the frozen trees; `results/` gitignored except a committed
`results/README.md` describing the layout. Phase 0 execution is NOT part of
the gate — it is the first live decision after it.

## 9. Decisions — all four ratified 2026-08-02

1. **[DECISION 1]** The five-phase matrix of §1, including S0′ exclusion
   from injection cells and strict phase ordering with DEVLOG-recorded
   go-aheads. Total 4,400 eval runs + 25 dev.
2. **[DECISION 2]** Process-per-run isolation with append-only per-run
   records, skip-complete resume, quarantine, per-phase token/dollar caps +
   kill file, parent parallelism default 4 across cases.
3. **[DECISION 3]** Raw/scored split: stdlib-only runner writes raw records
   with no oracle access; offline `score_runs.py` (pandas allowed) produces
   the single tidy `results/scored.csv` implementing every §6.1 rule.
4. **[DECISION 4]** Analysis pins: `bootstrap_seed 20260805`,
   `permutation_seed 20260806`, 1,000/1,000 draws, percentile CIs, d_z
   formula as §6, Holm over exactly the four named tests, mechanical §6.6
   evaluation, no tail-percentile latency.