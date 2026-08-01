# Oracle Grounding Document — Source of Truth

> **Read this file before writing or modifying any code in `src/oracle/` or `src/schemas/`.**
> This document is the single source of truth for the deterministic VAT rule engine (the "oracle").
> It is derived from the paper specification (§3 *VAT Determination as a Testbed*, §4 *Configurations*)
> and the project infrastructure plan. Where this document and a code comment disagree, **this document wins**.
> If a requirement here appears wrong or incomplete, stop and flag it — do not silently reinterpret it.

> **Amended 2026-08-01:** the §2.1 RCH exempt bullet now states `liable party = none`.
> The §3.3 spot-check (`docs/SPOTCHECK_3.3.md` §4.1) found the bullet silent on this field
> while the engine emits `"none"`; the amendment closes the specification gap. No rule
> semantics changed and no re-freeze is required — the frozen dataset already carries `"none"`.

---

## 0. Purpose and role in the project

This component is **Part 1** of the project: the deterministic VAT rule engine. It is the
foundation everything else depends on. No agent, orchestrator, API call, or accuracy number
has any meaning until the oracle exists and passes its tests.

The oracle has exactly five responsibilities:

1. **Generate** the synthetic VAT cases (40 evaluation + 8 development), stratified across four scenario families.
2. **Label** each case with a final VAT determination *and* the five intermediate step labels.
3. **Validate** any emitted trace (from an agent or from itself) against the validation-check set `V`.
4. **Score** an emitted trace against the oracle labels: final-answer accuracy, step-level accuracy, earliest-error attribution.
5. Be **fully deterministic**: same seed in → byte-identical cases and labels out, on any machine, forever.

What the oracle is **NOT**:
- It is **not** a competing agentic baseline. It never runs under stochastic repeats and never appears in cost/latency comparisons.
- It is **not** a deployable VAT engine. It makes **no legal-compliance claims**. It encodes a *bounded* rule set only.
- Its labels are **never exposed to the LLM agents** during case solving or repair. (Enforced at the harness layer, but the oracle must keep generation and labeling cleanly separable so the harness can withhold labels.)

This stage is **laptop-only**. Pure Python, no network, no API keys, no cloud, no GPU.

---

## 1. The bounded VAT rule set

The rule set is modeled on **EU-style** VAT treatment. It is deliberately small. It covers exactly
four scenario families and nothing else. Reverse-charge logic is modeled on the general B2B
place-of-supply / reverse-charge pattern associated with Council Directive 2006/112/EC, **simplified**.

> **Bounded means bounded.** Do not add Member-State-specific rates beyond the table below, special
> schemes (margin scheme, OSS/IOSS, triangulation, distance-selling thresholds), real-time rate
> updates, or any rule not listed here. If a case cannot be labeled by the rules below, it is an
> invalid case and the generator must not produce it.

### 1.1 Jurisdictions (closed set)

Use a small closed set of EU member jurisdictions. The pilot uses these four; do not add others
without updating this document:

| Code | Name        | Standard rate | Reduced rate | Notes                          |
|------|-------------|--------------:|-------------:|--------------------------------|
| DE   | Germany     | 19%           | 7%           | supplier or customer           |
| FR   | France      | 20%           | 5.5%         | supplier or customer           |
| IE   | Ireland     | 23%           | 13.5%        | supplier or customer           |
| ES   | Spain       | 21%           | 10%          | supplier or customer           |

A non-EU code `XX` (rest-of-world) **may** be used only as a customer country to force an export /
out-of-scope path **if** you choose to include it. For the bounded pilot, prefer keeping all parties EU.
If `XX` is used, it must be documented and labeled deterministically. Default: **EU-only**.

### 1.2 Product / service classification vocabulary (closed set)

Each line item classifies into exactly one category. This is the bounded vocabulary used by both the
generator and the rule engine:

