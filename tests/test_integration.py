"""main() orchestration with faked network. This is where --days 120 hid."""
import datetime, importlib.util, json, os, sys, types, pytest

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

# ================= CHAINS =================
def test_main_chains_writes_report_and_preserves_on_failure(tmp_path, monkeypatch):
    fc = load("fc2", "fetch_chains.py")
    monkeypatch.chdir(tmp_path); os.makedirs("data", exist_ok=True)
    json.dump({"ticker":"BAD","spot":9.99}, open("data/BAD.json","w"))  # last-known-good

    def fake_fetch(sym):
        if sym.startswith("BAD") or sym.startswith("_BAD"):
            raise RuntimeError("HTTP 500")
        return ({"timestamp":"2026-07-20 16:55:00",
                 "data":{"current_price":65.0,"prev_day_close":64.0,
                         "options":[{"option":"O260821C00065000","bid":1.0,"ask":1.1,
                                     "open_interest":500,"volume":5,"iv":.2,"delta":.3}]}}, sym)
    monkeypatch.setattr(fc, "fetch", fake_fetch)
    monkeypatch.setenv("TICKERS", "O,BAD")
    assert fc.main() == 0

    rep = json.load(open("data/_run_report.json"))
    assert rep["tickers"]["O"]["tradable"] is True
    assert rep["tickers"]["BAD"]["flags"] == ["FETCH_FAILED"]
    assert json.load(open("data/BAD.json"))["spot"] == 9.99, "clobbered last-known-good"
    assert os.path.exists("data/BAD.error.json")

def test_main_chains_no_tickers(tmp_path, monkeypatch):
    fc = load("fc3", "fetch_chains.py")
    monkeypatch.chdir(tmp_path); monkeypatch.setenv("TICKERS", "")
    assert fc.main() == 1

def test_main_chains_clears_stale_error_file(tmp_path, monkeypatch):
    fc = load("fc4", "fetch_chains.py")
    monkeypatch.chdir(tmp_path); os.makedirs("data", exist_ok=True)
    json.dump({"error":"old"}, open("data/O.error.json","w"))
    monkeypatch.setattr(fc, "fetch", lambda s: ({"timestamp":"2026-07-20 16:55:00",
        "data":{"current_price":65.0,"prev_day_close":64.0,
                "options":[{"option":"O260821C00065000","bid":1.0,"ask":1.1,
                            "open_interest":500,"volume":5,"iv":.2,"delta":.3}]}}, s))
    monkeypatch.setenv("TICKERS", "O"); fc.main()
    assert not os.path.exists("data/O.error.json")

# ================= DISTRIBUTIONS =================
class _S:
    def __init__(self, items): self._i = items
    def items(self): return self._i
    def __len__(self): return len(self._i)
    @property
    def index(self): return [k for k, _ in self._i]

class _TS:
    def __init__(self, d): self._d = d
    def date(self): return self._d

class _Hist:
    def __init__(self, vals, dates):
        self._v = vals; self.index = types.SimpleNamespace(date=dates)
        class C:
            def __init__(s, v): s._v = v
            @property
            def iloc(s): return s._v
        self._c = C(vals)
    def __len__(self): return len(self._v)
    def __getitem__(self, k): return self._c
    def __gt__(self, o): return self
    def __ge__(self, o): return self

def _mk_yf(divs, p0, p1, a0, a1, splits=()):
    mod = types.ModuleType("yfinance")
    class T:
        def __init__(self, tk): self.tk = tk
        fast_info = {"last_price": p1}
        @property
        def dividends(self): return _S(divs)
        @property
        def splits(self): return _S([( _TS(d), 3.0) for d in splits])
        def history(self, period=None, auto_adjust=False):
            dates = [datetime.date(2025,7,25), datetime.date(2026,7,15)]
            vals = ([a0, a1] if auto_adjust else [p0, p1])
            class H:
                def __init__(s): s.index = types.SimpleNamespace(date=dates)
                def __len__(s): return 30
                def __getitem__(s, k):
                    class C:
                        iloc = vals
                    return C()
            h = H()
            class Wrapped(H):
                pass
            return h
    mod.Ticker = T
    return mod

def _run_dist(tmp_path, monkeypatch, divs, p0, p1, a0, a1, splits=(), days=800):
    fd = load("fd2", "fetch_distributions.py")
    monkeypatch.chdir(tmp_path)
    os.makedirs("config", exist_ok=True); os.makedirs("data", exist_ok=True)
    open("config/tickers.txt","w").write("XYZ\n")
    fd.TICKERS = "config/tickers.txt"; fd.OUT = "data/DISTRIBUTIONS.json"; fd.YIELDS = "data/YIELDS.json"
    monkeypatch.setitem(sys.modules, "yfinance", _mk_yf(divs, p0, p1, a0, a1, splits))
    monkeypatch.setattr(sys, "argv", ["x", "--days", str(days)])
    # history filtering uses boolean masks on real frames; bypass by neutering it
    fd_orig = fd.fetch_one
    def patched(ticker, since):
        rows, price, st = [], p1, {}
        for ex, amt in divs:
            if ex.date() >= since:
                rows.append({"ex_date": ex.date().isoformat(), "amount": float(amt)})
        st = {"price_start": p0, "price_end": p1,
              "price_return_12m_pct": round((p1/p0-1)*100, 2),
              "total_return_12m_pct": round((a1/a0-1)*100, 2), "price_days": 250}
        if splits: st["split_dates"] = [d.isoformat() for d in splits]
        return rows, price, st
    fd.fetch_one = patched
    fd.main()
    return json.load(open("data/YIELDS.json"))["tickers"]["XYZ"]

