#!/usr/bin/env python3
"""
export_se_json.py — Sugar & Ethanol dashboard → hub JSON (three files)
======================================================================
    python "Hub Export/export_se_json.py"

The S&E extractor writes every dataset into se_dashboard.html as
`const NAME = …;` lines (3 MB). This script lifts them, drops the map blocks
(MAP_DATA, DEF_MAP, OTTO_MAP repeat the per-state series pivoted by date),
keys the states by their two-letter code and stores the series as columns.

CONTRACTS (schemaVersion 1), in "Sugar and Ethanol/"
  se-prices.json        { schemaVersion, asOf, d, sugar, eth, fx, equiv }
                        daily columns: NY11 (USDc/lb), hydrous ethanol CEPEA
                        Paulínia (BRL/m³), PTAX, ethanol-equivalent (USDc/lb)
  se-parity.json        { schemaVersion, asOf, dates, br, uf: { AC: [...] } }
                        weekly ethanol ÷ gasoline pump price (ANP), aligned with dates
  se-fuel-balance.json  { schemaVersion, asOf,
                          deficit: { months, br: {prod, vendas, saldo}, ufMonths, uf: { AC: {prod, vendas, saldo} } },
                          otto:    { months, uf: { AC: [...] }, brMonths, br } }
                        monthly hydrous ethanol production − sales (m³) and the
                        ethanol energy share of Otto-cycle fuel (ANP)
  asOf = the latest date in each file's data.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SE = ROOT / "Sugar and Ethanol"


def blocks(html, names):
    out = {}
    for name in names:
        m = re.search(rf"^const {name}\s*=\s*(.+?);\s*$", html, re.M)
        if not m:
            raise SystemExit(f"[EXPORT] block {name} not found — refusing to publish")
        out[name] = json.loads(m.group(1))
    return out


def write(name, doc, rows):
    if not rows:
        raise SystemExit(f"[EXPORT] {name}: no rows — refusing to publish")
    out = SE / name
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] S&E {name}: {rows} rows = {out.stat().st_size // 1024} KB (asOf {doc['asOf']})")


def main():
    html = (SE / "se_dashboard.html").read_text(encoding="utf-8")
    g = blocks(html, ["SE_DATA", "UF_SERIES", "BR_SERIES", "DEF_SERIES", "OTTO_SERIES", "DEF_MONTHS", "OTTO_MONTHS",
                      "BR_DEF_SERIES", "BR_OTTO_SERIES", "CODE_UF", "CODE_UF_SD"])

    prices = g["SE_DATA"]
    write("se-prices.json", {
        "schemaVersion": 1, "asOf": prices[-1]["d"] if prices else None,
        **{k: [r.get(k) for r in prices] for k in ("d", "sugar", "eth", "fx", "equiv")},
    }, len(prices))

    dates = [r["d"] for r in g["BR_SERIES"]]
    uf = {}
    for code, name in g["CODE_UF"].items():
        by_date = {r["d"]: r["p"] for r in g["UF_SERIES"].get(name, [])}
        if by_date:
            uf[code] = [by_date.get(d) for d in dates]
    write("se-parity.json", {
        "schemaVersion": 1, "asOf": dates[-1] if dates else None,
        "dates": dates, "br": [r["p"] for r in g["BR_SERIES"]], "uf": uf,
    }, len(dates))

    months = g["DEF_MONTHS"]
    cols = ("prod", "vendas", "saldo")
    deficit_uf, otto_uf = {}, {}
    for code, name in g["CODE_UF_SD"].items():
        rows = {r["d"]: r for r in g["DEF_SERIES"].get(name, [])}
        if rows:
            deficit_uf[code] = {c: [rows[m][c] if m in rows else None for m in months] for c in cols}
        otto = {r["d"]: r["p"] for r in g["OTTO_SERIES"].get(name, [])}
        if otto:
            otto_uf[code] = [otto.get(m) for m in g["OTTO_MONTHS"]]
    br_def = g["BR_DEF_SERIES"]
    write("se-fuel-balance.json", {
        "schemaVersion": 1, "asOf": max(months + g["OTTO_MONTHS"], default=None),
        "deficit": {"months": [r["d"] for r in br_def], "br": {c: [r[c] for r in br_def] for c in cols},
                    "ufMonths": months, "uf": deficit_uf},
        "otto": {"months": g["OTTO_MONTHS"], "uf": otto_uf,
                 "brMonths": [r["d"] for r in g["BR_OTTO_SERIES"]], "br": [r["p"] for r in g["BR_OTTO_SERIES"]]},
    }, len(months))


if __name__ == "__main__":
    main()
