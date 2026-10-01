#!/usr/bin/env python3
"""
Teste NASA AppEEARS — Sorriso/MT (IBGE 5107925)

O que este teste responde:
  1) O dado lido direto da NASA reproduz os valores que já obtivemos pelo Planetary Computer
     (janelas Terra 26/06/2023 e Aqua 04/07/2023)?
  2) As composições mais recentes de 2026 (Terra 29/08, Aqua 06/09) estão acessíveis?
  3) Quanto tempo a fila do AppEEARS leva e que arquivos ele entrega?

Credenciais: variáveis de ambiente EARTHDATA_USER e EARTHDATA_PASS (nunca no código).
No GitHub Actions, são os secrets de mesmo nome.

Uso:
  python test_appeears_sorriso.py --dry-run          # valida camadas (API pública) e monta as tarefas
  python test_appeears_sorriso.py                    # submete 2 tarefas, aguarda, baixa e calcula
  python test_appeears_sorriso.py --resume ID_A,ID_B # retoma tarefas já submetidas (fila lenta)
  (espera máxima padrão: 30 min por tarefa; as tarefas continuam na NASA mesmo se o script parar de esperar)
"""
import argparse, csv, datetime, json, os, re, sys, time
import numpy as np
import requests
import rasterio
from rasterio.features import geometry_mask
from rasterio.mask import mask
from rasterio.warp import transform_geom
from shapely.geometry import mapping, shape, MultiPolygon

API = "https://appeears.earthdatacloud.nasa.gov/api"
COD, NOME = 5107925, "Sorriso/MT"
PRODUTOS = ["MOD13Q1.061", "MYD13Q1.061"]                      # Terra e Aqua
CAMADAS = ["_250m_16_days_NDVI", "_250m_16_days_pixel_reliability", "_250m_16_days_composite_day_of_the_year"]

# Duas tarefas: (A) janelas de 2023 com referência do Planetary Computer; (B) janelas mais recentes de 2026
TAREFAS = {
    "A_2023": ("06-20-2023", "07-08-2023"),
    "B_2026": ("08-25-2026", "09-10-2026"),
}

# Valores de referência já obtidos em Sorriso (Planetary Computer, mesmo polígono IBGE 'intermediária')
REF = {
    ("MOD13Q1", "2023-06-26"): dict(ndvi=0.4995, p10=0.2835, p90=0.7817, pct=100.0, dobs=3),
    ("MYD13Q1", "2023-07-04"): dict(ndvi=0.4788, p10=0.2738, p90=0.7800, pct=100.0, dobs=6),
}
TOL = dict(ndvi=0.002, p10=0.005, p90=0.005, pct=0.5, dobs=1)


# ───────────────────────────── geometria ─────────────────────────────
def ibge_geom(cod, qualidade="intermediaria"):
    url = (f"https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{cod}"
           f"?formato=application/vnd.geo+json&qualidade={qualidade}")
    for t in range(6):
        r = requests.get(url, timeout=90)
        if r.status_code == 200:
            return shape(r.json()["features"][0]["geometry"])
        time.sleep(2 ** t)
    r.raise_for_status()


# ───────────────────────────── estatísticas ─────────────────────────────
def _ler(path, geom4326):
    """Lê a janela do polígono. Devolve (array float, máscara 'dentro do polígono')."""
    with rasterio.open(path) as src:
        g = transform_geom("EPSG:4326", src.crs, mapping(geom4326))
        arr, tf = mask(src, [g], crop=True, filled=False)
        a = np.ma.getdata(arr[0]).astype("float64")
        inside = geometry_mask([g], out_shape=a.shape, transform=tf, invert=True)   # centro do pixel dentro
        nod = src.nodata
    return a, inside, nod


def estatisticas(ndvi_p, rel_p, doy_p, geom4326, inicio):
    """Mesma regra usada no Planetary Computer: NDVI válido e confiabilidade 0 (bom) ou 1 (marginal)."""
    nd, inside, _ = _ler(ndvi_p, geom4326)
    rel, inside_r, _ = _ler(rel_p, geom4326)
    doy, _, _ = _ler(doy_p, geom4326)
    # Detecta escala: o AppEEARS pode entregar o valor bruto (int16, ×0,0001) ou já escalado (float32)
    # (a detecção ignora o valor de preenchimento, que pode vir bruto (-3000) mesmo num raster já escalado)
    cand = nd[inside & (nd > -2999)]
    bruto = bool(cand.size) and float(np.nanmax(np.abs(cand))) > 20
    ndvi = nd * 1e-4 if bruto else nd
    valido = inside & (ndvi >= -0.2) & (ndvi <= 1.0)             # -3000 bruto (= -0,3) cai fora
    usado = valido & np.isin(rel, (0, 1))
    n_in = int(inside.sum())
    v = ndvi[usado]
    dobs = None
    d = doy[usado & (doy > 0)]
    if d.size:
        dd = int(np.median(d))
        ef = datetime.date(inicio.year, 1, 1) + datetime.timedelta(days=dd - 1)
        if ef < inicio - datetime.timedelta(days=3):
            ef = datetime.date(inicio.year + 1, 1, 1) + datetime.timedelta(days=dd - 1)
        dobs = (ef - inicio).days
    return dict(escala="bruto×0,0001" if bruto else "já escalado", pixels=n_in, usados=int(usado.sum()),
                pct=100 * usado.sum() / max(n_in, 1),
                ndvi=float(v.mean()) if v.size else None,
                p10=float(np.percentile(v, 10)) if v.size else None,
                p90=float(np.percentile(v, 90)) if v.size else None, dobs=dobs)


