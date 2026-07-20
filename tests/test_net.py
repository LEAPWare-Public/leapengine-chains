"""Network layer: retry, 404 fallback, index symbols, last-known-good."""
import datetime, importlib.util, io, json, urllib.error, pytest

def load(n,p):
    s=importlib.util.spec_from_file_location(n,p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
fc = load("fcn","fetch_chains.py")
fd = load("fdn","fetch_distributions.py")

class R(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self,*a): return False

def test_fetch_plain_symbol(monkeypatch):
    monkeypatch.setattr(fc.urllib.request,"urlopen",lambda *a,**k: R(b'{"ok":1}'))
    raw,sym = fc.fetch("O"); assert raw=={"ok":1} and sym=="O"

def test_fetch_404_falls_back_to_underscore(monkeypatch):
    calls=[]
    def fake(req,**k):
        calls.append(req.full_url)
        if "_SPX" not in req.full_url:
            raise urllib.error.HTTPError(req.full_url,404,"nf",{},None)
        return R(b'{"ok":"idx"}')
    monkeypatch.setattr(fc.urllib.request,"urlopen",fake)
    monkeypatch.setattr(fc.time,"sleep",lambda s: None)
    raw,sym = fc.fetch("SPX")
    assert sym=="_SPX" and raw=={"ok":"idx"} and len(calls)==2

def test_fetch_retries_then_succeeds(monkeypatch):
    n={"i":0}
    def fake(req,**k):
        n["i"]+=1
        if n["i"]<3: raise urllib.error.HTTPError(req.full_url,503,"busy",{},None)
        return R(b'{"ok":2}')
    monkeypatch.setattr(fc.urllib.request,"urlopen",fake)
    monkeypatch.setattr(fc.time,"sleep",lambda s: None)
    assert fc.fetch("O")[0]=={"ok":2} and n["i"]==3

def test_fetch_exhausts_and_raises(monkeypatch):
    monkeypatch.setattr(fc.urllib.request,"urlopen",
                        lambda *a,**k: (_ for _ in ()).throw(OSError("dns")))
    monkeypatch.setattr(fc.time,"sleep",lambda s: None)
    with pytest.raises(RuntimeError): fc.fetch("O")

def test_num_coercion_matrix():
    assert fc._num("1.5")==1.5 and fc._num(2)==2.0 and fc._num(None) is None
    assert fc._num("abc") is None and fc._num(True) is None and fc._num(" 3 ")==3.0

def test_market_date_utc_rollover():
    # 00:30 UTC Aug-21 == 20:30 ET Aug-20
    assert fc.market_date(datetime.datetime(2026,8,21,0,30)) == datetime.date(2026,8,20)
    assert fc.market_date(datetime.datetime(2026,8,20,17,0)) == datetime.date(2026,8,20)

def test_age_parse_both_formats():
    FA=datetime.datetime(2026,7,20,17,0)
    for ts in ("2026-07-20 13:00:00","2026-07-20T13:00:00"):
        r={"timestamp":ts,"data":{"current_price":65.0,"prev_day_close":64.0,
           "options":[{"option":"O260821C00065000","bid":1,"ask":1.1,
                       "open_interest":500,"volume":1,"iv":.2,"delta":.3}]}}
        assert fc.build("O",r,"O",FA)["quality"]["cboe_ts_age_hours"]==4.0

def test_age_unparseable_flags_missing_timestamp():
    FA=datetime.datetime(2026,7,20,17,0)
    r={"timestamp":"not-a-date","data":{"current_price":65.0,"prev_day_close":64.0,
       "options":[{"option":"O260821C00065000","bid":1,"ask":1.1,
                   "open_interest":500,"volume":1,"iv":.2,"delta":.3}]}}
    q=fc.build("O",r,"O",FA)["quality"]
    assert q["cboe_ts_age_hours"] is None and "NO_CBOE_TIMESTAMP" in q["flags"]

def test_lapse_and_split_guards():
    assert fd.lapse_flag(None,4,datetime.date(2026,7,20)) is False
    assert fd.lapse_flag(datetime.date(2026,1,1),None,datetime.date(2026,7,20)) is False
    assert fd.split_flag(None,datetime.date(2025,7,20)) is False
    assert fd.split_flag([],datetime.date(2025,7,20)) is False

def test_load_existing_missing_file(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path); fd.OUT="data/nope.json"
    assert fd.load_existing()["records"]==[]
