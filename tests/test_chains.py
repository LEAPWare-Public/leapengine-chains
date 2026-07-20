"""Adversarial suite - fetch_chains.build/quality. Written to BREAK the code."""
import datetime, importlib.util, json, math, sys, pytest
spec = importlib.util.spec_from_file_location("fc", "fetch_chains.py")
fc = importlib.util.module_from_spec(spec); spec.loader.exec_module(fc)

FA = datetime.datetime(2026, 7, 20, 17, 0, 0)

def opt(t="O", exp="260821", cp="C", strike=65.0, **kw):
    d = {"option": f"{t}{exp}{cp}{int(strike*1000):08d}", "bid": 1.0, "ask": 1.1,
         "open_interest": 500, "volume": 10, "iv": 0.2, "delta": 0.3,
         "last_trade_time": None}
    d.update(kw); return d

def raw(spot=65.0, prev=64.0, opts=None, ts="2026-07-20 16:55:00"):
    return {"timestamp": ts, "data": {"current_price": spot, "prev_day_close": prev,
                                      "options": opts if opts is not None else [opt()]}}

# ---------- structural ----------
def test_happy_path():
    o = fc.build("O", raw(), "O", FA)
    assert o["quality"]["contracts"] == 1
    assert o["quality"]["tradable"] is True
    assert o["fetched_at_utc"].endswith("Z")

def test_no_contracts_untradable():
    q = fc.build("O", raw(opts=[]), "O", FA)["quality"]
    assert "NO_CONTRACTS" in q["flags"] and q["tradable"] is False

def test_no_spot_untradable():
    r = {"timestamp": None, "data": {"current_price": None, "close": None,
                                     "prev_day_close": 1, "options": [opt()]}}
    q = fc.build("O", r, "O", FA)["quality"]
    assert "NO_SPOT" in q["flags"] and q["tradable"] is False

def test_no_greeks_flag():
    q = fc.build("O", raw(opts=[opt(delta=0.0), opt(strike=66.0, delta=0.0)]), "O", FA)["quality"]
    assert "NO_GREEKS" in q["flags"] and q["tradable"] is False

def test_sparse_greeks_flag():
    os_ = [opt(strike=60+i, delta=(0.3 if i < 2 else 0.0)) for i in range(10)]
    q = fc.build("O", raw(opts=os_), "O", FA)["quality"]
    assert "SPARSE_GREEKS" in q["flags"]

def test_no_bids_flag():
    q = fc.build("O", raw(opts=[opt(bid=0)]), "O", FA)["quality"]
    assert "NO_BIDS" in q["flags"] and q["tradable"] is False

def test_spot_equals_prev_flag():
    q = fc.build("O", raw(spot=65.0, prev=65.0), "O", FA)["quality"]
    assert "SPOT_EQUALS_PREV_CLOSE" in q["flags"]

def test_stale_cboe_ts_flag():
    q = fc.build("O", raw(ts="2026-07-18 00:00:00"), "O", FA)["quality"]
    assert "CBOE_TS_OVER_24H" in q["flags"]
    assert q["cboe_ts_age_hours"] > 24

# ---------- FINDING CANDIDATES ----------
def test_F1_stale_data_must_not_be_tradable():
    """OKE shipped tradable=True on a 41.7h-old payload. The exact failure the
    fix was supposed to prevent."""
    q = fc.build("OKE", raw(spot=93.52, prev=93.52, ts="2026-07-18 23:32:24",
                            opts=[opt(t="OKE", strike=93.0)]), "OKE", FA)["quality"]
    assert q["contracts"] == 1, "test fixture broken - symbol offset"
    assert q["tradable"] is False, "stale + spot==prev must not be tradable"

def test_F2_dte_uses_market_date_not_utc_date():
    """20:00 ET Aug-20 = 00:00 UTC Aug-21. UTC date rolls; the trading day has not.
    DTE would read 0 instead of 1 and annualised yield would divide by zero/blow up."""
    late = datetime.datetime(2026, 8, 21, 0, 30, 0)  # = Aug-20 20:30 ET
    o = fc.build("O", raw(), "O", late)
    assert o["options"], "same-day-expiry contract vanished due to UTC date roll"
    assert o["options"][0]["dte"] >= 1

def test_F3_duplicate_contracts_deduped():
    q = fc.build("O", raw(opts=[opt(), opt()]), "O", FA)["quality"]
    assert q["contracts"] == 1, "duplicate option symbols not deduplicated"

def test_F4_index_symbol_offset():
    """Resolved '_SPX' -> lstrip gives 'SPX' (3). Real SPX weekly symbols are
    'SPXW...' (4). Slice offset is wrong and strikes decode to garbage."""
    r = raw(spot=5000.0, prev=4990.0,
            opts=[{"option": "SPXW260821C05000000", "bid": 1, "ask": 1.1,
                   "open_interest": 500, "volume": 1, "iv": .2, "delta": .3}])
    o = fc.build("SPX", r, "_SPX", FA)
    if o["options"]:
        assert abs(o["options"][0]["strike"] - 5000.0) < 0.01, \
            f"strike decoded as {o['options'][0]['strike']}"

def test_F5_string_numerics_do_not_crash():
    q = fc.build("O", raw(opts=[opt(bid="1.0", ask="1.1", delta="0.3")]), "O", FA)["quality"]
    assert isinstance(q["contracts"], int)

def test_F6_negative_delta_puts_count_as_greeks():
    q = fc.build("O", raw(opts=[opt(cp="P", delta=-0.18)]), "O", FA)["quality"]
    assert q["with_greeks"] == 1

def test_F7_malformed_symbol_skipped_not_fatal():
    q = fc.build("O", raw(opts=[{"option": "GARBAGE", "bid": 1, "ask": 2},
                                opt()]), "O", FA)["quality"]
    assert q["contracts"] == 1

def test_F8_strike_window_boundaries():
    os_ = [opt(strike=45.5), opt(strike=91.0), opt(strike=65.0)]  # 0.70x, 1.40x, atm
    o = fc.build("O", raw(spot=65.0), "O", FA)
    assert len(o["options"]) >= 1

def test_F9_zero_strike_rejected():
    o = fc.build("O", raw(opts=[opt(strike=0.0)]), "O", FA)
    assert all(x["strike"] > 0 for x in o["options"])

def test_F10_expiry_in_past_excluded():
    o = fc.build("O", raw(opts=[opt(exp="260101")]), "O", FA)
    assert all(x["dte"] > 0 for x in o["options"])
