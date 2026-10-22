#!/usr/bin/env python3
"""
export_market_watch_json.py — CVM buybacks/insider and short interest → hub JSON
================================================================================
    python "Hub Export/export_market_watch_json.py" cvm|short

cvm_buybacks.py and short_interest.py already compute every dataset the legacy
pages show and write them into the pages as `const NAME = {...};` blocks. This
script lifts those blocks out of the freshly built HTML, so the hub reads
exactly the numbers the legacy pages show, without a second implementation.

CONTRACTS (schemaVersion 1)
  cvm    → CVM Buybacks/cvm.json
           { schemaVersion, asOf, buybackDaily, buybackMonthly, insiderAgg,
             insiderSeries, programs, realized, consolidated, companyNames }
           (asOf = latest trade date in the data)
  short  → Market Watch/Short Interest/short-interest.json
           { schemaVersion, asOf, series, latest, companyNames }
           (asOf = the B3 reference date of the latest snapshot)
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def block(html, name):
    m = re.search(rf"^const {name}\s*=\s*(.+?);\s*$", html, re.M)
    if not m:
        raise SystemExit(f"[EXPORT] block {name} not found — refusing to publish")
    return json.loads(m.group(1))


def cvm():
    html = (ROOT / "CVM Buybacks/cvm_buybacks.html").read_text(encoding="utf-8")
    doc = {
        "buybackDaily": block(html, "BUYBACK_DAILY"),
        "buybackMonthly": block(html, "BUYBACK_MONTHLY"),
        "insiderAgg": block(html, "INSIDER_AGG"),
        "insiderSeries": block(html, "INSIDER_SERIES"),
        "programs": block(html, "PROGRAMS_FULL"),
        "realized": block(html, "REALIZED"),
        "consolidated": block(html, "CONSOLIDATED"),
        "companyNames": block(html, "COMPANY_NAMES"),
    }
    dates = [r["d"] for rows in doc["buybackDaily"].values() for r in rows if r.get("d")]
    dates += [r["d"] for rows in doc["insiderAgg"].values() for r in rows if r.get("d")]
    return ROOT / "CVM Buybacks/cvm.json", {"schemaVersion": 1, "asOf": max(dates, default=None), **doc}, len(doc["companyNames"])


def short():
    html = (ROOT / "Market Watch/Short Interest/short_interest.html").read_text(encoding="utf-8")
    doc = {
        "series": block(html, "SI_SERIES"),
        "latest": block(html, "SI_LATEST"),
        "companyNames": block(html, "COMPANY_NAMES"),
    }
    as_of = re.search(r'^const LAST_UPDATE\s*=\s*"([^"]*)";', html, re.M)
    return (ROOT / "Market Watch/Short Interest/short-interest.json",
            {"schemaVersion": 1, "asOf": as_of.group(1) if as_of else None, **doc}, len(doc["series"]))


MODULES = {"cvm": cvm, "short": short}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODULES:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(MODULES)}")
    out, doc, tickers = MODULES[sys.argv[1]]()
    if not tickers:
        raise SystemExit(f"[EXPORT] {sys.argv[1]}: no tickers — refusing to publish")
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] {sys.argv[1]}: {tickers} tickers → {out.name} = {out.stat().st_size // 1024} KB (asOf {doc['asOf']})")
