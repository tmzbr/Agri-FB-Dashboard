#!/usr/bin/env python3
"""
Atualiza roni.json (raiz do repo) a partir do NOAA CPC — Relative Oceanic Niño
Index (RONI). Estrutura: {"roni": {"YYYY": [12 valores por mês-do-meio Jan..Dez]}}.
As 12 estações trimestrais (DJF..NDJ) mapeiam direto para os meses Jan..Dez.
Mantém precisão de 2 casas (igual ao arquivo-fonte). Roda no workflow de clima.
"""
import json, os, urllib.request, datetime
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "roni.json")
URL = "https://www.cpc.ncep.noaa.gov/data/indices/RONI.ascii.txt"
YEAR_FROM = 2009   # suficiente p/ janela de 15a + borda inicial da detecção de episódio

def main():
    txt = urllib.request.urlopen(URL, timeout=90).read().decode()
    byyr = defaultdict(list)
    for line in txt.split("\n")[1:]:
        p = line.split()
        if len(p) < 3:
            continue
        try:
            y, a = int(p[1]), float(p[2])
        except ValueError:
            continue
        byyr[y].append(round(a, 2))
    roni = {str(y): byyr[y] for y in sorted(byyr) if y >= YEAR_FROM}
    out = {
        "source": "NOAA CPC — Relative Oceanic Niño Index (RONI). 12 estações trimestrais por ano (DJF..NDJ) = meses Jan..Dez (mês-do-meio).",
        "url": URL,
        "updated": datetime.date.today().isoformat(),
        "roni": roni,
    }
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print(f"roni.json -> {min(roni)}–{max(roni)} | última {max(roni)} com {len(roni[max(roni)])} meses")

if __name__ == "__main__":
    main()
