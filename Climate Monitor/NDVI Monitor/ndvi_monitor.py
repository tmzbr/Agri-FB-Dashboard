#!/usr/bin/env python3
"""
ndvi_monitor.py — NDVI por município (MODIS Terra + Aqua) num único arquivo.

  python ndvi_monitor.py sql                 imprime o SQL que cria a tabela no Supabase (rode no SQL Editor, uma vez)
  python ndvi_monitor.py seed  --mode ...    baixa a NASA e grava o NDVI por município no Supabase (full | missing | tail)
  python ndvi_monitor.py build --locations . gera ndvi_history.json (regiões e fazendas) a partir do Supabase
  python ndvi_monitor.py xlsx  --locations . gera o histórico em Excel (ndvi_historico.xlsx), sem Supabase
  python ndvi_monitor.py seed --help         (ou build --help) para as opções

Onde as coisas ficam: o NDVI por município vive no SUPABASE (tabela ndvi_municipio, criada pelo `sql`); no GITHUB
ficam só o código, o ndvi_dashboard.html e o ndvi_history.json (~0,7 MB, regenerado toda semana).

Seções: 1) núcleo (malha, estatística zonal, codificação)  2) fontes de raster  3) seed  4) build  5) SQL.
"""
import argparse, base64, datetime as dt, gzip, json, os, re, struct, sys, time, unicodedata
import urllib.error, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import requests
import rasterio
from pyproj import CRS, Transformer
from rasterio.features import rasterize
from rasterio.transform import Affine
from shapely.geometry import MultiPolygon, Point, mapping, shape
from shapely.ops import transform as shp_transform

HERE = os.path.dirname(os.path.abspath(__file__))


# ══════════════════════════════════════════════════════════════════════
# 1) NÚCLEO
# ══════════════════════════════════════════════════════════════════════
# ───────────────────────────── constantes ─────────────────────────────
SLOTS, MISSING = 46, -32768
VARS = ("ndvi", "p10", "p90", "pct", "dobs")
MODEL = "modis"
PRODUCTS = {"MOD13Q1": "Terra", "MYD13Q1": "Aqua"}       # versão 061
UF_CODES = {43: "RS", 41: "PR", 50: "MS", 51: "MT", 52: "GO", 21: "MA", 17: "TO", 22: "PI", 29: "BA"}

# Municípios fora dos 9 estados onde há fazendas SLC/BrasilAgro do dashboard (códigos IBGE conferidos na API de localidades):
# Unaí/MG, Santana do Araguaia/PA, Bonito de Minas/MG, Brotas/SP. Entram no universo para as fazendas terem NDVI.
EXTRA_MUNICIPIOS = {3170404: "MG", 1506708: "PA", 3108255: "MG", 3507902: "SP"}

SIN = CRS.from_proj4("+proj=sinu +lon_0=0 +x_0=0 +y_0=0 +R=6371007.181 +units=m +no_defs")
_TO_SIN = Transformer.from_crs("EPSG:4326", SIN, always_xy=True).transform
TILE_M = 1111950.5196666666                 # lado do tile MODIS
PIX = TILE_M / 4800                         # pixel "de 250 m" = 231,656 m
X0, Y0 = -20015109.354, 10007554.677        # canto superior esquerdo da grade global
FILL_NDVI = -3000


def to_sin(geom):
    return shp_transform(_TO_SIN, geom)


# ───────────────────────────── HTTP com retry ─────────────────────────────
def http_json(url, params=None, tries=6, timeout=120, session=None, method="get", **kw):
    s = session or requests
    last = None
    for t in range(tries):
        try:
            r = getattr(s, method)(url, params=params, timeout=timeout, **kw)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = repr(e)
        time.sleep(min(60, 2 ** t))
    raise RuntimeError(f"{url} falhou após {tries} tentativas: {last}")


# ───────────────────────────── municípios ─────────────────────────────
def _fetch_one(cod, uf, quality):
    url = (f"https://servicodados.ibge.gov.br/api/v3/malhas/municipios/{cod}"
           f"?formato=application/vnd.geo+json&qualidade={quality}")
    f = http_json(url, timeout=180)["features"][0]
    f["properties"] = {"cod": cod, "uf": uf}
    return f


def load_municipios(cache_path=None, quality="intermediaria"):
    """[{cod, uf, geom}]: 2.359 municípios dos 9 estados + EXTRA_MUNICIPIOS (IBGE). Usa/atualiza o cache gz.

    A malha 'intermediária' erra <1,5% da área contra a máxima (medido em 7 municípios).
    """
    fc = None
    if cache_path and os.path.exists(cache_path):
        fc = json.loads(gzip.open(cache_path, "rt", encoding="utf-8").read())
    changed = False
    if fc is None:
        feats = []
        for ufc, uf in UF_CODES.items():
            url = (f"https://servicodados.ibge.gov.br/api/v3/malhas/estados/{ufc}"
                   f"?intrarregiao=municipio&formato=application/vnd.geo+json&qualidade={quality}")
            for f in http_json(url, timeout=180)["features"]:
                f["properties"] = {"cod": int(f["properties"]["codarea"]), "uf": uf}
                feats.append(f)
        fc, changed = {"type": "FeatureCollection", "features": feats}, True
    have = {f["properties"]["cod"] for f in fc["features"]}
    for cod, uf in EXTRA_MUNICIPIOS.items():
        if cod not in have:
            fc["features"].append(_fetch_one(cod, uf, quality)); changed = True
    if changed and cache_path:
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        with gzip.open(cache_path, "wt", encoding="utf-8") as o:
            json.dump(fc, o)
    muns = [{"cod": f["properties"]["cod"], "uf": f["properties"]["uf"], "geom": shape(f["geometry"])}
            for f in fc["features"]]
    muns.sort(key=lambda m: m["cod"])
    return muns


