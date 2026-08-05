# HARNESS_GROUNDING_1_SURFACE.md

**Version 1.2 — 2026-08-05.** Supersedes the 2026-07-28 draft (v1.0), which was
authored against a separate reconstruction of the oracle and is not to be used.
The v1.0→v1.1 changes are listed in §12; the four open design decisions in v1.0
were ratified on 2026-07-31 (one with an amendment, incorporated below).

> **Amended 2026-08-05 (v1.1 → v1.2):** §3.2 $S_{\text{RCH}}$ now includes
> `line_items`. Rationale — the **closed-operand principle**: every atom required
> by a subtask's contractual output fields must be part of that subtask's visible
> input state. RCH's output contract includes `vat_amount = rate ×
> line_items[].amount`, but v1.1 gave RCH only upstream records (no case atoms),
> so the operand reached RCH solely through a *voluntary* echo of `amount` in an
> upstream CLS worker's record `support` — an unspecified channel that varied
> across partitions. Discovered via the Phase-1 C2 anomaly, where the bundled
> CLS+JUR worker did not echo it (116/117 C2 zeros failed at RCH with
> `vat_amount=0`). Only the §3.2 RCH row changes; no other atom or slice is
> touched.

**SOURCE OF TRUTH for Part 2, Layer 1.** Claude Code reads this before touching
any harness code. Authority order:

> 1. `docs/ORACLE_GROUNDING.md` and the frozen code under `src/oracle/`,
>    `src/schemas/`, `data/`.
> 2. The paper: §3.2 (activity surface), §4 (configurations), §6 (methodology).
> 3. This document — for harness design decisions not fixed by (1) or (2).
>
> If a requirement here appears wrong, ambiguous, or in conflict with (1) or (2),
> **stop and flag it — do not silently reinterpret.**

**Part 1 is frozen.** Do not edit anything under `src/oracle/` or `src/schemas/`,
and do not regenerate the dataset. The frozen artifact is identified by:

| Field | Value |
|---|---|
| Repository | `github.com/pedro-santos-eng/Right-sizing-LLM-agent-decomposition-in-VAT-determination` |
| `oracle_commit` | `e2d2bdd22b85ea2915e3d719d7c12c6f18eac577` — tag `part1-frozen` |
| Seed | `42` |
| `dataset_sha256` | `3dc683ec418666fa2e8823a2ea622bfd90f638254377d83fe95d5247563e599e` |
| `case_files_sha256` | `3472544ffcd1434d59427c912ba5c77a8294de0fb675ba7d32f1572c5e410302` |

The authoritative record is `data/MANIFEST.json`; the authoritative check is
`python -m scripts.freeze_dataset --verify` (must print `VERIFY OK`). Run it
before any harness work session. *Provenance note:* a value `d874d29d…` cited in
the v1.0 draft (via `SPOTCHECK_3.3.md`) was computed over a different
serialization in a separate pre-patch reconstruction; it does not identify this
artifact and must not appear in code, tests, or the paper.

---

## 0. Purpose, scope, and the layer plan

Part 2 is built in four gated layers, each with its own grounding document:

| Layer | Grounding doc | Contents |
|---|---|---|
| **1 (this doc)** | `HARNESS_GROUNDING_1_SURFACE.md` | Subtask/dependency contract, worker-slice algebra, agent-visible case projection (label isolation), the four tools, the reference set, incremental validation adapter, run-record schema, injection seams (interfaces only) |
| 2 | orchestrator & agents | Magentic-One-style orchestrator on AutoGen v0.4, ledgers, retry budgets, worker agents, prompts $P_r$, S0 agent, model client |
| 3 | failure injection | Injection content: deterministic target sampling, injected-record construction, outage schedule |
| 4 | runner & analysis | Sweep runner (process isolation), repeats, aggregation, bootstrap/permutation analysis, figures |

**Layer 1 contains no LLM, no network, no API key, and no orchestrator.** It is
pure Python, fully testable on a laptop with zero API calls — exactly like
Part 1. Every later layer consumes Layer 1 through the interfaces defined here;
nothing in a later layer may redefine them.

Rationale for the split: paper §4.2 fixes the tool set, schemas, validation
checks, dependency order, and reference materials as **invariant across all
conditions**. Encoding those invariants once, in one module, is what makes the
paper's "only the partition of subtasks across workers changes" claim
mechanically true rather than a matter of prompt discipline.

---

## 1. Non-negotiable: label isolation *(ratified 2026-07-31)*

The single most damaging implementation error possible in Part 2 is leaking
oracle labels into agent context. It would invalidate every accuracy number in
the paper. ORACLE_GROUNDING §0: labels are "never exposed to the LLM agents
during case solving or repair. (Enforced at the harness layer…)". **This layer
is that enforcement.**

### 1.1 The agent-visible case projection

`Case` objects from the frozen generator **contain a label**: each `LineItem`
carries `true_category`, which *is* the CLS oracle label. The frozen case files
carry it inside `input.line_items[*]`, adjacent to `description`. The full
`Case` (or the raw `input` block) therefore never crosses into agent context.

Module `src/harness/surface.py` defines:

```
agent_case_view(case: Case) -> dict
```

returning exactly and only:

```
{
  "case_id": str,
  "supplier_country": str,          # Jurisdiction value
  "customer_country": str,          # Jurisdiction value
  "customer_vat_registered": bool,
  "transaction_type": str,          # "B2B" | "B2C"
  "line_items": [
    {"line_id": str, "description": str, "amount": number}
  ]
}
```

No other key may appear. The view is validated against
`src/harness/schemas/agent_case_view.schema.json`
(`"additionalProperties": false`, all fields required). All agent-facing
serialization goes through this projection; there is no other path from a
`Case` to a prompt.

**Test requirement — key-based, never string-based.** The projection test
asserts the *absence of the `true_category` key* (recursively, on the view and
on every serialized form that reaches agent context). A substring test of the
form "no `Category` value appears in the serialized view" is **forbidden and
would always fail**, because `description` legitimately contains the category
name (`synthetic-GEN_SERVICE` — see §1.3). Assert on structure, not on text.

### 1.2 Import-graph isolation

`src/oracle/labeler.py` and `src/oracle/scorer.py` must be **unreachable** from
any module that constructs agent context (`surface.py`, `tools.py`, and every
Layer-2 agent/prompt module). Required test (`test_label_isolation.py`): import
each agent-context module in a fresh interpreter and assert that neither
`src.oracle.labeler` nor `src.oracle.scorer` is present in `sys.modules`.
`rules.py` is importable (tools need its tables); `validator.py` is importable
(validation is condition-invariant machinery, not a label source).

### 1.3 Flag, not fix: CLS is near-trivial by construction *(ratified as flag)*

Frozen-dataset fact (verified on the artifact): the 54 eval line items carry
exactly **5 distinct descriptions**, all of the form `synthetic-<CATEGORY>`.
The CLS signal sits verbatim in the input text; CLS is string matching, not
classification. **This is frozen — do not "fix" it** (no description
obfuscation, no distractors; that would change the activity surface and
invalidate the frozen labels).

Consequences to carry into analysis and writing:

- Difficulty concentrates in JUR path selection (domestic 15 /
  intra_community_b2b 11 / b2c_cross_border 14 across the 40 eval cases), RCH
  synthesis (standard_charge 31 / reverse_charge 12 / exempt 11 across lines),
  and citation/consistency discipline — not in CLS.
- **The C2→C3 contrast is structurally weakened.** C2 co-locates {CLS, JUR};
  C3 separates them. With CLS near-free, that separation has little to improve,
  so a flat C2→C3 step is expected *by dataset construction* and must not be
  read as evidence against the granularity hypothesis. This reading belongs in
  §7 (results interpretation) and as a one-liner in §10, alongside the
  precedence-coverage note (§12, item 5).

---

## 2. Subtasks and dependency order ($T$, $\mathcal{D}$)

Module: `src/harness/surface.py`. Fixed, imported everywhere, never redefined:

- `SUBTASKS: tuple[str, ...] = ("CLS", "JUR", "RAT", "EXM", "RCH")` — the fixed
  order used for earliest-error attribution (matches `scorer.py`).
- `DEPENDS: dict[str, frozenset[str]]`:
  - `CLS: {}` — line-item fields only.
  - `JUR: {CLS}` — plus customer-status inputs (case fields, not a subtask).
  - `RAT: {CLS, JUR}`
  - `EXM: {CLS, JUR}`
  - `RCH: {CLS, JUR, RAT, EXM}`
- `LAYERS: tuple[frozenset[str], ...] = ({CLS}, {JUR}, {RAT, EXM}, {RCH})` —
  the topological layering; `PARALLEL_ELIGIBLE = frozenset({"RAT", "EXM"})`.
- The VAT-registration validity check is a **tool call** available to the
  JUR-owning worker (ORACLE_GROUNDING §2: it is "called directly from the
  customer VAT-registration field and is consumed by jurisdiction
  determination"). It is not a subtask and never appears in `SUBTASKS`.

Tests: `DEPENDS` is acyclic; `LAYERS` is consistent with `DEPENDS`; `SUBTASKS`
order equals the scorer's fixed order.

---

## 3. Worker partitions and slice algebra (C1–C4, S0)

### 3.1 Partitions (paper §4.3, verbatim in substance)

```
PARTITIONS = {
  "C1": ({"CLS","JUR","RAT","EXM","RCH"},),
  "C2": ({"CLS","JUR"}, {"RAT","EXM","RCH"}),
  "C3": ({"CLS"}, {"JUR"}, {"RAT","EXM","RCH"}),
  "C4": ({"CLS"}, {"JUR"}, {"RAT"}, {"EXM"}, {"RCH"}),
}
```

Workers within a partition are ordered by the position of their earliest
subtask in `SUBTASKS`. S0 is **not** a partition entry; it is a separate
no-orchestrator condition (§3.4). Tests: every partition covers $T$ exactly
once; entries match paper §4.3 exactly.

### 3.2 Atom slices (the C4 rows; paper §4.3)

Per-subtask visible state $S_\tau$ and tool permissions $F_\tau$ — the atoms
from which every coarser worker is composed:

| $\tau$ | Visible state $S_\tau$ | Tools $F_\tau$ |
|---|---|---|
| CLS | line-item `{line_id, description, amount}` for all lines | `classification_reference` |
| JUR | `supplier_country`, `customer_country`, `transaction_type`, `customer_vat_registered`, CLS records | `vat_registration_check`, `rule_citation_retrieval` |
| RAT | JUR record, CLS records | `rate_table_lookup` |
| EXM | JUR record, CLS records, **exemption table from $\mathcal{R}$** | `rule_citation_retrieval` |
| RCH | all prior structured records (CLS, JUR, RAT, EXM), plus line-item `{line_id, amount}` for all lines (`line_items` — the `vat_amount = rate × line_items[].amount` operand; v1.2) | `rule_citation_retrieval` |

Keep $F_\tau$ (callable) and $S_\tau$ (visible state) conceptually separate in
code — the paper (§4.1) leans on this distinction against the homogeneity
critique.

### 3.3 Composition rule for coarser workers

```
slice_for(assigned: frozenset[str]) -> WorkerSlice
```

- `tools` = $\bigcup_{\tau \in \text{assigned}} F_\tau$.
- `input_state` = $\bigcup_{\tau \in \text{assigned}} S_\tau$ **minus any
  subtask record produced by this same worker.** (A C2 worker owning
  {CLS, JUR} does not receive CLS records as input — it produces them. Its
  input state is the union of the *case-field* atoms: line items +
  party/transaction fields.)
- The C1 slice must equal: full `agent_case_view` + full $\mathcal{F}$ +
  $\mathcal{R}$.

Tests: `slice_for` reproduces the §3.2 table for each C4 singleton;
intra-worker subtraction is correct on C2/C3; C1 slice as stated.

### 3.4 S0

S0 receives the same full `agent_case_view`, the same $\mathcal{F}$ and
$\mathcal{R}$, and must emit the same assembled final-trace shape — with no
orchestrator, no ledgers, and the **whole trace** as its repair unit
(paper §4). Layer 1 provides S0 the same surface objects; everything
S0-specific (prompt, retry loop) is Layer 2.

---

## 4. The four tools ($\mathcal{F}$) *(semantics ratified 2026-07-31)*

Module: `src/harness/tools.py`. All four tools: deterministic; import their
tables from `src/oracle/rules.py` (**zero restated tables**); enforce closed
sets; return structured errors (never raise into agent context); log every
invocation to the run record (§7.2); return no wall-clock content. Signatures
are frozen here.

1. **`classification_reference() -> dict`** — static, case-independent. Returns
   the closed category vocabulary with each category's kind, derived from
   `rules.CATEGORY_TABLE`:
   `{"categories": [{"category": str, "kind": "goods"|"service"|"either"}]}`.
   It does not return rate bands or exemption flags (those belong to
   `rate_table_lookup` semantics and $\mathcal{R}$ respectively), and it never
   sees a case.

2. **`vat_registration_check(case_id: str) -> dict`** — the **only case-keyed
   tool**. Returns `{"case_id": ..., "customer_vat_registered": bool}` read
   from the pinned case's *input field* — an input the JUR worker already has
   in $S_{JUR}$, not a label. Unknown `case_id` →
   `{"error": "UNKNOWN_CASE"}`. Rationale on file for reviewers: the tool is
   part of the fixed $\mathcal{F}$ of paper §3.2 and models the production
   registry check; its redundancy with visible state is disclosed, not hidden.

3. **`rate_table_lookup(jurisdiction: str, band: str) -> dict`** — serves
   **only the eight real rows** of `rules.RATE_TABLE` (DE/FR/IE/ES ×
   standard/reduced): `{"jurisdiction": ..., "band": ..., "rate": float}`.
   Any other input — unknown jurisdiction, band `"exempt"`, `null` band —
   returns `{"error": "NO_SUCH_ENTRY"}`. The exempt path
   (`rate_band = null`, citation `RATE.NA_EXEMPT`) is **reasoned by the
   agent**, never served by the tool; this keeps EXM an agent decision.
   This tool carries the Layer-3 outage seam (§8.3).

4. **`rule_citation_retrieval(rule_key: str) -> dict`** — closed set =
   `rules.RULE_KEYS` (13 keys). Returns `{"rule_key": ..., "text": str}` from
   a `RULE_TEXT: dict[str, str]` mapping defined in `tools.py`. Unknown key →
   `{"error": "UNKNOWN_RULE_KEY"}`. `RULE_TEXT` is **the only newly authored
   content in Layer 1**: one to two sentences per key, statutory-register,
   describing the rule in general terms. Texts must not name cases, decide
   outcomes for specific inputs, or state the resolution of any concrete
   scenario; they describe conditions, not answers. Frozen byte-identical
   across all conditions and included in the released configuration.

---

## 5. Reference set $\mathcal{R}$: the exemption-table artifact

$\mathcal{R}$ consists of exactly one artifact: the bounded exemption table,
rendered **once** in `surface.py` from `rules.CATEGORY_TABLE` (category →
exempt flag; under the frozen rules only `EXEMPT_SUPPLY` is exempt) into a
fixed, deterministic string constant `EXEMPTION_TABLE_TEXT`. It is delivered as
**visible state** to whichever worker owns EXM (never as a callable), identical
bytes across all conditions, and included verbatim in the released
configuration (paper §4.1). Test: the constant is byte-stable across processes
and consistent with `CATEGORY_TABLE`.

---

## 6. Schemas and emitted records ($\mathcal{I}$, $\mathcal{O}$)

*(Corrected in v1.1 — the repository has **one consolidated schema**; the
per-record schema files named in v1.0 do not exist and must not be created.)*

- $\mathcal{I}$: the oracle-side case shape is enforced by the `Case`
  dataclass (`rules.py`, `__post_init__` invariant). The agent-visible input
  contract is the §1.1 projection, validated by
  `src/harness/schemas/agent_case_view.schema.json`
  (`additionalProperties: false`).
- $\mathcal{O}$: `src/schemas/final_trace.schema.json` — loaded, never copied.
  Per-record validation targets are its `$defs`:
  `#/$defs/cls`, `#/$defs/jur`, `#/$defs/rat`, `#/$defs/exm`, `#/$defs/rch`,
  plus `#/$defs/line` and `#/$defs/final` for assembly.