| Category code | Kind    | Rate band  | Exemption-eligible |
|---------------|---------|------------|--------------------|
| GEN_GOODS     | goods   | standard   | no                 |
| RED_GOODS     | goods   | reduced    | no                 |
| GEN_SERVICE   | service | standard   | no                 |
| RED_SERVICE   | service | reduced    | no                 |
| EXEMPT_SUPPLY | either  | n/a        | yes (always exempt)|

`kind` (goods vs service) matters for place-of-supply / reverse-charge logic.
`rate band` selects standard vs reduced rate within the jurisdiction's table.
`EXEMPT_SUPPLY` is the only category that triggers an exemption and it is *always* exempt
(no conditional exemptions in the bounded set).

### 1.3 Transaction roles

- `supplier_country` ∈ jurisdictions
- `customer_country` ∈ jurisdictions
- `customer_vat_registered` ∈ {true, false}
- `transaction_type` ∈ {B2B, B2C} — must be internally consistent: **B2B ⇒ customer_vat_registered = true**;
  **B2C ⇒ customer_vat_registered = false**. The generator must never produce B2B with an unregistered customer.

---

## 2. The five subtasks `T` (fixed, ordered, never change)

Abbreviations are fixed and used everywhere in code, configs, and the paper:

| Code | Subtask                  | Depends on (logical)                          |
|------|--------------------------|-----------------------------------------------|
| CLS  | Goods/services classification | (line-item fields only)                  |
| JUR  | Jurisdiction determination    | CLS, customer status                     |
| RAT  | Rate lookup                   | JUR, CLS                                 |
| EXM  | Exemption check               | JUR, CLS                                 |
| RCH  | Reverse-charge decision       | CLS, JUR, RAT, EXM (all prior)           |

Logical depth `d = 5` is fixed. RAT and EXM are **parallel-eligible** once JUR and CLS are done.
RCH is the terminal synthesis step and depends on everything.

### 2.1 Per-subtask oracle logic (the actual rules)

**CLS — classification.** Map each line-item description to exactly one category in the vocabulary
(§1.2). In the synthetic harness the generator *assigns* the true category and writes a description
consistent with it; the oracle label for CLS is simply that assigned category. (The oracle does not do
NLP — it knows the ground truth because it generated the case. The agents do the hard classification.)

**JUR — jurisdiction / place of supply (simplified).**
- If `supplier_country == customer_country` → **domestic**; place of supply = that country.
- If `supplier_country != customer_country`:
  - **Goods, B2B (customer VAT-registered, different EU country)** → intra-community supply; place of supply = customer country.
  - **Services, B2B (general rule)** → place of supply = customer country (where the customer is established).
  - **B2C (goods or services), cross-border** → place of supply = **supplier** country (bounded simplification: treat as origin; do NOT model distance-selling thresholds or OSS).
- Output: `jurisdiction` (the country whose VAT regime applies) + the classification path taken (domestic / intra_community_b2b / b2c_cross_border).

**RAT — rate lookup.**
- Look up the rate for `(jurisdiction, rate_band)` from the table in §1.1 / §1.2.
- `GEN_*` → standard rate of the jurisdiction. `RED_*` → reduced rate. `EXEMPT_SUPPLY` → rate is not applicable (0 / `null`), but RAT still records "exempt — no rate applies" deterministically.
- The rate value must correspond to an entry in the bounded rate table (this is also a `V` check).

**EXM — exemption check.**
- `EXEMPT_SUPPLY` → exempt = true, cite the exemption-table rule.
- All other categories → exempt = false, no exemption citation required.
- The bounded exemption table `R` contains exactly the rule(s) for EXEMPT_SUPPLY. No conditional exemptions.

