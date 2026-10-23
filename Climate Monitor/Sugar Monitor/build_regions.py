#!/usr/bin/env python3
"""
Séries climáticas das regiões canavieiras (SP, MG, MT, PR, GO, MS) para o
Climate Monitor de S&E — mesma mecânica do build_regions.py do Weather Monitor.

Composição (locations.json -> "regioes", gerado por build_cane_munis.py): todos
os municípios com produção de cana > 0, por mesorregião IBGE, com cana_t
(SIDRA 1612, 2025) como peso.

Os municípios caem em ~409 células da grade do NASA POWER; a série de cada
mesorregião é a média das células ponderada pela cana dos municípios de cada
uma — idêntica à média ponderada sobre todos os municípios. Só NASA POWER,
como as regiões do Farms.

O cache de células é o MESMO do Weather Monitor (Weather Monitor/_cells_nasa.json):
MT, PR, GO e MS já são baixados pelo build das regiões de grãos, então no CI,
rodando logo depois dele, só as células novas (SP, MG e as canavieiras que o
Farms não cobre) vão à API.

Uso:  python3 build_regions.py [--only-aggregate]
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "Weather Monitor"))
import build_regions as br          # Weather Monitor/build_regions.py

LOC = os.path.join(HERE, "locations.json")
OUT = os.path.join(HERE, "history.json")
CACHE = os.path.join(os.path.dirname(HERE), "Weather Monitor", br.MODELS["nasa"]["cache"])

# Uma só ponderação: produção de cana. A chave vira o sufixo do ponto ("UF|Meso|c").
PESOS = {"c": ("produção de cana-de-açúcar, 2025 (t)", lambda m: m.get("cana_t", 0))}


def load_cells():
    regs = json.load(open(LOC, encoding="utf-8"))["regioes"]
    cells = {}
    for m in regs:
        la, lo = br.cell_of(m["lat"], m["lon"])
        c = cells.setdefault(f"{la},{lo}", {"lat": la, "lon": lo, "regioes": {}})
        acc = c["regioes"].setdefault(m["uf"] + "|" + m["regiao"], {p: 0 for p in PESOS})
        for p, (_, fn) in PESOS.items():
            acc[p] += fn(m)
    return regs, cells


def main():
    br.PESOS = PESOS                 # aggregate() itera sobre br.PESOS
    regs, cells = load_cells()
    print(f"{len(regs)} municípios -> {len(cells)} células da grade\n", flush=True)
    if "--only-aggregate" in sys.argv:
        cache = json.load(open(CACHE, encoding="utf-8"))
        falta = [k for k in cells if k not in cache]
        if falta:
            sys.exit(f"cache incompleto: faltam {len(falta)} células — rode sem --only-aggregate")
    else:
        cache = br.download_nasa(cells)
    points = br.aggregate("nasa", cells, cache)

    # preserva as usinas (chave "lat,lon") e regrava todas as regiões
    h = json.load(open(OUT, encoding="utf-8"))
    base = {k: v for k, v in h["points"].items() if "|" not in k}
    base.update(points)
    info = {}
    for m in regs:
        d = info.setdefault(m["uf"] + "|" + m["regiao"], {"municipios": 0, "cana_t": 0})
        d["municipios"] += 1
        d["cana_t"] += m["cana_t"]
    h["meta"].update({"n_pontos": len(base),
                      "pesos": {k: v[0] for k, v in PESOS.items()},
                      "regioes": dict(sorted(info.items())),
                      "grade_regioes": f"{br.DLAT}x{br.DLON} (NASA POWER); regiões = média das células ponderada pela cana municipal"})
    h["years"] = sorted(set(h["years"]) | set(range(br.Y0, br.Y1 + 1)))
    h["points"] = base
    json.dump(h, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"OK -> history.json ({os.path.getsize(OUT)/1024/1024:.1f} MB, "
          f"{len(base)} pontos, {len(points)} regiões)", flush=True)


if __name__ == "__main__":
    main()