- Emitted-record shape (all subtasks):
  `{subtask, decision, support, rule_reference}`, matching the frozen `$defs`
  and the oracle's own emitted form (`validator.trace_to_emitted` is the
  reference implementation). Workers emit these records; the assembled full
  trace must be exactly the `final_trace` shape, including the `final`
  aggregation block, in every condition.

---

## 7. Incremental validation adapter and run logging

### 7.1 Incremental validation ($\mathcal{V}$ at the subtask repair unit) *(invariant ratified as amended, 2026-07-31)*

`validator.validate_trace` operates on a **complete** trace — correct for S0's
whole-trace repair unit, but C1–C4 retry at the **subtask** level (paper §4.2),
so the orchestrator needs per-record verdicts as records arrive. Module
`src/harness/validation.py`:

```
validate_record(subtask: str, record: dict, accepted: dict[str, dict]) -> RecordVerdict
```

- Implements the same four check families as $\mathcal{V}$ — (1) schema
  conformance, (2) required-field presence, (3) citation presence against the
  closed key set, (4) citation–decision consistency — scoped to one record:
  schema-validate against the subtask's `$defs` entry; run every consistency
  check whose inputs are satisfied by `record` + the `accepted` upstream
  records (e.g. RCH `reverse_charge=true` requires accepted JUR path
  `intra_community_b2b` and a registered customer; a RAT rate must be a real
  `RATE_TABLE` entry for the stated jurisdiction/band; EXM `exempt=true`
  requires the exemption citation).
