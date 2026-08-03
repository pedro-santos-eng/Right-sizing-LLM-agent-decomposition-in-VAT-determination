# HARNESS_GROUNDING_2_ORCHESTRATION.md

**Version 1.1 — 2026-07-31.** Layer-2 source of truth for Part 2. The four
design decisions marked **[DECISION n]** were **ratified on 2026-07-31** (§12);
Decision 1's author check was resolved by a full-paper read the same day
(§3.1). Implementation may begin once Layer 1 passes its readiness gate.

> **Amended 2026-08-02:** the §1 sampling pin is temperature = 0.2 only;
> `top_p` is unset (echoed as `null` in run records). The pinned model's
> API rejects specifying both sampling parameters (400: "temperature and
> top_p cannot both be specified"), surfaced by the first live smoke with
> $0 spent and zero runs produced. Temperature is the operative
> low-variance control; at T = 0.2 a 0.95 nucleus cut is behaviorally
> near-inert, so the intended regime is preserved. Ratified by the author;
> paper §4.6 amended to match. No run ever used the old pair.

> **Amended 2026-08-03:** the §5 `RAT` instruction text (P_r) now states the
> band←category bounded rule, GENERATED from the frozen `rules.CATEGORY_TABLE`
> (never hand-listed). Phase-0 attempt 1 failed gate 1 at C1 = 0%: the sole
> root cause was a systematic RAT=null failure — the RAT instruction required
> the band as a tool input but never told a "never invent" worker how to derive
> it from the category, so a compliant worker had no path to a rate. This is a
> completeness fix to the instruction surface, not a decoding change; citation
> and decision scoring remain measured. Ratified by the author under the
> reopened model-choice decision.

**SOURCE OF TRUTH for Part 2, Layer 2.** Claude Code reads this before touching
any Layer-2 code. Authority order:

> 1. `docs/ORACLE_GROUNDING.md` and the frozen code under `src/oracle/`,
>    `src/schemas/`, `data/`.
> 2. The paper: §3.2 (activity surface), §4 (configurations, esp. §4.2, §4.5,
>    §4.6), §6 (methodology, esp. §6.3, §6.4).
> 3. `docs/HARNESS_GROUNDING_1_SURFACE.md` (v1.1) — Layer-1 interfaces are
>    binding; nothing here may redefine them.
> 4. This document — for orchestration decisions not fixed by (1)–(3).
>
> If a requirement here appears wrong, ambiguous, or in conflict with (1)–(3),
> **stop and flag it — do not silently reinterpret.**

Part 1 identification and the `freeze_dataset --verify` precondition are as
stated in the Layer-1 doc header and are not repeated here.

---

## 0. Scope of Layer 2

Layer 2 delivers: the deterministic orchestrator (ledgers, dispatch, repair,
assembly), the worker agents and their prompts $P_r$, the S0 agent and its
whole-trace repair loop, the S0′ matched-token assembly knobs, the pinned model
client, and token/latency accounting — wired into the Layer-1 surface, tools,
validation, and runlog.

Layer 2 introduces the first LLM dependency, but **remains laptop-testable
without API calls**: every control-flow, repair, accounting, and assembly test
runs against a scripted model client (§8). Exactly one gate item (§10, live
smoke) touches the real API, and it is skip-if-unconfigured. The five-case dry
run (§11) is defined here but executed at the Layer-3/4 boundary.

Out of scope: injection *content* (Layer 3 — Layer 2 only honors the Layer-1
seams at the correct points in the flow), the sweep runner, repeats,
aggregation, and statistics (Layer 4).

---

## 1. Fixed execution constants (paper §4.6; completed and pinned here)

Module: `src/harness/model_client.py` exposes a single frozen constants object
`EXECUTION_CONSTANTS`, echoed verbatim into every run record:

| Constant | Value | Source |
|---|---|---|
| `model` | `claude-haiku-4-5-20251001` | pinned (research pass 2026-06-24) |
| `temperature` | `0.2` | paper §4.6 (committed, not open) |
| `top_p` | unset — see amendment note | paper §4.6, amended 2026-08-02 (was `0.95`; model rejects temp+top_p) |
| `max_tokens` (per call) | `4096` | **[DECISION 4]** |
| `timeout_s` (per model call) | `120` | **[DECISION 4]** |
| `case_wall_cap_s` (safety) | `1200` | **[DECISION 4]** |
| `subtask_retry_budget` | `3` | paper §4.5 fairness principle (§3.3 below) |
| `s0_trace_repair_budget` | `3` | paper §4.5 ("up to three whole-trace repair attempts") |
| `api_transport_retries` | `3`, backoff 2/4/8 s | **[DECISION 4]** — infrastructure, logged separately |
| `seed` | n/a | Anthropic API has no seed control; paper §4.6's "otherwise" branch applies — repeats are repeated executions under identical settings, disclosed |

Notes. `temperature`/`top_p` come from the paper and are **not** open here;
what §12 ratifies is the completion of the pin list (`max_tokens`, timeouts,
transport policy). Transport retries (429/5xx) are infrastructure, not
experimental repair: they are counted in `api_retries` and included in
wall-clock latency, never in validation-retry counts. The API key is read from
`ANTHROPIC_API_KEY` only; it never appears in code, config files, logs, or run
records.

---

## 2. Model client **[DECISION 1, part b]**

`src/harness/model_client.py`:

```
make_model_client() -> ChatCompletionClient      # real: AnthropicChatCompletionClient
make_scripted_client(script) -> ChatCompletionClient  # tests: replay/scripted
```

- Real client: `autogen_ext` `AnthropicChatCompletionClient` configured from
  `EXECUTION_CONSTANTS`. Pin `autogen-agentchat==0.7.5` and
  `autogen-ext[anthropic]==0.7.5` in `pyproject.toml` (AutoGen v0.4 line,
  stable but in maintenance mode — pinning is the mitigation).
- Scripted client for tests: `autogen_ext`'s replay client if its 0.7.5 API
  suffices; otherwise a ~50-line in-repo `ChatCompletionClient` implementation
  that replays scripted responses and synthesizes `usage` numbers. All Layer-2
  tests except the live smoke use it.
- **Fallback clause:** if the maintenance-mode AutoGen Anthropic client blocks
  anything required here (usage metadata per call, tool-call plumbing,
  timeout behavior), implement the same `make_model_client()` interface
  directly on the `anthropic` SDK. The interface is ours; the paper's claims
  reference the AutoGenBench-style *isolation pattern* (§6.7), not any
  specific client class. Flag before switching.

Every model call records: input_tokens, output_tokens (from the API `usage`
block), per-call latency, api_retry count, and the stop reason.

---

## 3. The orchestrator (conditions C1–C4) **[DECISION 1, DECISION 2]**

Module: `src/harness/orchestrator.py`.

### 3.1 [DECISION 1] Deterministic programmatic orchestrator — no orchestrator LLM

The orchestrator implements the Magentic-One **ledger semantics** of paper
§4.2 — task ledger, progress ledger, dependency-ordered dispatch,
validation-gated retry — as deterministic Python. **The orchestrator makes no
LLM calls.** LLM calls happen only inside worker invocations.

Rationale, on the paper's own text: §4.2 requires the orchestrator to be
invariant across C1–C4 and §4.6 requires "tool ordering, case ordering, and
validation execution" to be deterministic. Since $\mathcal{D}$ is fixed and
known, there is no planning problem for an orchestrator LLM to solve; a stock
LLM-driven `MagenticOneGroupChat` would add uncontrolled stochasticity and
cost to the very component the paper holds fixed. Nothing in §4.2 claims
dynamic LLM planning, so the deterministic implementation satisfies the
"Magentic-One-style" citation as written. *(Author check resolved 2026-07-31: a full read of all nine written paper
sections found no passage requiring an LLM orchestrator. Five wording edits
align the paper with this implementation — §4 ¶2 "re-planning" →
"dependency-ordered dispatch"; a determinism sentence appended to §4.2; the
"orchestrator-level planning calls" clause removed from §6.1 Cost;
"replanning" removed from the §6.2 and §10.4 component lists — converging on
§5.4's existing formulation.)*

Ledger structures (serialized into the run record each transition):

```
TaskLedger:    {subtask: {status: pending|in_flight|accepted|failed_terminal,
                          attempts: int, owner_worker: str}}
ProgressLedger: accepted records, per-attempt RecordVerdicts, retry counts,
                unresolved failures, injection markers
```

### 3.2 [DECISION 2] Dispatch model: per-worker bundle invocation, per-subtask repair

This mirrors paper §6.4's injection-unit definition ("the worker invocation
responsible for τ… under C2 and C3, the responsible worker may cover several
subtasks") and §4.2's subtask-level retry, simultaneously:

- **Initial dispatch = one invocation per worker, covering its full assigned
  bundle.** Workers are invoked in worker order (earliest subtask in
  `SUBTASKS`), each as soon as all its *external* dependencies are accepted.
  The invocation message contains: the worker's `input_state` from
  `slice_for` (view slice + accepted upstream records), and the output
  contract for **all** its subtasks (emit one JSON payload with one record per
  assigned subtask, in `SUBTASKS` order; the RCH-owning worker's contract
  additionally includes the `final` aggregation block — §3.5).
- **Validation** runs per record, in `SUBTASKS` order, via Layer-1
  `validate_record` with the accepted upstream context. Passing records are
  accepted immediately; failing records enter repair.
- **Repair dispatch = per-subtask follow-up invocations to the owning
  worker,** in the same persistent worker conversation, containing exactly:
  the verbatim `failed_checks` strings for that record, and the output
  contract for that subtask only. Budget: `subtask_retry_budget = 3` repairs
  per subtask (initial bundle emission is not a repair). Repair feedback is
  **only** validator output — never scorer output, never oracle content
  (label-isolation corollary; §9 guardrails).
- **Worker context is persistent within a case** and discarded across cases.
  This is the mechanism by which coarser granularity can help (continuity
  across its own subtasks) or hurt (context pollution) — i.e., the object of
  study; do not "optimize" it away.
- **Terminal failure:** a subtask exhausting its budget →
  `failed_terminal`; since every downstream subtask depends on it
  (transitively), the case ends with terminal status
  `validation_exhausted`, and the partial trace is retained (paper §4.2).
  Timeouts at the invocation level decrement nothing by themselves; a timed-out
  invocation counts as a failed attempt for every not-yet-accepted subtask it
  was responsible for, then normal repair applies. Case-level statuses:
  `ok | validation_exhausted | timeout | no_trace` (Layer-1 runlog).

### 3.3 Retry-budget symmetry (paper §4.5)

The fairness principle grants equal retry opportunities at each condition's
natural repair unit: **3 per subtask** for C1–C4, **3 per whole trace** for
S0. Worst-case call counts therefore differ by design; the asymmetry is
measured (total token cost is the primary cost metric), not equalized.

### 3.4 Concurrency within a case

Under bundle dispatch, separate parallel-eligible workers exist only in C4
(RAT ‖ EXM, both unlocked by accepted CLS+JUR). They are dispatched
concurrently via asyncio with a within-case cap of 2. C1–C3 and S0 have no
within-case parallelism (RAT/EXM co-resident). Cross-case concurrency and the
global provider-contention cap (paper §4.6) are Layer 4.

### 3.5 Assembly and the full-trace gate

The `final` aggregation block is **emitted by the RCH-owning worker** as part
of its bundle payload (its visible state $S_{RCH}$ contains all prior
records, which is exactly what the aggregation needs). The harness never
computes `final` for C1–C4: S0 must emit it unaided, so having the harness
compute it for orchestrated conditions would be an unfair assist.

Assembly: accepted records + `final` → the exact `final_trace` shape →
authoritative `validator.validate_trace` gate (Layer-1 §7.1). Any gate failure
is attributed to the culpable record via its `failed_checks` prefix and routed
as a repair to that record's owner, consuming that subtask's budget;
`final`-block failures are attributed to the RCH owner. (Per the Layer-1
equivalence invariant this path should be rare; it exists so no failure is
unroutable.) Case latency runs from case submission to validated trace or
terminal failure (paper §6.1).

### 3.6 Injection seam placement (hooks only; content is Layer 3)

- **Timeout seam** wraps the worker invocation responsible for the designated
  τ (the whole invocation, per §6.4's injection unit).
- **Interception seam** runs between payload extraction and validation: it
  replaces the τ record inside the responsible invocation's payload (for S0:
  the τ slot of the emitted trace). Interception is logged; validation then
  proceeds normally — a conforming injected record fires no retry, by design.
- **Outage seam** lives inside `rate_table_lookup` (Layer 1) and needs no
  Layer-2 code beyond passing the case context through.

---

## 4. Workers and prompt assembly ($P_r$)

Modules: `src/harness/agents.py`, `src/harness/prompts.py`.

- `make_worker(slice: WorkerSlice, client) -> AssistantAgent` — one AutoGen
  `AssistantAgent` per worker: system message = role preamble assembled from
  the slice; tools = `FunctionTool` wrappers of exactly `slice.tools` (Layer-1
  implementations; no re-wrapping of semantics).
- **Prompt assembly is a pure function of the slice.** `prompts.py` holds
  static components: a shared role-preamble template, per-subtask instruction
  blocks, per-subtask output contracts (the `$defs` excerpt rendered from the
  frozen schema — generated, not hand-copied), the tool-use rules ("cite only
  keys returned by `rule_citation_retrieval`; never guess table values; use
  the tools"), and the `EXEMPTION_TABLE_TEXT` inclusion rule (EXM owner only).
  `assemble_prompt(slice)` composes them; identical slices ⇒ identical
  prompts, so "only the partition changes" holds at the prompt level too, by
  construction.
- **No per-condition tuning of worker prompts.** Any wording change during
  scripted testing or the dry run edits the shared components and propagates
  to every condition uniformly.
- **Prompt freezing:** `PROMPT_HASHES` (SHA-256 of every assembled system
  message and instruction block, per condition) are computed at run time and
  written into every run record. Prompts are frozen before the eval sweep;
  a hash change after freeze aborts the run.

---

## 5. Structured output channel **[DECISION 3]**

Workers and S0 return their records as **one JSON object in a fenced
```` ```json ```` block, the last such block in the final assistant message.**
The harness extracts it, parses it, and hands records to `validate_record` /
`validate_trace`. There is no reliance on provider-side JSON mode or response
formats; tool calls still use native function calling.

Failure ladder: no extractable block, unparsable JSON, or a payload missing a
required subtask record ⇒ counts as a **validation failure** for the affected
subtask(s) (identifier `payload: …`), consuming repair budget exactly like any
other failed check, with the extraction error as feedback. Rationale: JSON
reliability is a declared primary risk of the model choice; making its failure
mode identical to validation failure keeps the retry machinery single-pathed
and makes the reliability measurable from run records (dry-run gate, §11).

---

## 6. S0 and the matched-token variants

Module: `src/harness/s0.py`.

- **S0** (paper §4.5): one `AssistantAgent`, ReAct/function-calling style,
  full `agent_case_view`, full $\mathcal{F}$, $\mathcal{R}$ as visible state;
  emits the complete `final_trace` JSON (all records + `final`) in one
  payload. Validation: authoritative `validate_trace` only (whole-trace repair
  unit). On failure: whole-trace repair message with the verbatim
  `failed_checks`, same persistent context, up to `s0_trace_repair_budget = 3`
  repairs. No ledgers, no per-subtask dispatch, no incremental verdicts.
- **S0 prompt tuning** happens **only** on `dev_001..dev_008` (paper §4.5,
  §10.2): manual iteration allowed there, forbidden anywhere near eval cases;
  final prompt, tuning budget, and dev identifiers go to the paper appendix.
- **S0′ assembly knobs** (paper §6.3): `s0.py` exposes the three sanctioned
  budget-expansion slots — extended role description, exemplar slots (filled
  only with dev-case-derived exemplars), and an intermediate-scratchpad
  instruction — so Layer 4 can tune $\text{S0}'_{C2}$ (and later
  $\text{S0}'_{C^\star}$) to within ±10% of the measured per-case token
  budget on the dev split. Layer 2 provides the knobs and the token
  measurement; the tuning loop itself is Layer 4.

---

## 7. Accounting

Per model call: input_tokens, output_tokens (API `usage`), latency,
api_retries, stop reason. Per (condition, case, repeat): totals per worker and
overall; validation-retry counts per subtask; tool-call counts per tool;
case latency (submission → validated/terminal). **Total token cost =
Σ(input+output) over all calls including repairs** — the paper's primary cost
metric; retry volume is part of the measured cost (§4.5), never netted out.
Dollar cost is derived in Layer-4 analysis from one pinned price sheet, not
computed here. All fields land in the Layer-1 run record; `EXECUTION_CONSTANTS`
and `PROMPT_HASHES` are echoed into every record.

---

## 8. Scripted-client test harness

All Layer-2 logic is exercised deterministically with `make_scripted_client`:

- **Happy path** per condition (C1–C4, S0): scripted payloads → assembled
  trace passes `validate_trace`; ledgers and run record complete and correct.
- **Repair path:** scripted first-attempt failures (bad citation, wrong-band
  rate, malformed payload) → correct per-subtask feedback, budget decrements,
  eventual accept; and budget exhaustion → correct terminal status + partial
  trace retained.
- **Timeout path:** scripted delay > `timeout_s` → invocation treated as
  failed attempt for its unaccepted subtasks; seam-forced timeout logged.
- **C4 concurrency:** RAT ‖ EXM dispatched concurrently, cap honored,
  deterministic merge of results.
- **Accounting:** synthesized usage numbers propagate to the run record
  exactly.

No test outside the live smoke may require the network or an API key.

---

## 9. Guardrails for the coding assistant

All Layer-1 §11 guardrails remain in force. Additionally:

- The orchestrator makes **no** LLM calls; do not introduce an LLM "planner",
  summarizer, or judge anywhere in Layer 2.
- Repair feedback is the verbatim `failed_checks` list and nothing else — no
  hints, no restated rules, no oracle-derived content.
- Do not import `labeler` or `scorer` from `orchestrator.py`, `agents.py`,
  `prompts.py`, `s0.py`, or `model_client.py` (extend the Layer-1 import-graph
  test to these modules).
- Do not tune worker prompts per condition; shared components only.
- Do not compute the `final` block harness-side for any condition.
- Do not equalize call counts across conditions; the asymmetry is measured.
- Never place the API key, wall-clock values, or file paths inside
  agent-visible content; never log the key.
- When something is ambiguous, choose the bounded interpretation, comment with
  a pointer to the section here, and flag for review.

---

## 10. Readiness gate — Layer-2 definition of done

Layer 3 (injection content) and Layer 4 (runner) do not start until:

- [ ] `EXECUTION_CONSTANTS` frozen, complete, echoed into run records
- [ ] Prompt assembly is a pure function of `WorkerSlice`; snapshot hashes
      stable across processes; output contracts generated from the frozen
      schema `$defs`, not hand-copied
- [ ] Scripted-client happy path green for C1, C2, C3, C4, S0 (assembled
      traces pass `validate_trace`; run records complete)
- [ ] Repair machinery: per-subtask feedback = verbatim `failed_checks`;
      budgets honored; exhaustion → correct terminal status with partial trace
- [ ] Payload-extraction failures consume repair budget via the §5 ladder and
      are visible in run records
- [ ] Timeout machinery covered (scripted delay + seam-forced), correct
      statuses
- [ ] C4 RAT ‖ EXM concurrency correct and capped; C1–C3/S0 strictly
      sequential
- [ ] S0 whole-trace repair loop green under scripted client; S0′ knobs
      present and covered
- [ ] Accounting fields correct end-to-end from synthesized usage
- [ ] Import-graph label-isolation test extended to all Layer-2 modules, green
- [ ] Injection seams honored at §3.6 placements (still no-op), logged
- [ ] Whole suite green alongside untouched Part-1 (27) and Layer-1 tests
- [ ] **Live smoke** (skip-if-no-`ANTHROPIC_API_KEY`): `dev_001` under C1 and
      S0 against the real API produces validated traces; usage and latency
      populate the run record

---

## 11. Five-case dry run (defined here; executed at the Layer-3/4 boundary)

Protocol: `dev_001..dev_005`, conditions C1–C4 + S0, 1 repeat, real API.
Gates before the main sweep:

1. **Difficulty band:** C1 final-answer accuracy in **[40%, 90%]** (the
   operating-point gate for the model choice). Note the §1.3 (Layer-1 doc)
   expectation: with CLS near-free, misses concentrate in JUR/RCH/citations.
2. **Payload reliability:** extraction/parse failure rate < 10% of
   invocations; if breached, revise shared prompt components (uniformly) and
   re-run the dry run — do not switch models silently.
3. **Cost extrapolation:** measured tokens/case × sweep size within budget;
   record the projection in the DEVLOG before proceeding.

Dry-run results are development data (dev split); they never enter reported
results.

---

## 12. Decisions — all four ratified 2026-07-31

1. **[DECISION 1] Deterministic programmatic orchestrator** implementing
   Magentic-One ledger semantics with no orchestrator LLM; AutoGen 0.7.5
   pinned for the model client, `AssistantAgent`, and `FunctionTool`; explicit
   fallback clause to the `anthropic` SDK behind the same interface if the
   maintenance-mode client blocks a requirement. The author check is
   resolved (§3.1); the five paper alignment edits are applied.
2. **[DECISION 2] Dispatch model:** initial per-worker **bundle** invocation +
   per-subtask **repair** follow-ups in persistent worker context; retry
   budget 3 per subtask / 3 whole-trace for S0; the RCH-owning worker emits
   the `final` block (harness never computes it). Grounded in paper §4.2,
   §4.5, and §6.4's injection-unit wording.
3. **[DECISION 3] Structured output channel:** fenced-JSON payload +
   extraction, with extraction/parse failures consuming repair budget through
   the same validation ladder; no provider JSON-mode dependency; native
   function calling for tools.
4. **[DECISION 4] Pin-list completion:** `max_tokens 4096`, per-call timeout
   120 s, case wall cap 1200 s, transport retries 3 with 2/4/8 s backoff
   (logged as infrastructure). (`temperature 0.2` is the paper's §4.6
   commitment, restated, not open; `top_p` is unset per the 2026-08-02
   amendment note above.)
