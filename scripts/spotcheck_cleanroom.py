"""Clean-room re-derivation of the VAT oracle labels (paper §3.3 spot-check).

Written ONLY from docs/ORACLE_GROUNDING.md §1 (bounded rule set), §2 (subtask
logic and the precedence rule) and §7 (dataset spec). It imports nothing from
src/oracle/ -- no rules.py, no labeler.py, no validator.py, no shared
constants. The rate table, category vocabulary, jurisdiction routing and
precedence ordering below are transcribed from the prose of the grounding
document. The only repo artefacts read are the frozen case JSON files.

Purpose: verify that the engine implements the document. It does NOT verify
that the document models real VAT law (out of scope, see paper §10.1), and it
invokes no model.

Usage:  python scripts/spotcheck_cleanroom.py
Exit:   0 if every field agrees, 1 otherwise.

See docs/SPOTCHECK_3.3.md for the recorded findings.
"""

from __future__ import annotations

import glob
import json
import sys
from collections import Counter, defaultdict

# --------------------------------------------------------------------------
# The exempt-branch liable party.
#
# The 2026-08-01 spot-check (docs/SPOTCHECK_3.3.md §4.1) found that
# ORACLE_GROUNDING §2.1 originally stated `liable party` for the domestic,
# intra-community B2B and B2C branches but was SILENT for the exempt branch;
# a document-only reading derived "supplier", while the engine emits "none",
# producing 22 mismatches across the 11 exempt lines. §2.1 was amended the
# same day to state `liable party = none` for exempt lines. This constant
# matches the amended document, so the script runs green against the frozen
# dataset. Set it to "supplier" to reproduce the pre-amendment divergence.
# --------------------------------------------------------------------------
EXEMPT_LIABLE_PARTY = "none"

# --- §1.1 rate table -------------------------------------------------------
RATES = {
    "DE": {"standard": 0.19, "reduced": 0.07},
    "FR": {"standard": 0.20, "reduced": 0.055},
    "IE": {"standard": 0.23, "reduced": 0.135},
    "ES": {"standard": 0.21, "reduced": 0.10},
}

# --- §1.2 classification vocabulary ---------------------------------------
BAND = {
    "GEN_GOODS": "standard",
    "GEN_SERVICE": "standard",
    "RED_GOODS": "reduced",
    "RED_SERVICE": "reduced",
    "EXEMPT_SUPPLY": None,
}


def derive_jur(inp: dict) -> dict:
    """§2.1 JUR. Kind (goods vs service) does not change the outcome under the
    bounded rules: cross-border B2B routes goods to the customer country by the
    intra-community rule and services to the customer country by the general
    place-of-supply rule. Domestic and B2C cross-border ignore kind."""
    sup, cus = inp["supplier_country"], inp["customer_country"]
    if sup == cus:
        return {"jur_path": "domestic", "jurisdiction": sup}
    if inp["transaction_type"] == "B2B":  # §1.3: B2B => customer_vat_registered
        return {"jur_path": "intra_community_b2b", "jurisdiction": cus}
    return {"jur_path": "b2c_cross_border", "jurisdiction": sup}


def derive_rat(jurisdiction: str, category: str) -> dict:
    """§2.1 RAT."""
    band = BAND[category]
    if band is None:
        return {"rate": None, "rate_band": None}
    return {"rate": RATES[jurisdiction][band], "rate_band": band}


def derive_exm(category: str) -> dict:
    """§2.1 EXM."""
    return {"exempt": category == "EXEMPT_SUPPLY"}


def derive_rch(jur_path: str, exempt: bool, rate, amount: float) -> dict:
    """§2.1 RCH, applying EXEMPT > REVERSE_CHARGE > STANDARD_CHARGE."""
    if exempt:
        return {"outcome": "exempt", "reverse_charge": False,
                "liable_party": EXEMPT_LIABLE_PARTY,
                "non_charging_reason": "exempt", "vat_amount": 0.0}
    if jur_path == "intra_community_b2b":
        return {"outcome": "reverse_charge", "reverse_charge": True,
                "liable_party": "customer",
                "non_charging_reason": "reverse_charge", "vat_amount": 0.0}
    return {"outcome": "standard_charge", "reverse_charge": False,
            "liable_party": "supplier", "non_charging_reason": None,
            "vat_amount": round(amount * rate, 2)}


def derive_case(inp: dict):
    jurs, lines = set(), []
    for li in inp["line_items"]:
        cat = li["true_category"]
        jur = derive_jur(inp)
        jurs.add((jur["jur_path"], jur["jurisdiction"]))
        rat = derive_rat(jur["jurisdiction"], cat)
        exm = derive_exm(cat)
        rch = derive_rch(jur["jur_path"], exm["exempt"], rat["rate"], li["amount"])
        lines.append({"line_id": li["line_id"], "category": cat,
                      "rat": rat, "exm": exm, "rch": rch})
    return jurs, lines


