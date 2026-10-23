#!/usr/bin/env python3
"""
export_cost_model_json.py — Ambev COGS inflation tracker → hub JSON
===================================================================
    python "Hub Export/export_cost_model_json.py"

Reads the same tables ambev_cogs_dashboard.html queries through sql.js and
writes only what that page uses. Every calculation (annual and monthly
averages, YoY in BRL, weights, the revision history) stays in the browser,
as in the legacy page, because the weights and macro inputs are editable.

CONTRACT (schemaVersion 1) → Cost Model/cost-model.json
  { schemaVersion, asOf,
    daily: { dates: [...],                       # union of all trading days
             aluminum_spot, aluminum_future,     # LME, USD/t
             corn, wheat,                        # CBOT, USD cents/bu
             barley,                             # NCDEX INR → USD via BRL crosses
             fx_market },                        # PTAX USD/BRL
             (each an array aligned with dates, null where missing)
    fxGuidance:   { year: USD/BRL },            # company-reported FX
    igpmRealized: { year: decimal },
    reported:     { year: decimal },            # Ambev COGS/hl growth
    igpmFocus:    { referenceYear: { d: [...], v: [...] } } }  # median, in %
  asOf = latest PTAX date (the legacy header's "Latest realized data").
  igpmFocus keeps, per reference year, the rows from the year before it on,
  plus the last row before that: all the revision chart can look up.
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "Cost Model/commodities_prices.db"
OUT = ROOT / "Cost Model/cost-model.json"


def num(v, digits=None):
    if v is None:
        return None
    return round(v, digits) if digits is not None else v


def main():
    con = sqlite3.connect(DB)
    q = lambda sql: con.execute(sql).fetchall()

    series = {
        "aluminum_spot": dict(q("SELECT date, spot FROM aluminum_lme")),
        "aluminum_future": dict(q("SELECT date, future FROM aluminum_lme")),
        "corn": dict(q("SELECT date, close FROM corn_cbot")),
        "wheat": dict(q("SELECT date, close FROM wheat_cbot")),
        # Same join as the legacy page: INR close ÷ (USD/BRL ÷ INR/BRL) = USD.
        "barley": {d: num(v, 8) for d, v in q("""
            SELECT b.date, b.close/(f1.rate/f2.rate)
            FROM barley_ncdex b
            JOIN fx_usdbrl f1 ON f1.date=b.date
            JOIN fx_inrbrl f2 ON f2.date=b.date""")},
        "fx_market": dict(q("SELECT date, rate FROM fx_usdbrl")),
    }
    dates = sorted(set().union(*series.values()))
    daily = {"dates": dates, **{k: [s.get(d) for d in dates] for k, s in series.items()}}

    focus = {}
    for d, ref, v in q("SELECT date, reference_year, expectation_median FROM igpm_focus ORDER BY reference_year, date"):
        focus.setdefault(ref, []).append((d, v))
    igpm_focus = {}
    for ref, rows in focus.items():
        start = f"{ref - 1}-01-01"
        first = next((i for i, (d, _) in enumerate(rows) if d >= start), len(rows))
        kept = rows[max(0, first - 1):]
        igpm_focus[str(ref)] = {"d": [d for d, _ in kept], "v": [v for _, v in kept]}

    as_of = q("SELECT MAX(date) FROM fx_usdbrl")[0][0]
    doc = {
        "schemaVersion": 1,
        "asOf": as_of,
        "daily": daily,
        "fxGuidance": {str(y): r for y, r in q("SELECT effective_year, rate FROM fx_usdbrl_guidance ORDER BY 1")},
        "igpmRealized": {str(y): v for y, v in q("SELECT year, value FROM igpm_realized ORDER BY 1")},
        "reported": {str(y): v for y, v in q("SELECT effective_year, value FROM cogs_inflation_reported ORDER BY 1")},
        "igpmFocus": igpm_focus,
    }
    con.close()

    if not dates or not igpm_focus:
        raise SystemExit("[EXPORT] cost model: no prices or no IGP-M Focus — refusing to publish")
    OUT.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] cost model: {len(dates)} days, {len(igpm_focus)} Focus years → {OUT.name} = "
          f"{OUT.stat().st_size // 1024} KB (asOf {as_of})")


if __name__ == "__main__":
    main()
