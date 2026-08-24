# Development Log

## 2026-08-24 — Release metadata: CITATION.cff, tool-agnostic grounding headers, v1.0.0 repositioned

Metadata-only pass ahead of the Zenodo deposit. No re-runs; `results/`, `data/`,
`src/`, and every hash are untouched. Three files changed, two commits, and the
`v1.0.0` tag was moved onto the new HEAD.

### CITATION.cff field changes

| Field | Before | After | Why |
| --- | --- | --- | --- |
| `authors[0].affiliation` | absent | `Independent Researcher` | Matches the arXiv v1 author block |
| `authors[0].email` | absent | personal address | Single canonical contact across paper, repo, Zenodo |
| `license` | `Apache-2.0` | list incl. `CC-BY-4.0` | Repo is dual-licensed; `data/LICENSE` is CC-BY-4.0 |
| `repository-code` | absent | GitHub URL | Zenodo reads this into the deposit metadata |
| `date-released` | `2026-08-07` | `2026-08-24` (release date) | Was stale |

Validated with `cffconvert --validate -i CITATION.cff` → valid against schema
1.2.0. No `.zenodo.json` was created — it would silently override `CITATION.cff`.
The DOI is deliberately absent: it does not exist until the GitHub Release fires
(done manually after the Zenodo toggle).

### Grounding-header wording (tool-agnostic)

`docs/HARNESS_GROUNDING_1_SURFACE.md` and `docs/HARNESS_GROUNDING_2_ORCHESTRATION.md`
each had one header line changed: "Claude Code reads this before touching" →
"Implementation tooling reads this before touching". **No semantic change** — the
authority order, the stop-and-flag rule, and everything below the header are
byte-for-byte identical. Past DEVLOG entries (including the 2026-08-04
history-rewrite entry) are records and were left verbatim; only the two living
contract docs get the phrasing change.

### v1.0.0 repositioned onto the new HEAD

The annotated `v1.0.0` tag (message "Artifact for the pilot controlled sweep
(paper v1)") was deleted and recreated on the new HEAD, then force-updated on the
remote. Safe because, at the time of the move:

1. **No GitHub Release exists** for `v1.0.0` (verified against the Releases API —
   empty);
2. **Software Heritage returned 404** for this origin — nothing archived to
   dangle;
3. **No Zenodo deposit** references the tag yet.

The tag stays annotated; it now dereferences to the grounding-header commit
(`5db92a6`). This DEVLOG entry is committed *after* the reposition, so the tagged
tree is not disturbed.

### Provenance correction — dangling anonymous-snapshot pointer

The anonymous-snapshot pointer `a60697b` no longer resolves: the earlier
`git filter-repo` rewrite (see the 2026-08-04 entry) changed every commit SHA in
the history. The **tree content is unaffected** — only the recorded pointer is
now dangling. Any external reference to `a60697b` should be treated as historical;
the current canonical pointer is the `v1.0.0` tag.

## 2026-08-07 — EXPERIMENTAL PROGRAM FINAL CLOSE (RQ1 fired, RQ2 not fired, RQ3 fired); §7 paper tables emitted

The sweep is complete. The final dataset is `results/scored.csv` at 4,400 runs and
`scripts/analyze.py` now emits every §7/§8 number, including the three headline
paper tables added this commit (`main_table`, `error_types`, `s0_family`). All
three reuse the §8 injection-delta bootstrap engine verbatim — `numpy.default_rng(42)`,
1,000 case-clustered resamples, percentile [2.5, 97.5] — generalised only so the
latency column bootstraps the median while accuracy/tokens bootstrap the mean.

Final falsification verdicts (`results/analysis/falsification.csv`, §6.6):
  - **RQ1 — intermediate-optimum: FIRED** (criterion triggered ⇒ hypothesis
    unsupported for this workload). Neither C2 nor C3 materially outperforms BOTH
    endpoints C1 and C4 on final-answer accuracy under the ratified materiality
    rule (bootstrap CI excludes zero positively AND d_z ≥ 0.2): C2>C1 and C3>C1
    hold, but C2>C4 and C3>C4 do not; the C1–C4 sequence is non-monotonic.
  - **RQ2 — orchestration benefit: NOT FIRED.** S0 does not weakly Pareto-dominate
    all of C1–C4 on the (token-cost, accuracy) plane: S0 (0.755, 11899.4) is beaten
    on accuracy by C2/C3 (0.830) and undercut on tokens by C1 (11316.7). The
    orchestrated-monolith advantage is therefore not falsified.
  - **RQ3 — prompt-budget confound: FIRED by the preregistered letter.** The
    S0'_C2 vs C2 accuracy difference is not significant under the paired
    permutation test with Holm–Bonferroni correction AND its paired bootstrap CI
    includes zero — the exact conjunction §6.6(3) requires. Point estimate
    −0.065 (d_z ≈ −0.20), CI [−0.165, +0.025]. The point DIRECTION favours C2, but
    per §6.5 this is reported as INCONCLUSIVE, not a null: "any C2 main-sweep
    advantage is consistent with a prompt-budget explanation." §7 MUST carry the
    §6.5 inconclusive-not-null framing; do not read the negative point estimate as
    evidence that C2 beats the budget-matched monolith.

S0-family matched-token check (this is the fix from the 2026-08-06 THIRD-DEFECT
entry landing on real data). Both tuned arms now resolve to knob rung **L5** and
assemble the IDENTICAL tuned prompt — prompt hash `3d56a837…` for both
`S0prime_C2` and `S0prime_Cstar`, distinct from plain S0's `9d7ba74f…` (disclosed:
the two arms are the same tuned monolith). On the eval set both land IN BAND
against their targets (±10%): `S0prime_C2` 13230.2 tokens vs target 14590.085 →
−9.3%; `S0prime_Cstar` 12864.4 vs 13590.655 → −5.3%. Because both arms carry the
same tuned prompt, **phase 4 is an independent replication** of the S0′ result
rather than a second tuning target: eval accuracy 0.765 (C2 arm) and 0.760 (C*
arm) — mutually consistent, and consistent with S0's 0.755. C* remains C3 by the
2026-08-06 tie-break (C2/C3 tie at 0.830; C3 cheaper).

Final dataset provenance — 4,400 runs = phase 1 (attempt 2) + phase 2 (attempt 2,
tuned S0′) + phase 3 (hallucination/outage + timeout attempt 2) + phase 4
(attempt 2). Archived, retained-not-deleted: phase 1 attempt 1, phase 3 timeout
attempt 1, and the degenerate phase 2/4 attempt 1 (S0′ == plain S0; see the
2026-08-06 entry). Program cost ≈ $142.

Reference §7 values reproduced by `main_table` (case-level mean acc + 95%
case-clustered CI; latency = median of per-case median latencies, seconds):
S0 0.755 [0.665, 0.840] 11.8 s; C1 0.720 [0.625, 0.810] 12.1 s; C2 0.830
[0.750, 0.910] 15.8 s; C3 0.830 [0.745, 0.915] 16.7 s; C4 0.770 [0.685, 0.855]
18.4 s. Point estimates match to the reported precision; the seed-42 CIs sit
within a bootstrap-notch of the earlier reference bounds (C3 lower 0.745 vs an
earlier 0.735).

Next: paper writing (§7 results, §8 injection, §9 discussion, §11 limitations —
carrying the §6.5 inconclusive-not-null RQ3 framing and the §6.4 D1-A sentence)
plus harness hardening. No further sweep runs are planned.

