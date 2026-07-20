"""Property-based fuzzing. Looking for invariant violations hand-written cases missed."""
import datetime, importlib.util
from hypothesis import given, strategies as st, settings, assume, HealthCheck
def load(n,p):
    s=importlib.util.spec_from_file_location(n,p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
fc = load("fcp","fetch_chains.py"); fd = load("fdp","fetch_distributions.py")
FA = datetime.datetime(2026,7,20,17,0)

@given(st.floats(min_value=0.01,max_value=1e5,allow_nan=False),
       st.floats(min_value=0.01,max_value=1e5,allow_nan=False),
       st.integers(min_value=1,max_value=99999))
@settings(max_examples=250, suppress_health_check=[HealthCheck.too_slow])
def test_prop_build_never_raises(spot, prev, strike_k):
    raw={"timestamp":"2026-07-20 16:00:00",
         "data":{"current_price":spot,"prev_day_close":prev,
                 "options":[{"option":f"O260821C{strike_k*1000:08d}","bid":0.5,"ask":0.6,
                             "open_interest":100,"volume":1,"iv":0.2,"delta":0.2}]}}
    o = fc.build("O",raw,"O",FA)
    q = o["quality"]
    assert isinstance(q["tradable"],bool)
    assert q["contracts"]==len(o["options"])
    assert q["with_greeks"]<=q["contracts"] and q["with_bids"]<=q["contracts"]
    for x in o["options"]:
        assert 0.70*spot <= x["strike"] <= 1.40*spot
        assert x["dte"] > 0

@given(st.lists(st.floats(min_value=-1e6,max_value=1e6,allow_nan=False,allow_infinity=False),
                min_size=0,max_size=6))
@settings(max_examples=200)
def test_prop_num_total(vals):
    for v in vals:
        r = fc._num(v)
        assert r is None or isinstance(r,float)
    for junk in ["", "  ", "nan", "1e999", None, True, False, [], {}]:
        r = fc._num(junk)
        assert r is None or isinstance(r,float)

@given(st.floats(min_value=-100,max_value=1000,allow_nan=False),
       st.floats(min_value=0.001,max_value=100,allow_nan=False))
@settings(max_examples=300)
def test_prop_a7r_is_total_and_consistent(tr, ttm):
    v = fd.a7r_verdict(tr, ttm)
    assert v in ("EARNED","UNDEREARNING","ERODING")
    if tr < 0: assert v=="ERODING"
    elif tr >= ttm: assert v=="EARNED"
    else: assert v=="UNDEREARNING"

@given(st.integers(min_value=1,max_value=60), st.integers(min_value=1,max_value=52))
@settings(max_examples=200)
def test_prop_infer_frequency_in_domain(n, spacing):
    base = datetime.date(2024,1,1)
    ds = [base + datetime.timedelta(days=spacing*i) for i in range(n)]
    f,g = fd.infer_frequency(ds)
    assert f in (None,1,2,4,12,52)
    if n < 2: assert f is None

@given(st.integers(min_value=0,max_value=2000), st.sampled_from([1,2,4,12,52]))
@settings(max_examples=200)
def test_prop_lapse_monotonic(days_ago, freq):
    today = datetime.date(2026,7,20)
    last = today - datetime.timedelta(days=days_ago)
    r = fd.lapse_flag(last, freq, today)
    assert isinstance(r,bool)
    if days_ago > (365/freq)*1.5: assert r is True

@given(st.floats(min_value=0.01,max_value=50,allow_nan=False),
       st.floats(min_value=0.01,max_value=50,allow_nan=False),
       st.integers(min_value=0,max_value=20), st.sampled_from([1,2,4,12]),
       st.booleans(), st.floats(min_value=-99,max_value=99,allow_nan=False))
@settings(max_examples=300)
def test_prop_yield_flags_never_raise(ttm, rr, cnt, freq, full, tr):
    f = fd.yield_flags(ttm, rr, cnt, freq, full, tr)
    assert isinstance(f,list) and all(isinstance(x,str) for x in f)
    if not full: assert "PARTIAL_HISTORY" in f
