#!/usr/bin/env python3
"""
export_wasde_json.py — WASDE tracker → hub JSON, one file per commodity
=======================================================================
    python "Hub Export/export_wasde_json.py"

wasde.html runs SQL on the whole wasde.db (22 MB) in the browser. The hub
reads one small file per commodity instead and runs the same lookups on it.
Facts keep the table's row order: a few lookups take "the first row" (the
unit of a measure, the World ending-stocks unit of a year), as SQL did.

CONTRACT (schemaVersion 1) → Wasde/wasde-{soybean,corn,cotton}.json
  { schemaVersion, asOf, commodity, latest: { wasde, title },
    reports: { wasde_number: report_title },
    dict:    { regions, years, attrs, units, flags },          # string tables
    facts:   [ [wasde_number, region, year, attr, unit, flag, value], ... ],
    psd:     [ [region, year, attr, value], ... ] }            # USDA PSD, latest
  Fact and PSD fields other than wasde_number/value are indexes into dict.
  asOf = "YYYY-MM" of the latest report (ingestion_log; release_date is empty).
"""

import json
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "Wasde/wasde.db"
FILES = {"Oilseed, Soybean": "wasde-soybean.json", "Corn": "wasde-corn.json", "Cotton": "wasde-cotton.json"}


class Dict:
    def __init__(self):
        self.items, self.index = [], {}

    def __call__(self, value):
        if value not in self.index:
            self.index[value] = len(self.items)
            self.items.append(value)
        return self.index[value]


def main():
    con = sqlite3.connect(DB)
    for commodity, name in FILES.items():
        regions, years, attrs, units, flags = Dict(), Dict(), Dict(), Dict(), Dict()
        facts, reports = [], {}
        for wn, title, region, year, attr, unit, flag, value in con.execute(
                """SELECT wasde_number, report_title, region, market_year, attribute, unit, proj_est_flag, value
                   FROM wasde_facts WHERE commodity=? ORDER BY rowid""", (commodity,)):
            reports[str(wn)] = title
            facts.append([wn, regions(region), years(year), attrs(attr), units(unit), flags(flag or ""), value])
        psd = [[regions(r), years(y), attrs(a), v] for r, y, a, v in con.execute(
            "SELECT region, market_year, attribute, value FROM psd_facts WHERE commodity=? ORDER BY rowid", (commodity,))]
        latest = max((int(k) for k in reports), default=None)
        row = con.execute("SELECT year, month FROM ingestion_log WHERE wasde_number=?", (latest,)).fetchone()
        as_of = f"{row[0]}-{row[1]:02d}" if row else None
        if not facts:
            raise SystemExit(f"[EXPORT] WASDE {commodity}: no facts — refusing to publish")
        doc = {
            "schemaVersion": 1, "asOf": as_of, "commodity": commodity,
            "latest": {"wasde": latest, "title": reports[str(latest)]},
            "reports": reports,
            "dict": {"regions": regions.items, "years": years.items, "attrs": attrs.items, "units": units.items, "flags": flags.items},
            "facts": facts, "psd": psd,
        }
        out = ROOT / "Wasde" / name
        out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"[EXPORT] WASDE {commodity}: {len(facts)} facts, {len(psd)} PSD rows → {name} = "
              f"{out.stat().st_size // 1024} KB (asOf {as_of}, {reports[str(latest)]})")
    con.close()


if __name__ == "__main__":
    main()
