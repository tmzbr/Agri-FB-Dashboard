#!/usr/bin/env python3
"""
export_agri_monitor_json.py — Agri Monitor modules → JSON for the research hub
==============================================================================
    python "Hub Export/export_agri_monitor_json.py" secex|crushing|imea|conab

Writes <module>.json next to the module DB for the Research Dashboard Hub.
Rows keep the DB column names; the hub filters, aggregates and charts them as
the legacy pages did. asOf is the newest updated_at in the data (not the clock),
so a run with nothing new rewrites a byte-identical file.

CONTRACTS (schemaVersion 1)
  secex     { schemaVersion, asOf, rows: [{co_ano, co_mes, co_ncm, categoria, produto,
              vl_fob, kg_liquido}] }  — one row per month × NCM
  crushing  { schemaVersion, asOf, series: {MT: [...], RS: [...]} } — the same daily
              points the legacy page embedded (d, soja_sc, farelo_t, farelo_usdkg, ptax,
              bio_m3, rec_farelo, rec_bio, custo, spread), from 2022 (B100 history start)
  imea      { schemaVersion, asOf, crops: {SOJA|MILHO|ALGODAO: {monthly: [...], annual: [...]}} }
              — dash_data.json as extractor_imea.py writes it (per-hectare revenue, costs,
              gross profit and margins; `ok` = complete cost sheet)
  conab     { schemaVersion, asOf, columns: [...], rows: [[...], ...] } — every survey row of
              the safra table as compact tuples in `columns` order (ano_agricola, safra, uf,
              produto, id_levantamento, area_plantada_mil_ha, producao_mil_t, produtividade_t_ha)
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def query(conn, sql):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(sql)]


def latest(conn, table):
    value = conn.execute(f"SELECT MAX(updated_at) FROM {table}").fetchone()[0]
    return value[:10] if value else None


def secex(conn):
    rows = query(conn, "SELECT co_ano, co_mes, co_ncm, categoria, produto, vl_fob, kg_liquido "
                       "FROM exportacoes ORDER BY co_ano, co_mes, categoria, co_ncm")
    return {"asOf": latest(conn, "exportacoes"), "rows": rows}, len(rows)


def crushing(conn):
    def rr(v, n=2):
        return round(v, n) if v is not None else None
    series = {"MT": [], "RS": []}
    rows = query(conn, """
        SELECT data_referencia, regiao, preco_soja_sc60, preco_farelo_usdkg, preco_farelo_ton, ptax,
               preco_bio_m3, receita_farelo, receita_biodiesel, custo_soja, spread_brl_ton
        FROM crushing_spread WHERE data_referencia >= '2022-01-01'
        ORDER BY data_referencia, regiao""")
    for r in rows:
        series[r["regiao"]].append({
            "d": r["data_referencia"], "soja_sc": rr(r["preco_soja_sc60"]), "farelo_t": rr(r["preco_farelo_ton"]),
            "farelo_usdkg": rr(r["preco_farelo_usdkg"], 6), "ptax": rr(r["ptax"], 4), "bio_m3": rr(r["preco_bio_m3"]),
            "rec_farelo": rr(r["receita_farelo"]), "rec_bio": rr(r["receita_biodiesel"]),
            "custo": rr(r["custo_soja"]), "spread": rr(r["spread_brl_ton"]),
        })
    as_of = max((r["data_referencia"] for r in rows), default=None)
    return {"asOf": as_of, "series": series}, len(rows)


def imea(conn):
    crops = json.loads((ROOT / "Agri Monitor/imea/dash_data.json").read_text(encoding="utf-8"))
    dates = [r["d"] for c in crops.values() for r in c.get("monthly", []) + c.get("annual", []) if r.get("d")]
    count = sum(len(c.get("monthly", [])) for c in crops.values())
    return {"asOf": max(dates, default=None), "crops": crops}, count


def conab(conn):
    columns = ["ano_agricola", "safra", "uf", "produto", "id_levantamento",
               "area_plantada_mil_ha", "producao_mil_t", "produtividade_t_ha"]
    rows = [list(r) for r in conn.execute(
        f"SELECT {', '.join(columns)} FROM safra ORDER BY ano_agricola, id_levantamento, uf")]
    return {"asOf": latest(conn, "safra"), "columns": columns, "rows": rows}, len(rows)


MODULES = {
    "secex": ("Agri Monitor/secex/secex.db", "secex.json", secex),
    "conab": ("Agri Monitor/conab/conab.db", "conab.json", conab),
    "imea": ("Agri Monitor/imea/imea.db", "imea.json", imea),
    "crushing": ("Agri Monitor/crushing_spread/crushing_spread.db", "crushing-spread.json", crushing),
}


def export(name):
    db_rel, out_name, build = MODULES[name]
    db_path = ROOT / db_rel
    conn = sqlite3.connect(db_path)
    body, count = build(conn)
    conn.close()
    if not count:
        raise SystemExit(f"[EXPORT] {name}: no rows — refusing to publish")
    out_path = db_path.parent / out_name
    doc = {"schemaVersion": 1, **body}
    out_path.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] {name}: {count} rows → {out_name} = {out_path.stat().st_size // 1024} KB (asOf {doc['asOf']})")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in MODULES:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(MODULES)}")
    export(sys.argv[1])