# ───────────────────────────── grade e rótulos ─────────────────────────────
def tile_of(x, y):
    return int((x - X0) // TILE_M), int((Y0 - y) // TILE_M)


def tile_transform(h, v):
    return Affine(PIX, 0, X0 + h * TILE_M, 0, -PIX, Y0 - v * TILE_M)


def labels_for_grid(geoms_sin, idxs, transform, shape_hw):
    """Raster de rótulos (0 = fora; i+1 = município i) na grade dada. Centro do pixel dentro do polígono
    (mesma regra do rasterio.mask / AppEEARS usada nos testes)."""
    minx, maxy = transform * (0, 0)
    maxx, miny = transform * (shape_hw[1], shape_hw[0])
    from shapely.geometry import box
    frame = box(min(minx, maxx), min(miny, maxy), max(minx, maxx), max(miny, maxy))
    shapes = [(geoms_sin[i], i + 1) for i in idxs if geoms_sin[i].intersects(frame)]
    if not shapes:
        return np.zeros(shape_hw, dtype="uint16")
    return rasterize(shapes, out_shape=shape_hw, transform=transform, fill=0, dtype="uint16", all_touched=False)


class TilePlan:
    """Para cada tile MODIS que cruza algum município: janela mínima (linhas/colunas) e rótulos recortados."""

    def __init__(self, h, v, window, transform, labels):
        self.h, self.v, self.window, self.transform, self.labels = h, v, window, transform, labels
        self.name = f"h{h:02d}v{v:02d}"


def build_tile_plans(municipios):
    """Rasteriza os municípios em cada tile e recorta à caixa dos municípios. Devolve (plans, split_mask)
    onde split_mask[i+1] = True se o município i tem pixels em 2+ tiles (precisa juntar pixels dos tiles)."""
    from rasterio.windows import Window
    geoms_sin = [to_sin(m["geom"]) for m in municipios]
    by_tile = {}
    for i, g in enumerate(geoms_sin):
        x0_, y0_, x1_, y1_ = g.bounds
        h0, v0 = tile_of(x0_, y1_)
        h1, v1 = tile_of(x1_, y0_)
        for h in range(h0, h1 + 1):
            for v in range(v0, v1 + 1):
                by_tile.setdefault((h, v), []).append(i)
    plans, tiles_of_mun = [], {}
    for (h, v), idxs in sorted(by_tile.items()):
        full = labels_for_grid(geoms_sin, idxs, tile_transform(h, v), (4800, 4800))
        rows, cols = np.where(full.any(axis=1))[0], np.where(full.any(axis=0))[0]
        if rows.size == 0:
            continue
        r0, r1, c0, c1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
        lab = np.ascontiguousarray(full[r0:r1, c0:c1])
        win = Window(c0, r0, c1 - c0, r1 - r0)
        tf = tile_transform(h, v) * Affine.translation(c0, r0)
        plans.append(TilePlan(h, v, win, tf, lab))
        for lbl in np.unique(lab):
            if lbl:
                tiles_of_mun.setdefault(int(lbl), set()).add((h, v))
    split = np.zeros(len(municipios) + 1, dtype=bool)
    for lbl, ts in tiles_of_mun.items():
        if len(ts) > 1:
            split[lbl] = True
    return plans, split


# ───────────────────────────── estatísticas zonais vetorizadas ─────────────────────────────
def _grouped_sorted(lab, val, nlab):
    """Ordena os valores por (rótulo, valor). Devolve (val_ordenado, contagem, início de cada grupo)."""
    key = (lab.astype(np.int64) << 16) | (val.astype(np.int64) + 32768)
    key.sort()
    vs = (key & 0xFFFF) - 32768
    cnt = np.bincount((key >> 16).astype(np.int64), minlength=nlab + 1)
    start = np.concatenate(([0], np.cumsum(cnt)[:-1]))
    return vs, cnt, start


def _percentile(vs, cnt, start, q):
    """Percentil com interpolação linear (igual ao np.percentile padrão), por grupo; NaN nos grupos vazios."""
    ok = cnt > 0
    pos = np.where(ok, (cnt - 1) * q / 100.0, 0.0)
    lo = np.floor(pos).astype(np.int64)
    hi = np.minimum(lo + 1, np.maximum(cnt - 1, 0))
    frac = pos - lo
    last = max(len(vs) - 1, 0)
    a = vs[np.minimum(start + lo, last)] if len(vs) else np.zeros_like(lo)
    b = vs[np.minimum(start + hi, last)] if len(vs) else np.zeros_like(lo)
    out = a * (1 - frac) + b * frac
    return np.where(ok, out, np.nan)


def group_stats(lab, nd, dy, nlab):
    """Só pixels JÁ filtrados (válidos e confiabilidade 0/1). Devolve cnt, mean, p10, p90, mediana do dia do ano."""
    if lab.size == 0:
        z = np.full(nlab + 1, np.nan)
        return np.zeros(nlab + 1, dtype=np.int64), z, z.copy(), z.copy(), z.copy()
    vs, cnt, start = _grouped_sorted(lab, nd, nlab)
    mean = np.where(cnt > 0, np.bincount(lab, weights=nd.astype(np.float64), minlength=nlab + 1) / np.maximum(cnt, 1), np.nan)
    p10, p90 = _percentile(vs, cnt, start, 10), _percentile(vs, cnt, start, 90)
    ds, dcnt, dstart = _grouped_sorted(lab, dy, nlab)
    med = _percentile(ds, dcnt, dstart, 50)
    return cnt, mean, p10, p90, med


def select_pixels(ndvi, rel, doy, labels):
    """Pixels dentro de algum município: rótulo, NDVI bruto, 'usado' (válido e bom/marginal), dia do ano."""
    inside = labels > 0
    lab, nd, rl, dy = labels[inside], ndvi[inside], rel[inside], doy[inside]
    valid = (nd >= -2000) & (nd <= 10000)               # exclui o preenchimento (-3000)
    used = valid & ((rl == 0) | (rl == 1))
    return lab, nd, used, dy


def dobs_from_median(med, start):
    """Dias entre o início da janela e a data efetiva (mediana do dia do ano), tratando a virada do ano."""
    sd = start.timetuple().tm_yday
    diy = 366 if (start.year % 4 == 0 and (start.year % 100 != 0 or start.year % 400 == 0)) else 365
    dd = np.floor(med)                                   # int(mediana), como no teste de Sorriso
    return np.where(dd < sd - 3, dd + diy - sd, dd - sd)


class WindowAccumulator:
    """Acumula, tile a tile, os pixels de UMA janela. Municípios em um único tile são calculados na hora;
    os que cruzam tiles (5,6%) guardam os pixels e são calculados juntos no fim."""

    def __init__(self, nmun, split_mask, start):
        self.nmun, self.split, self.start = nmun, split_mask, start
        self.cnt = np.zeros(nmun + 1, dtype=np.int64)
        self.tot = np.zeros(nmun + 1, dtype=np.int64)
        self.mean = np.full(nmun + 1, np.nan)
        self.p10, self.p90, self.med = self.mean.copy(), self.mean.copy(), self.mean.copy()
        self._sp = {"lab": [], "nd": [], "dy": []}
        self.seen = np.zeros(nmun + 1, dtype=bool)

    def add_raster(self, ndvi, rel, doy, labels):
        lab, nd, used, dy = select_pixels(ndvi, rel, doy, labels)
        tot = np.bincount(lab, minlength=self.nmun + 1)
        self.seen |= tot > 0
        sp = self.split[lab]
        # 1) municípios inteiros neste raster → estatística direta
        keep = used & ~sp
        c, m, a, b, md = group_stats(lab[keep], nd[keep], dy[keep], self.nmun)
        own = ~self.split & (tot > 0)
        self.tot[own] = tot[own]
        for dst, src in ((self.cnt, c), (self.mean, m), (self.p10, a), (self.p90, b), (self.med, md)):
            dst[own] = src[own]
        # 2) municípios divididos → guarda pixels e soma totais
        self.tot[self.split] += tot[self.split]
        k2 = used & sp
        if k2.any():
            self._sp["lab"].append(lab[k2]); self._sp["nd"].append(nd[k2]); self._sp["dy"].append(dy[k2])

    def finish(self):
        """Devolve {var: int16[nmun]} (índice 0 = município 0), MISSING onde não há medida."""
        if self._sp["lab"]:
            lab = np.concatenate(self._sp["lab"]); nd = np.concatenate(self._sp["nd"]); dy = np.concatenate(self._sp["dy"])
            c, m, a, b, md = group_stats(lab, nd, dy, self.nmun)
            s = self.split
            for dst, src in ((self.cnt, c), (self.mean, m), (self.p10, a), (self.p90, b), (self.med, md)):
                dst[s] = src[s]
        n = self.nmun
        meas = self.seen[1:] & (self.tot[1:] > 0)
        have = meas & (self.cnt[1:] > 0)
        out = {v: np.full(n, MISSING, dtype=np.int16) for v in VARS}
        r = lambda x: np.rint(np.nan_to_num(x[1:], nan=0.0)).astype(np.int64)
        out["ndvi"][have] = np.clip(r(self.mean), -2000, 10000)[have]
        out["p10"][have] = np.clip(r(self.p10), -2000, 10000)[have]
        out["p90"][have] = np.clip(r(self.p90), -2000, 10000)[have]
        out["pct"][meas] = np.rint(1000.0 * self.cnt[1:][meas] / self.tot[1:][meas]).astype(np.int16)
        out["dobs"][have] = dobs_from_median(self.med[1:], self.start)[have].astype(np.int16)
        return out


# ───────────────────────────── janelas e codificação ─────────────────────────────
def slot_of(d):
    return (d.timetuple().tm_yday - 1) // 8


def slot_start(year, slot):
    return dt.date(year, 1, 1) + dt.timedelta(days=8 * slot)


def sensor_of_slot(slot):
    return "Terra" if slot % 2 == 0 else "Aqua"


def enc(arr):
    return base64.b64encode(struct.pack(f"<{SLOTS}h", *[int(x) for x in arr])).decode()


def dec(s):
    return np.array(struct.unpack(f"<{SLOTS}h", base64.b64decode(s)), dtype=np.int16)


class YearStore:
    """Matrizes (município × 46 posições) por ano e variável. Espelha as linhas de ndvi_municipio."""

    def __init__(self, cods):
        self.cods = list(cods)
        self.row = {c: i for i, c in enumerate(self.cods)}
        self.data, self.dirty = {}, set()

    def ensure(self, year):
        if year not in self.data:
            self.data[year] = {v: np.full((len(self.cods), SLOTS), MISSING, dtype=np.int16) for v in VARS}
        return self.data[year]

    def load_rows(self, rows):
        for r in rows:
            i = self.row.get(r["cod_ibge"])
            if i is None:
                continue
            d = self.ensure(r["year"])
            for v in VARS:
                if r.get(v):
                    d[v][i] = dec(r[v])

    def set_window(self, year, slot, values):
        d = self.ensure(year)
        for v in VARS:
            d[v][:, slot] = values[v]
        self.dirty.add(year)

    def filled_slots(self, year, min_frac=0.5):
        """Posições com NDVI em ao menos metade dos municípios (janela realmente coletada)."""
        if year not in self.data:
            return set()
        have = (self.data[year]["ndvi"] != MISSING).mean(axis=0)
        return {int(s) for s in np.where(have >= min_frac)[0]}

    def rows(self, year):
        d = self.ensure(year)
        return [{"cod_ibge": c, "model": MODEL, "year": year, **{v: enc(d[v][i]) for v in VARS}}
                for i, c in enumerate(self.cods) if (d["ndvi"][i] != MISSING).any() or (d["pct"][i] != MISSING).any()]


# ───────────────────────────── catálogo da NASA (CMR) ─────────────────────────────
CMR = "https://cmr.earthdata.nasa.gov/search/granules.json"
REF_POINT = (-55.71, -12.54)        # Sorriso (tile h12v10): referência do calendário; todos os tiles têm as mesmas janelas


def cmr_windows(short_name, t0, t1, point=REF_POINT):
    """Datas de início das janelas JÁ PUBLICADAS pela NASA entre t0 e t1 (date). Público, sem login."""
    out, page = set(), 1
    while True:
        j = http_json(CMR, params={"short_name": short_name, "version": "061", "point": f"{point[0]},{point[1]}",
                                   "temporal": f"{t0.isoformat()}T00:00:00Z,{t1.isoformat()}T23:59:59Z",
                                   "page_size": 500, "page_num": page})
        ent = j["feed"]["entry"]
        for e in ent:
            d = dt.date.fromisoformat(e["time_start"][:10])
            if t0 <= d <= t1:
                out.add(d)
        if len(ent) < 500:
            return sorted(out)
        page += 1

# ══════════════════════════════════════════════════════════════════════
# 2) FONTES DE RASTER
# ══════════════════════════════════════════════════════════════════════
ID_RX = re.compile(r"^(?P<prod>M[OY]D13Q1)\.A(?P<y>\d{4})(?P<d>\d{3})\.(?P<tile>h\d\dv\d\d)\.061\.(?P<ver>\d+)$")


def _gdal_env():
    kw = dict(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", CPL_VSIL_CURL_ALLOWED_EXTENSIONS=".tif",
              GDAL_HTTP_MAX_RETRY="5", GDAL_HTTP_RETRY_DELAY="2", GDAL_HTTP_TIMEOUT="120")
    if os.environ.get("NDVI_INSECURE_SSL"):              # só para sandboxes com proxy TLS; nunca em produção
        kw["GDAL_HTTP_UNSAFESSL"] = "YES"
    return rasterio.Env(**kw)


# ═════════════════════════ Planetary Computer ═════════════════════════
class PCSource:
    STAC = "https://planetarycomputer.microsoft.com/api/stac/v1/search"
    SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token/{acct}/{cont}"
    COLLECTION = "modis-13Q1-061"
    BBOX = [-61.8, -33.9, -36.9, -2.5]                    # 9 estados
    ASSETS = {"ndvi": "250m_16_days_NDVI", "rel": "250m_16_days_pixel_reliability",
              "doy": "250m_16_days_composite_day_of_the_year"}

    def __init__(self):
        self._tok = {}                                      # (conta, container) -> (token, expira)

    # --- token SAS (vale ~45 min; renova 5 min antes de vencer)
    def _sign(self, href):
        u = urllib.parse.urlparse(href)
        acct, cont = u.netloc.split(".")[0], u.path.strip("/").split("/")[0]
        tok, exp = self._tok.get((acct, cont), (None, 0))
        if not tok or time.time() > exp - 300:
            j = http_json(self.SAS.format(acct=acct, cont=cont))
            tok = j["token"]
            se = urllib.parse.parse_qs(tok).get("se", [None])[0]
            exp = dt.datetime.fromisoformat(se.replace("Z", "+00:00")).timestamp() if se else time.time() + 1800
            self._tok[(acct, cont)] = (tok, exp)
        return href + "?" + tok

    # --- busca: {(produto, início): {tile: item}} (mantém a versão de processamento mais recente)
    def find(self, d0, d1, tiles):
        found, body = {}, {"collections": [self.COLLECTION], "bbox": self.BBOX, "limit": 500,
                           "datetime": f"{d0.isoformat()}T00:00:00Z/{d1.isoformat()}T23:59:59Z"}
        url, method, payload = self.STAC, "post", body
        while url:
            j = http_json(url, method=method, **({"json": payload} if method == "post" else {}))
            for it in j["features"]:
                m = ID_RX.match(it["id"])
                if not m or m["tile"] not in tiles:
                    continue
                start = dt.date(int(m["y"]), 1, 1) + dt.timedelta(days=int(m["d"]) - 1)
                key, cur = (m["prod"], start), found.setdefault((m["prod"], start), {})
                old = cur.get(m["tile"])
                if old is None or int(m["ver"]) > old["_ver"]:
                    it["_ver"] = int(m["ver"])
                    cur[m["tile"]] = it
            nxt = next((l for l in j.get("links", []) if l.get("rel") == "next"), None)
            if not nxt:
                break
            url, method = nxt["href"], nxt.get("method", "get").lower()
            if method == "post":
                payload = {**body, **nxt["body"]} if nxt.get("merge") else nxt.get("body", body)
            else:
                payload = None
        return found

    # --- leitura do recorte de um tile (3 camadas)
    def read_tile(self, item, plan):
        arrs = {}
        with _gdal_env():
            for k, a in self.ASSETS.items():
                with rasterio.open(self._sign(item["assets"][a]["href"])) as src:
                    if src.crs is None or src.transform == Affine.identity():
                        raise ValueError(f"{item['id']} {a}: raster sem georreferenciamento")
                    exp = tile_transform(plan.h, plan.v)
                    if abs(src.transform.c - exp.c) > 0.01 or abs(src.transform.f - exp.f) > 0.01 or abs(src.transform.a - exp.a) > 1e-6:
                        raise ValueError(f"{item['id']} {a}: grade diferente da esperada")
                    arrs[k] = src.read(1, window=plan.window)
        if not (arrs["ndvi"].shape == arrs["rel"].shape == arrs["doy"].shape == plan.labels.shape):
            raise ValueError(f"{item['id']}: formato inesperado {arrs['ndvi'].shape} vs {plan.labels.shape}")
        return arrs["ndvi"], arrs["rel"], arrs["doy"]


# ═════════════════════════ AppEEARS ═════════════════════════
class AppEEARSSource:
    API = "https://appeears.earthdatacloud.nasa.gov/api"
    LAYERS = ["_250m_16_days_NDVI", "_250m_16_days_pixel_reliability", "_250m_16_days_composite_day_of_the_year"]
    RX = re.compile(r"^(?P<prod>M[OY]D13Q1)\.061_+(?P<layer>.+?)_doy(?P<y>\d{4})(?P<d>\d{3})")

    def __init__(self, user, password):
        user, password = (user or "").strip(), (password or "").strip()
        r = requests.post(f"{self.API}/login", auth=(user, password), timeout=60)
        if r.status_code != 200:
            raise RuntimeError(f"Login AppEEARS recusado (HTTP {r.status_code}): confira usuário (não o e-mail) e senha.")
        self.H = {"Authorization": "Bearer " + r.json()["token"]}

    def submit(self, name, polygon, d0, d1, products=("MOD13Q1.061", "MYD13Q1.061")):
        task = {"task_type": "area", "task_name": name,
                "params": {"dates": [{"startDate": d0.strftime("%m-%d-%Y"), "endDate": d1.strftime("%m-%d-%Y")}],
                           "layers": [{"product": p, "layer": c} for p in products for c in self.LAYERS],
                           "geo": {"type": "FeatureCollection", "features": [
                               {"type": "Feature", "geometry": polygon, "properties": {"id": name}}]},
                           "output": {"format": {"type": "geotiff"}, "projection": "native"}}}
        for t in range(6):
            r = requests.post(f"{self.API}/task", json=task, headers=self.H, timeout=120)
            if r.status_code < 300:
                return r.json()["task_id"]
            if r.status_code < 500 and r.status_code != 429:
                raise RuntimeError(f"AppEEARS recusou a tarefa {name}: HTTP {r.status_code} {r.text[:300]}")
            time.sleep(min(120, 10 * (t + 1)))
        raise RuntimeError(f"AppEEARS indisponível ao submeter {name}")

    def wait(self, task_ids, max_min=60, poll=30):
        """Espera todas; devolve {id: status}. Tarefas ainda na fila voltam como 'pending'/'processing'."""
        t0, st = time.time(), {}
        while True:
            for tid in task_ids:
                if st.get(tid) in ("done", "error"):
                    continue
                r = requests.get(f"{self.API}/task/{tid}", headers=self.H, timeout=60)
                st[tid] = r.json().get("status") if r.status_code == 200 else st.get(tid, "pending")
            if all(st[t] in ("done", "error") for t in task_ids) or (time.time() - t0) / 60 > max_min:
                return st
            time.sleep(poll)

    def download(self, tid, folder):
        os.makedirs(folder, exist_ok=True)
        b = requests.get(f"{self.API}/bundle/{tid}", headers=self.H, timeout=60).json()
        for f in b["files"]:
            if not f["file_name"].lower().endswith(".tif"):
                continue
            dst = os.path.join(folder, os.path.basename(f["file_name"]))
            if os.path.exists(dst):
                continue
            for t in range(4):
                r = requests.get(f"{self.API}/bundle/{tid}/{f['file_id']}", headers=self.H, timeout=600, stream=True)
                if r.status_code == 200:
                    with open(dst + ".part", "wb") as o:
                        for ch in r.iter_content(1 << 20):
                            o.write(ch)
                    os.replace(dst + ".part", dst)
                    break
                time.sleep(5 * (t + 1))
            else:
                raise RuntimeError(f"download falhou: {f['file_name']}")

    @classmethod
    def group(cls, folder):
        """{(produto, início): {'ndvi','rel','doy': caminho}} a partir dos .tif baixados."""
        g = {}
        for f in sorted(os.listdir(folder)):
            m = cls.RX.match(f)
            if not (f.lower().endswith(".tif") and m):
                continue
            start = dt.date(int(m["y"]), 1, 1) + dt.timedelta(days=int(m["d"]) - 1)
            lay = m["layer"]
            k = "ndvi" if lay.endswith("NDVI") else "rel" if "pixel_reliability" in lay else "doy" if "composite_day" in lay else None
            if k:
                g.setdefault((m["prod"], start), {})[k] = os.path.join(folder, f)
        return {k: v for k, v in g.items() if len(v) == 3}

    @staticmethod
    def read(paths):
        """(ndvi int16 bruto, rel, doy, transform). Aceita raster já escalado (float) e converte para bruto."""
        out = {}
        for k, p in paths.items():
            with rasterio.open(p) as src:
                if src.crs is None or src.transform == Affine.identity():
                    raise ValueError(f"{p}: sem georreferenciamento")
                out[k] = src.read(1)
                tf = src.transform
        nd = out["ndvi"]
        if nd.dtype.kind == "f":                                   # escalado: valores válidos em [-0,2; 1]; fill pode vir -3000
            cand = nd[nd > -2999]
            if cand.size and float(np.nanmax(np.abs(cand))) < 20:
                nd = np.where(nd > -2999, np.rint(nd * 1e4), FILL_NDVI)
        return nd.astype("int16"), out["rel"], out["doy"].astype("int16"), tf


def uf_polygons(municipios, pad=0.02):
    """Retângulo (lon/lat) que cobre os municípios de cada UF — o AppEEARS recorta nesta caixa.
    Usa só os limites (bounds) de cada município: não depende de a geometria ser válida (unary_union quebrava em MT)."""
    from shapely.geometry import box, mapping
    out = {}
    for uf in sorted({m["uf"] for m in municipios}):
        b = [m["geom"].bounds for m in municipios if m["uf"] == uf]
        x0, y0 = min(t[0] for t in b), min(t[1] for t in b)
        x1, y1 = max(t[2] for t in b), max(t[3] for t in b)
        out[uf] = mapping(box(x0 - pad, y0 - pad, x1 + pad, y1 + pad))
    return out


# ───────────────────────────── Supabase ─────────────────────────────
class Supa:
    def __init__(self, url, key):
        self.url = url.rstrip("/") + "/rest/v1/ndvi_municipio"
        auth = {"apikey": key, "Authorization": "Bearer " + key}
        self.hget = dict(auth)    # GET sem "return=minimal": senão o PostgREST devolve corpo vazio
        self.h = {**auth, "Content-Type": "application/json", "Prefer": "resolution=merge-duplicates,return=minimal"}

    def rows(self, year, cols="cod_ibge,model,year,ndvi,p10,p90,pct,dobs"):
        out, off, PAGE = [], 0, 1000
        while True:
            u = (f"{self.url}?select={cols}&model=eq.{MODEL}&year=eq.{year}"
                 f"&order=cod_ibge&limit={PAGE}&offset={off}")
            req = urllib.request.Request(u, headers=self.hget, method="GET")
            with urllib.request.urlopen(req, timeout=120) as r:
                page = json.loads(r.read().decode())
            out += page
            if len(page) < PAGE:
                return out
            off += PAGE

    def upsert(self, rows, batch=200, tries=5):
        for i in range(0, len(rows), batch):
            body = json.dumps(rows[i:i + batch]).encode()
            for t in range(tries):
                try:
                    req = urllib.request.Request(self.url + "?on_conflict=cod_ibge,model,year", data=body, headers=self.h, method="POST")
                    with urllib.request.urlopen(req, timeout=180) as r:
                        if r.status < 300:
                            break
                except urllib.error.HTTPError as e:
                    detail = e.read()[:300].decode(errors="replace")
                    if e.code < 500 or t == tries - 1:
                        raise RuntimeError(f"upsert {e.code}: {detail}")
                except Exception:
                    if t == tries - 1:
                        raise
                time.sleep(4 * (t + 1))


class LocalSink:
    """Grava num JSON em vez do banco (testes e execuções a seco com resultado)."""

    def __init__(self, path):
        self.path = path
        self.data = json.load(open(path)) if os.path.exists(path) else {}

    def rows(self, year, cols=None):
        return self.data.get(str(year), [])

    def upsert(self, rows):
        for r in rows:
            d = {x["cod_ibge"]: x for x in self.data.get(str(r["year"]), [])}
            d[r["cod_ibge"]] = r
            self.data[str(r["year"])] = sorted(d.values(), key=lambda x: x["cod_ibge"])
        json.dump(self.data, open(self.path, "w"))


# ───────────────────────────── plano de trabalho ─────────────────────────────
def parse_years(s, today):
    out = set()
    for part in s.split(","):
        if "-" in part:
            a, b = part.split("-")
            out |= set(range(int(a), int(b) + 1))
        else:
            out.add(int(part))
    return sorted(y for y in out if 2000 <= y <= today.year)


def expected_windows(years, today):
    """[(produto, início)] publicados pela NASA nos anos pedidos (catálogo CMR, sem login)."""
    out = []
    for prod in PRODUCTS:
        for y in years:
            if y < FIRST_YEAR[prod]:
                continue
            for d in cmr_windows(prod, dt.date(y, 1, 1), min(dt.date(y, 12, 31), today)):
                out.append((prod, d))
    return sorted(out, key=lambda x: (x[1], x[0]))


def plan_todo(mode, years, store, today, refresh):
    exp = expected_windows(years, today)
    filled = {y: store.filled_slots(y) for y in years}
    if mode == "full":
        return exp, exp
    missing = [w for w in exp if slot_of(w[1]) not in filled[w[1].year]]
    if mode == "missing":
        return exp, missing
    # tail: faltantes + as últimas `refresh` janelas já gravadas de cada produto (reemissões da NASA)
    stored = [w for w in exp if slot_of(w[1]) in filled[w[1].year]]
    redo = []
    for prod in PRODUCTS:
        redo += [w for w in stored if w[0] == prod][-refresh:] if refresh > 0 else []
    return exp, sorted(set(missing) | set(redo), key=lambda x: (x[1], x[0]))


# ───────────────────────────── processamento ─────────────────────────────
def check_window(vals, nmun, label):
    cov = float((vals["ndvi"] != MISSING).sum()) / nmun
    if cov < 0.5:
        raise RuntimeError(f"{label}: só {cov:.0%} dos municípios com NDVI — janela descartada (provável leitura incompleta)")
    return cov


def process_pc(pc, plans, split, nmun, start, items, workers):
    acc = WindowAccumulator(nmun, split, start)
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(pc.read_tile, items[p.name], p): p for p in plans}
        for f in as_completed(futs):
            nd, rel, dy = f.result()
            acc.add_raster(nd, rel, dy, futs[f].labels)
            del nd, rel, dy
    return acc.finish()


