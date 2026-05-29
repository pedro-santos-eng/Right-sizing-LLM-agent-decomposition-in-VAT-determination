# Development Log

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