**RCH — reverse-charge / liable party synthesis.**
- **Domestic** → supplier charges VAT; liable party = supplier; reverse_charge = false.
- **Intra-community B2B (goods or general-rule services), cross-border, customer VAT-registered** → **reverse charge applies**; liable party = customer (self-accounts); reverse_charge = true; supplier charges no VAT (non-charging reason = "reverse charge, Art. 196-style").
- **B2C cross-border** → supplier charges VAT in the place of supply (supplier country per the bounded JUR rule); reverse_charge = false; liable party = supplier.
- **Exempt supply** → no VAT charged regardless of the above; reverse_charge = false; liable party = none (no party is liable where nothing is charged); non-charging reason = "exempt supply". (Exemption dominates: if the line is EXEMPT_SUPPLY, RCH records exempt, not reverse charge.)
- VAT amount: if VAT is charged, `amount = line_amount * rate`; otherwise `amount = 0` (or `null`) with an explicit non-charging reason ∈ {reverse_charge, exempt}.

> **Precedence rule (must be encoded explicitly):** EXEMPT dominates REVERSE-CHARGE dominates STANDARD-CHARGE.
> A single line item resolves to exactly one of: {standard_charge, reverse_charge, exempt}.

### 2.2 Line items and case-level aggregation

A case has one or more line items. CLS/RAT/EXM are **per line item**. JUR is per case (determined by
the party/transaction attributes; classification kind can matter for goods-vs-service place-of-supply,
so when a mixed case has both goods and services lines, JUR is computed per line where the kind affects
it — keep this deterministic and documented). RCH resolves per line item, then the case-level final
determination is the structured aggregation of all line items.

For **mixed goods/services invoices** (scenario family 4), different line items may land on different
categories, rates, exemptions, and even reverse-charge outcomes within the same case. The final trace
must carry per-line determinations.

---

## 3. Schemas (the typed I/O — `I`, `O`)

The contract is **one consolidated schema file** living at `src/schemas/final_trace.schema.json`.
Every emitted record is validated against it. It is strict (`"additionalProperties": false`) so
malformed traces fail loudly.

The per-record record types listed below are exposed as `$defs` inside the consolidated file and
are reachable as `#/$defs/<name>`. Keeping a single file (rather than seven) means there is one
place to edit and no cross-file `$ref` resolution to configure.

| `$defs` name              | Describes                                              |
|---------------------------|--------------------------------------------------------|
| (top level)               | A full emitted trace: case_id, jur, lines, final       |
| `$defs/cls`               | One CLS record (per line item)                         |
| `$defs/jur`               | One JUR record                                         |
| `$defs/rat`               | One RAT record (per line item)                         |
| `$defs/exm`               | One EXM record (per line item)                         |
| `$defs/rch`               | One RCH record (per line item)                         |
| `$defs/line`              | A line wrapper grouping CLS/RAT/EXM/RCH for one line item |
| `$defs/final`             | The case-level final determination                     |

The input-case shape (`I`) is enforced by the dataclass `Case` in `rules.py`
(`__post_init__` raises on the B2B/registered invariant). Validation of emitted *outputs* (the
agent-facing direction) is the validator's job and runs against the consolidated schema above.

Every subtask record carries at minimum: the decision fields, the supporting fields it consumed, and a
**`rule_reference`** string. A rule reference is a stable citation key into the bounded rule set
(e.g. `RC.B2B.INTRA_EU`, `RATE.STANDARD`, `EXM.EXEMPT_SUPPLY`, `JUR.DOMESTIC`). Define the closed set
of rule-reference keys in `rules.py` and reuse them everywhere; validation checks citation presence and
citation–decision consistency against this closed set.

---

## 4. Validation-check set `V`

`validator.py` enforces, in order:

1. **Schema conformance** — record validates against its JSON Schema.
2. **Required-field presence** — all mandatory decision + support fields present and non-null (except where `null` is the defined "not applicable" value, e.g. rate for exempt lines).
3. **Rule-citation presence** — every decision record has a `rule_reference` drawn from the closed key set; an exemption assertion *must* cite an exemption-table rule.
4. **Citation–decision consistency** — the cited rule matches the decision:
   - a RAT value must correspond to an entry in the bounded rate table for the stated jurisdiction/band;
   - an RCH reverse_charge=true must be consistent with JUR path = intra_community_b2b and customer_vat_registered=true;
   - an EXM exempt=true must carry the exemption citation and the line must be EXEMPT_SUPPLY;
   - an RCH exempt outcome must be consistent with EXM exempt=true.