- Checks whose inputs are not yet available are **deferred, not passed** —
  they run when the dependent record arrives or at final assembly.
- **The authoritative gate is unchanged in every condition:** a case is
  complete only when the assembled full trace passes
  `validator.validate_trace`. Incremental verdicts route retries; they never
  replace the final full-trace check. This preserves the paper's "same
  $\mathcal{V}$ across conditions" claim exactly.

**Equivalence invariant (amended wording — required test).** A per-record
verdict is defined only *given accepted upstream context*, applied in
dependency order (`LAYERS`). The invariant is a biconditional over the
**conjunction** of per-record verdicts, not over records in isolation:

> For a fully assembled trace, evaluating `validate_record` over `SUBTASKS` in
> dependency order (each record given the previously accepted ones) yields
> all-accept **iff** `validate_trace` passes on the assembled trace.

Required test corpus: (a) all 48 frozen oracle traces (all-accept ⇒
full-trace pass); (b) ≥40 single-field mutations spanning all four check
families (every `validate_trace` failure is caught by `validate_record` on the
culpable record no later than final assembly). A record-in-isolation
formulation of this invariant is not satisfiable (cross-record checks need
upstream context) and must not be implemented or tested.

### 7.2 Run-record schema (`src/harness/runlog.py`)

One run record per `(condition, case_id, repeat)`. Layer 1 defines the schema,
writer, reader, and validator; Layers 2–4 fill the fields. Required content:

