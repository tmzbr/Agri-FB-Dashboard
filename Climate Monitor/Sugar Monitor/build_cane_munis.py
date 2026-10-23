#!/usr/bin/env python3
"""
Monta locations.json -> "regioes": os municípios produtores de cana de SP, MG,
MT, PR, GO e MS, cada um com mesorregião IBGE, coordenada da sede e produção
de cana (t), que é o peso das séries regionais em build_regions.py.

Entradas (fora do repo, rodar só quando a PAM for atualizada):
  tabela1612-7.xlsx  SIDRA 1612, cana-de-açúcar, quantidade produzida (t),
                     2025, por município e mesorregião
  ibge_mun.json      API de Localidades do IBGE (município -> meso)
  municipios.csv     coordenadas da sede (github.com/kelvins/municipios-brasileiros)

O SIDRA exporta só "Nome (UF)", sem código IBGE: o join é por nome
normalizado + UF. As linhas de mesorregião que vêm no fim da planilha servem
de conferência — a soma dos municípios casados tem que bater com elas.

Uso: python3 build_cane_munis.py <tabela1612-7.xlsx> <ibge_mun.json> <municipios.csv>
"""
import csv, json, os, re, sys, unicodedata
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
LOC = os.path.join(HERE, "locations.json")
UFS = ("SP", "MG", "MT", "PR", "GO", "MS")


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


def main(xlsx, ibge_json, mun_csv):
    ibge = json.load(open(ibge_json, encoding="utf-8"))
    by_name, mesos = {}, {}
    for m in ibge:
        if not m.get("microrregiao"):       # criado depois da divisão em mesos (ex.: Boa Esperança do Norte/MT, 2024)
            continue
        meso = m["microrregiao"]["mesorregiao"]
        uf = meso["UF"]["sigla"]
        by_name[(norm(m["nome"]), uf)] = (m["id"], m["nome"], meso["nome"])
        mesos.setdefault((norm(meso["nome"]), uf), meso["nome"])
    coords = {int(r["codigo_ibge"]): (float(r["latitude"]), float(r["longitude"]))
              for r in csv.DictReader(open(mun_csv, encoding="utf-8"))}

    ws = openpyxl.load_workbook(xlsx, read_only=True).worksheets[0]
    muns, meso_tot, sem_match = [], {}, []
    # A planilha traz todos os municípios e, DEPOIS, o bloco de mesorregiões.
    # A posição é o que separa os dois: "Ribeirão Preto (SP)", "Bauru (SP)" etc.
    # são município e meso ao mesmo tempo. O bloco começa na primeira linha que
    # só existe como meso (ex.: "Madeira-Guaporé (RO)").
    em_meso = False
    for nome, val, *_ in ws.iter_rows(min_row=6, values_only=True):
        mt = re.match(r"^(.*) \(([A-Z]{2})\)$", str(nome or ""))
        if not mt:
            continue
        nm, uf = mt.group(1), mt.group(2)
        key = (norm(nm), uf)
        if not em_meso and key in mesos and key not in by_name:
            em_meso = True
        if uf not in UFS:
            continue
        t = val if isinstance(val, (int, float)) else 0   # "-", "...", "X" -> sem peso
        if em_meso:
            meso_tot[(mesos[key], uf)] = t
        elif key in by_name:
            cod, nome_ibge, meso = by_name[key]
            if t > 0:
                la, lo = coords[cod]
                muns.append({"uf": uf, "regiao": meso, "municipio": nome_ibge,
                             "codigo_ibge": cod, "lat": la, "lon": lo, "cana_t": int(t)})
        elif t > 0:                                        # sem produção não importa
            sem_match.append(f"{nm} ({uf})")
    if sem_match:
        sys.exit(f"sem match no IBGE: {sem_match}")

    # conferência contra os totais de meso da própria planilha
    soma = {}
    for m in muns:
        soma[(m["regiao"], m["uf"])] = soma.get((m["regiao"], m["uf"]), 0) + m["cana_t"]
    for k, tot in sorted(meso_tot.items(), key=lambda kv: (kv[0][1], -kv[1])):
        s = soma.get(k, 0)
        flag = "" if abs(s - tot) <= max(1, tot * 1e-6) else "   <-- DIVERGE"
        print(f"  {k[1]} {k[0]:38s} {tot/1e6:8.2f} Mt   municípios {s/1e6:8.2f} Mt{flag}")
    for uf in UFS:
        n = sum(1 for m in muns if m["uf"] == uf)
        print(f"{uf}: {n} municípios, {sum(m['cana_t'] for m in muns if m['uf']==uf)/1e6:.1f} Mt")

    loc = json.load(open(LOC, encoding="utf-8"))
    loc["_meta"]["fontes_regioes"] = (
        "Mesorregiões IBGE de SP, MG, MT, PR, GO e MS; entram todos os municípios com "
        "produção de cana > 0. Coordenada = sede do município (IBGE).")
    loc["_meta"]["fontes_pesos"] = (
        "Produção de cana-de-açúcar 2025 (t) por município — SIDRA tabela 1612 "
        "(Database/IGBE Grains/tabela1612_cana_2025.xlsx). Peso de cada município na série da sua mesorregião e do estado.")
    loc["regioes"] = sorted(muns, key=lambda m: (m["uf"], m["regiao"], m["municipio"]))
    with open(LOC, "w", encoding="utf-8") as f:
        f.write("{\n")
        f.write(f'  "_meta": {json.dumps(loc["_meta"], ensure_ascii=False)},\n')
        f.write('  "usinas": [\n' + ",\n".join("    " + json.dumps(u, ensure_ascii=False) for u in loc["usinas"]) + "\n  ],\n")
        f.write('  "regioes": [\n' + ",\n".join("    " + json.dumps(m, ensure_ascii=False) for m in loc["regioes"]) + "\n  ]\n}\n")
    print(f"OK -> locations.json ({len(muns)} municípios)")


if __name__ == "__main__":
    main(*sys.argv[1:4])
