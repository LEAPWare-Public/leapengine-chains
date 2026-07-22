#!/usr/bin/env python3
"""
derive_facts.py — THE SINGLE SOURCE OF TRUTH for every computed fact in LEAPEngine.

WHY THIS EXISTS (the Jul-20-2026 failures it prevents):
  F1  Numbers stated from memory that were wrong (PFFA "underearning", BTCI "broken").
  F2  A metric computed correctly but WRONG (A7-R vs a zero baseline). Tests were green.
  F3  A verdict corrected in HISTORY.md but NOT in the authoritative files a session obeys.
  F4  The same fact ("account value", "verdict", "yield") written by hand in 3-5 files that drift.

THE GOVERNANCE RULE (from data-contract practice):
  A derived fact is COMPUTED IN EXACTLY ONE PLACE, here, and every document that needs it
  READS it from FACTS.json. No fact is ever typed by hand into a ledger. Hand-typed facts are
  the drift. This file is the only producer; ledgers are consumers.

OUTPUT: data/FACTS.json — the golden record. Every value carries {value, as_of, source, method,
        inputs_hash}. A consumer that finds a fact older than its freshness SLA must refuse to act.
"""
import json, hashlib, datetime, os, sys

FACTS = "data/FACTS.json"
YIELDS = "data/YIELDS.json"