- identity: `condition`, `case_id`, `repeat`, `oracle_commit`,
  `dataset_sha256` (echoed from the manifest);
- per-worker events: dispatches, retries (count + per-retry verdicts),
  terminal status (`ok` | `validation_exhausted` | `timeout` | `no_trace`);
- tool invocations: tool name, arguments, result-or-structured-error, in call
  order;
- validation: every `RecordVerdict`, plus the final `validate_trace` outcome
  and `failed_checks`;
- injection events (§8), as no-op markers in Layer 1;
- accounting placeholders: token counts, latency (populated in Layers 2/4).

Wall-clock timestamps live **only** in the log envelope, never inside
agent-visible content or trace content. Test: schema round-trips
(write → validate → read) on synthetic examples.

---

## 8. Injection seams (interfaces only; content is Layer 3)

Three seams, matching the paper's §6.4 failure conditions. Layer 1 defines the
seam signatures, the no-op defaults, and the log event; target sampling,
injected-record construction, and outage schedules are specified in the
Layer-3 grounding doc, not here.

1. **Worker-timeout hook** — for a designated `(case_id, subtask)`, the
   dispatch wrapper treats the worker call as timed out (no record produced;
   normal retry/terminal-status machinery applies). No-op by default.
2. **Hallucinated-output interception** — for a designated
   `(case_id, subtask)`, replaces the worker's emitted record with a
   Layer-3-constructed record that is **schema-valid and citation-consistent
   but oracle-wrong** (the case ORACLE_GROUNDING §4 explicitly anticipates).
   Interception events are logged.
