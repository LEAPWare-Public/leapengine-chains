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
    # --- household high-yielders surfaced by the L1 self-audit (Jul-20-2026) ---
    "VZ":   ("T", "peer"),        # telecom single stock vs peer T
    "AMLP": ("MLPX", "peer"),     # midstream fund vs sister midstream fund
    "ET":   ("MLPX", "peer"),     # (barred from IRAs anyway, but audit it)
    "MPLX": ("MLPX", "peer"),
    "PAA":  ("MLPX", "peer"),
    "PFE":  ("XLV", "peer"),      # pharma single stock vs health-care sector
    "OMAH": ("SPY", "overlay"),   # buffer/option-income style
    "IAUI": ("GLD", "overlay"),   # gold option-income
    "PBDC": ("BIZD", "overlay"),  # BDC fund-of-funds vs BDC index
    "BXSL": ("BIZD", "peer"),
    "AHRT": ("VNQ", "peer"),      # healthcare REIT vs REIT index (thin, but audited)
    # OVL/OVS/OVF EXITED Jul-20 (sold from TRAD2); mapped so any residual/rebuy is audited
    "OVL":  ("SPY", "overlay"), "OVS": ("SPY", "overlay"), "OVF": ("SPY", "overlay"),
}
# manually-verified coverage facts (SEC yield vs distribution) — updated only with a cited source
# L2: SEC-yield/coverage cannot be fetched from the free feed, so these are HAND-ENTERED from cited
# sources. Each carries as_of + source; the deriver flags any entry older than COVERAGE_STALE_DAYS so a
# hand fact can never silently rot. NOT a live feed — treated as a dated manual input, and labelled so.
COVERAGE_STALE_DAYS = 45
COVERAGE = {
    "PFFA": {"sec_yield": 9.63, "distribution": 9.92, "verdict": "EARNED",
             "as_of": "2026-07-20", "source": "Virtus factsheet Jul-2026 (HAND-ENTERED)"},
    "PFFR": {"sec_yield": 8.33, "distribution": 8.23, "verdict": "EARNED",
             "as_of": "2026-07-20", "source": "run-rate proxy (HAND-ENTERED)"},
    "PCN":  {"sec_yield": None, "distribution": 11.55, "verdict": "CAVEAT",
             "as_of": "2026-07-20", "source": "PIMCO UNII <100%, ATM-supplemented (HAND-ENTERED)"},
}


def coverage_staleness(today):
    import datetime as _dt
    stale = []
    for t, c in COVERAGE.items():
        try:
            age = (today - _dt.date.fromisoformat(c["as_of"])).days
            if age > COVERAGE_STALE_DAYS:
                stale.append(f"{t}: coverage fact is {age}d old (SLA {COVERAGE_STALE_DAYS}d) — re-verify from source")
        except Exception:
            stale.append(f"{t}: coverage fact has no valid as_of date")
    return stale


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
    # RELATIVE test (did the strategy/company add or destroy value vs its baseline)
    if diff >= -3:
        rel = "EARNED" if cls == "overlay" else "TRACKS_PEERS"
    elif diff >= -10:
        rel = "LAGS"
    else:
        rel = "ERODING" if cls == "peer" else "OVERLAY_DRAG"
    # ABSOLUTE test (B-2, CLA Pass B): beating a collapsing benchmark is NOT "earned" for a
    # retirement account. A materially negative absolute total return is flagged regardless of
    # how the benchmark did. The relative test alone was the mirror of the Jul-20 zero-baseline bug.
    abs_flag = None
    if ftr <= -20:
        abs_flag = "ABSOLUTE_LOSS_SEVERE"     # >20% down in 12m
    elif ftr < 0:
        abs_flag = "ABSOLUTE_LOSS"
    # a name that beat its benchmark but is still deeply negative is not deployable, whatever rel says
    deployable = (rel in ("EARNED", "TRACKS_PEERS")) and ftr > 0
    return {"verdict": rel, "class": cls, "benchmark": bench,
            "fund_tr": ftr, "bench_tr": btr, "diff_pts": diff,
            "absolute_flag": abs_flag, "deployable": deployable,
            "note": "verdict is RELATIVE (vs benchmark); absolute_flag/deployable capture the "
                    "absolute loss so beating a collapsing benchmark is never mistaken for safe."}


HELD = ['ADC', 'AHRT', 'AIPI', 'AMLP', 'ARCC', 'ASGI', 'AVGO', 'BIP', 'BTCI', 'CAIQ', 'CEF', 'CEFS', 'CGDV', 'CTRE', 'DIVO', 'DOC', 'EGP', 'EMO', 'EPD', 'FRT', 'GIAX', 'GPIQ', 'GPIX', 'GRNY', 'HTGC', 'IAUI', 'IDVO', 'IGF', 'IGLD', 'IWMI', 'IYRI', 'JBBB', 'KIM', 'KRG', 'MAIN', 'MLPX', 'NNN', 'O', 'OVF', 'OVL', 'OVS', 'PAAA', 'PBDC', 'PCN', 'PDI', 'PFFA', 'PFFR', 'PTY', 'QDVO', 'QQQI', 'REXR', 'RQI', 'RYN', 'SCHD', 'SCHG', 'SCHY', 'SILJ', 'SPYI', 'STAG', 'TDAQ', 'TSPY', 'UDR', 'UTF', 'UTG', 'VICI', 'WPC']

