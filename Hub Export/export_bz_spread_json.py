#!/usr/bin/env python3
"""
export_bz_spread_json.py — BZ export spread trackers → JSON for the research hub
================================================================================
    python "Hub Export/export_bz_spread_json.py" chicken|pork|beef

Reads the tracker DB and writes bz-<protein>-export-spread.json next to it,
for the Research Dashboard Hub (rTaiShan/research-dashboards), which reads it
from Mindassets after the Dashboard Data Feeder upload.

CONTRACT (schemaVersion 1)
  {
    "schemaVersion": 1,
    "asOf": "2026-10-21",              # newest updated_at in the DB (data, not clock)
    "protein": "chicken",
    "costModel": "grainMargin",        # chicken/pork | "cattleRatio" for beef
    "monthly": [ { "period": "2006-01", "usdKg": 1.23, "fx": 2.27, "brlKg": 2.79,
                   "volTons": 210000.0,
                   "costBrlKg": 1.2, "spread": 1.05 } ],      # beef only
    "weekly":  [ { "start": "2026-10-10", "end": "2026-10-16", "usdKg": ..., "fx": ...,
                   "brlKg": ..., "volTons": ..., "bizDays": 4, "volTonsDaily": ...,
                   "costBrlKg": ..., "spread": ... } ],       # beef only
    "grainMonthly": [ { "period": "2004-08", "corn": 21.3, "soy": 41.0 } ]  # grainMargin only
  }

  grainMargin: the dashboard recomputes the grain cost and spread itself, because
  the analyst can change the corn/soy weights and the lag. grainMonthly holds the
  CEPEA monthly averages (BRL/sc60kg) of days with both prices, as the old page did.
  cattleRatio: cost and spread come from the extractor (CEPEA boi ÷ 15, price ÷ cost).
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

PROTEINS = {
    "chicken": ("Chicken/BZ Tracker/chicken_bz.db", "grainMargin"),
    "pork": ("Pork/BZ Tracker/pork_bz.db", "grainMargin"),
    "beef": ("Beef/BZ Tracker/beef_bz.db", "cattleRatio"),
}


def r6(v):
    return None if v is None else round(v, 6)


def rows(conn, sql):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql).fetchall()]


def export(protein):
    db_rel, cost_model = PROTEINS[protein]
    db_path = ROOT / db_rel
    out_path = db_path.parent / f"bz-{protein}-export-spread.json"
    conn = sqlite3.connect(db_path)
    ratio = cost_model == "cattleRatio"

    monthly = []
    for r in rows(conn, """
        SELECT m.period, m.secex_usd_kg, m.fx, m.secex_brl_kg, m.cepea_r_kg, m.spread,
               s.vol_tons, m.updated_at
        FROM monthly m LEFT JOIN _secex_raw s ON m.year = s.year AND m.month = s.month
        ORDER BY m.year, m.month"""):
        row = {"period": r["period"], "usdKg": r6(r["secex_usd_kg"]), "fx": r6(r["fx"]),
               "brlKg": r6(r["secex_brl_kg"]), "volTons": r6(r["vol_tons"])}
        if ratio:
            row.update(costBrlKg=r6(r["cepea_r_kg"]), spread=r6(r["spread"]))
        monthly.append(row)

    weekly = []
    for r in rows(conn, """
        SELECT start_date, end_date, secex_usd_kg, fx, secex_brl_kg, cepea_r_kg, spread,
               vol_tons, biz_days, vol_tons_daily
        FROM weekly ORDER BY start_date"""):
        row = {"start": r["start_date"], "end": r["end_date"], "usdKg": r6(r["secex_usd_kg"]),
               "fx": r6(r["fx"]), "brlKg": r6(r["secex_brl_kg"]), "volTons": r6(r["vol_tons"]),
               "bizDays": r["biz_days"], "volTonsDaily": r6(r["vol_tons_daily"])}
        if ratio:
            row.update(costBrlKg=r6(r["cepea_r_kg"]), spread=r6(r["spread"]))
        weekly.append(row)

    if not monthly:
        raise SystemExit(f"[EXPORT] {protein}: monthly is empty — refusing to publish")

    as_of = max(r["updated_at"] for r in rows(conn, """
        SELECT MAX(updated_at) AS updated_at FROM monthly
        UNION ALL SELECT MAX(updated_at) FROM weekly""") if r["updated_at"])[:10]

    doc = {"schemaVersion": SCHEMA_VERSION, "asOf": as_of, "protein": protein,
           "costModel": cost_model, "monthly": monthly, "weekly": weekly}
    if not ratio:
        doc["grainMonthly"] = [
            {"period": r["period"], "corn": r6(r["corn"]), "soy": r6(r["soy"])}
            for r in rows(conn, """
                SELECT strftime('%Y-%m', dt) AS period, AVG(corn_brl_sc) AS corn,
                       AVG(soy_brl_sc) AS soy
                FROM _cepea_grain_raw
                WHERE corn_brl_sc IS NOT NULL AND soy_brl_sc IS NOT NULL
                GROUP BY period ORDER BY period""")]
    conn.close()

    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    grain = "" if ratio else f", {len(doc['grainMonthly'])} grain months"
    print(f"[EXPORT] {protein}: {len(monthly)} months, {len(weekly)} weeks{grain}"
          f" → {out_path.name} = {out_path.stat().st_size // 1024} KB (asOf {as_of})")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in PROTEINS:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(PROTEINS)}")
    export(sys.argv[1])