3. **Tool transient-error hook** — inside `rate_table_lookup` only: for a
   designated `case_id`, the **first** invocation that reaches RAT returns a
   transient `{"error": "TOOL_UNAVAILABLE"}` and recovers on subsequent
   attempts (paper §6.4, shared-tool outage; $m = 1$, eight affected cases).

---

## 9. File layout

```
src/harness/
  __init__.py
  surface.py        # T, DEPENDS, LAYERS, PARTITIONS, atom slices, slice_for,
                    #   EXEMPTION_TABLE_TEXT, agent_case_view
  tools.py          # the four tools + RULE_TEXT mapping + logging + outage seam
  validation.py     # validate_record + assembly gate wrapper around validate_trace
  runlog.py         # run-record schema, writer, reader, validator
  schemas/
    agent_case_view.schema.json
tests/
  test_surface.py
  test_tools.py
  test_validation_incremental.py
  test_label_isolation.py
  test_runlog.py
```

Same conventions as Part 1: `pyproject.toml` pythonpath already covers imports;
pure modules; loud failures; no network, clock-in-content, or LLM anywhere in
Layer 1.

---

## 10. Readiness gate — Layer-1 definition of done

Layer 2 (orchestrator/agents) does not start until every box is checked:

- [ ] `SUBTASKS`/`DEPENDS`/`LAYERS` consistent, acyclic, order matches scorer
- [ ] `PARTITIONS` exactly match paper §4.3; every partition covers $T$ exactly once
- [ ] `slice_for` reproduces the C4 atom table; C1 slice = full view + full
      $\mathcal{F}$ + $\mathcal{R}$; intra-worker subtraction correct on C2/C3
