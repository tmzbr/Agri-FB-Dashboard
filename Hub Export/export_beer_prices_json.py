#!/usr/bin/env python3
"""
export_beer_prices_json.py — Supermarket Prices, beers only → hub JSON
======================================================================
    python "Hub Export/export_beer_prices_json.py"

gerar_dashboard.py writes the whole supermarket history (every food
category) and the scraper's error log into supermarket_prices.html as
`const NAME = …;` blocks (8.9 MB). The hub only shows beers, so this script
lifts the beer rows of HIST and the beer configuration (can sizes, brand of
each SKU, brewery weights). The error log is not published.

CONTRACT (schemaVersion 1) → Supermarket Prices/beer-prices.json
  { schemaVersion, asOf, dates, supermarkets,
    skus:    [ { name, size, brand, ml } ],                # brand/ml null when unmapped
    rows:    [ [date, supermarket, sku, price], ... ],     # indexes into the lists above, history order
    weights: { brewery: { brand: weight } } }              # brewery price index
  asOf = the latest collection date with a beer price.
"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SM = ROOT / "Supermarket Prices"


def js_object(html, name):
    """A `const NAME = {...};` block, single- or multi-line, as JSON (trailing commas allowed)."""
    m = re.search(rf"^const {name}\s*=\s*(\{{.*?\}});", html, re.M | re.S)
    if not m:
        raise SystemExit(f"[EXPORT] block {name} not found — refusing to publish")
    return json.loads(re.sub(r",(\s*[}\]])", r"\1", m.group(1)))


def main():
    html = (SM / "supermarket_prices.html").read_text(encoding="utf-8")
    hist = json.loads(re.search(r"^const HIST\s*=\s*(.+?);\s*$", html, re.M).group(1))
    ml_map, brand_map, weights = js_object(html, "ML_MAP"), js_object(html, "BRAND_MAP"), js_object(html, "BREWERY_WEIGHTS")

    beer = [r for r in hist if r.get("categoria") == "Cervejas" and r.get("preco_atual")]
    dates = sorted({r["data_coleta"] for r in beer})
    sms = sorted({r["supermercado"] for r in beer})
    skus, sku_ix = [], {}
    for r in beer:
        key = (r["nome_produto"], r["embalagem"])
        if key not in sku_ix:
            sku_ix[key] = len(skus)
            skus.append({"name": key[0], "size": key[1], "brand": brand_map.get(key[0]), "ml": ml_map.get(key[0], {}).get(key[1])})
    d_ix, s_ix = {d: i for i, d in enumerate(dates)}, {s: i for i, s in enumerate(sms)}
    rows = [[d_ix[r["data_coleta"]], s_ix[r["supermercado"]], sku_ix[(r["nome_produto"], r["embalagem"])], r["preco_atual"]] for r in beer]
    if not rows:
        raise SystemExit("[EXPORT] beer prices: no rows — refusing to publish")

    doc = {"schemaVersion": 1, "asOf": dates[-1], "dates": dates, "supermarkets": sms, "skus": skus, "rows": rows, "weights": weights}
    out = SM / "beer-prices.json"
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] beer prices: {len(rows)} rows, {len(skus)} SKUs, {len(dates)} days → {out.name} = "
          f"{out.stat().st_size // 1024} KB (asOf {dates[-1]})")


if __name__ == "__main__":
    main()
