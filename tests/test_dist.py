"""Adversarial suite - fetch_distributions yield/A7-R math."""
import datetime, importlib.util, json, os, sys, pytest
spec = importlib.util.spec_from_file_location("fd", "fetch_distributions.py")
fd = importlib.util.module_from_spec(spec); spec.loader.exec_module(fd)

D = datetime.date
def q(y, m, d): return D(y, m, d)

# ---------- infer_frequency ----------
def test_freq_monthly():
    assert fd.infer_frequency([q(2026,1,1),q(2026,2,1),q(2026,3,1)])[0] == 12
def test_freq_quarterly():
    assert fd.infer_frequency([q(2025,9,1),q(2025,12,1),q(2026,3,1)])[0] == 4
def test_freq_annual():
    assert fd.infer_frequency([q(2024,6,1),q(2025,6,1),q(2026,6,1)])[0] == 1
def test_freq_semi():
    assert fd.infer_frequency([q(2025,6,1),q(2025,12,1),q(2026,6,1)])[0] == 2
def test_freq_single_record_is_none():
    assert fd.infer_frequency([q(2026,1,1)]) == (None, None)
def test_freq_empty():
    assert fd.infer_frequency([]) == (None, None)
def test_freq_unsorted_input():
    assert fd.infer_frequency([q(2026,3,1),q(2026,1,1),q(2026,2,1)])[0] == 12

# ---------- FINDING CANDIDATES ----------
def test_G1_special_dividend_must_be_flagged():
    """RYN shipped an 11.60% TTM yield vs a 4.89% run rate - a special dividend
    counted as recurring income. Nothing flagged it. An income portfolio sized
    on 11.6% would be sized on a number that will not repeat."""
    assert hasattr(fd, "yield_flags"), "no special-dividend detection exists"
    f = fd.yield_flags(ttm_yield=11.60, run_rate=4.89, ttm_count=5, freq=4,
                       full=True, tr=0.42)
    assert "TTM_OVERCOUNTED" in f or "SPECIAL_DIVIDEND_SUSPECTED" in f

def test_G2_dividend_lapse_must_be_flagged():
    """History exists but nothing paid in 200 days = a suspended distribution.
    Silently reads as 'low yield' instead of 'Rule 9 trigger'."""
    assert hasattr(fd, "lapse_flag")
    assert fd.lapse_flag(last_ex=q(2025,12,1), freq=4, today=q(2026,7,20)) is True
def test_G2b_no_false_lapse():
    assert fd.lapse_flag(last_ex=q(2026,6,30), freq=4, today=q(2026,7,20)) is False

def test_G3_a7r_absence_is_explicit():
    """Verdict None with no flag is indistinguishable from 'not tested'."""
    f = fd.yield_flags(ttm_yield=None, run_rate=None, ttm_count=0, freq=None,
                       full=False, tr=None)
    assert "A7R_NOT_RUN" in f

def test_G4_split_in_window_flagged():
    """price_return_12m from unadjusted Close is catastrophically wrong across a
    split (SCHD 3-for-1). Must be detected, not silently reported."""
    assert hasattr(fd, "split_flag")
    assert fd.split_flag([q(2026,1,15)], q(2025,7,20)) is True
    assert fd.split_flag([q(2024,1,15)], q(2025,7,20)) is False

def test_G5_zero_price_no_div_by_zero():
    f = fd.yield_flags(ttm_yield=None, run_rate=None, ttm_count=4, freq=4,
                       full=True, tr=5.0)
    assert isinstance(f, list)

def test_G6_a7r_verdicts():
    assert fd.a7r_verdict(tr=9.62, ttm_yield=9.36) == "EARNED"
    assert fd.a7r_verdict(tr=6.21, ttm_yield=9.97) == "UNDEREARNING"
    assert fd.a7r_verdict(tr=-31.51, ttm_yield=6.89) == "ERODING"
    assert fd.a7r_verdict(tr=None, ttm_yield=5.0) is None
    assert fd.a7r_verdict(tr=5.0, ttm_yield=None) is None

def test_G7_a7r_boundary_equal():
    assert fd.a7r_verdict(tr=5.0, ttm_yield=5.0) == "EARNED"

def test_G8_read_tickers_dedupes_and_skips_comments():
    open("config/t2.txt","w").write("# c\nO, RQI\nO\n\nWPC\n")
    old = fd.TICKERS; fd.TICKERS = "config/t2.txt"
    try:
        assert fd.read_tickers() == ["O","RQI","WPC"]
    finally:
        fd.TICKERS = old
