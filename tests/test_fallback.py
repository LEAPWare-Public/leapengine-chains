"""Fallback source + Black-Scholes delta."""
import datetime, importlib.util, math, sys, types, pytest
from hypothesis import given, strategies as st, settings
def load(n,p):
    s=importlib.util.spec_from_file_location(n,p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
fc = load("fcb","fetch_chains.py")

def test_bs_atm_call_delta_near_half():
    d = fc.bs_delta(100,100,30,0.25,"C")
    assert 0.48 < d < 0.58
def test_bs_put_call_parity():
    c = fc.bs_delta(100,95,30,0.25,"C"); p = fc.bs_delta(100,95,30,0.25,"P")
    assert abs((c - p) - 1.0) < 1e-6
def test_bs_deep_itm_call_to_one():
    assert fc.bs_delta(100,10,30,0.25,"C") > 0.99
def test_bs_deep_otm_call_to_zero():
    assert fc.bs_delta(100,500,30,0.25,"C") < 0.01
def test_bs_dividend_yield_lowers_call_delta():
    a = fc.bs_delta(65,67.5,32,0.20,"C",q=0.0)
    b = fc.bs_delta(65,67.5,32,0.20,"C",q=0.05)
    assert b < a, "q must reduce call delta on a high yielder"
def test_bs_degenerate_inputs_return_none():
    for args in [(0,100,30,.2,"C"),(100,0,30,.2,"C"),(100,100,0,.2,"C"),
                 (100,100,30,0,"C"),(100,100,-5,.2,"C"),(None,100,30,.2,"C")]:
        assert fc.bs_delta(*args) is None

@given(st.floats(min_value=1,max_value=1000,allow_nan=False),
       st.floats(min_value=1,max_value=1000,allow_nan=False),
       st.integers(min_value=1,max_value=400),
       st.floats(min_value=0.01,max_value=3.0,allow_nan=False))
@settings(max_examples=400)
def test_prop_delta_in_bounds(s_,k,t,iv):
    c = fc.bs_delta(s_,k,t,iv,"C"); p = fc.bs_delta(s_,k,t,iv,"P")
    if c is not None: assert -0.0001 <= c <= 1.0001
    if p is not None: assert -1.0001 <= p <= 0.0001

# ---- fallback wiring ----
class _Row:
    def __init__(s,**kw): s.__dict__.update(kw)
class _F:
    def __init__(s,rows): s._r=rows
    def itertuples(s): return iter(s._r)
class _Ch:
    def __init__(s,c,p): s.calls=_F(c); s.puts=_F(p)

def _yf(spot=42.9, exps=("2026-08-21",), oi=850):
    m=types.ModuleType("yfinance")
    class T:
        def __init__(s,tk): s.tk=tk
        fast_info={"last_price":spot,"previous_close":spot-0.3}
        options=list(exps)
        def option_chain(s,e):
            calls=[_Row(strike=45.0,bid=0.55,ask=0.65,openInterest=oi,volume=12,impliedVolatility=0.28),
                   _Row(strike=47.5,bid=0.20,ask=0.30,openInterest=oi,volume=4,impliedVolatility=0.30)]
            puts=[_Row(strike=40.0,bid=0.45,ask=0.55,openInterest=oi,volume=9,impliedVolatility=0.29)]
            return _Ch(calls,puts)
    m.Ticker=T; return m

FA=datetime.datetime(2026,7,20,17,0)

def test_fallback_produces_greeks(monkeypatch):
    monkeypatch.setitem(sys.modules,"yfinance",_yf())
    o = fc.fetch_yf("CTRE", FA, {"CTRE":0.0342})
    assert o["quality"]["contracts"]==3
    assert o["quality"]["with_greeks"]==3
    assert o["quality"]["tradable"] is True
    assert "SOURCE_YFINANCE" in o["quality"]["flags"]
    assert o["quality"]["dividend_yield_used"]==0.0342
    assert all(x["delta"] is not None for x in o["options"])
    assert all(x["delta_source"]=="black_scholes" for x in o["options"])

def test_fallback_puts_have_negative_delta(monkeypatch):
    monkeypatch.setitem(sys.modules,"yfinance",_yf())
    o = fc.fetch_yf("CTRE", FA, {})
    assert all(x["delta"] < 0 for x in o["options"] if x["type"]=="P")

def test_fallback_no_spot_raises(monkeypatch):
    m=types.ModuleType("yfinance")
    class T:
        def __init__(s,tk): pass
        fast_info={}; options=[]
    m.Ticker=T; monkeypatch.setitem(sys.modules,"yfinance",m)
    with pytest.raises(RuntimeError): fc.fetch_yf("X", FA, {})

def test_main_triggers_fallback_on_stale_cboe(tmp_path, monkeypatch):
    fc2 = load("fcb2","fetch_chains.py")
    monkeypatch.chdir(tmp_path); import os; os.makedirs("data",exist_ok=True)
    monkeypatch.setattr(fc2,"fetch", lambda s: ({"timestamp":"2026-07-17 12:00:00",
        "data":{"current_price":42.9,"prev_day_close":42.9,
        "options":[{"option":"CTRE260821C00047500","bid":0.2,"ask":0.55,
                    "open_interest":1,"volume":0,"iv":.3,"delta":.17}]}}, s))
    monkeypatch.setitem(sys.modules,"yfinance",_yf())
    monkeypatch.setenv("TICKERS","CTRE")
    assert fc2.main()==0
    import json
    rep=json.load(open("data/_run_report.json"))["tickers"]["CTRE"]
    assert rep["source"]=="yfinance"
    assert "CBOE_FALLBACK_USED" in rep["flags"]
    assert rep["cboe_age_at_fallback"] > 24

def test_main_keeps_cboe_when_fresh(tmp_path, monkeypatch):
    fc3 = load("fcb3","fetch_chains.py")
    monkeypatch.chdir(tmp_path); import os; os.makedirs("data",exist_ok=True)
    monkeypatch.setattr(fc3,"fetch", lambda s: ({"timestamp":"2026-07-20 16:30:00",
        "data":{"current_price":65.0,"prev_day_close":64.0,
        "options":[{"option":"O260821C00065000","bid":1.0,"ask":1.1,
                    "open_interest":500,"volume":5,"iv":.2,"delta":.3}]}}, s))
    monkeypatch.setenv("TICKERS","O")
    fc3.main()
    import json
    assert json.load(open("data/_run_report.json"))["tickers"]["O"]["source"]=="cboe"