A trace that satisfies all four checks is **validation-passing**. Note this is necessary, not
sufficient, for correctness: a schema-valid, citation-consistent trace can still be **oracle-wrong**
(the hallucinated-output injection deliberately produces exactly this). `scorer.py` — not
`validator.py` — decides oracle correctness.

---

## 5. Scoring (`scorer.py`)

Given an emitted trace + the oracle labels for that case:

- **final_answer_accuracy** (bool): emitted final determination matches the oracle label across all
  output fields enumerated in the final-trace schema (jurisdiction, rate treatment, exemption,
  reverse-charge status, liable party, VAT amount or non-charging reason), for **all** line items.
- **step_accuracy** (dict per subtask in T): emitted subtask record matches the oracle intermediate label.
- **trace_consistent** (bool): all `V` checks pass (delegates to `validator.py`).
- **earliest_error_subtask**: the first subtask in fixed order `[CLS, JUR, RAT, EXM, RCH]` whose label
  is wrong; `None` if the trace is fully correct. If several subtasks at the same dependency layer are
  wrong, record all for analysis but report the fixed-order first one for single-label summaries.
- Scoring must handle **terminal failures** (no trace / incomplete trace): final = incorrect,
  trace_consistent = false, missing step fields marked missing and counted incorrect for end-to-end accuracy.

Scoring is pure comparison. It must never mutate the trace and never call an LLM.

---

## 6. Determinism contract (non-negotiable)

- A single `seed` fully determines the generated case set (cases, line items, party attributes,
  assigned true categories) and all labels.
- Re-running the generator with the same seed produces byte-identical output (stable JSON key ordering,
  no `set` iteration leaking into output, no wall-clock, no `dict` hash-order dependence, no unseeded RNG).
- Use one explicit `random.Random(seed)` (or `numpy` Generator) threaded through generation. **No global
  `random` calls.** No `datetime.now()`, `uuid4()`, or environment-dependent values in case content.
- Case IDs are deterministic and stable: `eval_001..eval_040`, `dev_001..dev_008`, assigned in a fixed order.
- The oracle version is identified by git commit hash; the harness records it. Code must not depend on
  anything outside the repo to reproduce labels.

---

## 7. Dataset specification

- **40 evaluation cases**, stratified: 10 per scenario family.
- **8 development cases**, drawn from the same generator, **disjoint** from the eval set, used only for
  S0 prompt tuning. The generator must guarantee dev/eval disjointness by construction (e.g. separate
  index ranges from the same seeded stream), and expose them as separate splits.

Scenario families (10 eval cases each):

1. **Domestic supply** — supplier and customer same jurisdiction; standard rate or exemption.
2. **Intra-community B2B with reverse charge** — different jurisdictions, customer VAT-registered, reverse-charge path exercised.
3. **B2C with rate differential** — customer status drives place-of-supply / rate; reduced-rate lines appear here.
4. **Mixed goods/services invoices** — one case, multiple line items needing different CLS/RAT/EXM/RCH handling.

Generation must guarantee each family actually exercises its intended path (e.g. family 2 cases must
all produce at least one reverse_charge=true line; family 4 must produce ≥2 distinct categories within a case).

---

## 8. File layout for Part 1

```
src/
  schemas/
    final_trace.schema.json   # single consolidated schema; per-record types live as $defs
  oracle/
    __init__.py
    rules.py        # bounded rule set: tables, rule-reference keys, the 5 subtask resolvers, precedence
    generator.py    # seeded synthetic case generation, stratified, dev/eval split
    labeler.py      # run rules over a case -> full oracle trace (final + intermediate labels)
    validator.py    # the V checks over any emitted trace
    scorer.py       # compare emitted trace vs oracle labels -> accuracy/step/earliest-error
tests/
  test_determinism.py
  test_dataset.py
  test_rules.py
  test_validator.py
  test_scorer.py
docs/
  ORACLE_GROUNDING.md   # this file
```