def run_pc(todo, a, ctx, commit):
    pc, plans, split, muns = PCSource(), ctx["plans"], ctx["split"], ctx["muns"]
    tiles = {p.name for p in plans}
    done, unavailable, failed = [], [], []
    by_year = {}
    for w in todo:
        by_year.setdefault(w[1].year, []).append(w)
    for year, ws in sorted(by_year.items()):
        try:
            found = pc.find(min(x[1] for x in ws), max(x[1] for x in ws), tiles)
        except Exception as e:
            print(f"  [PC] busca {year} falhou: {e}"); failed += ws; continue
        for prod, start in ws:
            items = found.get((prod, start), {})
            if set(items) != tiles:
                unavailable.append((prod, start)); continue
            t0 = time.time()
            try:
                vals = process_pc(pc, plans, split, len(muns), start, items, a.workers)
                cov = check_window(vals, len(muns), f"{prod} {start}")
            except Exception as e:
                print(f"  [PC] {prod} {start} FALHOU: {e}"); failed.append((prod, start)); continue
            ctx["store"].set_window(start.year, slot_of(start), vals)
            done.append((prod, start))
            print(f"  [PC] {prod} {start} ok ({time.time()-t0:.0f}s, NDVI em {cov:.0%} dos municípios)", flush=True)
            commit(len(done))
    return done, unavailable, failed