RX = re.compile(r"^(?P<prod>M[OY]D13Q1)\.061_+(?P<layer>.+?)_doy(?P<y>\d{4})(?P<d>\d{3})")


def agrupar(pasta):
    """{(produto, data_inicio): {'ndvi':..., 'rel':..., 'doy':...}} a partir dos .tif baixados."""
    g = {}
    for f in sorted(os.listdir(pasta)):
        if not f.lower().endswith(".tif"):
            continue
        m = RX.match(f)
        if not m:
            continue
        ini = datetime.date(int(m["y"]), 1, 1) + datetime.timedelta(days=int(m["d"]) - 1)
        L = m["layer"]
        k = "ndvi" if L.endswith("NDVI") else "rel" if "pixel_reliability" in L else "doy" if "composite_day" in L else None
        if k:
            g.setdefault((m["prod"], ini), {})[k] = os.path.join(pasta, f)
    return g


# ───────────────────────────── AppEEARS ─────────────────────────────
def montar(nome, ini, fim, geom):
    return {
        "task_type": "area",
        "task_name": f"{nome}_{datetime.datetime.now(datetime.timezone.utc):%Y%m%d%H%M%S}",
        "params": {
            "dates": [{"startDate": ini, "endDate": fim}],
            "layers": [{"product": p, "layer": c} for p in PRODUTOS for c in CAMADAS],
            "geo": {"type": "FeatureCollection",
                    "features": [{"type": "Feature", "geometry": mapping(geom),
                                  "properties": {"id": str(COD), "nome": NOME}}]},
            "output": {"format": {"type": "geotiff"}, "projection": "native"},
        },
    }


def validar_camadas():
    for p in PRODUTOS:
        j = requests.get(f"{API}/product/{p}", timeout=60)
        j.raise_for_status()
        j = j.json()
        for c in CAMADAS:
            ok = c in j and j[c].get("Available")
            print(f"  {p} {c:42} {'OK' if ok else 'AUSENTE'}")
            if not ok:
                sys.exit(f"camada ausente: {p} {c}")


def login(user, pw):
    r = requests.post(f"{API}/login", auth=(user, pw), timeout=60)
    if r.status_code != 200:
        sys.exit(f"Login recusado (HTTP {r.status_code}). Causas mais comuns:\n"
                 "  1) usar o e-mail em vez do NOME DE USUÁRIO do Earthdata;\n"
                 "  2) conta ainda não ativada (confirme o e-mail de verificação do Earthdata);\n"
                 "  3) senha incorreta no secret.\n"
                 "Teste entrando em https://appeears.earthdatacloud.nasa.gov/ com o mesmo usuário e senha: "
                 "se funcionar lá, recrie os secrets; se não, o problema é a conta. "
                 "No primeiro uso, aceite os termos do AppEEARS nessa página.")
    return {"Authorization": "Bearer " + r.json()["token"]}


def esperar(H, tid, max_min):
    t0 = time.time()
    while True:
        t = requests.get(f"{API}/task/{tid}", headers=H, timeout=60).json()
        st = t.get("status")
        print(f"    [{(time.time()-t0)/60:5.1f} min] {tid[:8]} {st}", flush=True)
        if st in ("done", "error"):
            return t
        if (time.time() - t0) / 60 > max_min:
            return None
        time.sleep(30)