def _hash(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[:12]


# ---- A7-R v3: the ONE definition of an earnings verdict, benchmark-relative ----
# Maps each fund/stock to its correct benchmark and test class. This table IS the rule.
BENCHMARK = {
    # option-income funds -> underlying (overlay must be judged vs what it holds)
    "BTCI": ("BITO", "overlay"), "AIPI": ("SMH", "overlay"), "SPYI": ("SPY", "overlay"),
    "QQQI": ("QQQ", "overlay"), "GPIX": ("SPY", "overlay"), "GPIQ": ("QQQ", "overlay"),
    "IYRI": ("XLRE", "overlay"), "IWMI": ("IWM", "overlay"), "DIVO": ("SPY", "overlay"),
    "IDVO": ("SPY", "overlay"), "QDVO": ("QQQ", "overlay"), "GIAX": ("GLD", "overlay"),
    "IGLD": ("GLD", "overlay"), "TSPY": ("SPY", "overlay"), "TDAQ": ("QQQ", "overlay"),
    # rate-sensitive -> coverage test (SEC yield), NOT total return
    "PFFA": (None, "coverage"), "PFFR": (None, "coverage"), "PCN": (None, "coverage"),
    "PTY": ("AGG", "coverage_tr"), "PDI": ("AGG", "coverage_tr"),
    # single stocks -> subsector peers (NOT a broad index)
    "VICI": ("GLPI", "peer"), "ARE": ("DOC", "peer"), "AMT": ("VNQ", "peer"),
    "ARCC": ("BIZD", "peer"), "HTGC": ("BIZD", "peer"), "MAIN": ("BIZD", "peer"),
}
# manually-verified coverage facts (SEC yield vs distribution) — updated only with a cited source
COVERAGE = {
    "PFFA": {"sec_yield": 9.63, "distribution": 9.92, "source": "Virtus factsheet Jul-2026", "verdict": "EARNED"},
    "PFFR": {"sec_yield": 8.33, "distribution": 8.23, "source": "run-rate proxy", "verdict": "EARNED"},
    "PCN":  {"sec_yield": None, "distribution": 11.55, "source": "PIMCO UNII <100%, ATM-supplemented", "verdict": "CAVEAT"},
}


def a7r_verdict(ticker, Y):
    """The ONLY place an A7-R verdict is produced. No verdict without a benchmark."""
    if ticker not in BENCHMARK:
        return {"verdict": "NOT_APPLICABLE", "reason": "not a distribution fund/stock under A7-R"}
    bench, cls = BENCHMARK[ticker]
    fund = Y.get(ticker, {})
    ftr = fund.get("total_return_12m_pct")
    tty = fund.get("ttm_yield_pct")

    if cls == "coverage":
        cov = COVERAGE.get(ticker, {})
        return {"verdict": cov.get("verdict", "UNRESOLVED"), "class": "coverage",
                "sec_yield": cov.get("sec_yield"), "distribution": cov.get("distribution"),
                "benchmark": None, "source": cov.get("source"),
                "note": "rate-sensitive: judged on SEC-yield coverage, NOT 12m total return"}

    if bench is None or bench not in Y or Y[bench].get("total_return_12m_pct") is None:
        return {"verdict": "UNRESOLVED", "class": cls, "benchmark": bench,
                "reason": f"benchmark {bench} unavailable — NO verdict without a benchmark (M12)"}

    btr = Y[bench]["total_return_12m_pct"]
    if ftr is None:
        return {"verdict": "UNRESOLVED", "reason": "fund total return missing"}
    diff = round(ftr - btr, 2)
    # overlay: destroyed value only if it materially trails its own underlying
    # peer: company-specific problem only if it materially trails its subsector
    if diff >= -3:
        v = "EARNED" if cls == "overlay" else "TRACKS_PEERS"
    elif diff >= -10:
        v = "LAGS"
    else:
        v = "ERODING" if cls == "peer" else "OVERLAY_DRAG"
    return {"verdict": v, "class": cls, "benchmark": bench,
            "fund_tr": ftr, "bench_tr": btr, "diff_pts": diff,
            "note": "ERODING requires trailing the benchmark by >10 pts (A7-R v3)"}


def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if not os.path.exists(YIELDS):
        print(f"FATAL: {YIELDS} missing — run fetch_distributions first", file=sys.stderr)
        return 1
    Y = json.load(open(YIELDS))["tickers"]

    facts = {"generated_at": now, "producer": "derive_facts.py", "facts": {}}

    # ---- A7-R verdicts: computed once, here ----
    verdicts = {}
    for t in BENCHMARK:
        v = a7r_verdict(t, Y)
        verdicts[t] = v
    facts["facts"]["a7r_verdicts"] = {
        "value": verdicts, "as_of": now, "source": "derive_facts.a7r_verdict",
        "method": "A7-R v3 benchmark-relative", "inputs_hash": _hash(verdicts)}

    # ---- run-rate vs headline (special-dividend guard, computed once) ----
    special = {}
    for t, v in Y.items():
        tty, rr = v.get("ttm_yield_pct"), v.get("run_rate_yield_pct")
        if tty and rr and rr > 0 and tty > rr * 1.35:
            special[t] = {"headline": tty, "run_rate": rr,
                          "note": "headline is a SPECIAL DIVIDEND; use run_rate for sizing"}
    facts["facts"]["special_dividends"] = {
        "value": special, "as_of": now, "source": "derive_facts",
        "method": "headline > 1.35x run rate", "inputs_hash": _hash(special)}

    json.dump(facts, open(FACTS, "w"), indent=2)

    # ---- human-readable verdict summary for ledgers to QUOTE (never restate by hand) ----
    lines = [f"# A7-R VERDICTS — generated {now} by derive_facts.py. DO NOT EDIT BY HAND.",
             "# Ledgers and CLAUDE.md must QUOTE these, never restate them. This is the golden record.",
             ""]
    for t in sorted(verdicts):
        v = verdicts[t]
        lines.append(f"{t:<6} {v.get('verdict'):<14} {v.get('note','')}")
    open("data/VERDICTS.txt", "w").write("\n".join(lines))

    n_er = sum(1 for v in verdicts.values() if v.get("verdict") in ("ERODING", "OVERLAY_DRAG"))
    print(f"Wrote {FACTS} and data/VERDICTS.txt: {len(verdicts)} verdicts, {n_er} eroding, "
          f"{len(special)} special-dividend flags")
    return 0


if __name__ == "__main__":
    sys.exit(main())