def run_appeears(todo, a, ctx, commit):
    user, pw = os.environ.get("EARTHDATA_USER", ""), os.environ.get("EARTHDATA_PASS", "")
    if not (user.strip() and pw.strip()):
        print("  [AppEEARS] sem EARTHDATA_USER/EARTHDATA_PASS — janelas não coletadas:", len(todo))
        return [], list(todo), []
    ap = AppEEARSSource(user, pw)
    muns, split = ctx["muns"], ctx["split"]
    polys = uf_polygons(muns)
    geoms_sin = ctx["geoms_sin"]
    uf_idx = {uf: [i for i, m in enumerate(muns) if m["uf"] == uf] for uf in polys}
    wanted = sorted(set(todo), key=lambda x: x[1])
    # agrupa janelas consecutivas (lacuna > 20 dias abre outro grupo); cada tarefa cobre no máximo 6 datas de início
    # (≈ 3 janelas por satélite) para os arquivos de uma UF grande (MT, ~70 MB por camada) caberem no disco do runner
    groups, cur = [], []
    for w in wanted:
        if cur and ((w[1] - cur[-1][1]).days > 20 or len({x[1] for x in cur}) >= 6):
            groups.append(cur); cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    par = max(1, getattr(a, "appeears_parallel", 6))
    ufs = list(polys.items())
    done, failed = [], []
    for gi, grp in enumerate(groups):
        d0, d1 = min(x[1] for x in grp), max(x[1] for x in grp)
        want = set(grp)
        print(f"  [AppEEARS] grupo {gi+1}/{len(groups)}: {d0} → {d1} ({len(want)} janelas) — {len(ufs)} tarefas (uma por UF, {par} por vez)", flush=True)
        accs = {w: WindowAccumulator(len(muns), np.zeros(len(muns) + 1, dtype=bool), w[1]) for w in want}
        ok_ufs = set()
        # ondas de `par` UFs: o limite de tarefas simultâneas da conta não é documentado
        for wi in range(0, len(ufs), par):
            tids = {}
            for uf, poly in ufs[wi:wi + par]:
                try:
                    tids[uf] = ap.submit(f"ndvi_{uf}_{d0:%Y%m%d}_{d1:%Y%m%d}", poly, d0, d1)
                except Exception as e:
                    print(f"    {uf}: não submetida ({e})")
            st = ap.wait(list(tids.values()), max_min=a.max_wait)
            for uf, tid in tids.items():
                if st.get(tid) != "done":
                    print(f"    {uf}: tarefa {tid} terminou como '{st.get(tid)}' — janelas deste grupo ficam para o modo missing")
                    continue
                folder = os.path.join(a.workdir, f"{uf}_{d0:%Y%m%d}")
                ap.download(tid, folder)
                for (prod, start), paths in ap.group(folder).items():
                    if (prod, start) not in want:
                        continue
                    nd, rel, dy, tf = ap.read(paths)
                    lab = labels_for_grid(geoms_sin, uf_idx[uf], tf, nd.shape)
                    accs[(prod, start)].add_raster(nd, rel, dy, lab)
                ok_ufs.add(uf)
                for f in os.listdir(folder):
                    os.remove(os.path.join(folder, f))
        for w, acc in accs.items():
            try:
                if ok_ufs != set(polys):
                    raise RuntimeError(f"UFs incompletas ({len(ok_ufs)}/{len(polys)}) — janela não gravada para não deixar buracos")
                vals = acc.finish()
                check_window(vals, len(muns), f"{w[0]} {w[1]}")
            except Exception as e:
                print(f"  [AppEEARS] {w[0]} {w[1]} FALHOU: {e}"); failed.append(w); continue
            ctx["store"].set_window(w[1].year, slot_of(w[1]), vals)
            done.append(w)
            print(f"  [AppEEARS] {w[0]} {w[1]} ok", flush=True)
            commit(len(done))
    return done, [], failed