def test_dist_earned(tmp_path, monkeypatch):
    divs = [(_TS(datetime.date(2024,12,31)),0.78),(_TS(datetime.date(2025,3,31)),0.78),
            (_TS(datetime.date(2025,6,30)),0.80),
            (_TS(datetime.date(2025,9,30)),0.81),(_TS(datetime.date(2025,12,31)),0.81),
            (_TS(datetime.date(2026,3,31)),0.81),(_TS(datetime.date(2026,6,30)),0.81)]
    y = _run_dist(tmp_path, monkeypatch, divs, 55.0, 65.0, 55.0, 67.0)
    assert y["frequency"] == 4 and y["ttm_payment_count"] == 4
    assert y["a7r_verdict"] == "EARNED" and y["deployable"] is True

def test_dist_eroding_not_deployable(tmp_path, monkeypatch):
    divs = [(_TS(datetime.date(2025,3,31)),0.87),(_TS(datetime.date(2025,6,30)),0.87),
            (_TS(datetime.date(2025,9,30)),0.87),(_TS(datetime.date(2025,12,31)),0.87),
            (_TS(datetime.date(2026,3,31)),0.87),(_TS(datetime.date(2026,6,30)),0.87)]
    y = _run_dist(tmp_path, monkeypatch, divs, 78.0, 50.5, 78.0, 53.4)
    assert y["a7r_verdict"] == "ERODING"
    assert y["deployable"] is False
    assert "NAV_DOWN_OVER_10PCT" in y["flags"]

def test_dist_special_dividend_flagged(tmp_path, monkeypatch):
    divs = [(_TS(datetime.date(2025,3,16)),0.26),(_TS(datetime.date(2025,6,16)),0.26),
            (_TS(datetime.date(2025,9,16)),0.26),(_TS(datetime.date(2025,12,16)),0.26),
            (_TS(datetime.date(2026,3,16)),0.26),(_TS(datetime.date(2026,6,16)),0.26),
            (_TS(datetime.date(2026,6,20)),1.43)]
    y = _run_dist(tmp_path, monkeypatch, divs, 23.5, 21.25, 23.5, 23.6)
    assert "SPECIAL_DIVIDEND_SUSPECTED" in y["flags"] or "TTM_OVERCOUNTED" in y["flags"]
    assert y["deployable"] is False

def test_dist_lapsed_distribution_flagged(tmp_path, monkeypatch):
    divs = [(_TS(datetime.date(2024,9,30)),0.50),(_TS(datetime.date(2024,12,31)),0.50),
            (_TS(datetime.date(2025,3,31)),0.50),(_TS(datetime.date(2025,6,30)),0.50),
            (_TS(datetime.date(2025,9,30)),0.50),(_TS(datetime.date(2025,12,1)),0.50)]
    y = _run_dist(tmp_path, monkeypatch, divs, 40.0, 30.0, 40.0, 32.0)
    assert "DISTRIBUTION_LAPSED" in y["flags"]
    assert y["deployable"] is False

def test_dist_split_flagged(tmp_path, monkeypatch):
    divs = [(_TS(datetime.date(2025,3,25)),0.25),(_TS(datetime.date(2025,6,25)),0.25),
            (_TS(datetime.date(2025,9,25)),0.25),(_TS(datetime.date(2025,12,24)),0.25),
            (_TS(datetime.date(2026,3,25)),0.26),(_TS(datetime.date(2026,6,24)),0.26)]
    y = _run_dist(tmp_path, monkeypatch, divs, 90.0, 33.0, 90.0, 34.0,
                  splits=(datetime.date(2026,1,10),))
    assert "SPLIT_IN_WINDOW" in y["flags"]
    assert y["deployable"] is False

def test_dist_120_day_regression(tmp_path, monkeypatch):
    """THE ORIGINAL BUG. 120d lookback must be detectable as partial history,
    never silently reported as a trailing-12-month yield."""
    divs = [(_TS(datetime.date(2026,3,31)),0.81),(_TS(datetime.date(2026,6,30)),0.81)]
    y = _run_dist(tmp_path, monkeypatch, divs, 60.0, 65.0, 60.0, 67.0, days=120)
    assert y["full_12m_history"] is False
    assert "PARTIAL_HISTORY" in y["flags"]
    assert y["deployable"] is False, "partial history must never be deployable"
