"""Direct coverage of fetch_one - the yfinance boundary."""
import datetime, importlib.util, sys, types, pytest
def load(n,p):
    s=importlib.util.spec_from_file_location(n,p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
fd = load("fdo","fetch_distributions.py")

class Col:
    def __init__(s,v): s.v=v
    @property
    def iloc(s): return s.v
class Idx:
    def __init__(s,d): s.date=d
class Frame:
    """Minimal frame supporting frame[frame.index.date >= cutoff] and ['Close']."""
    def __init__(s,dates,closes): s.index=Idx(dates); s._c=closes
    def __len__(s): return len(s._c)
    def __getitem__(s,k):
        if isinstance(k,list):
            d=[x for x,m in zip(s.index.date,k) if m]; c=[x for x,m in zip(s._c,k) if m]
            return Frame(d,c)
        return Col(s._c)
class DateArr(list):
    def __ge__(s,o): return [d>=o for d in s]

def mk(divs,closes_raw,closes_adj,splits=(),price=65.0,boom=None):
    mod=types.ModuleType("yfinance")
    dates=DateArr([datetime.date(2025,7,1)+datetime.timedelta(days=15*i) for i in range(len(closes_raw))])
    class S(list):
        def __init__(s,items): super().__init__(items); s._i=items
        def items(s): return s._i
        @property
        def index(s): return [k for k,_ in s._i]
    class T:
        def __init__(s,tk): s.tk=tk
        fast_info={"last_price":price}
        @property
        def dividends(s): return S(divs)
        @property
        def splits(s): return S([(types.SimpleNamespace(date=lambda d=d:d),3.0) for d in splits])
        def history(s,period=None,auto_adjust=False):
            if boom: raise RuntimeError(boom)
            return Frame(dates, closes_adj if auto_adjust else closes_raw)
    mod.Ticker=T; return mod

def ts(d): return types.SimpleNamespace(date=lambda: d)

def test_fetch_one_full(monkeypatch):
    divs=[(ts(datetime.date(2026,3,31)),0.81),(ts(datetime.date(2026,6,30)),0.81)]
    raw=[55.0+i for i in range(30)]; adj=[54.0+i*1.1 for i in range(30)]
    monkeypatch.setitem(sys.modules,"yfinance",mk(divs,raw,adj,splits=(datetime.date(2026,1,5),)))
    rows,price,st = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert len(rows)==2 and price==65.0
    assert "price_return_12m_pct" in st and "total_return_12m_pct" in st
    assert st["split_dates"]==["2026-01-05"]

def test_fetch_one_history_error_is_captured(monkeypatch):
    divs=[(ts(datetime.date(2026,6,30)),0.5)]
    monkeypatch.setitem(sys.modules,"yfinance",mk(divs,[1]*30,[1]*30,boom="yahoo down"))
    rows,price,st = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert st["price_error"]=="yahoo down" and len(rows)==1

def test_fetch_one_since_filter(monkeypatch):
    divs=[(ts(datetime.date(2024,1,1)),0.5),(ts(datetime.date(2026,6,30)),0.5)]
    monkeypatch.setitem(sys.modules,"yfinance",mk(divs,[55.0+i for i in range(30)],[55.0+i for i in range(30)]))
    rows,_,_ = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert len(rows)==1 and rows[0]["ex_date"]=="2026-06-30"

def test_fetch_one_sanity_drops_absurd_amount(monkeypatch):
    divs=[(ts(datetime.date(2026,6,30)),99.0)]  # > 25% of price
    monkeypatch.setitem(sys.modules,"yfinance",mk(divs,[55.0+i for i in range(30)],[55.0+i for i in range(30)]))
    rows,_,_ = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert rows==[]

def test_fetch_one_negative_and_zero_dropped(monkeypatch):
    divs=[(ts(datetime.date(2026,6,30)),0.0),(ts(datetime.date(2026,5,30)),-1.0),
          (ts(datetime.date(2026,4,30)),0.4)]
    monkeypatch.setitem(sys.modules,"yfinance",mk(divs,[55.0+i for i in range(30)],[55.0+i for i in range(30)]))
    rows,_,_ = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert len(rows)==1

def test_fetch_one_no_dividends(monkeypatch):
    monkeypatch.setitem(sys.modules,"yfinance",mk([],[55.0+i for i in range(30)],[55.0+i for i in range(30)]))
    rows,price,st = fd.fetch_one("XYZ", datetime.date(2025,1,1))
    assert rows==[] and price==65.0