# ───────────────────────────── main ─────────────────────────────
def cmd_seed(argv=None):
    ap = argparse.ArgumentParser(description=SEED_DOC, prog="ndvi_monitor.py seed", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["full", "tail", "missing"], required=True)
    ap.add_argument("--years", default="", help="ex.: 2000-2021 | 2024 | 2022,2025 (tail: ano corrente se vazio)")
    ap.add_argument("--source", choices=["pc", "appeears", "auto"], default="auto")
    ap.add_argument("--refresh", type=int, default=3, help="tail: reprocessa as últimas N janelas já gravadas por produto")
    ap.add_argument("--flush-every", type=int, default=8, help="grava no banco a cada N janelas")
    ap.add_argument("--workers", type=int, default=4, help="tiles lidos em paralelo (Planetary Computer)")
    ap.add_argument("--max-wait", type=int, default=90, help="AppEEARS: espera máxima por onda de tarefas (min)")
    ap.add_argument("--appeears-parallel", type=int, default=6, help="AppEEARS: tarefas (UFs) simultâneas")
    ap.add_argument("--workdir", default=os.path.join(HERE, "work"))
    ap.add_argument("--cache", default=DEFAULT_CACHE, help="cache gz da malha municipal")
    ap.add_argument("--from-date", help="AAAA-MM-DD: só janelas que começam a partir desta data (fatiar a carga)")
    ap.add_argument("--to-date", help="AAAA-MM-DD: só janelas que começam até esta data")
    ap.add_argument("--limit-windows", type=int, default=0, help="teste: processa só as N primeiras janelas do plano")
    ap.add_argument("--local-out", help="grava neste JSON em vez do Supabase")
    ap.add_argument("--dry-run", action="store_true", help="não busca raster nem escreve: só mostra o plano")
    ap.add_argument("--today", help="AAAA-MM-DD (testes)")
    a = ap.parse_args(argv)

    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    if a.years:
        years = parse_years(a.years, today)
    elif a.mode == "tail":
        years = sorted({today.year} | ({today.year - 1} if today.month <= 2 else set()))
    else:
        sys.exit("--years é obrigatório nos modos full e missing")

    if a.local_out:
        sink = LocalSink(a.local_out)
    else:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            if a.dry_run:
                sink = None
            else:
                sys.exit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios (ou use --local-out / --dry-run).")
        else:
            sink = Supa(url, key)

    print(f"modo={a.mode} anos={years[0]}–{years[-1]} ({len(years)}) fonte={a.source} hoje={today}")
    muns = load_municipios(cache_path=a.cache)
    store = YearStore([m["cod"] for m in muns])
    if sink is not None:
        for y in years:
            store.load_rows(sink.rows(y))
    exp, todo = plan_todo(a.mode, years, store, today, a.refresh)
    if a.from_date:
        todo = [w for w in todo if w[1] >= dt.date.fromisoformat(a.from_date)]
    if a.to_date:
        todo = [w for w in todo if w[1] <= dt.date.fromisoformat(a.to_date)]
    if a.limit_windows:
        todo = todo[:a.limit_windows]
    print(f"janelas publicadas pela NASA: {len(exp)} | já gravadas: {sum(len(store.filled_slots(y)) for y in years)} posições | a processar: {len(todo)}")
    if a.dry_run or not todo:
        for w in todo[:12]:
            print("   ", w[0], w[1], f"(posição {slot_of(w[1])} de {w[1].year}, {PRODUCTS[w[0]]})")
        if len(todo) > 12:
            print(f"    … +{len(todo)-12}")
        return 0

    t0 = time.time()
    plans, split = build_tile_plans(muns)
    ctx = dict(muns=muns, plans=plans, split=split, store=store, geoms_sin=[to_sin(m["geom"]) for m in muns])
    print(f"grade pronta em {time.time()-t0:.0f}s: {len(plans)} tiles, {int(split.sum())} municípios em 2+ tiles")

    flushed = {"n": 0}

    def flush():
        for y in sorted(store.dirty):
            rows = store.rows(y)
            sink.upsert(rows)
            print(f"  → gravadas {len(rows)} linhas de {y}", flush=True)
        store.dirty.clear()

    def commit(n):
        if n % a.flush_every == 0:
            flush()

    done, unavail, failed = [], [], []
    try:
        if a.source in ("pc", "auto"):
            d, u, f = run_pc(todo, a, ctx, commit)
            done += d; unavail += u; failed += f
        rest = todo if a.source == "appeears" else unavail
        if a.source == "auto":
            unavail = []
        if a.source in ("appeears", "auto") and rest:
            d, u, f = run_appeears(rest, a, ctx, commit)
            done += d; unavail += u; failed += f
    finally:
        flush()

    print(f"\nresumo: {len(done)} janelas gravadas | {len(unavail)} indisponíveis na fonte | {len(failed)} falhas | {(time.time()-t0)/60:.1f} min")
    for lbl, lst in (("indisponíveis", unavail), ("falhas", failed)):
        if lst:
            print(f"  {lbl}: " + ", ".join(f"{p} {d}" for p, d in lst[:10]) + (" …" if len(lst) > 10 else ""))
    # falha parcial → código de saída ≠ 0 para o workflow acusar (o próximo missing completa)
    return 1 if failed else 0




