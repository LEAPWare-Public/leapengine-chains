#!/usr/bin/env python3
"""
LEAPEngine chain fetcher (public repo, D1 path).

v2 - Jul-20-2026. Fixes the "phantom staleness" defect:
  v1 wrote feed_timestamp = raw['timestamp'] or data['last_trade_time'], a
  CBOE-supplied field with inconsistent semantics across symbols. It was NOT
  the fetch time. Consumers (correctly) read an old value as "the pipeline is
  frozen" and refused to trade names whose data was in fact current.
  v2 records fetched_at_utc explicitly and emits machine-readable quality flags.
"""
import json, math, os, sys, time, datetime, urllib.request, urllib.error
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    ET = None

BLOCKING_FLAGS = {"NO_CONTRACTS", "NO_SPOT", "NO_GREEKS", "NO_BIDS",
                  "CBOE_TS_OVER_24H", "SUSPECT_STALE"}


def _num(v):
    """CBOE occasionally returns numerics as strings. Never crash a whole ticker."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def market_date(dt_utc):
    """Trading date in US/Eastern. A UTC-date rollover at 20:00 ET must not
    advance the expiry calendar and silently zero out DTE."""
    if ET is None:
        return (dt_utc - datetime.timedelta(hours=4)).date()
    return dt_utc.replace(tzinfo=datetime.timezone.utc).astimezone(ET).date()

UA = {"User-Agent": "Mozilla/5.0 (LEAPEngine chain-bot)"}
BASE = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
TIMEOUT, RETRIES = 30, 3
STALE_HOURS = float(os.environ.get("STALE_HOURS", "6"))   # CBOE age that triggers fallback
RISK_FREE = float(os.environ.get("RISK_FREE", "0.04"))


def _ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_delta(spot, strike, dte_days, iv, cp, q=0.0, r=RISK_FREE):
    """Black-Scholes delta. yfinance supplies IV but no greeks; without delta the
    A16 band cannot be applied and the whole chain is unusable. q = continuous
    dividend yield, material on 5%-yielding REITs."""
    try:
        if not (spot and strike and iv) or dte_days is None or dte_days <= 0 or iv <= 0:
            return None
        T = dte_days / 365.0
        d1 = (math.log(spot / strike) + (r - q + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
        d = math.exp(-q * T) * _ncdf(d1)
        return round(d if cp == "C" else d - math.exp(-q * T), 4)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None


def load_div_yields():
    """Use our own YIELDS.json for q. Absent -> q=0 (slightly overstates call delta
    on high yielders, which is the conservative direction for covered calls)."""
    try:
        with open("data/YIELDS.json") as fh:
            return {k: (v.get("ttm_yield_pct") or 0) / 100.0
                    for k, v in json.load(fh)["tickers"].items()}
    except Exception:  # noqa: BLE001
        return {}


def fetch_yf(ticker, fetched_at, div_yields=None):
    """Fallback source. Returns a payload shaped like build() output."""
    import yfinance as yf
    tk = yf.Ticker(ticker)
    fi = getattr(tk, "fast_info", None) or {}
    spot = _num(fi.get("last_price") or fi.get("lastPrice"))
    prev = _num(fi.get("previous_close") or fi.get("previousClose"))
    if not spot:
        raise RuntimeError("yfinance: no spot")
    q = (div_yields or {}).get(ticker, 0.0)
    today = market_date(fetched_at)
    out = {"ticker": ticker, "resolved_symbol": ticker, "source": "yfinance",
           "fetched_at_utc": fetched_at.isoformat(timespec="seconds") + "Z",
           "cboe_timestamp": None, "spot": spot, "prev_close": prev,
           "quality": {}, "options": []}
    greeks = bids = 0
    for exp in (tk.options or []):
        expd = datetime.date.fromisoformat(exp)
        dte = (expd - today).days
        if not (0 < dte <= 75):
            continue
        ch = tk.option_chain(exp)
        for cp, frame in (("C", ch.calls), ("P", ch.puts)):
            for row in frame.itertuples():
                strike = _num(getattr(row, "strike", None))
                if not strike or not (0.70 * spot <= strike <= 1.40 * spot):
                    continue
                iv = _num(getattr(row, "impliedVolatility", None))
                bid = _num(getattr(row, "bid", None))
                delta = bs_delta(spot, strike, dte, iv, cp, q=q)
                if delta is not None and delta != 0:
                    greeks += 1
                if bid is not None and bid > 0:
                    bids += 1
                out["options"].append({
                    "type": cp, "exp": exp, "dte": dte, "strike": strike,
                    "bid": bid, "ask": _num(getattr(row, "ask", None)),
                    "oi": _num(getattr(row, "openInterest", None)),
                    "volume": _num(getattr(row, "volume", None)),
                    "iv": iv, "delta": delta, "delta_source": "black_scholes",
                    "last_trade_time": None})
    n = len(out["options"])
    flags = ["SOURCE_YFINANCE", "DELTA_COMPUTED"]
    if n == 0:
        flags.append("NO_CONTRACTS")
    if greeks == 0 and n:
        flags.append("NO_GREEKS")
    if bids == 0 and n:
        flags.append("NO_BIDS")
    out["quality"] = {"contracts": n, "with_greeks": greeks, "with_bids": bids,
                      "cboe_ts_age_hours": None, "flags": flags,
                      "tradable": not (BLOCKING_FLAGS & set(flags)),
                      "blocked_by": sorted(BLOCKING_FLAGS & set(flags)),
                      "dividend_yield_used": q}
    return out


def fetch(symbol):
    """Try plain symbol, then underscore-prefixed (CBOE index convention)."""
    last = None
    for cand in (symbol, "_" + symbol):
        for attempt in range(RETRIES):
            try:
                req = urllib.request.Request(BASE.format(cand), headers=UA)
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    return json.load(r), cand
            except urllib.error.HTTPError as e:
                last = f"HTTP {e.code}"
                if e.code == 404:
                    break                      # wrong symbol form, try next
                time.sleep(2 * (attempt + 1))
            except Exception as e:             # noqa: BLE001
                last = str(e)
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(last or "unknown fetch failure")


def build(ticker, raw, resolved, fetched_at):
    d = raw.get("data", {}) or {}
    spot = _num(d.get("current_price")) or _num(d.get("close"))
    prev = _num(d.get("prev_day_close"))
    cboe_ts = raw.get("timestamp") or d.get("last_trade_time")

    out = {
        "ticker": ticker,
        "resolved_symbol": resolved,
        "fetched_at_utc": fetched_at.isoformat(timespec="seconds") + "Z",
        "cboe_timestamp": cboe_ts,
        "spot": spot,
        "prev_close": prev,
        "quality": {},
        "options": [],
    }

    today = market_date(fetched_at)
    greeks = bids = 0
    seen = set()
    for o in d.get("options", []) or []:
        sym = o.get("option", "")
        core = sym[len(resolved.lstrip("_")):]
        try:
            expd = datetime.date(int("20" + core[0:2]), int(core[2:4]), int(core[4:6]))
            cp = core[6]
            strike = int(core[7:]) / 1000.0
        except Exception:  # noqa: BLE001
            continue
        if not spot:
            continue
        dte = (expd - today).days
        if not (0 < dte <= 75 and 0.70 * spot <= strike <= 1.40 * spot):
            continue
        key = (cp, expd, strike)
        if key in seen:
            continue
        seen.add(key)
        delta = _num(o.get("delta"))
        bid = _num(o.get("bid"))
        if delta is not None and delta != 0:
            greeks += 1
        if bid is not None and bid > 0:
            bids += 1
        out["options"].append({
            "type": cp, "exp": expd.isoformat(), "dte": dte, "strike": strike,
            "bid": bid, "ask": _num(o.get("ask")),
            "oi": _num(o.get("open_interest")), "volume": _num(o.get("volume")),
            "iv": _num(o.get("iv")), "delta": delta,
            "last_trade_time": o.get("last_trade_time"),
        })

    n = len(out["options"])
    flags = []
    if n == 0:
        flags.append("NO_CONTRACTS")
    if spot is None:
        flags.append("NO_SPOT")
    if spot is not None and prev is not None and spot == prev:
        flags.append("SPOT_EQUALS_PREV_CLOSE")     # possible stale CBOE payload
    if n and greeks == 0:
        flags.append("NO_GREEKS")                  # A16 delta gate not adjudicable
    elif n and greeks / n < 0.5:
        flags.append("SPARSE_GREEKS")
    if n and bids == 0:
        flags.append("NO_BIDS")

    age = None
    if cboe_ts:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                age = round((fetched_at - datetime.datetime.strptime(cboe_ts[:19], fmt))
                            .total_seconds() / 3600.0, 2)
                break
            except ValueError:
                continue
    if age is not None and age > 24:
        flags.append("CBOE_TS_OVER_24H")
    if "SPOT_EQUALS_PREV_CLOSE" in flags and (age is None or age > 4):
        # frozen payload signature: price never moved AND the stamp is old
        flags.append("SUSPECT_STALE")
    if age is None:
        flags.append("NO_CBOE_TIMESTAMP")

    out["quality"] = {
        "contracts": n,
        "with_greeks": greeks,
        "with_bids": bids,
        "cboe_ts_age_hours": age,
        "flags": flags,
        "tradable": not (BLOCKING_FLAGS & set(flags)),
        "blocked_by": sorted(BLOCKING_FLAGS & set(flags)),
    }
    return out


def main():
    tickers = [t.strip().upper() for t in os.environ.get("TICKERS", "").split(",") if t.strip()]
    if not tickers:
        print("no tickers supplied", file=sys.stderr)
        return 1
    os.makedirs("data", exist_ok=True)
    fetched_at = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    report = {"run_utc": fetched_at.isoformat(timespec="seconds") + "Z", "tickers": {}}
    div_yields = load_div_yields()

    for t in tickers:
        path = f"data/{t}.json"
        try:
            out = None
            cboe_err = None
            try:
                raw, resolved = fetch(t)
                out = build(t, raw, resolved, fetched_at)
                out["source"] = "cboe"
            except Exception as e:  # noqa: BLE001
                cboe_err = str(e)

            age = (out or {}).get("quality", {}).get("cboe_ts_age_hours")
            need_fb = (out is None
                       or not out["quality"]["tradable"]
                       or (age is not None and age > STALE_HOURS))
            if need_fb:
                try:
                    alt = fetch_yf(t, fetched_at, div_yields)
                    if alt["quality"]["contracts"]:
                        if out is not None:
                            alt["cboe_timestamp"] = out.get("cboe_timestamp")
                            alt["quality"]["flags"].append("CBOE_FALLBACK_USED")
                            alt["quality"]["cboe_age_at_fallback"] = age
                        out = alt
                except Exception as e:  # noqa: BLE001
                    print(f"{t}: fallback failed - {e}", file=sys.stderr)
                    if out is not None:
                        out["quality"]["flags"].append("FALLBACK_FAILED")
            if out is None:
                raise RuntimeError(cboe_err or "no source available")
            json.dump(out, open(path, "w"), indent=1)
            if os.path.exists(f"data/{t}.error.json"):
                os.remove(f"data/{t}.error.json")
            report["tickers"][t] = dict(out["quality"], source=out.get("source", "cboe"))
            print(f"{t}: {out['quality']['contracts']} contracts via "
                  f"{out.get('source','cboe')}, flags={out['quality']['flags']}")
        except Exception as e:  # noqa: BLE001
            # NEVER clobber last-known-good data with an error stub.
            json.dump({"ticker": t, "fetched_at_utc": fetched_at.isoformat() + "Z",
                       "error": str(e)}, open(f"data/{t}.error.json", "w"), indent=1)
            report["tickers"][t] = {"flags": ["FETCH_FAILED"], "error": str(e), "tradable": False}
            print(f"{t}: FETCH FAILED - {e} (prior data/{t}.json preserved)", file=sys.stderr)
        time.sleep(0.4)

    json.dump(report, open("data/_run_report.json", "w"), indent=1)
    bad = [t for t, q in report["tickers"].items() if not q.get("tradable")]
    if bad:
        print(f"\nNOT TRADABLE THIS RUN: {', '.join(bad)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
