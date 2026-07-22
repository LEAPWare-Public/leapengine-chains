#!/usr/bin/env python3
"""
audit_consistency.py — the control that would have caught Jul-20's F3 and F4.

Runs at the END of every session (mandatory in /session). It does two things:

  1. FRESHNESS GATE: refuses to certify if FACTS.json / YIELDS.json are older than their SLA.
     A consumer acting on stale facts is the original sin. Stale = STOP, not warn.

  2. CROSS-FILE CONSISTENCY: greps the AUTHORITATIVE files (CLAUDE.md, PLAN.md, STRATEGY.md,
     LEDGER*.md, SAFEGUARDS.md) for any A7-R verdict claim and checks it against FACTS.json.
     A verdict that appears in an authoritative file but disagrees with the golden record is a
     BLOCKING error. This is exactly the drift that let CLAUDE.md keep calling PFFA "underearning"
     after it was withdrawn.

  History files (HISTORY.md, MISTAKES.md) are EXEMPT — they are append-only and intentionally
  retain old claims alongside corrections.

Exit non-zero on any blocking finding. Wire into CI and into /session so a session cannot be
"closed" while the authoritative files disagree with the golden record.
"""
import json, os, re, sys, datetime

AUTHORITATIVE = ["CLAUDE.md", "PLAN.md", "STRATEGY.md", "SAFEGUARDS.md",
                 "LEDGER.md", "LEDGER-SEP.md", "LEDGER-TRAD2.md", "LEDGER-ROTH1.md",
                 "LEDGER-TRAD3.md", "LEDGER-ROTH2.md", "PORTFOLIO.md", "WHEEL-ROSTER.md",
                 "TRAD1-REVIEW.md"]
HISTORY_EXEMPT = ["HISTORY.md", "MISTAKES.md"]
FRESHNESS_HOURS = {"FACTS.json": 24, "YIELDS.json": 168}
# both directions: a wrong "EARNED" is MORE dangerous than a wrong "ERODING" — it deploys capital
VERDICT_WORDS = ["ERODING", "UNDEREARNING", "BROKEN"]
POSITIVE_WORDS = ["EARNED", "DEPLOYABLE", "SAFE TO ADD", "EARNS ITS"]


def freshness_gate():
    findings = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for f, sla in FRESHNESS_HOURS.items():
        p = f"data/{f}"
        if not os.path.exists(p):
            findings.append(f"BLOCK: {p} missing — cannot certify without the golden record")
            continue
        try:
            gen = json.load(open(p)).get("generated_at", "")
            age = (now - datetime.datetime.fromisoformat(gen.replace("Z", "+00:00"))).total_seconds() / 3600
            if age > sla:
                findings.append(f"BLOCK: {p} is {age:.0f}h old (SLA {sla}h) — STALE. Re-run before acting.")
        except Exception as e:
            findings.append(f"BLOCK: {p} unreadable ({e})")
    return findings


def consistency_gate():
    findings = []
    if not os.path.exists("data/FACTS.json"):
        return ["BLOCK: data/FACTS.json missing — run derive_facts.py"]
    verdicts = json.load(open("data/FACTS.json"))["facts"]["a7r_verdicts"]["value"]
    # golden verdict per ticker, normalised
    golden = {t: v.get("verdict") for t, v in verdicts.items()}

    for fn in AUTHORITATIVE:
        if not os.path.exists(fn):
            continue
        for i, line in enumerate(open(fn), 1):
            low = line.lower()
            # EXEMPT: correction notes, rule-definition text, and refutations that name an old verdict
            # in order to correct it. These legitimately contain a verdict word.
            EXEMPT_MARKERS = ["!!", "corrected", "withdrawn", "stale", "superseded",
                              "not eroding", "not broken", "not underearning",
                              "no verdict", "manufactures false", "may be issued",
                              "may not", "was ", "prior line", "old ", "no longer",
                              "requires trailing", "refut", "(see history", "definition",
                              "e.g.", "e.g,", "example", "constructive roc", "two kinds",
                              "the test:", "engineering", "smoothing"]
            if line.strip().startswith("#") or any(m in low for m in EXEMPT_MARKERS):
                continue
            for tkr, gv in golden.items():
                if re.search(rf"\b{tkr}\b", line):
                    # skip if the ticker appears only as a benchmark reference ("vs DOC", "clears vs DOC")
                    if re.search(rf"vs\s+{tkr}\b", line) and not re.search(rf"\b{tkr}\b(?!\s*(?:clears|vs))", line.replace(f"vs {tkr}","")):
                        continue
                    for w in VERDICT_WORDS:
                        if w in line:
                            ok = (w == "ERODING" and gv in ("ERODING", "OVERLAY_DRAG")) or \
                                 (w == "UNDEREARNING" and gv in ("LAGS", "UNRESOLVED")) or \
                                 (gv and w in gv)
                            if not ok:
                                findings.append(
                                    f"BLOCK {fn}:{i} — asserts '{w}' for {tkr}, but golden verdict is "
                                    f"'{gv}'. Authoritative file disagrees with FACTS.json. "
                                    f"(If this is a correction note, add a marker like '!!' or 'was'.)")
                    # positive-direction: claiming a name is EARNED/safe when golden says it is not
                    # (same exemptions apply — already continue'd above if the line is a note/definition)
                    for w in POSITIVE_WORDS:
                        if w in line.upper():
                            # UNRESOLVED/CAVEAT/NOT_APPLICABLE are provisional, not contradictions —
                            # they mean "benchmark pending", not "this name is bad". Don't block on them.
                            definite_bad = gv in ("ERODING","OVERLAY_DRAG","LAGS","UNDEREARNING")
                            if definite_bad:
                                findings.append(
                                    f"BLOCK {fn}:{i} — asserts '{w}' for {tkr}, but golden verdict is "
                                    f"'{gv}' (NOT safe). A false-positive verdict deploys capital into a "
                                    f"bad name — the more dangerous direction.")
    return findings


def main():
    findings = freshness_gate() + consistency_gate()
    if findings:
        print("CONSISTENCY AUDIT FAILED — session may NOT be closed until resolved:\n")
        for f in findings:
            print("  " + f)
        print(f"\n{len(findings)} blocking finding(s).")
        return 1
    print("Consistency audit PASSED: golden record fresh, authoritative files agree.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