# ══════════════════════════════════════════════════════════════════════
# 4) BUILD (municípios → regiões e fazendas)
# ══════════════════════════════════════════════════════════════════════

BUILD_DOC = r"""
ndvi_monitor.py build — gera ndvi_history.json: a série de NDVI de cada REGIÃO produtora e de cada FAZENDA, que o
ndvi_dashboard.html lê (mesmo papel do history.json do Weather Monitor).

  • Regiões (51): média dos municípios da região, em 3 ponderações — simples (n), por produção de milho (m) e de
    soja (s) — exatamente como o build_regions.py faz com as células de clima. Pesos vêm do locations.json.
  • Fazendas (SLC, BrasilAgro): série do município onde a fazenda está.
  • Só entra no cálculo o município-janela com pelo menos --min-pct % de pixels utilizáveis (padrão 25): uma média
    feita com poucos pixels limpos é ruidosa. A cobertura de nuvem cai muito em jan–mar (medido em Sorriso: até 33%).
Baixar (seed) e agregar (build) são etapas separadas: mudar pesos ou o corte de qualidade
não exige rebaixar nada.

Uso:
  SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python ndvi_monitor.py build --locations "../Weather Monitor/locations.json"
  python ndvi_monitor.py build --locations locations.json --local-json rows.json --out ndvi_history.json   # teste
"""