## 2026-08-06 — THIRD DEFECT: S0′ arms ran degenerate (S0′ == plain S0); §6.3 tuning loop delivered, silent fallback killed, C* ratified

The RQ3 matched-token controls never executed as pre-registered. Phases 2
(`S0prime_C2`) and 4 (`S0prime_Cstar`) ran with EMPTY knobs — i.e. plain S0 —
so the "budget-matched S0′" comparison has, until now, been S0-vs-S0.

Evidence (triangulated, from the committed `results/scored.csv`):
  - Prompt hash. Phases 2/4 recorded S0′'s prompt hash as `9d7ba74f…`, which
    is EXACTLY `s0_prompt_hash()` for the DEFAULT (empty) `S0Knobs` — the
    tuned knobs would have changed the assembled prompt and thus the hash.
  - Tokens. Mean total_tokens: phase-2 `S0prime_C2` 11597.7, phase-4
    `S0prime_Cstar` 11782.9 — both hugging the plain-S0 phase-1 mean 11899.4
    and nowhere near their budget targets (14590.085 and 13590.655).
  - Accuracy. 0.755 (C2 arm) and 0.750 (C* arm) — statistically the S0
    phase-1 accuracy 0.755, as expected when the agent context is identical.

Three silent layers had to line up for this to ship unnoticed:
  1. L4 shipped WITHOUT the tuning loop. The L2 header (`s0.py` §6) states
     plainly: "The tuning LOOP itself is Layer 4; Layer 2 provides the knobs
     and the measurement." Layer 4 delivered the sweep matrix and the two
     `S0prime_*` cells but never the loop that fills the knobs — the one
     deliverable that makes the cells meaningful.
  2. `run_one._s0_knobs` fell back to plain S0 SILENTLY. Its comment even
     invited it ("Absent → plain S0 knobs, so the enumeration/dispatch is
     exercisable without the tuning") — a test convenience that became a
     production footgun: with no knobs file, phases 2/4 ran and looked
     healthy.
  3. The knobs directory was GITIGNORED (`/results/*`). Even had the loop
     run and written `results/s0prime_knobs/*.json`, the artifact could not
     propagate to the (fresh-process) phase-2/4 children on another machine —
     it would never be committed.
  Process gap: the chained "phases 2 & 4 GO" skipped the §6.3 step that must
  sit BETWEEN phase-1 scoring (which defines the budget targets) and phase 2.
  No gate asserted "S0′ knobs exist and are non-plain" before launch.

Consequence. RQ3 / §6.6(3) — "does a budget-matched monolith close the gap to
the orchestrated pipeline?" — is UNEVALUATED as pre-registered; the two 200-run
arms are re-designated attempt 1 (degenerate). They are retained, not deleted:
as an unintended 600-run replication (with phase-1 S0) of S0's none-mode
stability, they are footnote material (accuracy 0.750–0.755 across three
independent 200-run draws), not a headline.

C* ratification (DECISION, dated 2026-08-06). C* = argmax mean final-answer
accuracy over C1–C4, ties broken by LOWEST mean total token cost
(Pareto-consistent). Applied to the phase-1/mode-none frame: C2 and C3 TIE at
0.830 accuracy; C3 is cheaper (13590.655 < 14590.085 mean tokens) ⇒ **C* = C3**.
The rule was fixed AFTER observing the exact tie (disclosed for what it is);
§7 will state the rule and the tie together. The resolution is versioned to
`results/s0prime_knobs/Cstar.json` {cstar, rule, basis} so it is never silently
re-decided.

Fix (this commit; execution deferred to the re-run):
  - `scripts/tune_s0prime.py` — the §6.3 loop. Computes the budget targets from
    `scored.csv` (echoes 14590.085 / 13590.655), walks a deterministic knob
    ladder over the three sanctioned S0Knobs slots (extended role, dev-derived
    exemplars, scratchpad instruction; exemplars rendered ONLY from
    dev_001..dev_008 with oracle labels — asserted never to touch an eval case),
    measures each rung by running S0′ on the 8 dev cases (R=1) through `run_s0`
    with a TEMP out-root, and greedy-brackets to within ±10 % of target. Writes
    the tuned `S0prime_*.json` (exact `run_one` schema) + a `tuning_log_*.json`.
  - `run_one._s0_knobs` — loud-fail: a `S0prime_` condition with no committed
    knobs now raises `SystemExit`. The silent plain-S0 fallback is REMOVED.
  - `.gitignore` — the knobs dir is un-ignored (versioned artifact); a tuned
    `S0prime_C2.json` is now TRACKED (verified with `git check-ignore`).
  - Tests: ladder determinism, knob IO round-trip against `_s0_knobs`, run_one
    loud-fail, the C* tie-break (tied frame → C3), and the bracket walk on
    scripted token counts. `pytest` 247 passed, 2 skipped.

Re-run plan (~$10–12): tune both conditions live → commit the knobs (+ logs +
Cstar.json) → smoke one `S0prime_C2` child → re-run phases 2 and 4 (attempt 2,
2×200 runs) → re-score → update §6.6(3)/RQ3. Phases 2/4 attempt-1 archived, not
scored.

## 2026-08-06 — EXPERIMENTAL SPRINT CLOSED: substitution-semantics resolved (two columns), injection-delta table banked, pre-registered timeout prediction confirmed 4/5 & exceeded in C1

Final scoring/analysis commit of the experimental program. Scope: two
scorer columns, one new analysis table, DEVLOG; no harness/prompt/data
change (§6.7 provenance: every reported number now comes from a committed
script).

Pre-registered prediction (recorded BEFORE the Phase-3 timeout re-run):
the forced-timeout perturbation is absorbed — per-case accuracy deltas vs
the un-injected Phase-1 baseline straddle zero for every condition.
Outcome (`results/analysis/injection_deltas.csv`, seed-42 case-clustered
percentile bootstrap): CONFIRMED in 4/5 cells —
  S0 −0.020 [−0.110, +0.065], C2 −0.020 [−0.095, +0.045],
  C3 +0.005 [−0.030, +0.045], C4 +0.005 [−0.050, +0.070],
all CIs straddling zero (substitution_success 0.965–1.0, availability
perturbation absorbed) — and EXCEEDED in C1: delta +0.160,
CI95 [+0.060, +0.270], excludes zero. Descriptive status only: C1 is
outside the §6.5 pre-specified Holm family, so this is reported as a
descriptive effect, not a confirmatory rejection.

Mechanism. The post-9f7c298 forced timeout triggers a full-SCOPE redraw
behind the validation filter (draw again, keep only a validated trace),
NOT a targeted single-τ repair; the accuracy gain therefore scales with
the injected worker's scope: C1 (full pipeline in one worker) +0.160 >
C2/C3 (half-scope) ≈ 0 > C4 (single-subtask) ≈ 0. S0's normal mechanism
is already a whole-trace redraw, so forcing one changes nothing
(delta ≈ 0). The token penalty mirrors scope exactly: +21.8k C1,
+4.4–4.6k C2/C3, ≈ 0 C4, and NEGATIVE for S0 (the forced dispatch bills
nothing, so the redraw runs cheaper than the un-forced baseline path).

Substitution-semantics resolution (ratified). Two columns now, replacing
the mode-dependent single metric:
  - `substitution_success` — §6.4-LITERAL for ALL injected modes: the
    fraction of injected cases reaching a validated trace within budget
    (terminal_status == ok). Hallucination values as computed:
    C1 .885 / C2 .900 / C3 .890 / C4 .925 / S0 1.000. S0's 1.000 shows
    the poisoned record ALWAYS validates there (single worker, no
    cross-check to reject it). Timeout/outage unchanged (already literal
    since 9f7c298).
  - `record_substituted` (hallucination only; None elsewhere) — the
    pre-9f7c298 metric: did the injected record SURVIVE into the emitted
    τ slot. Rates C1 .775 / C2 .775 / C3 .780 / C4 .775 / S0 .580 — only
    this column reveals that at S0 just 58% of poisoned records survive
    even though 100% of runs validate.
Per-cell identity holds to machine precision: all_case_accuracy ==
substitution_success_rate × P(accurate | validated trace), max |diff|
≈ 1.1e-16. §8 will cite the paired deltas above with these
case-clustered CIs.

Final dataset: 4400 scored runs = Phase-1 att2 1000 + Phase-2 200 +
Phase-3 hallucination/outage 2000 + Phase-3 timeout att2 1000 +
Phase-4 200. Superseded attempts archived under `results/` (not scored):
Phase-1 att1 (surface v1.2 operand defect) and Phase-3 timeout att1
(forced-dispatch defect, fixed 9f7c298).

Program cost: ~$132.3 total ($1.26 ph0 + $19.81 + $20.42 + $3.53 +
$57.30 + $3.59 + $26.25 + ~$0.2 smokes/backfill) — within the >$125
pre-authorization plus the ratified timeout-arm re-run extension.

Next (out of scope for this commit): paper writing — §7–§9 and §11, plus
the one-sentence §6.4 D1-A disclosure of the immediate-raise
forced-timeout semantics — and the harness hardening list (history-aware
scripted client so a seam that delivers the wrong failure cannot pass the
gate again).

## 2026-08-06 — Timeout arm root-caused: forced-timeout seam delivered the wrong failure (task never sent); harness defect, not a design property

Defect: L3 §3.1 requires the worker_timeout seam to "fire once, then
[allow] natural repair mechanisms" — repairs are NEVER re-forced. In
practice 1000/1000 Phase-3 timeout runs ended `validation_exhausted`
with zero validated traces (commit 68ec367's "timeout uniformly fatal
sans fallback" was reporting THIS defect, not a design property).

Root cause (from raw records, $0): the forced-timeout initial dispatch
short-circuited `worker.run()` entirely (orchestrator `_process_worker`,
and the mirror in `run_s0`) — it set `payload=None` WITHOUT invoking the
worker. Because `worker.run()` is the only place the initial
`_input_state_message` is appended to conversation history, the injected
worker's conversation NEVER received the task bundle. The natural-repair
loop then sent only the terse verbatim `_repair_message` ("Your τ output
failed validation… Re-emit…") into an otherwise-empty conversation with
no case view, no rules, no instructions. The live model replied with
context-less prose (finish_reason end_turn, 60-170 output tokens, zero
tool_use, zero api_retries) and never a fenced bundle → every repair
failed extraction → validation_exhausted. Evidence: the injected worker
logs EXACTLY `budget` model_calls (all the repairs; no initial-dispatch
call) while non-injected workers show the normal initial tool_use call;
wall-clock 6-19 s confirms no real 120 s wait.

Framing: the seam simulated the WRONG failure — "task never delivered"
instead of the specified "task delivered, in-flight call lost." A real
120 s timeout leaves the input message in history (appended before the
model call that then times out); the scripted seam produced clean/empty
history instead. The scripted gate missed this because the scripted
client is history-blind — it pops a valid bundle for any message,
including a bare repair, so `test_seam_forced_timeout_logged_then_recovers`
passed against a conversation the live model could not recover from.

Ratifications (this session): D1-A — the forced timeout raises
immediately in the live path (no real 120 s wait); a one-sentence §6.4
disclosure of the immediate-raise semantics is a pending PAPER edit, not
a repo change. D3 — re-run scope is the timeout arm only (~$22-26); the
fix's behavior changes execute ONLY on the timed-out-invocation path, so
the none/hallucination/outage arms' banked results remain valid under the
fixed code.

Rectification: the initial bundle now ALWAYS goes through `worker.run()`;
a new `force_timeout` flag appends the input-state message to history then
raises `asyncio.TimeoutError` immediately — leaving exactly the state a
real timeout would (task in history, zero completed assistant turns, zero
logged model_calls, no wait). Natural repair then runs against a
conversation that carries the task, recovering just as every other arm
already does. The `_repair_message` is byte-identical; no prompt, slice,
or schema changed (C2/C3 shared slice f01dc266… preserved; all
prompt-hash tests green). Confined to the `forced` branch of the initial
dispatch (C1-C4 and S0); none/hallucination/outage paths behaviorally
unchanged, their tests untouched and green.

Live-shape repro (RED→GREEN): a new history-sensitive scripted behavior
`require_in_history` emits a valid bundle only if the initial input-state
message is present in history, else end_turn prose — reproducing the live
model deterministically. The repro fails before the fix
(`validation_exhausted != ok`) and passes after; D1-A is codified by a
worker-level test asserting immediate raise with the task in history and
no model call.

Scorer: §6.4 `substitution_success` now computed for all injected modes.
hallucination keeps record-survival (column byte-identical:
0.775/0.775/0.78/0.775/0.58); timeout/outage use the literal §6.4 form
(fraction of INJECTED cases reaching a validated trace within budget =
terminal_status ok), with non-fired cells `None` (not-applicable, dropped
by analyze) so the denominator is injected cells. Local check on the
attempt-1 records: outage ≈ 1.0 among-injected (absorbed); timeout 0.0
(the defect); results/ restored to HEAD after inspection.

Sweep: `--modes` added (comma-separated subset of a phase's modes;
default = all, §1 enumeration unchanged, only the runner's worklist
filtered), validated against the phase's modes so a typo can't silently
run zero cells.

Disposition: the timeout arm is attempt 1, to be archived server-side.
Re-run plan: timeout-only, ~$22-26, on the fixed code via
`python -m scripts.sweep --phase 3 --modes timeout`. Pending paper edit:
the §6.4 one-sentence disclosure of immediate-raise (D1-A) semantics.

## 2026-08-05 — Phase-1 C2 anomaly root-caused: unspecified operand channel; §3.2 amended (S_RCH += line_items)

Diagnosis (from raw records, $0): RCH's output contract includes
vat_amount = rate × line_items[].amount, but §3.2 gave RCH no case
atoms — only upstream records. The operand reached RCH only when an
upstream CLS worker voluntarily echoed `amount` in its record
`support` (an unspecified channel). Solo-CLS conditions (C3, C4)
happened to echo it; the bundled CLS+JUR worker (C2) did not: 116/117
C2 zeros fail exactly at RCH with vat_amount=0, terminal ok, traces
consistent; the case amount appears nowhere in the C2 record. S0/C1
are structurally immune (full case view).

Resolution (ratified): closed-operand principle — every atom required
by a subtask's contractual output fields must be in that subtask's
visible state. §3.2 amended: S_RCH += line_items (HARNESS_GROUNDING_1
v1.1 → v1.2). Regression test added (every RCH-owning worker's payload
carries line_items with amount); C2==C3 shared-slice equality
preserved.

Consequence: Phase 1 (mode none) becomes attempt 1 — C2 invalid by
defect; C3/C4 accuracies rested on the voluntary echo and are not
comparable under the amended surface; S0/C1 structurally unaffected
but will be re-run for a single-surface attempt. Plan: archive
attempt-1 raw + derived tables as diagnostic evidence, re-run Phase 1
in full as attempt 2 under v1.2 (~$20; ~$105 projected program total
vs >$125 credit). Attempt-1 falsification/contrast tables are
diagnostic only and will not be reported as results.

## 2026-08-04 — Phase 1 AUTHORIZED — main sweep launched on the measurement host

Preconditions verified: arrival gate green on the measurement host (210 passed / 2 skipped;
VERIFY OK; part1-frozen -> e2d2bdd); live smokes from the measurement host
212 passed / 0 skipped; deps fix (29184a9) proven by clean-venv
`pip install -e ".[test]"`; console credit/spend limit confirmed > $125
against the $84.12 projection (~40% headroom for retries).

Environment snapshot: results/env_gex44_phase1.txt (pip freeze; Python
3.12.3, Ubuntu 24.04.4, measurement host). Runner: `python -m scripts.sweep --phase 1`
under tmux; console log at results/phase1_console.log. n-parallel: sweep.py
default (recorded in the console log), held constant for phases 1-4.
Phase-0 attempt-2 records remain in results/raw/ per the re-run protocol
(different phase key; duplicate-guard still a pending follow-up). GO
recorded with this commit.

## 2026-08-04 — Measurement-host arrival closed; Phase-1 host fixed

Server: dedicated Hetzner bare-metal host (Ubuntu 24.04, i5-13500
14C/20T, 64 GB, 2×NVMe RAID1). Access was SSH-key-only. [Server ID, IP,
datacenter, SSH-key name, and repo-access PAT name redacted for public
release — see SANITIZATION_AUDIT.md. Host credentials and the
repo-access PAT are held out-of-band and were rotated/revoked at
release.]

Arrival gate green on the server: pytest → 210 passed, 2 skipped;
freeze_dataset --verify → VERIFY OK (oracle_commit e2d2bdd…,
dataset_sha256 3dc683ec…, case_files_sha256 3472544f…); part1-frozen →
e2d2bdd. Live smokes from the measurement host with the API key set:
212 passed, 0 skipped (2 live API calls; local gitignored key file).

Reproducibility defect caught by the gate: numpy/pandas (Layer-4
scoring/analysis) and pytest (runner) were undeclared — masked by the
dev venv. Fixed in this commit: numpy/pandas as runtime dependencies
(server resolves numpy 2.5.1, pandas 3.0.5 on Python 3.12.3); pytest as
a [test] extra (9.1.1). Acceptance: clean-venv `pip install -e ".[test]"`
+ full gate on the measurement host, post-push.

Decision (VM): Phase-1 host = the dedicated measurement host. Latency, retry counts, and
terminal failures are measured experimental outputs; the dedicated host
removes laptop-side confounds. Full environment snapshot (pip freeze)
to be captured at sweep launch.

## 2026-08-04 — History rewrite: commit-identity hygiene (no content change)

23 of 28 commits carried a work-machine identity (since scrubbed)
and 19 carried Claude Code attribution trailers. Rewrote history with
`git filter-repo` (mailmap → pedro-santos-eng
<pedromiguelbsantos@gmail.com>; trailers stripped) and force-pushed.

Integrity: unchanged commits keep their hashes — `part1-frozen` still
resolves to `e2d2bdd` (commit-map: e2d2bdd → e2d2bdd); `pytest -q` → 210
passed, 2 skipped; `freeze_dataset --verify` → VERIFY OK (all three hashes
unchanged). Trees byte-identical throughout; only metadata and messages
changed. HEAD `fb850e4` → `e2dec5b`. Hashes cited in entries ≥ 2026-08-01
refer to pre-rewrite history; the bridge is `docs/commit-map-rewrite.txt`.

Forward guard: repo-local user.email pinned to the canonical identity;
Claude Code attribution disabled (`~/.claude/settings.json`). This
entry's commit is the first post-rewrite commit and doubles as the live
check.

## 2026-08-03 — Go-live Phase 0: six harness amendments; gate-1 fail→fix→PASS; model confirmed

Phase-0 dry run (L2 §11) authorized. Two attempts: attempt 1 FAILED gate 1
(C1=0%), root-caused to a RAT instruction-completeness gap; a targeted
completeness fix under the reopened model-choice decision produced attempt 2,
which PASSES all three gates (C1=80%). Model claude-haiku-4-5-20251001
CONFIRMED under the pre-committed criteria.

### Ratified amendments (2026-08-02/03)
1. §4.6 sampling (`340b115`): model rejects temperature+top_p together (400);
   amended to temperature=0.2 only, top_p unset (echoed null). Surfaced by the
   first live smoke at $0. Paper §4.6 amended; no run used the old pair.
2. Prompt↔schema conformance (`1b090b4`): `final` contract schema-generated
   (named `lines`, closed keys); explicit per-line bundle listing; S0 final
   contract. Fixed live traces emitting `lines_summary` / omitting keys.
3. History hygiene + translator merge (`0e72181`): heal dangling tool_use on
   tool-cap exhaustion (else 400 on next repair); canonical same-role merge in
   the Anthropic translator. Found via the C1 diagnostic.
4. Citation vocabulary (`4a7357b`): record contracts enumerate the closed
   citation set from `rules.RULE_KEYS` by namespace — workers were inventing
   keys off table-reference wording. V4 citation–decision consistency stays
   measured.
5. Live smoke best-of-2 plumbing semantics (`4a7357b`): the smoke certifies
   the pipeline, not a single stochastic draw (~80%-green process; the
   single-draw gate was ~20% flaky by construction).
6. RAT band←category rule (`9ad01c3`): the RAT instruction now states the
   bounded band-from-category rule, GENERATED from `rules.CATEGORY_TABLE`.
   This was the gate-1 root cause (below).

Provenance decision (2026-08-03): the canonical prompt-hash family is
REPO-derived. The validator sandbox never carried `1b090b4`, so its
`5efe69a4…` expectation was computed on a pre-conformance chain and is VOID.
Protocol: byte-sha convergence applies only to validator-delivered exact
files; in-repo ratified code is verified by content assertions + invariants
(C2==C3, determinism) + self-consistency; canonical hashes are the repo's,
recorded at commit.

### C1 secondary-failure diagnostic (5-sample, dev_001)
Green 4/5; all failures REPEATED-INVALID citation-presence (EXM-dominant),
zero omission. Motivated amendment #4. The history-hygiene bug (dangling
tool_use) was found and fixed here (amendment #3).

### Phase 0 attempt 1 (caps 5M/$10) — FAIL
25/25 complete, 0 terminal, $0.78, 563,150 tokens.
Accuracy: S0 40%, C1/C2/C3/C4 0%. earliest_failing_subtask {RAT: 21, JUR: 2}.
Root cause: systematic RAT=null — the instruction required the band as a
`rate_table_lookup` input but never stated the band←category rule, so a
compliant "never invent" worker had no derivable path to a rate. (The frozen
validator has no EXM↔RAT cross-check, so these wrong-but-self-consistent
traces still validated — the exact class paper §10.4 "Validation visibility"
already discloses.) Gate 1 FAILED (0% < 40%) → halt; model-choice decision
reopened under pre-committed criteria: fix-and-rerun once; <40% after a
complete surface = capability verdict; >90% = ceiling.

### Completeness fix + Phase 0 attempt 2 (caps unchanged) — PASS
Amendment #6 applied; no wording iteration.
25/25 complete, 0 terminal, $0.48, 328,028 tokens.
Accuracy: S0 80%, C1 80%, C2 40%, C3 100%, C4 100%. rate_ok 25/25.
earliest_failing_subtask {RCH: 5} (residual measured behavior);
retries 31 → 2; tokens −42%.

### Gates (attempt 2)
1. C1 accuracy ∈ [40%, 90%]: C1 = 80% → PASS → model CONFIRMED.
2. Extraction/parse failure < 10%: 0/60 = 0.0% → PASS.
3. Full-sweep projection (4,400 runs @ $1/$5): 51.1M in + 6.6M out ≈ $84.

### Independent validation (2026-08-03, second Claude instance, clean sandbox)
Canonical zip with full history: `git fsck` clean; the six local commits
sit exactly on `e0d164b`; tag `part1-frozen → e2d2bdd`; working tree clean
except the two declared untracked throwaways. Offline suite 210 passed +
2 skipped; spot-check exit 0; `freeze_dataset --verify` OK. RAT-instruction
content assertions pass; the canonical C2==C3 shared-slice hash reproduces
exactly (`40ecd9610e61f4d6…`). Both Phase-0 attempts were independently
re-scored and re-analyzed from the raw run records: every reported number
matches — attempt-2 accuracies, rate_ok 25/25, earliest {RCH: 5}, retries 2,
$0.48, and the $84.12 projection to the cent; attempt-1 {RAT: 21, JUR: 2},
retries 31, 563,150 tokens, $0.78. Gate verdicts confirmed from first
principles.

### Phase re-run protocol (recorded)
`score_runs` aggregates everything under `results/raw/`; a phase re-run must
first archive the prior attempt OUTSIDE `results/raw/` (attempt 1 lives at
`results/phase0_attempt1_raw/`) or records double-count. A duplicate-guard in
`score_runs` (error on repeated (phase, mode, condition, case, repeat)) is a
non-blocking follow-up that may ride with any future commit.

### Resolution and authorization
C1 = 80% ∈ [40, 90] → "model confirmed, proceed" branch. The gate-1 failure
was an instruction-completeness gap, not a capability ceiling (0% → 80% on
the fix alone). Total go-live spend: $1.26. Phase 1 NOT started — it awaits
its own authorization and the VM decision (measured latencies belong to the
VM per the standing principle). Batch push of the full local chain
authorized with this close.

## 2026-08-02 — Part 2, Layer 4 close: flags reviewed; §6.6 rendered faithfully; harness COMPLETE

Independent validation (second Claude instance, clean sandbox): zip with full
history — `git fsck` clean, HEAD `b676d3b`, tag `part1-frozen` intact, working
tree clean; gate reproduced (199 passed + 2 skipped, spot-check exit 0,
`freeze_dataset --verify` OK); all `src/harness/*.py` byte-identical to the
Layer-3 state (additive test extensions only), frozen trees untouched.

### Flags reviewed and CLOSED

1. **§6.6 falsification criteria — RESOLVED WITH CORRECTION.** The three
   implemented criteria did not match the paper: they tested C2>C1, C1 not
   materially below C4, and S0′ not materially above C2. Replaced with the
   faithful rendering in `analyze.evaluate_falsification`:
   (1) *intermediate-optimum (RQ1)* — triggered iff neither C2 nor C3
   materially outperforms **both** endpoints C1 and C4 on final-answer
   accuracy, or the C1–C4 accuracy sequence is monotonic in either direction
   (automatic); this requires three supplementary accuracy contrasts
   (C2–C4, C3–C1, C3–C4), added as a descriptive `supplementary_contrasts`
   table **outside** the Holm family per §6.5;
   (2) *orchestration benefit (RQ2)* — triggered iff S0 weakly
   Pareto-dominates all of C1–C4 on the (token-cost, accuracy) point
   estimates; order-independent evaluation with an honest `None` when a
   condition is missing and no definitive defeat exists;
   (3) *prompt-budget confound (RQ3)* — the exact conjunction: S0′_C2 vs C2
   not significant under the Holm-corrected paired permutation test AND the
   paired bootstrap CI includes zero.
   The falsification table now reports `triggered` / `verdict` / `evidence` /
   `paper_rule` per criterion; "materially outperforms" remains the ratified
   rule (CI excludes zero positively AND d_z ≥ 0.2). Tests rewritten to the
   real semantics, covering: supported-via-C3, no-intermediate-beats-both,
   monotonic auto-trigger, Pareto dominance with ties, dominance defeated on
   one axis, and the RQ3 conjunction in all four states. Suite 199 → 204.
   A statistical refinement was folded in: the sampled permutation p now uses
   the (1 + hits)/(1 + B) estimator (Phipson & Smyth, 2010), so a reported p
   is never exactly zero; the exact-enumeration branch already contained the
   identity permutation and is unchanged. The real 40-case path is unaffected
   in mechanics (always the pinned 1,000 flips).
2. **Price sheet — RESOLVED, confirmed.** $1/$5 per 1M input/output tokens
   for `claude-haiku-4-5` confirmed against Anthropic's published pricing as
   of 2026-08-02 (product page and launch announcement; cross-checked against
   an Aug-1-2026 price tracker listing `claude-haiku-4-5-20251001` at $1/$5).
   `data/price_sheet.json` note updated from FLAG to CONFIRMED.
3. **Permutation scale switch — APPROVED.** Exact sign-flip enumeration only
   at test scale (2^k ≤ 2^14, hand-computable fixtures); the 40-case path
   always uses the pinned 1,000 random flips under `permutation_seed
   20260806`.

Gate after corrections: **204 passed, 2 skipped**; zero diffs under the
frozen trees; Layers 1–3 modules untouched.

### Part 2 harness is COMPLETE (Layers 1–4)

Everything from here is a live decision, in order, each with a
DEVLOG-recorded go-ahead:
1. Configure `ANTHROPIC_API_KEY` → the two live smokes flip skipped→passed.
2. **Phase 0 dry run** (L2 §11): dev_001..005, S0+C1–C4, R=1, mode none;
   gates — C1 accuracy in [40%, 90%], extraction/parse failures < 10%, cost
   projection within budget (projection recorded here; it sets the per-phase
   token/dollar caps).
3. VM provisioning for the measured phases (latency/retries are measured
   outputs; the laptop remains the controller only).
4. Phase 1 main sweep, then phases 2–4 per HARNESS_GROUNDING_4_SWEEP §1.

## 2026-08-01 — Part 2, Layer 3 (failure injection) implemented; gate green; flags reviewed and closed

Layer-3 source of truth: HARNESS_GROUNDING_3_INJECTION.md v1.0 (contract
`fe2c25d`). Built on frozen Part-1 + Layers 1–2. Zero diffs under
`src/oracle/`, `src/schemas/`, `data/eval_cases/`, `data/dev_cases/`;
`freeze_dataset --verify` OK.

Delivered (§1–§9), commit `c3b1d07` + the review-driven follow-up commit:
- scripts/generate_injection_plan.py — offline generator (imports `labeler`,
  offline only; imported by no `src/harness/` module). Per record it asserts
  both that record-level `validate_record` passes and that the decision
  differs from the oracle label.
- data/injection_plan.json — committed artifact. `injection_seed 20260801`;
  uniform τ over T (CLS 8 / JUR 11 / RAT 8 / EXM 4 / RCH 9); first-line
  targeting; 8 outage cases (eval_004/009/014/018/021/029/035/039, exactly
  one per block of 5); `content_sha256 1b3c3b77…`. Regeneration is
  byte-identical.
- src/harness/injection.py — stdlib-only controller; three frozen seams with
  per-run fire-once state (timeout once on the initial responsible
  invocation, repairs never re-forced; hallucinate once per case on the
  first-line τ record; outage on the first `rate_table_lookup` per case,
  then recovery) + §6 marker accessors.
- Run-record marker `accounting.injection {mode, tau, fired, plan_sha256,
  details}` echoed into every record, all four modes.
- agents.py `TOOL_CAP_EXHAUSTED` (2026-08-01 follow-up) — distinct
  extraction_error at tool-cap with tools still requested.
- tests/test_injection.py (§8); label-isolation extended to `injection.py`.

Gate: every §10 box green; 167 passed, 2 skipped (Layer-2 live smoke,
skip-if-unconfigured).

### Independent validation (2026-08-01, second Claude instance, clean sandbox)

Zip carried real history: `git fsck --full` clean, HEAD `c3b1d07`, tag
`part1-frozen → e2d2bdd`, working tree clean. Gate reproduced: 167 passed +
2 skipped; spot-check exit 0; `VERIFY OK`. Plan regeneration reproduced
byte-identical to the committed git blob (a worktree CRLF artifact of the
Windows checkout was ruled out against `git show HEAD:…`). Targeted reads
confirmed every behavior the flags depend on.

### Flags reviewed and CLOSED

1. **Input-blind interception — APPROVED.** Validated in code: the
   `accepted={}` context is scoped per line and fires only for the ids the
   interception seam actually substituted
   (`ctx = {} if lid in injected_ids else full accepted context`), so
   non-injected records — including sibling lines in the same payload — keep
   full-context validation, and un-injected runs are untouched. Assembly
   failures route through the pre-existing §3.5 machinery
   (`_route_gate_failure` → culpable owner, verbatim gate checks); no
   injection special-casing exists anywhere in that path. This is the
   ratified DECISION 3 reading, and it is what preserves the RQ4 probe: a
   context-aware interception check would fire the τ-owner's own retry with
   pinpoint feedback, collapsing the silent-error condition into a
   measurement of the validator.
   *Phenomenology recorded from the frozen cross-check inventory:* the EXM
   check is one-directional (`exempt=true` on a non-EXEMPT_SUPPLY category
   fails; `exempt=false` on an EXEMPT_SUPPLY category passes) and no check
   requires an exempt EXM to yield outcome `exempt`; so EXM true→false and
   RCH exempt→standard_charge injections can survive the assembly gate
   (silent, detectable only downstream), while EXM false→true,
   RCH standard→reverse and RCH reverse→exempt are caught at assembly and
   repaired through the natural path. This asymmetry is the §6.4 mechanism
   ("especially when τ is the terminal subtask RCH"), not a defect.
2. **standard_charge RCH citation — RESOLVED as path-aware; plan unchanged.**
   The generator now selects the citation by the case's oracle `jur_path`
   (domestic → `RC.DOMESTIC.SUPPLIER_CHARGES`, b2c_cross_border →
   `RC.B2C.SUPPLIER_CHARGES`; intra-EU cannot reach the branch for the
   frozen corpus — standard_charge injections arise only from oracle-exempt
   first lines, and no exempt line occurs inside an intra-EU B2B case,
   SPOTCHECK_3.3 §4.2 — so the domestic key is the total-function default).
   Measured against the committed plan, the flagged branch is unpopulated
   under seed 20260801: zero standard_charge injections exist, so the
   correction is a zero-delta change — regeneration remains byte-identical
   and `content_sha256 1b3c3b77…` stands. The fix hardens the released
   generator against future seeds/corpora rather than altering this
   experiment.

Guardrails honored (§9 + L1 §11 + L2 §9): one mode per run, never combined;
controller is stdlib-only, so the validated Layer-2 import graph is
untouched; no controller state enters agent-visible content except the
injected record itself (the intended exception); the plan is written only by
the generator; zero edits under the frozen trees.

Informational, no action: the uniform τ draw yields EXM = 4 — a small cell
for any per-τ breakdown. §6.4's reported metrics aggregate per (injection,
configuration) cell over τ, so nothing in the paper's analysis plan is
affected; noted for results reading.

Next: Layer 4 (sweep runner) + the five-case dry run at the Layer-3/4
boundary. Prerequisites now: a configured `ANTHROPIC_API_KEY` (turns the two
live smokes from skipped to passed) and the VM decision for the measured
sweep.

## 2026-08-01 — Part 2, Layer 2 (orchestration) implemented; gate green; flags reviewed and closed

Layer-2 source of truth: HARNESS_GROUNDING_2_ORCHESTRATION.md v1.1. Built on the
frozen Layer-1 surface/tools/validation/runlog; Part 1 (oracle) and the frozen
dataset untouched (`freeze_dataset --verify` OK; `dataset_sha256 3dc683ec…`).

Delivered (§0–§8), commit `4654dc1`:
- model_client.py — frozen `EXECUTION_CONSTANTS` (§1); provider-neutral
  ModelClient interface; in-repo scripted client (per-tag queues → deterministic
  under C4 concurrency) + lazy anthropic-SDK real client (key read from env,
  never stored elsewhere, never logged).
- orchestrator.py — deterministic Magentic-One ledger semantics, NO orchestrator
  LLM; per-worker bundle dispatch, per-subtask repair, wave scheduler with
  within-case concurrency cap 2 (C4 RAT‖EXM), assembly + authoritative
  `validate_trace` gate, injection seams, full per-call accounting.
- agents.py / prompts.py — worker tool loop + P_r assembly as a pure function of
  the slice; output contracts generated from the frozen schema `$defs`;
  `PROMPT_HASHES`.
- s0.py — S0 whole-trace repair loop + S0′ matched-token knobs + token
  measurement.
- Tests: scripted-client harness (147 passed, 2 skipped) covering every §8/§10
  item; live smoke skip-if-unconfigured; import-graph isolation extended to all
  Layer-2 modules.

### Independent validation (2026-08-01, second Claude instance, clean sandbox)

Gate reproduced from the working tree: 147 passed + 2 skipped; spot-check
clean-room script exit 0; `freeze_dataset --verify` OK with the frozen hashes;
`src/oracle/`, `src/schemas/`, `data/` byte-identical to the `part1-frozen`
state. Targeted code reads on orchestrator.py, agents.py, model_client.py,
prompts.py confirmed the behaviors the flags depend on.

### Flags reviewed and CLOSED

1. **§2 fallback invoked — APPROVED.** Live client on the anthropic SDK behind
   our ModelClient interface, not AutoGen's AssistantAgent. Basis: the ratified
   DECISION 1 contains an explicit fallback clause ("to the `anthropic` SDK
   behind the same interface if the maintenance-mode client blocks a
   requirement"), and the blocked requirements are textual — DECISION 4's
   per-call timeout/transport-retry pins, §7's per-call latency / api_retry /
   stop-reason accounting, §5's last-fenced-JSON extraction — all inside the
   AssistantAgent loop boundary in 0.7.5. Paper unaffected: §4.2
   ("Magentic-One-style … [Fourney et al., 2024]") and §6
   ("AutoGenBench-style … pattern") are design-pattern citations, not
   framework claims; no manuscript edit required. `autogen 0.7.5` retained as a
   pinned dependency with the rationale documented in pyproject.toml; it is
   imported by no Layer-2 module.
2. **Import-graph resolution — APPROVED (within the ratified L1 rule, not a
   deviation).** L1 §1.2 explicitly declares `validator.py` importable
   ("validation is condition-invariant machinery, not a label source").
   Verified on the frozen validator: the `labeler` import is the `CaseTrace`
   type only, and every `failed_checks` string is built from the agent's own
   emitted trace plus `rules.py` tables (schema errors, citation presence,
   citation–decision consistency, RAT-vs-table) — zero references to oracle
   labels. Strict {oracle, rules} isolation holds on the true agent-context
   modules (prompts/agents/model_client, fresh-interpreter test);
   scorer-unreachability + no-direct-import on orchestrator/s0 exceeds the
   requirement.
3. **Repair-message contents — doc-internal tension resolved, reading
   RECORDED.** §3.2 says the repair follow-up contains the verbatim
   `failed_checks` *and the output contract for that subtask*; §9 says verbatim
   `failed_checks` only. Implementation follows §9 (and paper §4.2/§10.2:
   feedback limited to structured validator output): fixed condition-invariant
   template + verbatim checks, no contract re-inclusion. The contract remains
   in-context via the persistent worker system prompt, so no information is
   lost. This reading is the accepted one; §3.2 is not amended.

### Bounded choice, accepted with one follow-up

`MAX_TOOL_ITERATIONS = 8` safety cap on the worker tool loop (not a paper
knob; runtime otherwise bounded by per-call timeout + wall cap). On
exhaustion, the turn falls through to payload extraction and surfaces through
the §5 ladder — budget-consuming and visible in verdicts, never silent.
Follow-up before the live sweep (non-blocking, may ride with Layer 3): tag
cap-exhaustion distinctly in the worker turn (e.g. extraction_error
`"TOOL_CAP_EXHAUSTED"` when the last response still requested tools) so sweep
analysis can attribute these terminals separately from ordinary
no-fenced-block failures.

Guardrails honored: orchestrator makes no LLM calls; repair feedback is
verbatim validator output; `final` emitted by the RCH owner (never
harness-computed); no per-condition prompt tuning; S0 tuning confined to
`dev_001..dev_008`.

Next: Layer 3 (injection content). Layer 4 (sweep) blocked on: live smoke with
a configured `ANTHROPIC_API_KEY`, the TOOL_CAP marker above, and the Finding-3
decision (recorded 2026-08-01 earlier entry: resolved as §10.2 disclosure).

## 2026-08-01 — Layer-2 contract committed; §3.3 spot-check reconstructed; line endings normalized

### Committed
- `docs/HARNESS_GROUNDING_2_ORCHESTRATION.md` v1.1 (`404d2ac`). The file was
  authored 2026-07-31 but the copy into `docs/` failed silently; the repo went
  one session without its Layer-2 source of truth. Verified byte-identical
  (modulo line endings) to the authored version. All four `[DECISION n]` items
  ratified; §12 reflects this.
- `docs/SPOTCHECK_3.3.md` — verification record for paper §3.3. The 2026-07-28
  record was produced but never reached disk; this is a fresh re-execution, not
  a transcript, and it supersedes the earlier one.
- `scripts/spotcheck_cleanroom.py` — the clean-room re-derivation behind that
  record. Imports nothing from `src/oracle/`; reads only the frozen case files.
- `.gitattributes` (`* text=auto`) + `git add --renormalize .`.

### Spot-check result
Clean-room re-derivation from `ORACLE_GROUNDING.md` §1–§2 over the whole corpus:
48 cases (40 eval + 8 dev), 65 line determinations (54 + 11). Jurisdiction
routing, classification, rate selection, exemption detection, reverse-charge
routing, arithmetic, rounding and case-level aggregation reproduce exactly.
Rule-reference keys are single-valued per semantic branch.

**One divergence, 22 records, one field:** `liable_party` on the exempt branch.
The engine emits `"none"`; §2.1 specifies `liable party` for the domestic,
intra-community B2B and B2C branches but is silent for exempt, so a
document-only reader derives `"supplier"`. Setting the script's
`EXEMPT_LIABLE_PARTY` constant to `"none"` drops mismatches to 0 — that field is
the entire gap. Engine correct, document incomplete.

### Findings carried forward
1. **`ORACLE_GROUNDING.md` §2.1 needs `liable party = none` on the exempt
   bullet.** Blocking: `liable party` is a scored field (paper §6.1) and worker
   prompts derive from this text, so agents would be scored on a field the
   prompt does not determine. Doc-only change; no re-freeze.
2. **EXEMPT-over-REVERSE_CHARGE precedence is unexercised** — zero
   `EXEMPT_SUPPLY` lines inside `intra_community_b2b` cases, in *both* splits
   (extends the 2026-07-28 finding, which covered eval only). A constructed
   probe against the real engine confirms exemption dominates per line and the
   trace validates. Sampling gap, not a logic defect; disclose in §10.2.
3. **The dev split contains no exempt line at all** (0 of 11). S0 is tuned on
   dev_001–dev_008 while 11 of 54 eval determinations (20%) are exempt, so S0
   never sees the `rate: null` shape or the `liable_party: "none"` value.
   Threatens the §4.5 fairness claim. Decide before S0 tuning: disclose in
   §10.2, or regenerate the dev split with exempt coverage. Correction to an
   earlier note: both manifest hashes cover eval **and** dev, so regeneration
   requires a re-freeze and changes `dataset_sha256` / `case_files_sha256`
   (eval files and the `part1-frozen` tag untouched). Disclosure is the
   schedule-safe default.
4. **JUR is resolved from `line_items[0]`**, where §2.2 describes per-line
   resolution "where the kind affects it". Cannot differ under the bounded rules
   (checked: no case yields a non-unique case-level jurisdiction). Recorded for
   documentation accuracy; becomes load-bearing if the rule set widens.

### Integrity
`part1-frozen` → `e2d2bdd` unchanged. `dataset_sha256 3dc683ec…`,
`case_files_sha256 3472544f…` unchanged. No oracle source, schema or evaluation
case was modified.

### Next
Layer 2 implementation may begin once action 1 is applied and Layer 1's
readiness gate is re-confirmed green.

## 2026-07-31 — Part 2, Layer 1 complete: activity surface

Layer-1 readiness gate (HARNESS_GROUNDING_1_SURFACE §10) is met. Implemented the
five modules exactly per §9: `src/harness/{surface,tools,validation,runlog}.py`
and `src/harness/schemas/agent_case_view.schema.json`, with the five test files
(`test_surface`, `test_tools`, `test_validation_incremental`,
`test_label_isolation`, `test_runlog`).

**Gate result.** `pytest -q` → **86 passed** (the untouched **27** Part-1 tests +
**59** new Layer-1 tests). `python -m scripts.freeze_dataset --verify` →
**VERIFY OK** (`dataset_sha256 3dc683ec…`, `case_files_sha256 3472544f…`,
`oracle_commit e2d2bdd`). `src/oracle/`, `src/schemas/`, and `data/` are
**untouched** (git diff empty). Every §10 box maps to a passing test — notably
the key-based label-isolation projection on all 48 cases (§1.1), the
fresh-interpreter import-graph isolation of `labeler`/`scorer` from `surface`/
`tools` (§1.2), and the incremental ⟺ full-trace equivalence biconditional on 48
oracle traces + 360 single-field mutations (≥40 breakers across all four check
families, §7.1).

**Four bounded design choices** (mechanism the spec left to authority-order
item 3; reviewed and accepted — item 2 with the adjustment below):

1. **Tool logging + outage seam.** §4 froze the tool signatures as positional
   with no logger/context arg, yet §7.2 requires logging every invocation and
   §8.3 puts the outage seam inside `rate_table_lookup` (no `case_id` param).
   Reconciled with a module-level, no-op-by-default `ToolContext` (log sink +
   active case + injection controller) installed per run via `using_context`;
   the agent-facing call surface stays exactly as §4 froze it.
2. **`vat_registration_check` source.** Builds a cached registry from the frozen
   `data/` artifact holding **exactly `{case_id: bool}`** — only the bare
   registration field, never the input block (which carries the CLS label
   `true_category`); `oracle_trace` is never read. Test asserts values are bare
   booleans.
3. **Injection seams 1 & 2** (worker-timeout, hallucination) have no dispatch
   module in Layer 1, so their interfaces + no-op defaults live on
   `InjectionController` in `tools.py` alongside the outage seam; Layer-2
   dispatch will call them.
4. **Run-record schema is embedded** in `runlog.py` (not a file), since §9 lists
   only `agent_case_view.schema.json` under `schemas/` and §11 forbids creating
   other schema files.

## 2026-05-29 — Part 1 complete: dataset frozen @ seed 42

Part 1 readiness gate (§9) is met and the dataset is frozen to disk. Finalized
as four commits (Option A, hash-verified between steps):

1. `oracle: Part 1 — …` — oracle code, consolidated schema, tests, freeze script.
2. `gitignore: un-ignore data/ …` — **the pinned `oracle_commit`**:
   `e2d2bdd22b85ea2915e3d719d7c12c6f18eac577`.
3. `data: freeze 40+8 cases @ seed 42` — frozen dataset (MANIFEST + 48 case files).
4. `docs: DEVLOG — close Part 1` — this entry.

Frozen artifact (`data/MANIFEST.json`, seed 42):
- `oracle_commit`: `e2d2bdd22b85ea2915e3d719d7c12c6f18eac577` (`oracle_commit_dirty: false`)
- `dataset_sha256`: `3dc683ec418666fa2e8823a2ea622bfd90f638254377d83fe95d5247563e599e`
- `case_files_sha256`: `3472544ffcd1434d59427c912ba5c77a8294de0fb675ba7d32f1572c5e410302`

Verification: `python -m scripts.freeze_dataset --verify` → **VERIFY OK**;
`pytest -q` → **27 passed**. Families 10/10/10/10 (eval), 2/2/2/2 (dev).

## 2026-05-29 — Cleanup: drop eval_001 anchor + consolidate schemas

Applied two pure refactors (supplied as patch files, now removed from the root).
**27/27 tests pass** after both.

**Diff #1 — remove the eval_001 anchor.** Deleted `_anchor_case` and the
`if len(eval_cases) == 0` branch in `generator.py`; eval_001 is now just the
first RNG draw from the domestic builder (under seed=42 it becomes a
DE/DE/B2B/GEN_SERVICE standard-band case — varies with seed, as §6 intends).
`test_oracle.py` gained a module-level `_pick_swap_case(ds, labeler)` helper that
finds an eval case with a standard-band first line in a non-ES jurisdiction, and
the three anchor-dependent tests now derive the swap target's reduced rate from
`RATE_TABLE` instead of hardcoding `0.07`.

**Diff #2 — consolidate schemas.** Deleted the six unused per-record schema files
(`case/cls/jur/rat/exm/rch.schema.json`); only `final_trace.schema.json` remains,
carrying the per-record types as `$defs` (and now an explanatory `description`).
The deletions script used `git rm`, but this tree is **not a git repo**, so the
files were removed with `rm` instead — same end state. Updated `ORACLE_GROUNDING.md`
§3 (consolidated-schema design; input shape `I` enforced by `Case.__post_init__`;
rule-ref example `RATE.DE.STANDARD` → `RATE.STANDARD`) and §8 (file-layout block).

### Determinism after the refactor (corrected hashes)
Post-patch canonical-JSON SHA-256 in **this** tree (identical across two processes):
- seed 12345 → `16cd1d21…` (was `0c3b23c3…` pre-patch — shifted because the RNG
  stream now draws eval_001 instead of skipping it).
- seed 42 → `3dc683ec…`.

**Flag:** these do not match the `b163a2b5…` cited when the patches were authored.
The patches were written as diffs against a separate reconstruction
(`/mnt/project/…` → `cleanup/…`), so its generator consumes the RNG slightly
differently. The patches apply cleanly here and all gates pass, but the frozen
dataset must be pinned to **this** tree's output, not the patch author's hash.
Families verified 10/10/10/10 (eval) and 2/2/2/2 (dev); all 48 traces validate.

---

## 2026-05-28 — Part 1: VAT oracle (generator / labeler / validator / scorer)

Implemented the four remaining oracle modules to satisfy `tests/test_oracle.py`.
**27/27 tests pass.** Skip guards removed (all five modules now land).

### Layout
The repo was flat (three files at root with display names), but the suite does
`from src.oracle import rules`. Established the canonical grounding-§8 layout by
**moving** (no content changes) the three files into place:
- `Oracle grounding.md` → `docs/ORACLE_GROUNDING.md`
- `Rules.py` → `src/oracle/rules.py`
- `Test oracle.py` → `tests/test_oracle.py`

Added `src/__init__.py`, `src/oracle/__init__.py`, and `pyproject.toml`
(`[tool.pytest.ini_options] pythonpath = ["."]`) so the package imports resolve
when running `pytest` from the project root.

### Implemented
- `src/oracle/generator.py` — single threaded `random.Random(seed)`; 40 eval
  (10/family) + 8 dev; disjoint id ranges from the same stream; `family_of`,
  `to_canonical_json` (sorted keys, no set/hash-order leak).
- `src/oracle/labeler.py` — composes the resolvers into a `CaseTrace`
  (case-level JUR + per-line CLS/RAT/EXM/RCH + final aggregation).
- `src/oracle/validator.py` — the four V checks (schema conformance via
  `jsonschema`, required-field presence, citation presence, citation–decision
  consistency) + `trace_to_emitted`.
- `src/oracle/scorer.py` — `final_answer_accuracy`, per-subtask `step_accuracy`,
  `trace_consistent` (delegates to validator), fixed-order
  `earliest_error_subtask`, and `score_terminal_failure`.
- `src/schemas/*.schema.json` — the 7 schemas from §3 (`final_trace` is
  self-contained via internal `$defs`, so validation needs no cross-file refs).
  _(Superseded 2026-05-29: consolidated to `final_trace.schema.json` only.)_

Did **not** modify `rules.py`'s tables or the grounding doc.

### Decisions / flags
- **JUR is case-level**, computed from the first line's category — goods/service
  kind doesn't change country selection in the bounded set (§2.2 + rules.py
  comment). Tests access `trace.jur` (singular), consistent with this.
- **`eval_001` is a fixed DE-domestic, standard-rate (GEN_GOODS) anchor** that
  does not consume the RNG. Still a valid domestic-family case, but guarantees a
  stable DE/standard-band example for the validator/scorer fixtures (which mutate
  `eval_cases[0]`, swapping the rate to DE's 0.07 reduced). Same-seed output stays
  byte-identical; different seeds still differ (cases 2–48 vary).
  _(Superseded 2026-05-29: anchor removed; fixtures use `_pick_swap_case` instead.)_
- **4th family labeled `"b2c"`** — the doc names it "B2C with rate differential"
  but gives no code; tests assert only `"intra_community_b2b"` and `"mixed"` by
  name, plus 4×10 counts.
- **New dependency surface:** `validator.py` reads the local schema file and uses
  `jsonschema` (already installed). Pure/local — no network, clock, or LLM — but
  it is file I/O, a slight extension beyond rules.py's "no I/O." Flagging in case
  an in-module embedded schema is preferred.

### Determinism check
Same seed across two separate processes → identical canonical-JSON SHA-256
(`0c3b23c3…` for seed 12345). Cross-process reproducibility confirmed.
_(Superseded 2026-05-29: anchor removal shifts the stream; seed 12345 → `16cd1d21…`.)_
