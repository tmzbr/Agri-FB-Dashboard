#!/usr/bin/env python3
"""
export_cattle_cycle_json.py — cattle_cycle.db → cattle-cycle.json
=================================================================
Publishes the Cattle Cycle data as one JSON file for the Research Dashboard
Hub (rTaiShan/research-dashboards), which reads it from Mindassets after the
upload through the Dashboard Data Feeder. The hub never sees the SQLite file.

CONTRACT (schemaVersion 1)
  {
    "schemaVersion": 1,
    "asOf":   "2026-09-29",          # last time the extractor rewrote any data
    "countries": {
      "br": {
        "source":      "IBGE — Pesquisa Trimestral do Abate, SIDRA table 1092",
        "dataThrough": "2026-06",
        "categories":  ["cows", "heifers", "bulls", "steers"],
        "female":      ["cows", "heifers"],
        "male":        ["bulls", "steers"],
        "hasCarcass":  true,
        "rows": [
          { "period": "1997-01",
            "heads":     {"cows": 123, "heifers": 45, "bulls": 678, "steers": 90},
            "carcassKg": {"cows": 1.2e7, ...},          # br only
            "femaleShare": 0.4321,
            "estimated": false }                        # see below
        ]
      },
      "us": { ... "categories": ["cowsHeifers", "bulls", "steers"],
                  "hasCarcass": false, rows without carcassKg }
    }
  }

  - Heads and carcass kg are raw numbers (heads, kg); the dashboard divides by
    1,000 for display and computes kg/head, the 12-month rolling share and the
    quarterly/annual aggregation (Σ females ÷ Σ total) itself.
  - Only months the source published are listed. Gaps (U.S. 1958–1970) are
    simply absent; the dashboard fills them with empty months.
  - "estimated" = br: heifers imputed as the residual (novilhas_imputed);
    us: 1982 quarterly total spread over its 3 months (quarter_split).
    The table marks these rows with "*".
  - asOf comes from the data (not the clock), so a run with nothing new
    rewrites a byte-identical file and the workflow makes no commit.
"""

import json
import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "cattle_cycle.db"
OUT_PATH = HERE / "cattle-cycle.json"

SCHEMA_VERSION = 1

# country → (table, source, flag column, [(json key, db suffix)], has carcass)
COUNTRIES = {
    "br": ("br_monthly",
           "IBGE — Pesquisa Trimestral do Abate de Animais, SIDRA table 1092",
           "novilhas_imputed",
           [("cows", "vacas"), ("heifers", "novilhas"),
            ("bulls", "bois"), ("steers", "novilhos")],
           True),
    "us": ("us_monthly",
           "USDA NASS — Quick Stats, Livestock Slaughter (commercial FI)",
           "quarter_split",
           [("cowsHeifers", "cows_heifers"), ("bulls", "bulls"),
            ("steers", "steers")],
           False),
}
FEMALE = {"br": ["cows", "heifers"], "us": ["cowsHeifers"]}
MALE = {"br": ["bulls", "steers"], "us": ["bulls", "steers"]}


def num(v):
    """Heads and kg are whole numbers in the source; keep null as null."""
    return None if v is None else int(round(v))


def export_country(conn, key):
    table, source, flag, cats, carcass = COUNTRIES[key]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY period").fetchall()
    if not rows:
        raise SystemExit(f"[EXPORT] {table} is empty — refusing to publish")
    out = []
    for r in rows:
        row = {
            "period": r["period"],
            "heads": {k: num(r[f"heads_{c}"]) for k, c in cats},
        }
        if carcass:
            row["carcassKg"] = {k: num(r[f"carcass_kg_{c}"]) for k, c in cats}
        share = r["female_share"]
        row["femaleShare"] = None if share is None else round(share, 6)
        row["estimated"] = bool(r[flag])
        out.append(row)
    as_of = max(r["updated_at"] for r in rows if r["updated_at"])[:10]
    return as_of, {
        "source": source,
        "dataThrough": rows[-1]["period"],
        "categories": [k for k, _ in cats],
        "female": FEMALE[key],
        "male": MALE[key],
        "hasCarcass": carcass,
        "rows": out,
    }


def main():
    conn = sqlite3.connect(DB_PATH)
    countries, dates = {}, []
    for key in COUNTRIES:
        as_of, countries[key] = export_country(conn, key)
        dates.append(as_of)
    conn.close()
    doc = {"schemaVersion": SCHEMA_VERSION, "asOf": max(dates), "countries": countries}
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n",
                        encoding="utf-8")
    for key, c in countries.items():
        print(f"[EXPORT] {key}: {len(c['rows'])} months, "
              f"{c['rows'][0]['period']} → {c['dataThrough']}")
    print(f"✓ {OUT_PATH.name} = {OUT_PATH.stat().st_size // 1024} KB (asOf {doc['asOf']})")


if __name__ == "__main__":
    main()
