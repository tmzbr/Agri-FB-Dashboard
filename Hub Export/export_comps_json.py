#!/usr/bin/env python3
"""
export_comps_json.py — Comps Table (read-only) → hub JSON
=========================================================
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python "Hub Export/export_comps_json.py"

Models keep being uploaded and reviewed in the current site (comps_table.html,
admin only); this script publishes the final table for the hub. It reads
comps_company, comps_estimate and the latest close of comps_price, and does
the page's math here (metrics() in comps_table.html):
  Mkt Cap = price × shares · EV_t = Mkt Cap + net debt_t + minorities_t
  P/E_t = Mkt Cap / net income_t · EV/EBITDA_t = EV_t / EBITDA_t
  FCFE yield_t = FCFE_t / Mkt Cap · upside = target / price − 1
Whatever an admin hid for a company (comps_company.hidden) is left out of the
file, so the public JSON shows exactly what the page shows.

CONTRACT (schemaVersion 1) → Market Watch/Comps Table/comps-table.json
  { schemaVersion, asOf, priceSources, years: [2026..2030],
    companies: [ { ticker, name, currency, sector, order, recommendation,
                   targetLabel, shares, price: { value, date, live },
                   marketCap, target, upside, hasModel,
                   years: { "2026": { label, revenue, ebitda, netIncome, fcfe,
                                      netDebt, minorities, ev, pe, evEbitda, fcfeYield } } } ] }
  asOf = the latest close used. Missing values are null.
"""

import json
import math
import os
import datetime as dt
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "Market Watch/Comps Table/comps-table.json"
YEARS = [2026, 2027, 2028, 2029, 2030]
# Same defaults as the page for companies saved before comps_company.sector existed.
SECTOR_DEFAULT = {
    "ABEV3": ("fnb", 1), "MDIA3": ("fnb", 2), "CAML3": ("fnb", 3), "JBS": ("fnb", 4), "BEEF3": ("fnb", 5), "MBRF3": ("fnb", 6),
    "SLCE3": ("agri", 1), "AGRO3": ("agri", 2), "TTEN3": ("agri", 3), "VITT3": ("agri", 4), "SOJA3": ("agri", 5),
    "SMTO3": ("agri", 6), "JALL3": ("agri", 7), "AGRO": ("agri", 8),
}


def ok(v):
    return v is not None and isinstance(v, (int, float)) and math.isfinite(v)


def num(v):
    return float(v) if ok(v) else None


def rest(url, key, query):
    r = requests.get(f"{url}/rest/v1/{query}", headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    return r.json()


def company(c, est, px):
    hidden = set(c.get("hidden") or [])
    close = px.get(c.get("price_symbol"))
    if close:
        price = {"value": float(close["close"]), "date": close["date"], "live": True}
    elif ok(c.get("model_price")):
        price = {"value": float(c["model_price"]), "date": c.get("model_date"), "live": False}
    else:
        price = None
    shares = num(c.get("shares_mn"))
    mc = price["value"] * shares if price and shares is not None else None
    tp = None if "target" in hidden else num(c.get("target_price"))
    rows = est.get(c["ticker"], {})
    years = {}
    for y in YEARS:
        raw = rows.get(y, {})
        ni, ebitda, fcfe, nd = num(raw.get("net_income")), num(raw.get("ebitda")), num(raw.get("fcfe")), num(raw.get("net_debt"))
        ev = mc + nd + (num(raw.get("minorities")) or 0) if mc is not None and nd is not None else None
        years[str(y)] = {
            "label": raw.get("period_label"),
            "revenue": None if "revenue" in hidden else num(raw.get("revenue")),
            "ebitda": None if "ebitda" in hidden else ebitda,
            "netIncome": None if "net_income" in hidden else ni,
            "fcfe": fcfe, "netDebt": nd, "minorities": num(raw.get("minorities")), "ev": ev,
            # Multiples use the raw figures: hiding revenue must not blank the P/E.
            "pe": mc / ni if "pe" not in hidden and mc is not None and ni else None,
            "evEbitda": ev / ebitda if "ev_ebitda" not in hidden and ev is not None and ebitda else None,
            "fcfeYield": fcfe / mc if "fcfe_yld" not in hidden and mc and fcfe is not None else None,
        }
    sector, order = (c["sector"], c.get("display_order")) if c.get("sector") else SECTOR_DEFAULT.get(c["ticker"], ("other", c.get("display_order") or 100))
    return {
        "ticker": c["ticker"], "name": c.get("name"), "currency": c.get("currency") or "BRL", "sector": sector, "order": order,
        "recommendation": c.get("recommendation"), "targetLabel": c.get("target_label"), "shares": shares,
        "price": price, "marketCap": mc, "target": tp,
        "upside": tp / price["value"] - 1 if price and tp is not None else None,
        "hasModel": c["ticker"] in est, "years": years,
    }


def main():
    url, key = os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    since = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    companies = rest(url, key, "comps_company?select=*&order=display_order,ticker")
    estimates = rest(url, key, "comps_estimate?select=*")
    prices = rest(url, key, f"comps_price?select=price_symbol,date,close,source&date=gte.{since}&order=date.desc")
    est = {}
    for r in estimates:
        est.setdefault(r["ticker"], {})[r["year"]] = r
    px = {}
    for r in prices:  # newest first: keep the first close per symbol
        px.setdefault(r["price_symbol"], r)
    if not companies:
        raise SystemExit("[EXPORT] comps: no companies — refusing to publish")
    doc = {
        "schemaVersion": 1,
        "asOf": max((p["date"] for p in px.values()), default=None),
        "priceSources": sorted({p.get("source") for p in px.values() if p.get("source")}),
        "years": YEARS,
        "companies": [company(c, est, px) for c in companies],
    }
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] comps: {len(companies)} companies, {len(est)} with a model → {OUT.name} = "
          f"{OUT.stat().st_size // 1024} KB (asOf {doc['asOf']})")


if __name__ == "__main__":
    main()
