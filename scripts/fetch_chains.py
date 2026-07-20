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
import json, os, sys, time, datetime, urllib.request, urllib.error
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

    for t in tickers:
        path = f"data/{t}.json"
        try:
            raw, resolved = fetch(t)
            out = build(t, raw, resolved, fetched_at)
            json.dump(out, open(path, "w"), indent=1)
            if os.path.exists(f"data/{t}.error.json"):
                os.remove(f"data/{t}.error.json")
            report["tickers"][t] = out["quality"]
            print(f"{t}: {out['quality']['contracts']} contracts, flags={out['quality']['flags']}")
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
