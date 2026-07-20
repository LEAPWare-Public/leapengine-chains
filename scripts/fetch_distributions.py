#!/usr/bin/env python3
"""
fetch_distributions.py  (public-repo edition) -- v2, Jul-20-2026

v1 DEFECTS FIXED:
  1. Workflow called this with --days 120. Four months of history. Trailing-12-month
     distribution yield was therefore IMPOSSIBLE to compute, and any consumer that
     tried got numbers ~3x too low (O read 1.67% against a true 5.00%). Default is
     now 800 days and the workflow passes --days 800.
  2. 'frequency' was never populated (always null), forcing consumers to guess the
     payment cadence from ex-date spacing. Now inferred and stored.
  3. Price history was fetched for a sanity check and then DISCARDED. A7-R (the
     NAV-erosion audit) needs 12-month price and total return alongside the
     distribution yield, so A7-R could never run. Now captured.
  4. No computed output. Every consumer re-derived yields by hand. Now emits
     data/YIELDS.json with the A7-R verdict precomputed and coverage declared.

Carries ONLY public data: ticker, ex-date, per-share amount, price/return series.
No holdings, no dollars, no account info.
Merge rules: keyed by (ticker, ex_date); confirmed records never overwritten;
amounts above 25% of price dropped as data errors.
"""
import argparse, datetime, json, os, statistics, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS = os.path.join(HERE, "config", "tickers.txt")
OUT = os.path.join(HERE, "data", "DISTRIBUTIONS.json")
YIELDS = os.path.join(HERE, "data", "YIELDS.json")
SANITY_MAX_FRACTION_OF_PRICE = 0.25


def read_tickers():
    out = []
    with open(TICKERS) as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for tok in line.replace(",", " ").split():
                out.append(tok.strip().upper())
    return sorted(set(out))


def load_existing():
    if os.path.exists(OUT):
        with open(OUT) as fh:
            return json.load(fh)
    return {"generated_at": None, "records": []}


def infer_frequency(dates):
    """Payments per year from median ex-date spacing."""
    if len(dates) < 2:
        return None, None
    ds = sorted(dates)
    gaps = [(ds[i + 1] - ds[i]).days for i in range(len(ds) - 1)]
    g = statistics.median(gaps)
    if g < 11:   return 52, g
    if g < 45:   return 12, g
    if g < 135:  return 4, g
    if g < 250:  return 2, g
    return 1, g