def norm(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn").strip()


def load_store(a, cods, years):
    store = YearStore(cods)
    if a.local_json:
        data = json.load(open(a.local_json))
        for y in years:
            store.load_rows(data.get(str(y), []))
    else:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not (url and key):
            sys.exit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios (ou use --local-json).")
        sp = Supa(url, key)
        for y in years:
            store.load_rows(sp.rows(y, cols="cod_ibge,model,year,ndvi,pct"))
    return store


def farm_codes(loc, muns):
    """Fazenda → código IBGE do município. Primário: nome+UF no próprio locations.json; conferência: ponto dentro do polígono."""
    from shapely.geometry import Point
    by_name = {(m["uf"], norm(m["municipio"])): m["codigo_ibge"] for m in loc["regioes"]}
    out, notes = {}, []
    for f in loc["fazendas"]:
        key = f"{f['empresa']}|{f['fazenda']}|{f['municipio']}"
        by_n = by_name.get((f["uf"], norm(f["municipio"])))
        by_p = next((m["cod"] for m in muns if m["geom"].contains(Point(f["lon"], f["lat"]))), None)
        if by_n and by_p and by_n != by_p:
            notes.append(f"{key}: nome→{by_n}, ponto→{by_p} (usando o nome)")
        cod = by_n or by_p
        if cod is None:
            notes.append(f"{key}: fora dos 9 estados — sem NDVI")
        else:
            out[key] = cod
    return out, notes


def cmd_build(argv=None):
    ap = argparse.ArgumentParser(description=BUILD_DOC, prog="ndvi_monitor.py build", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--locations", required=True)
    ap.add_argument("--out", default=os.path.join(HERE, "ndvi_history.json"))
    ap.add_argument("--min-pct", type=float, default=25.0, help="% mínimo de pixels utilizáveis por município-janela")
    ap.add_argument("--from-year", type=int, default=2000)
    ap.add_argument("--local-json", help="rows gravadas pelo seed com --local-out (teste); senão lê o Supabase")
    ap.add_argument("--cache", default=os.path.join(HERE, "cache", "municipios_intermediaria.geojson.gz"))
    a = ap.parse_args(argv)

    loc = json.load(open(a.locations, encoding="utf-8"))
    muns = load_municipios(cache_path=a.cache)
    cods = [m["cod"] for m in muns]
    codset = {m["codigo_ibge"] for m in loc["regioes"]}
    base = set(cods) - set(EXTRA_MUNICIPIOS)
    if codset != base:
        print(f"AVISO: locations.json e a malha do IBGE divergem: só no locations={len(codset - base)}, só na malha={len(base - codset)}")
    years = list(range(a.from_year, dt.date.today().year + 1))
    store = load_store(a, cods, years)
    years = [y for y in years if y in store.data]
    if not years:
        sys.exit("nenhum dado no banco para os anos pedidos")
    pos = {c: i for i, c in enumerate(cods)}

    # séries por município com o corte de qualidade (NaN = sem dado)
    V = {}
    for y in years:
        nd = store.data[y]["ndvi"].astype(float)
        pct = store.data[y]["pct"].astype(float) / 10.0
        v = np.where(nd == MISSING, np.nan, nd / 1e4)
        V[y] = np.where((pct == MISSING / 10.0) | (pct < a.min_pct), np.nan, v)

    def encode(series_by_year):
        out = {}
        for y, arr in series_by_year.items():
            ints = np.where(np.isnan(arr), MISSING, np.rint(arr * 1e4)).astype(np.int16)
            if (ints != MISSING).any():
                out[str(y)] = enc(ints)
        return out

    points, regions = {}, {}
    for m in loc["regioes"]:
        regions.setdefault(f"{m['uf']}|{m['regiao']}", []).append(m)
    WGT = {"n": lambda m: 1.0, "m": lambda m: float(m.get("milho_t") or 0), "s": lambda m: float(m.get("soja_t") or 0)}
    for key, ms in sorted(regions.items()):
        ix = np.array([pos[m["codigo_ibge"]] for m in ms if m["codigo_ibge"] in pos])
        ms = [m for m in ms if m["codigo_ibge"] in pos]
        for wk, wf in WGT.items():
            w = np.array([wf(m) for m in ms])[:, None]
            if w.sum() <= 0:
                continue
            ser = {}
            for y in years:
                v = V[y][ix]
                valid = ~np.isnan(v) & (w > 0)
                den = (valid * w).sum(axis=0)
                num = np.where(valid, v * w, 0.0).sum(axis=0)
                ser[y] = np.where(den > 0, num / np.maximum(den, 1e-12), np.nan)
            points[f"{key}|{wk}"] = encode(ser)

    fcodes, notes = farm_codes(loc, muns)
    for key, cod in fcodes.items():
        points[f"f|{key}"] = encode({y: V[y][pos[cod]] for y in years})

    out = {"model": MODEL, "slots": SLOTS, "min_pct": a.min_pct, "years": years,
           "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "farm_cod": fcodes, "points": points}
    json.dump(out, open(a.out, "w"), separators=(",", ":"))
    print(f"{len(regions)} regiões × 3 ponderações + {len(fcodes)} fazendas = {len(points)} séries | {len(years)} anos "
          f"({years[0]}–{years[-1]}) | {os.path.getsize(a.out)/1e6:.2f} MB → {a.out}")
    for n in notes:
        print("  nota:", n)




# ══════════════════════════════════════════════════════════════════════
# 4b) XLSX (histórico em Excel, sem Supabase)
# ══════════════════════════════════════════════════════════════════════
XLSX_DOC = """Gera o Excel com o histórico: abas Regioes, Fazendas, Municipios e Leia-me.
Entrada: o JSON gravado por `seed --local-out rows.json` (use --local-json) ou o Supabase (sem --local-json).
  python ndvi_monitor.py xlsx --locations "../Weather Monitor/locations.json" --local-json rows.json --out ndvi_historico.xlsx"""


def cmd_xlsx(argv=None):
    import tempfile
    from openpyxl import Workbook
    ap = argparse.ArgumentParser(prog="ndvi_monitor.py xlsx", description=XLSX_DOC, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--locations", required=True)
    ap.add_argument("--local-json")
    ap.add_argument("--out", default=os.path.join(HERE, "ndvi_historico.xlsx"))
    ap.add_argument("--min-pct", type=float, default=25.0)
    ap.add_argument("--from-year", type=int, default=2000)
    ap.add_argument("--cache", default=os.path.join(HERE, "cache", "municipios_intermediaria.geojson.gz"))
    a = ap.parse_args(argv)
    tmp = os.path.join(tempfile.mkdtemp(), "h.json")
    cmd_build(["--locations", a.locations, "--out", tmp, "--min-pct", str(a.min_pct), "--from-year", str(a.from_year),
               "--cache", a.cache] + (["--local-json", a.local_json] if a.local_json else []))
    h = json.load(open(tmp))
    loc = json.load(open(a.locations, encoding="utf-8"))
    muns = load_municipios(cache_path=a.cache)
    cods = [m["cod"] for m in muns]
    years = h["years"]
    store = load_store(argparse.Namespace(local_json=a.local_json), cods, years)
    info = {m["codigo_ibge"]: (m["uf"], m["municipio"]) for m in loc["regioes"]}
    info.update({c: (u, "(município de fazenda)") for c, u in EXTRA_MUNICIPIOS.items()})

    def series(key, y):
        r = h["points"].get(key, {}).get(str(y))
        return [None if v == MISSING else round(v / 1e4, 4) for v in dec(r)] if r else [None] * SLOTS

    suf = {"n": "simples", "m": "milho", "s": "soja"}
    rkeys = sorted(k for k in h["points"] if not k.startswith("f|"))
    fkeys = sorted(k for k in h["points"] if k.startswith("f|"))
    wb = Workbook(write_only=True)
    ws_i = wb.create_sheet("Leia-me")
    for line in ["NDVI MODIS (Terra + Aqua, 250 m, composições de 16 dias, uma a cada 8 dias) por município",
                 "Cada linha = uma janela; 'Data' = início da janela de 16 dias; 'Satélite' = Terra (MOD13Q1) ou Aqua (MYD13Q1).",
                 "NDVI médio dos pixels bons/marginais do município (sem nuvem/neve). Vazio = sem dado.",
                 f"Regioes/Fazendas: município-janela com <= {a.min_pct:g}% de pixels usados é descartado; regiões ponderadas por nº de municípios (simples), milho ou soja.",
                 "Municipios: mesma regra de corte. Fazendas = NDVI do município onde a fazenda está.",
                 f"Gerado em {h['generated']}. Fonte: NASA MODIS v6.1."]:
        ws_i.append([line])
    sheets = {n: wb.create_sheet(n) for n in ("Regioes", "Fazendas", "Municipios")}
    sheets["Regioes"].append(["Data", "Satélite"] + [f"{k.rsplit('|', 1)[0]} ({suf[k.rsplit('|', 1)[1]]})" for k in rkeys])
    sheets["Fazendas"].append(["Data", "Satélite"] + [k[2:].replace("|", " · ") for k in fkeys])
    for lbl, f in (("cod_ibge", lambda c: c), ("UF", lambda c: info.get(c, ("", ""))[0]), ("Município", lambda c: info.get(c, ("", ""))[1])):
        sheets["Municipios"].append([lbl, ""] + [f(c) for c in cods])
    n_rows = 0
    for y in years:
        nd = store.data.get(y, {}).get("ndvi")
        pc = store.data.get(y, {}).get("pct")
        if nd is None:
            continue
        v = np.where((nd == MISSING) | (pc < a.min_pct * 10), np.nan, nd / 1e4)
        reg = {k: series(k, y) for k in rkeys}
        far = {k: series(k, y) for k in fkeys}
        for sl in range(SLOTS):
            col = v[:, sl]
            if np.isnan(col).all():
                continue
            d, sat = slot_start(y, sl), sensor_of_slot(sl)
            sheets["Regioes"].append([d, sat] + [reg[k][sl] for k in rkeys])
            sheets["Fazendas"].append([d, sat] + [far[k][sl] for k in fkeys])
            sheets["Municipios"].append([d, sat] + [None if np.isnan(x) else round(float(x), 4) for x in col])
            n_rows += 1
    wb.save(a.out)
    print(f"{n_rows} janelas × {len(cods)} municípios → {a.out} ({os.path.getsize(a.out)/1e6:.1f} MB)")
    return 0


# ══════════════════════════════════════════════════════════════════════
# 5) SQL da tabela + ponto de entrada
# ══════════════════════════════════════════════════════════════════════
SQL = r"""
-- ═══════════════════════════════════════════════════════════════════
-- Migration: ndvi_municipio — NDVI por município (MODIS Terra+Aqua; VIIRS depois)
-- ═══════════════════════════════════════════════════════════════════
-- Modelada no climate_cell: uma linha por (município, modelo, ano), cada variável
-- em base64 de int16[46] little-endian. As 46 posições são a grade de 8 dias do ano:
--     posição = (dia_do_ano_do_início_da_janela - 1) / 8      (divisão inteira)
--     dia do ano do início da janela = posição * 8 + 1
-- MODIS: Terra (MOD13Q1) ocupa as posições pares e Aqua (MYD13Q1) as ímpares, com
-- janelas de 16 dias. O VIIRS gera uma janela de 16 dias a cada 8 dias e ocupará todas
-- as posições; por isso entra como model = 'viirs', como o climate_cell separa 'nasa'
-- e 'era5'. -32768 = sem dado.
--
--   ndvi  NDVI médio dos pixels usados no município        × 10000  (0,4712 -> 4712)
--   p10   percentil 10 do NDVI entre os pixels              × 10000
--   p90   percentil 90 do NDVI entre os pixels              × 10000
--   pct   % dos pixels do município que foram usados        × 10     (97,3 % -> 973)
--         (pixels válidos com confiabilidade 0=bom ou 1=marginal; exclui nuvem e neve/gelo)
--   dobs  dias entre o início da janela e a data efetiva    × 1      (mediana dos pixels)
--
-- A janela de 16 dias é um composto: cada pixel traz a melhor observação do período,
-- e a data real dela pode cair até ~2 semanas depois do início. 'dobs' guarda isso para
-- alinhar com chuva e temperatura diárias.
--
-- Depende de: migration_rls_lockdown.sql (is_current_user_admin()). Idempotente.
-- ═══════════════════════════════════════════════════════════════════

create table if not exists public.ndvi_municipio (
  cod_ibge   integer  not null,
  model      text     not null default 'modis',
  year       smallint not null,
  ndvi       text,
  p10        text,
  p90        text,
  pct        text,
  dobs       text,
  updated_at timestamptz not null default now(),
  primary key (cod_ibge, model, year)
);

comment on table public.ndvi_municipio is
  'NDVI por município (código IBGE). Janelas de 16 dias do MODIS Terra+Aqua. Cada coluna = base64 de int16[46] na grade de 8 dias; -32768 = sem dado.';

alter table public.ndvi_municipio enable row level security;

drop policy if exists "select_authenticated" on public.ndvi_municipio;
drop policy if exists "modify_admin"         on public.ndvi_municipio;

-- Dado público entre usuários autenticados (medição de satélite). A escrita normal vem
-- do carregador com service role (que ignora RLS); a política abaixo cobre correção manual.
create policy "select_authenticated" on public.ndvi_municipio
  for select using (auth.uid() is not null);
create policy "modify_admin" on public.ndvi_municipio
  for all using (public.is_current_user_admin())
  with check (public.is_current_user_admin());

"""


def main():
    cmds = {"seed": cmd_seed, "build": cmd_build, "xlsx": cmd_xlsx}
    if len(sys.argv) < 2 or sys.argv[1] not in ("seed", "build", "xlsx", "sql"):
        sys.exit(__doc__)
    if sys.argv[1] == "sql":
        print(SQL.strip())
        return 0
    return cmds[sys.argv[1]](sys.argv[2:]) or 0


if __name__ == "__main__":
    sys.exit(main())
