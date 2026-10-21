#!/usr/bin/env python3
"""
export_bz_domestic_json.py — Chicken BZ Domestic → JSON for the research hub
===========================================================================
Reads Chicken/BZ Tracker/chicken_bz_domestic.db and writes
bz-chicken-domestic-spread.json next to it for the Research Dashboard Hub.

CONTRACT (schemaVersion 1)
  {
    "schemaVersion": 1,
    "asOf":   "2026-10-20",                       # last CEPEA chicken quote
    "chicken": [["2004-01-02", 1.85], ...],       # CEPEA frango resfriado SP, BRL/kg
    "grain":   [["2004-08-02", 21.3, 41.0], ...]  # CEPEA corn, soy PNA, BRL/sc60kg
  }

  Raw daily quotes only (days with both grain prices, as the old page read
  them): the dashboard builds the daily/weekly/monthly/quarterly spread itself,
  because the analyst changes the corn/soy weights and the lag.
"""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "Chicken/BZ Tracker/chicken_bz_domestic.db"
OUT_PATH = DB_PATH.parent / "bz-chicken-domestic-spread.json"


def main():
    conn = sqlite3.connect(DB_PATH)
    chicken = [[dt, round(v, 6)] for dt, v in conn.execute(
        "SELECT dt, brl_kg FROM _cepea_frango_raw WHERE brl_kg IS NOT NULL ORDER BY dt")]
    grain = [[dt, round(c, 6), round(s, 6)] for dt, c, s in conn.execute(
        "SELECT dt, corn_brl_sc, soy_brl_sc FROM _cepea_grain_raw "
        "WHERE corn_brl_sc IS NOT NULL AND soy_brl_sc IS NOT NULL ORDER BY dt")]
    conn.close()
    if not chicken:
        raise SystemExit("[EXPORT] chicken domestic: no CEPEA chicken quotes — refusing to publish")
    doc = {"schemaVersion": 1, "asOf": chicken[-1][0], "chicken": chicken, "grain": grain}
    OUT_PATH.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] chicken domestic: {len(chicken)} chicken days, {len(grain)} grain days"
          f" → {OUT_PATH.name} = {OUT_PATH.stat().st_size // 1024} KB (asOf {doc['asOf']})")


if __name__ == "__main__":
    main()