`rules.py` holds pure functions with no I/O. `labeler.py` composes them. `generator.py` is the only
file that touches the RNG. `validator.py` and `scorer.py` are pure comparison/validation. Nothing in
`src/oracle/` imports an LLM client, makes a network call, or reads the clock.

---

## 9. Readiness gate for Part 1 (definition of done)

Part 1 is **done** when the pytest suite is green and all of the following hold (these map directly to
the infra doc's "oracle should pass unit tests" gate):

- [ ] same seed produces identical cases (determinism)
- [ ] 40 eval + 8 dev cases generated; eval/dev disjoint; 10 per family
- [ ] every generated case has a complete oracle label (final + all 5 intermediate)
- [ ] every final label is recomputable deterministically from the case via the rules
- [ ] each scenario family demonstrably exercises its intended path
- [ ] malformed traces fail validation
- [ ] schema-valid-but-semantically-wrong traces are scored **wrong** by the scorer (but may pass the validator)
- [ ] precedence (exempt > reverse-charge > standard) holds on targeted cases
- [ ] scorer correctly attributes earliest-error subtask on injected single-step errors
- [ ] the dataset is **frozen to disk** via `scripts/freeze_dataset.py` and the manifest verifies (see §11)

Only after this gate passes do we move to schemas-as-contract for agents, then C1/S0, then the harness.

---

## 10. Guardrails for the coding assistant

- Do **not** expand the rule set, vocabulary, jurisdiction list, or scenario families beyond §1–§2 without changing this document first.
- Do **not** introduce any LLM, API, or network dependency into `src/oracle/`.
- Do **not** use global/unseeded randomness or wall-clock anywhere in generation or labeling.
- Do **not** make the oracle "smart" (no NLP, no fuzzy matching). It is ground-truth-by-construction.
- Keep `additionalProperties: false` on schemas; prefer failing loudly over silent coercion.
- When a rule is ambiguous, choose the **bounded, simplest** interpretation and add a one-line code
  comment pointing back to the relevant section here — then flag it for human review rather than guessing elaborately.

---

## 11. Freezing the dataset

Once Part 1 is otherwise green, the dataset is frozen to disk so downstream
consumers (the harness, analysis pipelines) read a pinned artifact rather than
regenerating on demand. The freeze is what makes "the oracle commit hash
identifies the labels" (§6) actually true.

**Freeze procedure (in order):**

1. Commit all oracle code (`rules.py`, `generator.py`, `labeler.py`, `validator.py`, `scorer.py`,
   `src/schemas/final_trace.schema.json`, this doc). The git tree must be clean —
   the freeze refuses otherwise.
2. Run `python -m scripts.freeze_dataset --seed 42` from the repo root.
3. The script writes:
    - `data/MANIFEST.json` — pinned seed, oracle commit hash, dataset SHA-256 (input cases),
      `case_files_sha256` (input + oracle trace), per-family counts, UTC freeze time.
    - `data/eval_cases/eval_001.json` … `eval_040.json` — one canonical JSON per eval case,
      each containing the input case + its full oracle trace.
    - `data/dev_cases/dev_001.json` … `dev_008.json` — same for the dev split.
4. Commit the `data/` tree.

**Verification.** `python -m scripts.freeze_dataset --verify` regenerates from the manifest's
seed, compares hashes and every file byte-by-byte, and exits non-zero on any drift. The
harness, CI, and any analysis pipeline should run `--verify` before doing official work —
this is what catches "did someone tweak `rules.py` and break label reproducibility?".

**Re-freezing.** Any change to `rules.py`, `generator.py`, or `labeler.py` that affects the
oracle output requires a new freeze: bump the commit, re-run `freeze_dataset`, re-commit the
`data/` tree. Treat this as a deliberate event — the old commit hash now identifies a
different dataset than the new one, and any in-flight experiments must restart.

**Never use `--allow-dirty` for an official freeze.** It exists for local experimentation
only. An official-freeze commit has `oracle_commit_dirty: false` in the manifest.