#!/usr/bin/env python3
"""
fetch_distributions.py  (public-repo edition)
Pull recent per-share distributions for a generic ticker universe and merge them
into data/DISTRIBUTIONS.json.

This runs in a PUBLIC repo so it uses unlimited free Actions minutes. It carries
ONLY public data: ticker + ex-date + per-share amount. No holdings, no dollars, no
account info. The receipt estimation that combines this with private share counts
runs elsewhere, privately.

Source: Yahoo Finance via yfinance (ex-date + per-share amount). No record/pay date
or ROC classification -- those are added downstream from Fidelity activity / 19a-1.

Merge rules: keyed by (ticker, ex_date); confirmed records never overwritten;
amounts above 25% of price dropped as data errors.
"""
import argparse, datetime, json, os, sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TICKERS = os.path.join(HERE, "config", "tickers.txt")
OUT = os.path.join(HERE, "data", "DISTRIBUTIONS.json")
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


def fetch_one(ticker, since):
    import yfinance as yf
    t = yf.Ticker(ticker)
    divs = t.dividends
    last_price = None
    try:
        fi = getattr(t, "fast_info", None)
        if fi:
            last_price = fi.get("last_price") or fi.get("lastPrice")
    except Exception:
        last_price = None
    out = []
    if divs is None or len(divs) == 0:
        return out, last_price
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
    return out, last_price


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    args = ap.parse_args()
    since = datetime.date.today() - datetime.timedelta(days=args.days)

    tickers = read_tickers()
    existing = load_existing()
    idx = {(r["ticker"], r["ex_date"]): r for r in existing["records"]}
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    added = skipped_confirmed = errors = 0
    for tk in tickers:
        try:
            rows, price = fetch_one(tk, since)
        except Exception as e:
            errors += 1
            print(f"  [error] {tk}: {e}", file=sys.stderr)
            continue
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

    records = sorted(idx.values(), key=lambda r: (r["ex_date"], r["ticker"]))
    json.dump({"generated_at": now, "universe_size": len(tickers),
               "record_count": len(records), "records": records},
              open(OUT, "w"), indent=2)
    print(f"Wrote {OUT}: {len(records)} records (+{added} new, "
          f"{skipped_confirmed} confirmed kept, {errors} fetch errors) over {len(tickers)} tickers")


if __name__ == "__main__":
    main()
