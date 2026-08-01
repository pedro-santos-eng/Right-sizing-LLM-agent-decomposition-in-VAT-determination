# Development Log

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
