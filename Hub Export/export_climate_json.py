#!/usr/bin/env python3
"""
export_climate_json.py — Climate Monitor (Weather + Sugar) → hub JSON
=====================================================================
    python "Hub Export/export_climate_json.py" nasa|era5|all

The legacy pages load history.json (12 MB for the Weather Monitor) and match
each point to its series in the browser. This script does the matching once
and splits the data by what a screen shows, so the hub loads one small file
per group and model. The live Open-Meteo layer and the users' own clusters
(Supabase) are not part of the hub.

CONTRACT (schemaVersion 1) → Climate Monitor/Hub/climate-<file>[-era5].json
  files: capitals, farms (SLC + BrasilAgro), sugar (mills), and — NASA only —
         regions-rs, regions-pr, regions-ms, regions-mt, regions-go,
         regions-matopiba (MA, TO, PI and BA together), sugar-regions (cane
         mesoregions of SP, GO, MG, MS, PR and MT, weighted by cane output)
  { schemaVersion, asOf, model, years,
    roni:   { year: [12 monthly RONI values] },
    points: [ { id, group, name, sub, w, area?, empresa?, s } ]       # s = series key
         or [ { id, group, name, uf, n, milho_t, soja_t,
                s: { n, m, s } } ]                                    # regions: one series per weighting
                                                               # (cane regions: cana_t and s: { c })
    series: { key: { t: { year: [366] }, p: { year: [366] } } } }
  Values are tenths (°C ×10, mm ×10) as integers, null where missing; index
  0 = Jan 1 … 59 = Feb 29 … 365 = Dec 31 in every year.
  asOf = the last day with data in the latest year, over the file's series.
"""

import datetime
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CM = ROOT / "Climate Monitor"
OUT = CM / "Hub"
MODELS = {"nasa": "history.json", "era5": "history_era5.json"}
REGION_FILES = {"rs": ["RS"], "pr": ["PR"], "ms": ["MS"], "mt": ["MT"], "go": ["GO"], "matopiba": ["MA", "TO", "PI", "BA"]}


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def tenths(values):
    return [None if v is None else round(v * 10) for v in values]


def pack(series):
    return {var: {y: tenths(vals) for y, vals in series[var].items()} for var in ("t", "p")}


def nearest(hist, lat, lon):
    """The legacy matchHist: closest grid coordinate within 0.02° (L1)."""
    best, dist = None, 1e9
    for key in hist["points"]:
        if "|" in key:
            continue
        la, lo = map(float, key.split(","))
        d = abs(la - lat) + abs(lo - lon)
        if d < dist:
            best, dist = key, d
    return best if dist < 0.02 else None


def as_of(doc):
    year = str(max(doc["years"]))
    last = -1
    for s in doc["series"].values():
        for var in ("t", "p"):
            arr = s[var].get(year) or []
            idx = max((i for i, v in enumerate(arr) if v is not None), default=-1)
            last = max(last, idx)
    if last < 0:
        return None
    # index 59 is Feb 29: map through a leap year, then check the real calendar
    md = datetime.date(2000, 1, 1) + datetime.timedelta(days=last)
    return f"{year}-{md.month:02d}-{md.day:02d}"


def document(model, hist, points, keys):
    roni = load(CM / "roni.json").get("roni", {})
    doc = {
        "schemaVersion": 1, "asOf": None, "model": model, "years": hist["years"],
        "roni": roni, "points": points,
        "series": {k: pack(hist["points"][k]) for k in sorted(keys)},
    }
    doc["asOf"] = as_of(doc)
    return doc


def simple_points(hist, rows):
    points, keys = [], set()
    for row in rows:
        key = nearest(hist, row["lat"], row["lon"])
        if key:
            row["s"] = key
            keys.add(key)
        points.append(row)
    return points, keys


def capitals(hist):
    html = (CM / "Weather Monitor/weather_dashboard.html").read_text(encoding="utf-8")
    pop = json.loads(re.search(r"^const POP=(\{.*?\});", html, re.M).group(1))
    loc = load(CM / "Weather Monitor/locations.json")
    rows = [{"id": "c|" + c["nome"], "group": "cap", "name": c["nome"], "sub": c["uf"], "lat": c["lat"], "lon": c["lon"],
             "w": pop.get(c["nome"], 0)} for c in loc["capitais"]]
    return simple_points(hist, rows)