def fetch_one(ticker, since):
    """Returns (dividend rows, last_price, price_stats)."""
    import yfinance as yf
    t = yf.Ticker(ticker)

    last_price = None
    try:
        fi = getattr(t, "fast_info", None)
        if fi:
            last_price = fi.get("last_price") or fi.get("lastPrice")
    except Exception:  # noqa: BLE001
        pass

    # --- price + total return over the trailing 12 months (A7-R inputs) ---
    stats = {}
    try:
        raw = t.history(period="13mo", auto_adjust=False)   # price only
        adj = t.history(period="13mo", auto_adjust=True)    # distributions reinvested
        if raw is not None and len(raw) > 20:
            cutoff = datetime.date.today() - datetime.timedelta(days=365)
            r = raw[raw.index.date >= cutoff]
            a = adj[adj.index.date >= cutoff] if adj is not None else None
            if len(r) > 20:
                p0, p1 = float(r["Close"].iloc[0]), float(r["Close"].iloc[-1])
                stats["price_start"] = round(p0, 4)
                stats["price_end"] = round(p1, 4)
                stats["price_return_12m_pct"] = round((p1 / p0 - 1) * 100, 2)
                stats["price_days"] = int(len(r))
                if last_price is None:
                    last_price = p1
            if a is not None and len(a) > 20:
                a0, a1 = float(a["Close"].iloc[0]), float(a["Close"].iloc[-1])
                stats["total_return_12m_pct"] = round((a1 / a0 - 1) * 100, 2)
    except Exception as e:  # noqa: BLE001
        stats["price_error"] = str(e)

    out = []
    try:
        divs = t.dividends
    except Exception as e:  # noqa: BLE001
        return out, last_price, stats
    if divs is None or len(divs) == 0:
        return out, last_price, stats
    for ex_ts, amt in divs.items():
        ex_date = ex_ts.date()
        if ex_date < since:
            continue
        amt = float(amt)
        if amt <= 0:
            continue
        if last_price and amt > SANITY_MAX_FRACTION_OF_PRICE * last_price:
            print(f"  [skip] {ticker} {ex_date} amt {amt} vs price {last_price}", file=sys.stderr)
            continue
        out.append({"ex_date": ex_date.isoformat(), "amount": round(amt, 6)})
    return out, last_price, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=800)
    args = ap.parse_args()
    since = datetime.date.today() - datetime.timedelta(days=args.days)
    today = datetime.date.today()
    yr_ago = today - datetime.timedelta(days=365)

    tickers = read_tickers()
    existing = load_existing()
    idx = {(r["ticker"], r["ex_date"]): r for r in existing["records"]}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    added = skipped_confirmed = errors = 0
    prices, pstats = {}, {}
    for tk in tickers:
        try:
            rows, price, st = fetch_one(tk, since)
        except Exception as e:  # noqa: BLE001
            errors += 1
            print(f"  [error] {tk}: {e}", file=sys.stderr)
            continue
        if price:
            prices[tk] = round(float(price), 4)
        if st:
            pstats[tk] = st
        for row in rows:
            key = (tk, row["ex_date"])
            if key in idx:
                if idx[key].get("status") == "confirmed":
                    skipped_confirmed += 1
                    continue
                idx[key]["amount_per_share"] = row["amount"]
                idx[key]["last_seen"] = now
                continue
            idx[key] = {
                "ticker": tk, "ex_date": row["ex_date"],
                "record_date": None, "pay_date": None,
                "amount_per_share": row["amount"], "frequency": None,
                "source": "yfinance", "status": "estimated", "roc_status": "unknown",
                "first_seen": now, "last_seen": now,
            }
            added += 1

    # ---- stamp inferred frequency onto every record ----
    per = {}
    for r in idx.values():
        per.setdefault(r["ticker"], []).append(r)
    for tk, rs in per.items():
        freq, gap = infer_frequency([datetime.date.fromisoformat(r["ex_date"]) for r in rs])
        for r in rs:
            r["frequency"] = freq

    records = sorted(idx.values(), key=lambda r: (r["ex_date"], r["ticker"]))
    json.dump({"generated_at": now, "universe_size": len(tickers),
               "record_count": len(records), "lookback_days": args.days,
               "records": records}, open(OUT, "w"), indent=2)

    # ---- computed yields + A7-R verdicts ----
    ylds = {}
    for tk, rs in per.items():
        ds = sorted(datetime.date.fromisoformat(r["ex_date"]) for r in rs)
        ttm = [r for r in rs if datetime.date.fromisoformat(r["ex_date"]) >= yr_ago]
        ttm_sum = round(sum(r["amount_per_share"] for r in ttm), 6)
        freq, gap = infer_frequency(ds)
        px = prices.get(tk)
        st = pstats.get(tk, {})
        cov = (ds[-1] - ds[0]).days if len(ds) > 1 else 0
        full = cov >= 350 and len(ttm) >= 1

        ttm_yield = round(ttm_sum / px * 100, 3) if (px and ttm_sum) else None
        # run-rate yield: latest payment annualised. Usable when history is short.
        rr = None
        if px and freq and rs:
            latest = max(rs, key=lambda r: r["ex_date"])["amount_per_share"]
            rr = round(latest * freq / px * 100, 3)

        tr = st.get("total_return_12m_pct")
        pr = st.get("price_return_12m_pct")
        flags = []
        if not full:
            flags.append("PARTIAL_HISTORY")
        if ttm_yield and len(ttm) < (freq or 4) * 0.75:
            flags.append("TTM_UNDERCOUNTED")
        # A7-R: is the distribution economically earned?
        verdict = None
        if tr is not None and ttm_yield:
            if tr < 0:
                verdict = "ERODING"          # total return negative despite paying
            elif tr < ttm_yield:
                verdict = "UNDEREARNING"     # paying more than it makes
            else:
                verdict = "EARNED"
            if pr is not None and pr < -10:
                flags.append("NAV_DOWN_OVER_10PCT")
        ylds[tk] = {
            "price": px, "frequency": freq, "median_gap_days": gap,
            "ttm_distributions_per_share": ttm_sum, "ttm_payment_count": len(ttm),
            "ttm_yield_pct": ttm_yield, "run_rate_yield_pct": rr,
            "history_days": cov, "full_12m_history": full,
            "price_return_12m_pct": pr, "total_return_12m_pct": tr,
            "a7r_verdict": verdict, "flags": flags,
        }

    json.dump({"generated_at": now, "as_of": today.isoformat(),
               "method": "ttm = sum of ex-dates in trailing 365d / price. "
                         "run_rate = latest payment x inferred frequency / price. "
                         "a7r: EARNED if 12m total return >= ttm yield; "
                         "UNDEREARNING if positive but below it; ERODING if negative.",
               "tickers": ylds}, open(YIELDS, "w"), indent=2)

    nofull = sum(1 for v in ylds.values() if not v["full_12m_history"])
    print(f"Wrote {OUT}: {len(records)} records (+{added} new, {skipped_confirmed} confirmed kept, "
          f"{errors} fetch errors) over {len(tickers)} tickers, lookback {args.days}d")
    print(f"Wrote {YIELDS}: {len(ylds)} tickers, {nofull} still lacking full 12m history")


if __name__ == "__main__":
    main()