def baixar(H, tid, pasta):
    os.makedirs(pasta, exist_ok=True)
    b = requests.get(f"{API}/bundle/{tid}", headers=H, timeout=60).json()
    tot = 0
    for f in b["files"]:
        if not f["file_name"].lower().endswith(".tif"):
            continue
        r = requests.get(f"{API}/bundle/{tid}/{f['file_id']}", headers=H, timeout=300, allow_redirects=True, stream=True)
        r.raise_for_status()
        with open(os.path.join(pasta, os.path.basename(f["file_name"])), "wb") as o:
            for ch in r.iter_content(65536):
                o.write(ch)
        tot += f["file_size"]
    print(f"    {len([x for x in b['files'] if x['file_name'].lower().endswith('.tif')])} .tif baixados ({tot/1e6:.1f} MB); "
          f"todos os arquivos do bundle: {[x['file_name'][:40] for x in b['files'] if not x['file_name'].lower().endswith('.tif')]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", help="IDs das tarefas A,B já submetidas")
    ap.add_argument("--max-min", type=int, default=30, help="espera máxima por tarefa (min); depois disso, retome com --resume")
    a = ap.parse_args()

    print("Validando camadas na API pública do AppEEARS...")
    validar_camadas()
    geom = ibge_geom(COD)
    geom = max(geom.geoms, key=lambda p: p.area) if isinstance(geom, MultiPolygon) else geom
    tarefas = {k: montar(f"ndvi_{k}", *v, geom) for k, v in TAREFAS.items()}
    for k, t in tarefas.items():
        print(f"Tarefa {k}: {len(t['params']['layers'])} camadas, {TAREFAS[k][0]} -> {TAREFAS[k][1]}, "
              f"polígono com {len(geom.exterior.coords)} vértices")
    if a.dry_run:
        print("[dry-run] nada foi enviado.")
        return

    raw_u, raw_p = os.environ.get("EARTHDATA_USER", ""), os.environ.get("EARTHDATA_PASS", "")
    user, pw = raw_u.strip(), raw_p.strip()
    if not (user and pw):
        sys.exit("Defina EARTHDATA_USER e EARTHDATA_PASS (ou use --dry-run).")
    # Diagnóstico sem expor segredos: só indica se havia espaço/quebra de linha sobrando
    print("Credenciais: "
          f"usuário {'tinha espaço/linha extra (removido)' if raw_u != user else 'sem espaço extra'}; "
          f"senha {'tinha espaço/linha extra (removido)' if raw_p != pw else 'sem espaço extra'}.")
    if "@" in user:
        print("ATENÇÃO: o usuário parece um e-mail. O Earthdata pede o NOME DE USUÁRIO, não o e-mail.")
    H = login(user, pw)
    t0 = time.time()
    ids = dict(zip(TAREFAS, a.resume.split(","))) if a.resume else {}
    if not ids:
        for k, t in tarefas.items():
            r = requests.post(f"{API}/task", json=t, headers=H, timeout=120)
            if r.status_code >= 300:
                sys.exit(f"Falha ao submeter {k}: HTTP {r.status_code} {r.text[:300]}")
            ids[k] = r.json()["task_id"]
            print(f"Submetida {k}: {ids[k]}  (para retomar depois: --resume {','.join(ids.values())})", flush=True)

    linhas = []
    for k, tid in ids.items():
        print(f"\n== Tarefa {k} ({tid})")
        t = esperar(H, tid, a.max_min)
        if t is None:
            print(f"   ainda na fila após {a.max_min} min. Retome depois com: --resume {','.join(ids.values())}")
            continue
        if t["status"] == "error":
            print("   ERRO:", t.get("error")); continue
        print(f"   concluída; {(time.time()-t0)/60:.1f} min desde o início")
        pasta = f"appeears_{k}"
        baixar(H, tid, pasta)
        for (prod, ini), f in sorted(agrupar(pasta).items(), key=lambda x: x[0][1]):
            if len(f) < 3:
                print(f"   {prod} {ini}: arquivos incompletos {sorted(f)}"); continue
            s = estatisticas(f["ndvi"], f["rel"], f["doy"], geom, ini)
            ref = REF.get((prod, ini.isoformat()))
            print(f"   {prod} janela {ini} | escala {s['escala']} | pixels {s['pixels']} usados {s['usados']} ({s['pct']:.2f}%) "
                  f"| NDVI {s['ndvi']:.4f} p10 {s['p10']:.4f} p90 {s['p90']:.4f} | dobs {s['dobs']}")
            if ref:
                diffs = {m: (s[m] - ref[m]) for m in TOL}
                ok = all(abs(diffs[m]) <= TOL[m] for m in TOL)
                print(f"      referência Planetary Computer: {ref}")
                print(f"      diferenças: { {m: round(v, 4) for m, v in diffs.items()} } -> {'OK, reproduz' if ok else 'DIVERGE'}")
            linhas.append({"tarefa": k, "produto": prod, "inicio": ini, **{x: s[x] for x in ('pixels', 'usados', 'pct', 'ndvi', 'p10', 'p90', 'dobs')}})
    if linhas:
        with open("appeears_resultado.csv", "w", newline="") as o:
            w = csv.DictWriter(o, fieldnames=list(linhas[0])); w.writeheader(); w.writerows(linhas)
        print("\nSalvo appeears_resultado.csv")


if __name__ == "__main__":
    main()