def farms(hist):
    loc = load(CM / "Weather Monitor/locations.json")
    rows = []
    for f in loc["fazendas"]:
        slc = f["empresa"] == "SLC"
        rows.append({"id": f"f|{f['fazenda']}|{f['municipio']}", "group": "slc" if slc else "ba", "name": f["fazenda"],
                     "sub": f"{f['municipio']}/{f['uf']}", "empresa": f["empresa"], "lat": f["lat"], "lon": f["lon"],
                     "area": f.get("area_ha") or 0, "w": (f.get("area_ha") or 0) if slc else 0})
    return simple_points(hist, rows)


def sugar(hist):
    loc = load(CM / "Sugar Monitor/locations.json")
    rows = [{"id": f"u|{u['usina']}|{u['municipio']}", "group": u["empresa"].lower(), "name": u["usina"],
             "sub": f"{u['municipio']}/{u['uf']}", "empresa": u["empresa"], "lat": u["lat"], "lon": u["lon"],
             "area": u.get("moagem_mt") or 0, "w": u.get("moagem_mt") or 0} for u in loc["usinas"]]
    return simple_points(hist, rows)


def regions(hist, ufs):
    """One point per region; the series are already aggregated over every municipality by build_regions.py."""
    loc = load(CM / "Weather Monitor/locations.json")
    by = {}
    for m in loc.get("regioes", []):
        if m["uf"] not in ufs:
            continue
        key = f"{m['uf']}|{m['regiao']}"
        r = by.setdefault(key, {"id": "r|" + key, "group": "uf-" + m["uf"].lower(), "name": m["regiao"], "uf": m["uf"],
                                "n": 0, "milho_t": 0, "soja_t": 0})
        r["n"] += 1
        r["milho_t"] += m.get("milho_t") or 0
        r["soja_t"] += m.get("soja_t") or 0
    points, keys = [], set()
    for key, r in by.items():
        r["s"] = {k: f"{key}|{k}" for k in ("n", "m", "s") if f"{key}|{k}" in hist["points"]}
        keys.update(r["s"].values())
        points.append(r)
    return points, keys


def cane_regions(hist):
    """Cane mesoregions of the Center South (Sugar locations.json), one cane-weighted series each ("UF|Meso|c")."""
    loc = load(CM / "Sugar Monitor/locations.json")
    by = {}
    for m in loc.get("regioes", []):
        key = f"{m['uf']}|{m['regiao']}"
        r = by.setdefault(key, {"id": "r|" + key, "group": "uf-" + m["uf"].lower(), "name": m["regiao"], "uf": m["uf"], "n": 0, "cana_t": 0})
        r["n"] += 1
        r["cana_t"] += m.get("cana_t") or 0
    points, keys = [], set()
    for key, r in by.items():
        if f"{key}|c" in hist["points"]:
            r["s"] = {"c": f"{key}|c"}
            keys.add(r["s"]["c"])
        points.append(r)
    return points, keys


def strip(points):
    for p in points:
        p.pop("lat", None)
        p.pop("lon", None)
    return points


def export(model):
    suffix = "" if model == "nasa" else "-era5"
    weather = load(CM / "Weather Monitor" / MODELS[model])
    sugar_hist = load(CM / "Sugar Monitor" / MODELS[model])
    jobs = {"capitals": (weather, capitals), "farms": (weather, farms), "sugar": (sugar_hist, sugar)}
    if any("|" in k for k in weather["points"]):
        for name, ufs in REGION_FILES.items():
            jobs["regions-" + name] = (weather, lambda h, ufs=ufs: regions(h, ufs))
    if any("|" in k for k in sugar_hist["points"]):
        jobs["sugar-regions"] = (sugar_hist, cane_regions)
    OUT.mkdir(exist_ok=True)
    for name, (hist, build) in jobs.items():
        points, keys = build(hist)
        if not keys:
            raise SystemExit(f"[EXPORT] climate {name}{suffix}: no series matched — refusing to publish")
        out = OUT / f"climate-{name}{suffix}.json"
        doc = document(model, hist, strip(points), keys)
        out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8")
        print(f"[EXPORT] climate {name}{suffix}: {len(points)} points, {len(keys)} series → {out.name} = "
              f"{out.stat().st_size // 1024} KB (asOf {doc['asOf']})")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) == 2 else ""
    if arg not in ("nasa", "era5", "all"):
        raise SystemExit(f"usage: {Path(__file__).name} nasa|era5|all")
    for m in (["nasa", "era5"] if arg == "all" else [arg]):
        export(m)
