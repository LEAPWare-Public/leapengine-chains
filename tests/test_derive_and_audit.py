"""Tests for the single-source-of-truth deriver and the consistency gate.
These test the CONTROLS, i.e. the things that failed on Jul-20."""
import json, os, importlib.util, datetime, sys, pytest

def load(n,p):
    s=importlib.util.spec_from_file_location(n,p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
df = load("df","scripts/derive_facts.py")

def Y(**kw): return kw

# ---- A7-R v3 verdict logic: the exact Jul-20 cases ----
def test_btci_not_broken_beats_underlying():
    y={"BTCI":Y(total_return_12m_pct=-39.20,ttm_yield_pct=41.5),
       "BITO":Y(total_return_12m_pct=-45.27)}
    v=df.a7r_verdict("BTCI",y)
    assert v["verdict"]=="EARNED", "BTCI beat BITO by 6pts; must NOT read as eroding"

def test_aipi_overlay_drag_confirmed():
    y={"AIPI":Y(total_return_12m_pct=14.14),"SMH":Y(total_return_12m_pct=101.68)}
    v=df.a7r_verdict("AIPI",y)
    assert v["verdict"]=="OVERLAY_DRAG"

def test_vici_eroding_vs_peer():
    y={"VICI":Y(total_return_12m_pct=-13.59),"GLPI":Y(total_return_12m_pct=2.19)}
    v=df.a7r_verdict("VICI",y)
    assert v["verdict"]=="ERODING"  # trails its subsector peer by >10pts

def test_arcc_tracks_peers_not_eroding():
    y={"ARCC":Y(total_return_12m_pct=-8.48),"BIZD":Y(total_return_12m_pct=-14.70)}
    v=df.a7r_verdict("ARCC",y)
    assert v["verdict"]=="TRACKS_PEERS", "ARCC beat its sector; not eroding"

def test_pffa_coverage_not_total_return():
    y={"PFFA":Y(total_return_12m_pct=6.26,ttm_yield_pct=9.96)}
    v=df.a7r_verdict("PFFA",y)
    assert v["class"]=="coverage" and v["verdict"]=="EARNED"
    assert "SEC-yield" in v["note"]

def test_no_verdict_without_benchmark():
    y={"BTCI":Y(total_return_12m_pct=-39.2)}  # BITO missing
    v=df.a7r_verdict("BTCI",y)
    assert v["verdict"]=="UNRESOLVED", "must refuse a verdict when the benchmark is absent (M12)"

def test_eroding_needs_10pt_gap():
    # trails by only 5 pts -> LAGS, not ERODING
    y={"AMT":Y(total_return_12m_pct=9.0),"VNQ":Y(total_return_12m_pct=14.0)}
    assert df.a7r_verdict("AMT",y)["verdict"]=="LAGS"

# ---- consistency gate ----
ac = load("ac","scripts/audit_consistency.py")

def _setup(tmp, facts, files, yields_age_h=1, facts_age_h=1):
    os.chdir(tmp); os.makedirs("data",exist_ok=True)
    now=datetime.datetime.now(datetime.timezone.utc)
    def stamp(h): return (now-datetime.timedelta(hours=h)).isoformat()
    json.dump({"generated_at":stamp(facts_age_h),"facts":{"a7r_verdicts":{"value":facts}}}, open("data/FACTS.json","w"))
    json.dump({"generated_at":stamp(yields_age_h),"tickers":{}}, open("data/YIELDS.json","w"))
    for name,txt in files.items(): open(name,"w").write(txt)

def test_gate_blocks_stale_verdict_in_authoritative_file(tmp_path):
    _setup(tmp_path, {"PFFA":{"verdict":"EARNED"}},
           {"CLAUDE.md":"PFFA is UNDEREARNING on $164K\n"})
    assert ac.main()==1, "must block: CLAUDE.md says UNDEREARNING, golden says EARNED"

def test_gate_allows_correction_notes(tmp_path):
    _setup(tmp_path, {"PFFA":{"verdict":"EARNED"}},
           {"CLAUDE.md":"!! CORRECTED: PFFA was UNDEREARNING, now EARNED\n"})
    assert ac.main()==0, "correction notes naming the old verdict are allowed"

def test_gate_exempts_history(tmp_path):
    _setup(tmp_path, {"BTCI":{"verdict":"EARNED"}},
           {"HISTORY.md":"BTCI was BROKEN and ERODING\n","CLAUDE.md":"clean\n"})
    assert ac.main()==0, "HISTORY.md is append-only and exempt"

def test_gate_blocks_stale_data(tmp_path):
    _setup(tmp_path, {"BTCI":{"verdict":"EARNED"}}, {"CLAUDE.md":"clean\n"}, yields_age_h=999)
    assert ac.main()==1, "must block when YIELDS.json exceeds its freshness SLA"

def test_gate_passes_when_consistent(tmp_path):
    _setup(tmp_path, {"VICI":{"verdict":"ERODING"}},
           {"CLAUDE.md":"VICI is ERODING vs GLPI\n"})
    assert ac.main()==0

# ---- CLA Pass A finding A-1: correction notes must NOT trigger false blocks ----
def test_gate_exempts_stale_marker_correction_note(tmp_path):
    _setup(tmp_path, {"BTCI":{"verdict":"EARNED"}},
           {"STRATEGY.md":"PLAN.md's BTCI BROKEN line is STALE and superseded.\n"})
    assert ac.main()==0, "a correction note using 'STALE' must not false-block"

def test_gate_exempts_rule_definition_text(tmp_path):
    _setup(tmp_path, {"AMT":{"verdict":"ERODING"}},
           {"STRATEGY.md":'NO ERODING VERDICT MAY BE ISSUED WITHOUT A BENCHMARK.\n'})
    assert ac.main()==0, "rule-definition text naming a verdict word must not false-block"

def test_gate_exempts_refutation(tmp_path):
    _setup(tmp_path, {"BTCI":{"verdict":"EARNED"}},
           {"CLAUDE.md":"BTCI is NOT broken - kept, T2 disarmed.\n"})
    assert ac.main()==0, "a refutation ('NOT broken') must not false-block"

def test_gate_still_blocks_bare_stale_claim(tmp_path):
    # no marker, no refutation - a genuine live stale claim must STILL block
    _setup(tmp_path, {"PFFA":{"verdict":"EARNED"}},
           {"CLAUDE.md":"PFFA yields 9.96% and is UNDEREARNING on the position.\n"})
    assert ac.main()==1, "a genuine stale claim with no correction marker must block"

# ---- CLA Pass B finding B-2: beating a collapsing benchmark is not "safe" ----
def test_b2_absolute_loss_flagged_even_when_beating_benchmark():
    y={"BTCI":Y(total_return_12m_pct=-60),"BITO":Y(total_return_12m_pct=-62)}
    v=df.a7r_verdict("BTCI",y)
    assert v["verdict"]=="EARNED"                    # relatively, it beat its underlying
    assert v["absolute_flag"]=="ABSOLUTE_LOSS_SEVERE" # but absolutely it cratered
    assert v["deployable"] is False                   # and must not take new money

def test_b2_deployable_requires_positive_absolute_return():
    y={"SPYI":Y(total_return_12m_pct=17.8),"SPY":Y(total_return_12m_pct=20.3)}
    v=df.a7r_verdict("SPYI",y)
    assert v["verdict"]=="EARNED" and v["deployable"] is True

def test_b2_mild_negative_flagged_not_severe():
    y={"VICI":Y(total_return_12m_pct=-13.59),"GLPI":Y(total_return_12m_pct=2.19)}
    v=df.a7r_verdict("VICI",y)
    assert v["absolute_flag"]=="ABSOLUTE_LOSS" and v["deployable"] is False

# ---- CLA final finding B-3: gate must catch FALSE-POSITIVE verdicts (the dangerous direction) ----
def test_gate_blocks_false_earned_claim(tmp_path):
    _setup(tmp_path, {"VICI":{"verdict":"ERODING"}},
           {"CLAUDE.md":"VICI is EARNED and safe to add\n"})
    assert ac.main()==1, "claiming a bad name is EARNED must block - deploys capital wrongly"

def test_gate_allows_true_earned_claim(tmp_path):
    _setup(tmp_path, {"SPYI":{"verdict":"EARNED"}},
           {"CLAUDE.md":"SPYI is EARNED\n"})
    assert ac.main()==0

# ---- CLA LOW fixes: self-audit surfaces map + coverage problems ----
def test_l1_flags_unmapped_held_highyielder(monkeypatch):
    df.HELD.append("ZZZTEST")
    y={"ZZZTEST":Y(ttm_yield_pct=9.0)}
    issues=df.validate_benchmark_map(y)
    df.HELD.remove("ZZZTEST")
    assert any("ZZZTEST" in i and "blind spot" in i for i in issues)

def test_l1_flags_missing_benchmark_in_feed():
    y={}  # GLD etc absent
    issues=df.validate_benchmark_map(y)
    assert any("GLD" in i and "not in YIELDS" in i for i in issues)

def test_l2_flags_stale_coverage_fact():
    import datetime
    df.COVERAGE["ZZZOLD"]={"sec_yield":9,"distribution":9,"verdict":"EARNED",
                           "as_of":"2020-01-01","source":"test"}
    stale=df.coverage_staleness(datetime.date.today())
    del df.COVERAGE["ZZZOLD"]
    assert any("ZZZOLD" in s for s in stale)

def test_l2_fresh_coverage_not_flagged():
    import datetime
    stale=df.coverage_staleness(datetime.date.today())
    assert not any("PFFA" in s for s in stale), "PFFA coverage is dated today; must not be stale"

# ---- cold-start findings: gate must not over-block on provisional/reference mentions ----
def test_gate_does_not_block_earned_vs_unresolved(tmp_path):
    # UNRESOLVED means "benchmark pending", not "bad name" — EARNED claim must NOT block
    _setup(tmp_path, {"UTF":{"verdict":"UNRESOLVED"}},
           {"STRATEGY.md":"UTF EARNED +12.43% Jul-20\n"})
    assert ac.main()==0, "EARNED vs provisional UNRESOLVED must not block"

def test_gate_still_blocks_earned_vs_definite_bad(tmp_path):
    _setup(tmp_path, {"RQI":{"verdict":"LAGS"}},
           {"STRATEGY.md":"RQI EARNED and deployable\n"})
    assert ac.main()==1, "EARNED vs a DEFINITE bad verdict (LAGS) must still block"

def test_gate_ignores_benchmark_reference(tmp_path):
    # 'clears vs DOC' names DOC as ARE's benchmark, not as an ERODING subject
    _setup(tmp_path, {"DOC":{"verdict":"TRACKS_PEERS"}},
           {"LEDGER-SEP.md":"ARE: no entry until it clears vs DOC. AMT ERODING.\n"})
    assert ac.main()==0, "a 'vs DOC' benchmark reference must not false-match DOC"