# held income names not individually mapped above -> class default (keeps the audit COMPLETE, not noisy).
# option-income ETFs -> their index; preferred/credit -> coverage; CEFs/BDCs -> sector; equities -> peer.
HELD_DEFAULT = {
    # option-income / covered-call ETFs
    "IWMI":("IWM","overlay"),"IDVO":("SPY","overlay"),"DIVO":("SPY","overlay"),"QDVO":("QQQ","overlay"),
    "GPIQ":("QQQ","overlay"),"GPIX":("SPY","overlay"),"TSPY":("SPY","overlay"),"TDAQ":("QQQ","overlay"),
    "CAIQ":("QQQ","overlay"),
    # CEFs (equity) -> sector proxy
    "UTF":("XLU","overlay"),"UTG":("XLU","overlay"),"ASGI":("VNQ","overlay"),"CEFS":("SPY","overlay"),
    "RQI":("XLU","overlay"),  # infra/utility CEF, not a REIT — vs utilities sector"CEF":("GLD","overlay"),"EMO":("AMLP","peer"),"PBDC":("BIZD","overlay"),
    # preferred / credit -> coverage class
    "PCN":(None,"coverage"),"PTY":("AGG","coverage_tr"),"PDI":("AGG","coverage_tr"),
    "JBBB":("AGG","coverage_tr"),"PAAA":("AGG","coverage_tr"),
    # BDCs -> BDC index
    "MAIN":("BIZD","peer"),
    # REIT singles -> REIT index
    "DOC":("VNQ","peer"),"KIM":("VNQ","peer"),"NNN":("VNQ","peer"),"O":("VNQ","peer"),"WPC":("VNQ","peer"),
    "FRT":("VNQ","peer"),"KRG":("VNQ","peer"),"ADC":("VNQ","peer"),"STAG":("VNQ","peer"),"UDR":("VNQ","peer"),
    "EGP":("VNQ","peer"),"CTRE":("VNQ","peer"),"REXR":("VNQ","peer"),"RYN":("VNQ","peer"),
    # equity / growth -> broad
    "AVGO":("SMH","peer"),"CGDV":("SPY","peer"),"SCHG":("SPY","peer"),"SCHY":("SPY","peer"),
    "GRNY":("SPY","peer"),"IGF":("SPY","peer"),"SILJ":("GDX","peer"),
    # gold income
    "IAUI":("GLD","overlay"),"GIAX":("GLD","overlay"),"IGLD":("GLD","overlay"),
    # infra/midstream funds
    "AMLP":("MLPX","peer"),"MLPX":("MLPX","peer"),"EPD":("MLPX","peer"),
}
# fold defaults into the main map at import
for _t,_bc in HELD_DEFAULT.items():
    BENCHMARK.setdefault(_t,_bc)


def validate_benchmark_map(Y):
    """L1 guard: a wrong/missing benchmark yields a confident-but-wrong verdict. Surface both."""
    issues = []
    for tkr, (bench, cls) in BENCHMARK.items():
        if cls in ("overlay", "peer", "coverage_tr"):
            if bench is None:
                issues.append(f"{tkr}: class {cls} requires a benchmark but none is set")
            elif bench not in Y:
                issues.append(f"{tkr}: benchmark {bench} not in YIELDS.json (verdict will be UNRESOLVED)")
    # HELD income names with a real yield but NO A7-R mapping = a real blind spot (scoped to holdings)
    for tkr in HELD:
        v = Y.get(tkr, {})
        y = v.get("ttm_yield_pct")
        if y and y >= 5.0 and tkr not in BENCHMARK:
            issues.append(f"{tkr}: HELD, {y:.1f}% yield, NO A7-R benchmark — unaudited blind spot")
    return issues


def main():
    now = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    if not os.path.exists(YIELDS):
        print(f"FATAL: {YIELDS} missing — run fetch_distributions first", file=sys.stderr)
        return 1
    Y = json.load(open(YIELDS))["tickers"]
    import datetime as _dt
    today = _dt.date.today()

    facts = {"generated_at": now, "producer": "derive_facts.py", "facts": {}}

    # L1 + L2 self-audit: map correctness and hand-fact staleness are surfaced, never silent
    map_issues = validate_benchmark_map(Y)
    cov_issues = coverage_staleness(today)
    facts["self_audit"] = {"benchmark_map_issues": map_issues,
                           "coverage_staleness": cov_issues,
                           "clean": not (map_issues or cov_issues)}

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
    sa = facts.get("self_audit", {})
    lines = [f"# A7-R VERDICTS — generated {now} by derive_facts.py. DO NOT EDIT BY HAND.",
             "# Ledgers and CLAUDE.md must QUOTE these, never restate them. This is the golden record.",
             f"# SELF-AUDIT: {'CLEAN' if sa.get('clean') else str(len(sa.get('benchmark_map_issues',[])))+' map notes (see FACTS.json.self_audit)'}",
             "# CAVEAT (L3): verdicts are machine-computed but the BENCHMARK MAP and COVERAGE facts are",
             "# human-reviewed inputs. A verdict is only as right as its benchmark. UNRESOLVED = STOP, not guess.",
             ""]
    for t in sorted(verdicts):
        v = verdicts[t]
        lines.append(f"{t:<6} {v.get('verdict'):<14} {v.get('note','')}")
    open("data/VERDICTS.txt", "w").write("\n".join(lines))

    n_er = sum(1 for v in verdicts.values() if v.get("verdict") in ("ERODING", "OVERLAY_DRAG"))
    print(f"Wrote {FACTS} and data/VERDICTS.txt: {len(verdicts)} verdicts, {n_er} eroding, "
          f"{len(special)} special-dividend flags")
    for msg in map_issues + cov_issues:
        print(f"  SELF-AUDIT: {msg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