- [ ] All four tools deterministic, closed-set-enforcing, structured-error,
      logging; zero restated tables (imports from `rules.py` only)
- [ ] Exemption-table artifact fixed, byte-identical across conditions
- [ ] `agent_case_view` projection test passes on all 48 cases (**key-based
      assertions**, §1.1); view schema strict
- [ ] Import-graph test: `labeler`/`scorer` unreachable from agent-context modules
- [ ] Incremental ⟺ full-trace validation equivalence (amended invariant, §7.1)
      on 48 oracle traces + ≥40 mutations spanning all four check families
- [ ] Run-record schema round-trips (write → validate → read) on synthetic examples
- [ ] Injection seams present as no-ops, logged, covered by tests
- [ ] Whole suite green alongside the untouched 27 Part-1 tests
- [ ] `python -m scripts.freeze_dataset --verify` prints `VERIFY OK` in the
      same environment the harness tests ran in

---

## 11. Guardrails for the coding assistant

- Do **not** modify anything in `src/oracle/` or `src/schemas/`, or regenerate
  the dataset with a different seed.
- Do **not** add tools, visible-state fields, subtasks, or reference materials
  beyond those defined here — the paper's fixed-surface claim depends on it.
- Do **not** let any tool return a per-case decision beyond the semantics in §4
  (registration status is the only case-keyed lookup, and it serves an input
  field).
- Do **not** import `labeler` or `scorer` from any module reachable by
  agent-context construction.
- Do **not** put wall-clock values inside agent-visible content or trace
  content (log envelope only).
- Do **not** create per-record schema files (`cls/jur/rat/exm/rch.schema.json`)
  — the consolidated `final_trace.schema.json` `$defs` are the contract (§6).
- Do **not** test label isolation by substring — key-absence assertions only
  (§1.1).
- When something is ambiguous, choose the bounded interpretation, add a
  one-line comment pointing to the relevant section of this document, and flag
  it for review rather than improvising.

---

## 12. Revision note — v1.0 (2026-07-28) → v1.1 (2026-07-31)

1. **Dataset identification corrected.** v1.0 pinned the dataset to
   `d874d29d…` "verified in SPOTCHECK_3.3.md"; that value was computed over a
   different serialization in a pre-patch reconstruction. v1.1 pins the
   manifest values (`3dc683ec…` / `3472544f…`, `oracle_commit e2d2bdd`, tag
   `part1-frozen`) and makes `freeze_dataset --verify` the authoritative check.
2. **Equivalence invariant restated** (§7.1) as a dependency-ordered
   conjunction biconditional; the record-in-isolation form is explicitly
   non-satisfiable and banned.
3. **Schema references corrected** (§6) to the consolidated
   `final_trace.schema.json` `$defs`; v1.0 referenced six per-record schema
   files that do not exist in the frozen repository.
4. **Ratification recorded.** Decisions 1 (label isolation, with the
   key-based-test requirement), 2 (CLS triviality as a frozen flag, with the
   C2→C3 interpretive consequence), 3 (incremental validation, as amended),
   and 4 (tool semantics: eight real rate rows; `RATE.NA_EXEMPT` reasoned, not
   served; registration returns an input field; rule-citation text as the only
   newly authored content) are ratified and binding.
5. **Coverage note for §10.2 disclosure.** Verified on the frozen artifact:
   zero line items across eval+dev combine `EXEMPT_SUPPLY` with
   `intra_community_b2b`; the `EXEMPT > REVERSE_CHARGE` precedence edge is
   never exercised by the dataset. This is a sampling-coverage gap, not a
   logic defect; it requires a one-sentence disclosure in paper §10.2, not a
   fix.