def main() -> int:
    files = (sorted(glob.glob("data/eval_cases/*.json"))
             + sorted(glob.glob("data/dev_cases/*.json")))
    if not files:
        print("no case files found -- run from the repository root", file=sys.stderr)
        return 2

    mismatches = []
    branch_keys = defaultdict(set)
    counts = defaultdict(lambda: Counter())
    exempt_in_ic_b2b = []

    for path in files:
        case = json.load(open(path, encoding="utf-8"))
        cid = case["case_id"]
        split = "eval" if cid.startswith("eval") else "dev"
        inp, oracle = case["input"], case["oracle_trace"]
        jurs, mine = derive_case(inp)
        counts[split]["cases"] += 1

        if len({j for _, j in jurs}) != 1:
            mismatches.append((cid, "-", "case jurisdiction not unique", jurs, None))
        my_path, my_jur = next(iter(jurs))
        counts[split][my_path] += 1

        eng = oracle["jur"]["decision"]
        want = {"jur_path": my_path, "jurisdiction": my_jur}
        if eng != want:
            mismatches.append((cid, "-", "JUR", want, eng))
        branch_keys["JUR:" + my_path].add(oracle["jur"]["rule_reference"])

        eng_lines = {l["line_id"]: l for l in oracle["lines"]}
        fin_lines = {l["line_id"]: l for l in oracle["final"]["lines"]}

        for m in mine:
            counts[split]["lines"] += 1
            lid = m["line_id"]
            el = eng_lines[lid]

            for sub, want_dec in (("cls", {"category": m["category"]}),
                                  ("rat", m["rat"]),
                                  ("exm", m["exm"]),
                                  ("rch", m["rch"])):
                if el[sub]["decision"] != want_dec:
                    mismatches.append((cid, lid, sub.upper(), want_dec,
                                       el[sub]["decision"]))

            branch_keys["CLS"].add(el["cls"]["rule_reference"])
            branch_keys[f"RAT:{m['rat']['rate_band']}"].add(el["rat"]["rule_reference"])
            branch_keys[f"EXM:{m['exm']['exempt']}"].add(el["exm"]["rule_reference"])
            branch_keys[f"RCH:{m['rch']['outcome']}:{my_path}"].add(
                el["rch"]["rule_reference"])

            want_final = {"line_id": lid, "category": m["category"],
                          "exempt": m["exm"]["exempt"],
                          "rate": m["rat"]["rate"],
                          "rate_band": m["rat"]["rate_band"],
                          **m["rch"]}
            if fin_lines[lid] != want_final:
                mismatches.append((cid, lid, "FINAL.line", want_final,
                                   fin_lines[lid]))

            counts[split]["cat:" + m["category"]] += 1
            counts[split]["out:" + m["rch"]["outcome"]] += 1
            if m["exm"]["exempt"] and my_path == "intra_community_b2b":
                exempt_in_ic_b2b.append((cid, lid))

        total = round(sum(m["rch"]["vat_amount"] for m in mine), 2)
        if abs(oracle["final"]["total_vat_amount"] - total) > 1e-9:
            mismatches.append((cid, "-", "FINAL.total", total,
                               oracle["final"]["total_vat_amount"]))
        if oracle["final"]["jurisdiction"] != my_jur:
            mismatches.append((cid, "-", "FINAL.jurisdiction", my_jur,
                               oracle["final"]["jurisdiction"]))

    for split in ("eval", "dev"):
        c = counts[split]
        print(f"[{split}] cases={c['cases']} lines={c['lines']}")
        print("   jur_path:", {k: v for k, v in c.items()
                               if k in ("domestic", "intra_community_b2b",
                                        "b2c_cross_border")})
        print("   category:", {k[4:]: v for k, v in sorted(c.items())
                               if k.startswith("cat:")})
        print("   outcome :", {k[4:]: v for k, v in sorted(c.items())
                               if k.startswith("out:")})

    print(f"\nmismatches: {len(mismatches)}")
    by_field = Counter(m[2] for m in mismatches)
    for field, n in by_field.most_common():
        print(f"   {field}: {n}")
    for m in mismatches[:5]:
        print("   e.g.", m)

    print("\nrule-reference key per semantic branch:")
    multi = 0
    for branch in sorted(branch_keys):
        keys = sorted(branch_keys[branch])
        flag = "  <-- NOT SINGLE-VALUED" if len(keys) != 1 else ""
        multi += len(keys) != 1
        print(f"   {branch:<42} {keys}{flag}")

    print("\nprecedence edge EXEMPT over REVERSE_CHARGE:")
    print(f"   EXEMPT_SUPPLY lines inside intra_community_b2b cases: "
          f"{len(exempt_in_ic_b2b)}")

    return 1 if (mismatches or multi) else 0


if __name__ == "__main__":
    sys.exit(main())
