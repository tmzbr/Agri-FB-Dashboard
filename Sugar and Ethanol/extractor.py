#!/usr/bin/env python3
"""
extractor.py — IBBA Agri Monitor · Unified Daily Extractor
===========================================================
Runs daily via GitHub Actions. Each section has its own schedule logic:

  S&E (Sugar NY11, Ethanol UDOP, FX PTAX) → every weekday
  Fuel Parity (ANP weekly prices)           → Thursdays only
  Supply/Demand (ANP monthly volumes)       → 5th of each month only

If it's not the right day for a section, it skips silently (no error).
If it IS the right day and the fetch fails, it raises so GitHub marks the run red.

Sources:
  NY11   → Yahoo Finance (SB=F)
  Etanol → UDOP (udop.com.br) via undetected-chromedriver + Xvfb
  FX     → BCB PTAX API (olinda.bcb.gov.br)
  Fuel   → ANP Série Histórica de Preços (semanal, xlsx)
  Vendas → ANP dados abertos (vendas-etanol-hidratado-m3-{Y}.csv, vendas-gasolina-c-m3-{Y}.csv)
  Produção → ANP dados abertos (producao-etanol-hidratado-m3.csv)
"""

import io
import logging
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

# ── Chrome / Selenium (only imported when needed) ──────────────────────────
try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    HAS_CHROME = True
except ImportError:
    HAS_CHROME = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH       = Path(__file__).parent / "commodities.db"
HISTORY_START = "2010-01-01"
TODAY         = date.today()
NOW_STR       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

FORCE_ALL = False  # overridden in main() if --force-all passed

# ─────────────────────────────────────────────────────────────────────────────
# Schedule helpers — silent skip if not the right day
# ─────────────────────────────────────────────────────────────────────────────

def is_weekday()  -> bool: return TODAY.weekday() < 5           # Mon–Fri
def is_thursday() -> bool: return FORCE_ALL or TODAY.weekday() == 3
def is_month_5th()-> bool: return FORCE_ALL or TODAY.day == 5


# ─────────────────────────────────────────────────────────────────────────────
# DB helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sugar_ny11 (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, preco_usdclb REAL NOT NULL,
        open_usdclb REAL, high_usdclb REAL, low_usdclb REAL, volume REAL,
        fonte TEXT DEFAULT 'Yahoo/SB=F', updated_at TEXT, UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_sugar ON sugar_ny11(data_referencia);

    CREATE TABLE IF NOT EXISTS etanol_cepea (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, preco_brl_m3 REAL NOT NULL,
        fonte TEXT DEFAULT 'UDOP/CEPEA-Paulinia', updated_at TEXT,
        UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_etanol ON etanol_cepea(data_referencia);

    CREATE TABLE IF NOT EXISTS fx_usdbrl (
        id INTEGER PRIMARY KEY AUTOINCREMENT, data_referencia TEXT NOT NULL,
        ano INTEGER, mes INTEGER, ptax_venda REAL NOT NULL,
        fonte TEXT DEFAULT 'BCB/PTAX', updated_at TEXT,
        UNIQUE(data_referencia));
    CREATE INDEX IF NOT EXISTS idx_fx ON fx_usdbrl(data_referencia);

    CREATE TABLE IF NOT EXISTS anp_estados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_inicial TEXT NOT NULL, data_final TEXT NOT NULL,
        regiao TEXT, estado TEXT NOT NULL, produto TEXT NOT NULL,
        preco_medio_revenda REAL, updated_at TEXT,
        UNIQUE(data_inicial, estado, produto));
    CREATE INDEX IF NOT EXISTS idx_anp_est ON anp_estados(data_inicial, estado, produto);

    CREATE TABLE IF NOT EXISTS anp_brasil (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        data_inicial TEXT NOT NULL, data_final TEXT NOT NULL,
        produto TEXT NOT NULL, preco_medio_revenda REAL, updated_at TEXT,
        UNIQUE(data_inicial, produto));
    CREATE INDEX IF NOT EXISTS idx_anp_br ON anp_brasil(data_inicial, produto);

    CREATE TABLE IF NOT EXISTS anp_vendas_uf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ano INTEGER NOT NULL, mes INTEGER NOT NULL, estado TEXT NOT NULL,
        eth_hid_m3 REAL, gas_c_m3 REAL, updated_at TEXT,
        UNIQUE(ano, mes, estado));
    CREATE INDEX IF NOT EXISTS idx_vendas ON anp_vendas_uf(ano, mes, estado);

    CREATE TABLE IF NOT EXISTS anp_producao_uf (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ano INTEGER NOT NULL, mes INTEGER NOT NULL, estado TEXT NOT NULL,
        eth_hid_m3 REAL, eth_ani_m3 REAL, updated_at TEXT,
        UNIQUE(ano, mes, estado));
    CREATE INDEX IF NOT EXISTS idx_prod ON anp_producao_uf(ano, mes, estado);
    """)
    conn.commit()


def last_date(conn, table, col="data_referencia"):
    r = conn.execute(f"SELECT MAX({col}) FROM {table}").fetchone()
    return r[0] if r and r[0] else None


def last_year_month(conn, table):
    r = conn.execute(
        f"SELECT MAX(ano), MAX(mes) FROM {table} "
        f"WHERE ano=(SELECT MAX(ano) FROM {table})"
    ).fetchone()
    return (int(r[0]), int(r[1])) if r and r[0] else None


def safe_float(val):
    try:
        f = float(val)
        return None if str(f) == "nan" else f
    except:
        return None


def parse_date(raw):
    for fmt in ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(raw).strip(), fmt).strftime("%Y-%m-%d")
        except:
            continue
    return None


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helpers
# ─────────────────────────────────────────────────────────────────────────────

ANP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/csv,application/vnd.ms-excel,*/*",
    "Referer": "https://www.gov.br/anp/pt-br/",
}

def download(url: str, label: str, fatal: bool = True) -> bytes | None:
    for attempt in range(1, 4):
        try:
            log.info(f"[{label}] Downloading (attempt {attempt}): {url}")
            r = requests.get(url, headers=ANP_HEADERS, timeout=60)
            r.raise_for_status()
            log.info(f"[{label}] {len(r.content):,} bytes")
            return r.content
        except requests.RequestException as e:
            log.warning(f"[{label}] Attempt {attempt} failed: {e}")
            if attempt < 3:
                time.sleep(10 * attempt)
    msg = f"[{label}] All download attempts failed."
    if fatal:
        raise RuntimeError(msg)   # marks GitHub run red
    log.error(msg)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 1: S&E  (runs every weekday) ════════════════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

def run_se(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[S&E] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("S&E — Sugar NY11 · Ethanol UDOP · FX PTAX")
    log.info("=" * 60)

    results = {}
    results["ny11"] = fetch_sugar_ny11(conn)
    results["fx"]   = fetch_fx_usdbrl(conn)
    results["eth"]  = fetch_etanol_cepea(conn)   # Chrome — last, heaviest
    return results


# ── NY11 ──────────────────────────────────────────────────────────────────────

def fetch_sugar_ny11(conn) -> int:
    try:
        import yfinance as yf
    except ImportError:
        raise RuntimeError("[NY11] yfinance not installed")

    log.info("[NY11] Fetching Yahoo Finance (SB=F)...")
    ld = last_date(conn, "sugar_ny11")
    start = (datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") \
            if ld else HISTORY_START
    if start > TODAY.strftime("%Y-%m-%d"):
        log.info("[NY11] Already up to date.")
        return 0

    df = yf.Ticker("SB=F").history(start=start, end=TODAY.strftime("%Y-%m-%d"),
                                   auto_adjust=False)
    if df is None or df.empty:
        log.info("[NY11] No new data.")
        return 0

    df.index = pd.to_datetime(df.index).tz_localize(None)
    inserted = 0
    for ts, row in df.iterrows():
        dr = ts.strftime("%Y-%m-%d")
        cl = safe_float(row.get("Close"))
        if not cl:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO sugar_ny11 "
            "(data_referencia,ano,mes,preco_usdclb,open_usdclb,high_usdclb,low_usdclb,volume,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (dr, int(dr[:4]), int(dr[5:7]), cl,
             safe_float(row.get("Open")), safe_float(row.get("High")),
             safe_float(row.get("Low")),  safe_float(row.get("Volume")), NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[NY11] {inserted} rows inserted.")
    return inserted


# ── FX PTAX ───────────────────────────────────────────────────────────────────

BCB_URL = (
    "https://olinda.bcb.gov.br/olinda/servico/PTAX/versao/v1/odata/"
    "CotacaoDolarPeriodo(dataInicial=@dataInicial,dataFinalCotacao=@dataFinalCotacao)"
    "?@dataInicial='{di}'&@dataFinalCotacao='{df}'"
    "&$top=1000&$skip={skip}&$orderby=dataHoraCotacao%20asc"
    "&$format=json&$select=cotacaoVenda,dataHoraCotacao"
)

def fetch_fx_usdbrl(conn) -> int:
    log.info("[FX] Fetching BCB PTAX...")
    ld = last_date(conn, "fx_usdbrl")
    start = (datetime.strptime(ld, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d") \
            if ld else HISTORY_START
    if start > TODAY.strftime("%Y-%m-%d"):
        log.info("[FX] Already up to date.")
        return 0

    di = datetime.strptime(start, "%Y-%m-%d").strftime("%m-%d-%Y")
    df = TODAY.strftime("%m-%d-%Y")
    inserted = 0
    skip = 0
    while True:
        url = BCB_URL.format(di=di, df=df, skip=skip)
        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            data = r.json().get("value", [])
        except Exception as e:
            raise RuntimeError(f"[FX] BCB API failed at skip={skip}: {e}")

        if not data:
            break
        for item in data:
            raw_dt = item.get("dataHoraCotacao", "")[:10]
            ptax   = item.get("cotacaoVenda")
            if not raw_dt or ptax is None:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO fx_usdbrl "
                "(data_referencia,ano,mes,ptax_venda,updated_at) VALUES(?,?,?,?,?)",
                (raw_dt, int(raw_dt[:4]), int(raw_dt[5:7]), float(ptax), NOW_STR))
            if conn.execute("SELECT changes()").fetchone()[0]:
                inserted += 1
        log.info(f"[FX] skip={skip}: {len(data)} records")
        if len(data) < 1000:
            break
        skip += 1000
        time.sleep(0.3)

    conn.commit()
    log.info(f"[FX] {inserted} rows inserted.")
    return inserted


# ── Ethanol UDOP ──────────────────────────────────────────────────────────────

UDOP_URL = "https://www.udop.com.br/indicadores-etanol"

def make_driver():
    if not HAS_CHROME:
        raise RuntimeError("[ETANOL] undetected-chromedriver not installed")
    chrome = subprocess.run(["which", "google-chrome"], capture_output=True, text=True).stdout.strip()
    ver    = subprocess.run([chrome, "--version"], capture_output=True, text=True).stdout.strip()
    major  = int(ver.split()[-1].split(".")[0])
    log.info(f"[ETANOL] Chrome {ver} (major={major})")
    opts = uc.ChromeOptions()
    opts.binary_location = chrome
    for arg in ["--no-sandbox","--disable-dev-shm-usage","--disable-gpu",
                "--window-size=1280,900","--lang=pt-BR"]:
        opts.add_argument(arg)
    return uc.Chrome(options=opts, version_main=major)

def fetch_etanol_cepea(conn) -> int:
    ld = last_date(conn, "etanol_cepea")
    log.info(f"[ETANOL] Last in DB: {ld or 'none'}")
    driver, rows = None, []
    try:
        driver = make_driver()
        log.info(f"[ETANOL] Navigating to {UDOP_URL}")
        driver.get(UDOP_URL)
        time.sleep(8)
        try:
            driver.find_element(By.XPATH,
                "//button[contains(text(),'Diário') or contains(text(),'Di')]").click()
            time.sleep(2)
        except: pass
        try:
            driver.find_element(By.XPATH,
                "//button[contains(text(),'São Paulo')]").click()
            time.sleep(2)
        except: pass

        table = driver.find_element(By.CSS_SELECTOR, "table")
        for linha in table.find_elements(By.TAG_NAME, "tr"):
            cels = [c.text.strip() for c in linha.find_elements(By.TAG_NAME, "td")]
            if len(cels) < 2:
                continue
            dr = parse_date(cels[0])
            if not dr:
                continue
            try:
                val = float(cels[1].replace(".", "").replace(",", "."))
                if val > 0:
                    rows.append({"data_ref": dr, "preco_m3": val})
            except: continue

        log.info(f"[ETANOL] {len(rows)} rows read | "
                 f"{rows[-1]['data_ref'] if rows else '—'} → {rows[0]['data_ref'] if rows else '—'}")
    except Exception as e:
        raise RuntimeError(f"[ETANOL] Scraping failed: {e}")
    finally:
        if driver:
            try: driver.quit()
            except: pass

    if not rows:
        raise RuntimeError("[ETANOL] No data obtained from UDOP")

    if ld:
        rows = [r for r in rows if r["data_ref"] > ld]
    if not rows:
        log.info("[ETANOL] Nothing new.")
        return 0

    inserted = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO etanol_cepea "
            "(data_referencia,ano,mes,preco_brl_m3,updated_at) VALUES(?,?,?,?,?)",
            (r["data_ref"], int(r["data_ref"][:4]), int(r["data_ref"][5:7]),
             r["preco_m3"], NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[ETANOL] {inserted} rows inserted.")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 2: Fuel Parity  (runs Thursdays only) ═══════════════════════════
# ─────────────────────────────────────────────────────────────────────────────

ANP_BASE     = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos"
FUEL_EST_URL = "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-estados-desde-2013.xlsx"
FUEL_BR_URL  = "https://www.gov.br/anp/pt-br/assuntos/precos-e-defesa-da-concorrencia/precos/precos-revenda-e-de-distribuicao-combustiveis/shlp/semanal/semanal-brasil-desde-2013.xlsx"
PRODUTOS     = {"ETANOL HIDRATADO", "GASOLINA COMUM"}

def run_fuel(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[Fuel] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("Fuel Parity — ANP weekly prices (Etanol + Gasolina)")
    log.info("=" * 60)

    return {
        "estados": ingest_fuel_estados(conn),
        "brasil":  ingest_fuel_brasil(conn),
    }


def parse_anp_fuel_excel(content: bytes, label: str) -> pd.DataFrame | None:
    try:
        raw = pd.read_excel(io.BytesIO(content), sheet_name=0, header=None)
        header_row = next(
            (i for i, row in raw.iterrows() if "DATA INICIAL" in str(row.values)),
            None
        )
        if header_row is None:
            raise ValueError("'DATA INICIAL' header not found")
        df = pd.read_excel(io.BytesIO(content), sheet_name=0, header=header_row)
        df = df.dropna(subset=["DATA INICIAL"])
        df = df[df["PRODUTO"].isin(PRODUTOS)]
        df["DATA INICIAL"] = pd.to_datetime(df["DATA INICIAL"]).dt.strftime("%Y-%m-%d")
        df["DATA FINAL"]   = pd.to_datetime(df["DATA FINAL"]).dt.strftime("%Y-%m-%d")
        df["PREÇO MÉDIO REVENDA"] = pd.to_numeric(df["PREÇO MÉDIO REVENDA"], errors="coerce")
        log.info(f"[{label}] Parsed {len(df)} rows | "
                 f"{df['DATA INICIAL'].min()} → {df['DATA INICIAL'].max()}")
        return df
    except Exception as e:
        raise RuntimeError(f"[{label}] Excel parse failed: {e}")


def ingest_fuel_estados(conn) -> int:
    ld = last_date(conn, "anp_estados", "data_inicial")
    content = download(FUEL_EST_URL, "fuel-estados", fatal=True)
    df = parse_anp_fuel_excel(content, "fuel-estados")
    if ld:
        df = df[df["DATA INICIAL"] > ld]
    if df.empty:
        log.info("[fuel-estados] Nothing new.")
        return 0
    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_estados "
            "(data_inicial,data_final,regiao,estado,produto,preco_medio_revenda,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (r["DATA INICIAL"], r["DATA FINAL"],
             r.get("REGIÃO") or r.get("REGIAO"),
             r["ESTADO"], r["PRODUTO"],
             float(r["PREÇO MÉDIO REVENDA"]) if pd.notna(r["PREÇO MÉDIO REVENDA"]) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[fuel-estados] {inserted} rows inserted.")
    return inserted


def ingest_fuel_brasil(conn) -> int:
    ld = last_date(conn, "anp_brasil", "data_inicial")
    content = download(FUEL_BR_URL, "fuel-brasil", fatal=True)
    df = parse_anp_fuel_excel(content, "fuel-brasil")
    if ld:
        df = df[df["DATA INICIAL"] > ld]
    if df.empty:
        log.info("[fuel-brasil] Nothing new.")
        return 0
    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_brasil "
            "(data_inicial,data_final,produto,preco_medio_revenda,updated_at) "
            "VALUES(?,?,?,?,?)",
            (r["DATA INICIAL"], r["DATA FINAL"], r["PRODUTO"],
             float(r["PREÇO MÉDIO REVENDA"]) if pd.notna(r["PREÇO MÉDIO REVENDA"]) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[fuel-brasil] {inserted} rows inserted.")
    return inserted


# ─────────────────────────────────────────────────────────────────────────────
# ══ SECTION 3: Supply/Demand  (runs on 5th of each month) ═══════════════════
# ─────────────────────────────────────────────────────────────────────────────

VENDAS_CSV_URL = "https://www.gov.br/anp/pt-br/centrais-de-conteudo/dados-abertos/arquivos/vdpb/vendas-derivados-petroleo-e-etanol/vendas-combustiveis-m3-1990-2025.csv"
PRODUCAO_URL   = "https://www.gov.br/anp/pt-br/assuntos/producao-e-fornecimento-de-biocombustiveis/etanol/arquivos-etanol/pb-da-etanol.zip"

MES_PT = {
    "JAN":1,"FEV":2,"MAR":3,"ABR":4,"MAI":5,"JUN":6,
    "JUL":7,"AGO":8,"SET":9,"OUT":10,"NOV":11,"DEZ":12,
}
ESTADO_NORM = {
    "Acre":"ACRE","Alagoas":"ALAGOAS","Amapá":"AMAPÁ","Amazonas":"AMAZONAS",
    "Bahia":"BAHIA","Ceará":"CEARÁ","Distrito Federal":"DISTRITO FEDERAL",
    "Espírito Santo":"ESPÍRITO SANTO","Goiás":"GOIÁS","Maranhão":"MARANHÃO",
    "Mato Grosso":"MATO GROSSO","Mato Grosso do Sul":"MATO GROSSO DO SUL",
    "Minas Gerais":"MINAS GERAIS","Pará":"PARÁ","Paraíba":"PARAÍBA",
    "Paraná":"PARANÁ","Pernambuco":"PERNAMBUCO","Piauí":"PIAUÍ",
    "Rio de Janeiro":"RIO DE JANEIRO","Rio Grande do Norte":"RIO GRANDE DO NORTE",
    "Rio Grande do Sul":"RIO GRANDE DO SUL","Rondônia":"RONDÔNIA",
    "Roraima":"RORAIMA","Santa Catarina":"SANTA CATARINA",
    "São Paulo":"SÃO PAULO","Sergipe":"SERGIPE","Tocantins":"TOCANTINS",
}

def run_supply_demand(conn: sqlite3.Connection) -> dict:
    if not is_weekday():
        log.info("[Supply/Demand] Not a weekday — skipping.")
        return {"skipped": True}

    log.info("=" * 60)
    log.info("Supply/Demand — ANP monthly volumes (Vendas + Produção)")
    log.info("=" * 60)

    return {
        "vendas":  ingest_vendas(conn),
        "producao": ingest_producao(conn),
    }


def parse_vendas_year(content: bytes, year: int, label: str) -> pd.DataFrame | None:
    for enc in ("latin-1", "utf-8-sig", "utf-8"):
        try:
            text = content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    df = pd.read_csv(io.StringIO(text), sep=";", on_bad_lines="skip")
    df.columns = [c.strip().upper() for c in df.columns]
    uf_col = next(
        (c for c in df.columns if any(k in c for k in ("FEDERAÇÃO","FEDERACAO","ESTADO"," UF"))),
        None
    )
    if not uf_col:
        raise RuntimeError(f"[{label}] UF column not found. Cols: {list(df.columns)}")
    df = df[df[uf_col].notna()]
    df = df[~df[uf_col].str.upper().str.contains(r"TOTAL|BRASIL|REGIÃO|REGIAO|GRANDE",
                                                    na=False, regex=True)]
    mes_cols = {col: MES_PT[col[:3].upper()] for col in df.columns if col[:3].upper() in MES_PT}
    if not mes_cols:
        raise RuntimeError(f"[{label}] No month columns found")
    rows = []
    for _, row in df.iterrows():
        uf = str(row[uf_col]).strip().upper()
        for col, mes_num in mes_cols.items():
            val = row.get(col)
            if pd.isna(val):
                continue
            try:
                v = float(str(val).replace(".", "").replace(",", "."))
                rows.append({"ano": year, "mes": mes_num, "estado": uf, "volume": v})
            except: continue
    return pd.DataFrame(rows) if rows else None


def ingest_vendas(conn) -> int:
    """
    Downloads the consolidated ANP vendas CSV (all years, all products)
    and inserts only rows newer than last in DB.
    Format: ANO;MÊS;GRANDE REGIÃO;UNIDADE DA FEDERAÇÃO;PRODUTO;VENDAS
    """
    last = last_year_month(conn, "anp_vendas_uf")
    last_ano = last[0] if last else 2013
    last_mes = last[1] if last else 0

    content = download(VENDAS_CSV_URL, "vendas", fatal=True)

    for enc in ("utf-8-sig", "latin-1", "utf-8"):
        try:
            text = content.decode(enc); break
        except UnicodeDecodeError: continue

    df = pd.read_csv(io.StringIO(text), sep=";", on_bad_lines="skip")
    df.columns = [c.strip() for c in df.columns]

    # Find columns
    ano_col    = next((c for c in df.columns if c.upper() in ("ANO","AÑO")), None)
    mes_col    = next((c for c in df.columns if "MÊS" in c.upper() or "MES" in c.upper()), None)
    uf_col     = next((c for c in df.columns if "FEDERAÇÃO" in c.upper() or "FEDERACAO" in c.upper()), None)
    prod_col   = next((c for c in df.columns if "PRODUTO" in c.upper()), None)
    vendas_col = next((c for c in df.columns if "VENDAS" in c.upper()), None)

    if not all([ano_col, mes_col, uf_col, prod_col, vendas_col]):
        raise RuntimeError(f"[vendas] Missing columns. Got: {list(df.columns)}")

    # Filter to only our products
    df = df[df[prod_col].isin(["ETANOL HIDRATADO", "GASOLINA C"])].copy()

    # Map month names to numbers
    df["mes_num"] = df[mes_col].str[:3].str.upper().map(MES_PT)
    df = df[df["mes_num"].notna()].copy()
    df["mes_num"] = df["mes_num"].astype(int)
    df["ano_num"] = pd.to_numeric(df[ano_col], errors="coerce").astype("Int64")
    df = df[df["ano_num"].notna()].copy()

    # Convert vendas values
    df["volume"] = pd.to_numeric(
        df[vendas_col].astype(str).str.replace(".", "").str.replace(",", "."),
        errors="coerce"
    )
    df["estado"] = df[uf_col].str.strip().str.upper()

    # Pivot eth + gas into same row
    piv = df.pivot_table(
        index=["ano_num", "mes_num", "estado"],
        columns=prod_col,
        values="volume",
        aggfunc="sum"
    ).reset_index()
    piv.columns.name = None
    piv = piv.rename(columns={
        "ano_num": "ano", "mes_num": "mes",
        "ETANOL HIDRATADO": "eth_hid_m3",
        "GASOLINA C": "gas_c_m3"
    })
    if "eth_hid_m3" not in piv.columns: piv["eth_hid_m3"] = None
    if "gas_c_m3"   not in piv.columns: piv["gas_c_m3"]   = None

    # Only new rows
    piv = piv[
        (piv["ano"] > last_ano) |
        ((piv["ano"] == last_ano) & (piv["mes"] > last_mes))
    ]

    if piv.empty:
        log.info("[vendas] Nothing new.")
        return 0

    log.info(f"[vendas] {len(piv)} new rows to insert | "
             f"up to {int(piv['ano'].max())}-{int(piv['mes'].max()):02d}")

    inserted = 0
    for _, r in piv.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_vendas_uf "
            "(ano,mes,estado,eth_hid_m3,gas_c_m3,updated_at) VALUES(?,?,?,?,?,?)",
            (int(r.ano), int(r.mes), r.estado,
             float(r.eth_hid_m3) if pd.notna(r.get("eth_hid_m3")) else None,
             float(r.gas_c_m3)   if pd.notna(r.get("gas_c_m3"))   else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[vendas] {inserted} rows inserted.")
    return inserted


def ingest_producao(conn) -> int:
    import zipfile
    last = last_year_month(conn, "anp_producao_uf")
    last_ano = last[0] if last else 2016
    last_mes = last[1] if last else 0

    content = download(PRODUCAO_URL, "producao", fatal=True)

    # Extract Etanol_Produção.csv from zip
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            csv_name = next((n for n in zf.namelist()
                             if "rodu" in n.lower() and n.endswith(".csv")), None)
            if not csv_name:
                raise RuntimeError(f"[producao] Etanol_Produção.csv not found in zip. Files: {zf.namelist()}")
            log.info(f"[producao] Extracting: {csv_name}")
            raw = zf.read(csv_name)
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"[producao] Bad zip file: {e}")

    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw.decode(enc); break
        except UnicodeDecodeError: continue

    df = pd.read_csv(io.StringIO(text), sep=",")
    df.columns = [c.strip() for c in df.columns]
    date_col = next((c for c in df.columns if "MÊS" in c.upper() or "MES" in c.upper()), None)
    hid_col  = next((c for c in df.columns if "HIDRATADO" in c.upper()), None)
    ani_col  = next((c for c in df.columns if "ANIDRO"   in c.upper()), None)
    est_col  = next((c for c in df.columns if "ESTADO"   in c.upper()), None)
    if not all([date_col, hid_col, est_col]):
        raise RuntimeError(f"[producao] Missing columns. Got: {list(df.columns)}")

    df["mes_ano"]    = pd.to_datetime(df[date_col], format="%m/%Y")
    df["ano"]        = df["mes_ano"].dt.year.astype(int)
    df["mes"]        = df["mes_ano"].dt.month.astype(int)
    df["estado"]     = df[est_col].str.strip().map(ESTADO_NORM).fillna(
                          df[est_col].str.strip().str.upper())
    df["eth_hid_m3"] = pd.to_numeric(df[hid_col], errors="coerce")
    df["eth_ani_m3"] = pd.to_numeric(df[ani_col], errors="coerce") if ani_col else None

    df = df[(df["ano"] > last_ano) | ((df["ano"] == last_ano) & (df["mes"] > last_mes))]
    if df.empty:
        log.info("[producao] Nothing new.")
        return 0

    inserted = 0
    for _, r in df.iterrows():
        conn.execute(
            "INSERT OR IGNORE INTO anp_producao_uf "
            "(ano,mes,estado,eth_hid_m3,eth_ani_m3,updated_at) VALUES(?,?,?,?,?,?)",
            (int(r.ano), int(r.mes), r.estado,
             float(r.eth_hid_m3) if pd.notna(r.eth_hid_m3) else None,
             float(r.eth_ani_m3) if pd.notna(r.eth_ani_m3) else None,
             NOW_STR))
        if conn.execute("SELECT changes()").fetchone()[0]:
            inserted += 1
    conn.commit()
    log.info(f"[producao] {inserted} rows inserted.")
    return inserted



# ─────────────────────────────────────────────────────────────────────────────
# ══ DASHBOARD GENERATION (runs after every scraper section) ═════════════════
# ─────────────────────────────────────────────────────────────────────────────

# HTML template stored as compressed base64 (before/after the data block)
_TMPL_BEFORE_B64 = "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"
_TMPL_AFTER_B64  = "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"

def generate_dashboard(conn: sqlite3.Connection) -> None:
    """Regenerate se_dashboard.html with latest data from DB."""
    import base64, gzip
    from collections import OrderedDict

    log.info("[Dashboard] Regenerating se_dashboard.html...")

    # ── Extract all data ──────────────────────────────────────────────────────
    ATR_VHP=1.05; ATR_HYD=1.68; FRETE=85.0; ELEVACAO=10.5; CONV_L_TON=1.04; CONV_TON_LB=22.0

    # Use sugar_ny11 as the spine (most complete), join ethanol/FX with tolerance
    # For missing ethanol: use last available price on or before that date
    # For missing FX: use last available rate on or before that date
    se_rows = conn.execute("""
        SELECT
            s.data_referencia,
            s.preco_usdclb,
            (SELECT e.preco_brl_m3 FROM etanol_cepea e
             WHERE e.data_referencia <= s.data_referencia
             ORDER BY e.data_referencia DESC LIMIT 1) AS preco_brl_m3,
            (SELECT f.ptax_venda FROM fx_usdbrl f
             WHERE f.data_referencia <= s.data_referencia
             ORDER BY f.data_referencia DESC LIMIT 1) AS ptax_venda
        FROM sugar_ny11 s
        ORDER BY s.data_referencia
    """).fetchall()
    se_data = []
    for dr, sugar, eth_m3, fx in se_rows:
        if not all([sugar, eth_m3, fx]): continue
        equiv = (((eth_m3*ATR_VHP/ATR_HYD)+FRETE+(ELEVACAO*fx))/CONV_L_TON/CONV_TON_LB)/fx
        se_data.append({"d":dr,"sugar":round(sugar,4),"eth":round(eth_m3,2),
                         "fx":round(fx,4),"equiv":round(equiv,2),"diff":round(equiv-sugar,2)})

    uf_series = {}
    for date, uf, parity in conn.execute("""
        SELECT e.data_inicial, e.estado, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_estados e
        JOIN anp_estados g ON g.data_inicial=e.data_inicial AND g.estado=e.estado AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
        ORDER BY e.data_inicial
    """).fetchall():
        if uf not in uf_series: uf_series[uf] = []
        uf_series[uf].append({"d":date,"p":parity})

    br_series = [{"d":r[0],"p":r[1]} for r in conn.execute("""
        SELECT e.data_inicial, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_brasil e
        JOIN anp_brasil g ON g.data_inicial=e.data_inicial AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
        ORDER BY e.data_inicial
    """).fetchall()]

    map_data = {}
    for date, uf, parity in conn.execute("""
        SELECT e.data_inicial, e.estado, ROUND(e.preco_medio_revenda/g.preco_medio_revenda,4)
        FROM anp_estados e
        JOIN anp_estados g ON g.data_inicial=e.data_inicial AND g.estado=e.estado AND g.produto='GASOLINA COMUM'
        WHERE e.produto='ETANOL HIDRATADO' AND e.preco_medio_revenda IS NOT NULL AND g.preco_medio_revenda IS NOT NULL
    """).fetchall():
        if date not in map_data: map_data[date] = {}
        map_data[date][uf] = parity

    map_dates = sorted(map_data.keys())
    month_map = OrderedDict()
    for dt in map_dates:
        month_map[dt[:7]] = dt
    MONTH_DATES  = list(month_map.values())
    MONTH_LABELS = list(month_map.keys())

    deficit_rows = conn.execute("""
        SELECT v.ano, v.mes, v.estado,
               ROUND(v.eth_hid_m3) AS vendas_m3,
               ROUND(COALESCE(p.eth_hid_m3,0)) AS prod_m3,
               ROUND(COALESCE(p.eth_hid_m3,0) - v.eth_hid_m3) AS saldo_m3
        FROM anp_vendas_uf v
        LEFT JOIN anp_producao_uf p ON p.ano=v.ano AND p.mes=v.mes AND p.estado=v.estado
        WHERE v.ano >= 2017 AND v.eth_hid_m3 IS NOT NULL
        ORDER BY v.ano, v.mes, v.estado
    """).fetchall()

    otto_rows = conn.execute("""
        SELECT ano, mes, estado,
               ROUND(eth_hid_m3*0.70/(eth_hid_m3*0.70+gas_c_m3),4)
        FROM anp_vendas_uf
        WHERE eth_hid_m3 IS NOT NULL AND gas_c_m3 IS NOT NULL
          AND (eth_hid_m3*0.70+gas_c_m3) > 0
        ORDER BY ano, mes, estado
    """).fetchall()

    deficit_series = {}; deficit_map = {}
    for ano, mes, estado, vendas, prod, saldo in deficit_rows:
        d = f"{ano}-{mes:02d}"
        if estado not in deficit_series: deficit_series[estado] = []
        deficit_series[estado].append({"d":d,"vendas":vendas,"prod":prod,"saldo":saldo})
        if d not in deficit_map: deficit_map[d] = {}
        deficit_map[d][estado] = {"s":saldo,"v":vendas,"p":prod}

    otto_series = {}; otto_map = {}
    for ano, mes, estado, pene in otto_rows:
        d = f"{ano}-{mes:02d}"
        if estado not in otto_series: otto_series[estado] = []
        otto_series[estado].append({"d":d,"p":float(pene)})
        if d not in otto_map: otto_map[d] = {}
        otto_map[d][estado] = float(pene)

    def_months  = sorted(deficit_map.keys())
    otto_months = sorted(otto_map.keys())

    def build_by_year(months):
        by_year = {}
        for m in months:
            y, mo = m[:4], m[5:7]
            if y not in by_year: by_year[y] = []
            by_year[y].append(mo)
        return by_year

    def_by_year = build_by_year(def_months)
    ott_by_year = build_by_year(otto_months)
    def_years   = sorted(def_by_year.keys(), reverse=True)
    ott_years   = sorted(ott_by_year.keys(), reverse=True)

    by_month2 = {}
    for uf, arr in deficit_series.items():
        for r in arr:
            d = r["d"]
            if d not in by_month2: by_month2[d] = {"vendas":0,"prod":0}
            by_month2[d]["vendas"] += (r.get("vendas") or 0)
            by_month2[d]["prod"]   += (r.get("prod") or 0)
    br_def = [{"d":d,"vendas":round(v["vendas"]),"prod":round(v["prod"]),"saldo":round(v["prod"]-v["vendas"])}
               for d,v in sorted(by_month2.items())]

    by_month3 = {}
    for uf, arr in deficit_series.items():
        for r in arr:
            d = r["d"]
            otto_val = otto_map.get(d, {}).get(uf)
            if otto_val is None or not r.get("vendas"): continue
            eth_eq = r["vendas"] * 0.70
            gas    = eth_eq * (1 - otto_val) / otto_val
            if d not in by_month3: by_month3[d] = {"eth_eq":0,"gas":0}
            by_month3[d]["eth_eq"] += eth_eq
            by_month3[d]["gas"]    += gas
    br_otto = [{"d":d,"p":round(v["eth_eq"]/(v["eth_eq"]+v["gas"]),4)}
                for d,v in sorted(by_month3.items()) if (v["eth_eq"]+v["gas"])>0]

    UF_CODE_SD = {
        'ACRE':'AC','ALAGOAS':'AL','AMAPÁ':'AP','AMAZONAS':'AM','BAHIA':'BA',
        'CEARÁ':'CE','DISTRITO FEDERAL':'DF','ESPÍRITO SANTO':'ES','GOIÁS':'GO',
        'MARANHÃO':'MA','MATO GROSSO':'MT','MATO GROSSO DO SUL':'MS','MINAS GERAIS':'MG',
        'PARÁ':'PA','PARAÍBA':'PB','PARANÁ':'PR','PERNAMBUCO':'PE','PIAUÍ':'PI',
        'RIO DE JANEIRO':'RJ','RIO GRANDE DO NORTE':'RN','RIO GRANDE DO SUL':'RS',
        'RONDÔNIA':'RO','RORAIMA':'RR','SANTA CATARINA':'SC','SÃO PAULO':'SP',
        'SERGIPE':'SE','TOCANTINS':'TO'
    }


    CODE_UF_PARITY = {"AC": "ACRE", "AL": "ALAGOAS", "AP": "AMAPA", "AM": "AMAZONAS", "BA": "BAHIA", "CE": "CEARA", "DF": "DISTRITO FEDERAL", "ES": "ESPIRITO SANTO", "GO": "GOIAS", "MA": "MARANHAO", "MT": "MATO GROSSO", "MS": "MATO GROSSO DO SUL", "MG": "MINAS GERAIS", "PA": "PARA", "PB": "PARAIBA", "PR": "PARANA", "PE": "PERNAMBUCO", "PI": "PIAUI", "RJ": "RIO DE JANEIRO", "RN": "RIO GRANDE DO NORTE", "RS": "RIO GRANDE DO SUL", "RO": "RONDONIA", "RR": "RORAIMA", "SC": "SANTA CATARINA", "SP": "SAO PAULO", "SE": "SERGIPE", "TO": "TOCANTINS"}
    CODE_NAME_MAP  = {"AC": "Acre", "AL": "Alagoas", "AP": "Amap\u00e1", "AM": "Amazonas", "BA": "Bahia", "CE": "Cear\u00e1", "DF": "Distrito Federal", "ES": "Esp\u00edrito Santo", "GO": "Goi\u00e1s", "MA": "Maranh\u00e3o", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Par\u00e1", "PB": "Para\u00edba", "PR": "Paran\u00e1", "PE": "Pernambuco", "PI": "Piau\u00ed", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rond\u00f4nia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "S\u00e3o Paulo", "SE": "Sergipe", "TO": "Tocantins"}
    CODE_UF_SD_MAP   = {"AC": "ACRE", "AL": "ALAGOAS", "AP": "AMAP\u00c1", "AM": "AMAZONAS", "BA": "BAHIA", "CE": "CEAR\u00c1", "DF": "DISTRITO FEDERAL", "ES": "ESP\u00cdRITO SANTO", "GO": "GOI\u00c1S", "MA": "MARANH\u00c3O", "MT": "MATO GROSSO", "MS": "MATO GROSSO DO SUL", "MG": "MINAS GERAIS", "PA": "PAR\u00c1", "PB": "PARA\u00cdBA", "PR": "PARAN\u00c1", "PE": "PERNAMBUCO", "PI": "PIAU\u00cd", "RJ": "RIO DE JANEIRO", "RN": "RIO GRANDE DO NORTE", "RS": "RIO GRANDE DO SUL", "RO": "ROND\u00d4NIA", "RR": "RORAIMA", "SC": "SANTA CATARINA", "SP": "S\u00c3O PAULO", "SE": "SERGIPE", "TO": "TOCANTINS"}
    CODE_NAME_SD_MAP = {"AC": "Acre", "AL": "Alagoas", "AP": "Amap\u00e1", "AM": "Amazonas", "BA": "Bahia", "CE": "Cear\u00e1", "DF": "Distrito Federal", "ES": "Esp\u00edrito Santo", "GO": "Goi\u00e1s", "MA": "Maranh\u00e3o", "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Par\u00e1", "PB": "Para\u00edba", "PR": "Paran\u00e1", "PE": "Pernambuco", "PI": "Piau\u00ed", "RJ": "Rio de Janeiro", "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rond\u00f4nia", "RR": "Roraima", "SC": "Santa Catarina", "SP": "S\u00e3o Paulo", "SE": "Sergipe", "TO": "Tocantins"}
    UF_COORDS_SD_MAP = {"AC": [-9.02, -70.81], "AL": [-9.57, -36.78], "AM": [-3.47, -65.1], "AP": [1.41, -51.77], "BA": [-12.96, -41.7], "CE": [-5.5, -39.32], "DF": [-15.78, -47.93], "ES": [-19.19, -40.34], "GO": [-15.83, -49.84], "MA": [-5.42, -45.44], "MG": [-18.1, -44.38], "MS": [-20.77, -54.79], "MT": [-12.64, -55.42], "PA": [-3.41, -52.29], "PB": [-7.24, -36.78], "PE": [-8.38, -37.86], "PI": [-6.6, -42.28], "PR": [-24.89, -51.55], "RJ": [-22.25, -42.66], "RN": [-5.81, -36.59], "RO": [-10.83, -63.34], "RR": [1.99, -61.33], "RS": [-30.03, -53.2], "SC": [-27.45, -50.94], "SE": [-10.57, -37.45], "SP": [-22.25, -48.59], "TO": [-10.25, -48.25]}
    import json as _json
    J = lambda x: _json.dumps(x, separators=(',',':'))

    data_block = f"""
const SE_DATA      = {J(se_data)};
const UF_SERIES    = {J(uf_series)};
const BR_SERIES    = {J(br_series)};
const MAP_DATA     = {J(map_data)};
const MONTH_DATES  = {J(MONTH_DATES)};
const MONTH_LABELS = {J(MONTH_LABELS)};
const DEF_SERIES   = {J(deficit_series)};
const OTTO_SERIES  = {J(otto_series)};
const DEF_MAP      = {J(deficit_map)};
const OTTO_MAP     = {J(otto_map)};
const DEF_MONTHS   = {J(def_months)};
const OTTO_MONTHS  = {J(otto_months)};
const DEF_BY_YEAR  = {J(def_by_year)};
const OTT_BY_YEAR  = {J(ott_by_year)};
const DEF_YEARS    = {J(def_years)};
const OTT_YEARS    = {J(ott_years)};
const BR_DEF_SERIES  = {J(br_def)};
const BR_OTTO_SERIES = {J(br_otto)};
const UF_CODE_SD   = {J(UF_CODE_SD)};
const UF_COORDS_SD = {J(UF_COORDS_SD_MAP)};
const CODE_UF_SD   = {J(CODE_UF_SD_MAP)};
const CODE_NAME_SD = {J(CODE_NAME_SD_MAP)};
const CODE_UF      = {J(CODE_UF_PARITY)};
const CODE_NAME    = {J(CODE_NAME_MAP)};
const MONTH_NAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];"""

    # Decompress templates and assemble
    tmpl_before = gzip.decompress(base64.b64decode(_TMPL_BEFORE_B64)).decode("utf-8")
    tmpl_after  = gzip.decompress(base64.b64decode(_TMPL_AFTER_B64)).decode("utf-8")
    html = tmpl_before + data_block + tmpl_after

    out_path = DB_PATH.parent / "se_dashboard.html"
    out_path.write_text(html, encoding="utf-8")
    log.info(f"[Dashboard] Written: {out_path} ({len(html):,} chars)")

# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────

def summary(conn):
    log.info("=" * 60)
    log.info("DB SUMMARY")
    pairs = [
        ("sugar_ny11",      "data_referencia"),
        ("etanol_cepea",    "data_referencia"),
        ("fx_usdbrl",       "data_referencia"),
        ("anp_estados",     "data_inicial"),
        ("anp_brasil",      "data_inicial"),
    ]
    for tbl, col in pairs:
        r = conn.execute(f"SELECT COUNT(*), MIN({col}), MAX({col}) FROM {tbl}").fetchone()
        log.info(f"  {tbl:22}: {r[0]:7,} | {r[1] or '—'} → {r[2] or '—'}")
    for tbl in ["anp_vendas_uf","anp_producao_uf"]:
        r = conn.execute(f"SELECT COUNT(*), MIN(ano), MAX(ano) FROM {tbl}").fetchone()
        lm = conn.execute(
            f"SELECT MAX(ano), MAX(mes) FROM {tbl} WHERE ano=(SELECT MAX(ano) FROM {tbl})"
        ).fetchone()
        log.info(f"  {tbl:22}: {r[0]:7,} | {r[1]}→{r[2]} | latest: {lm[0]}-{lm[1]:02d}")
    log.info("=" * 60)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    # Support --dashboard-only and --force-all flags
    dashboard_only = "--dashboard-only" in sys.argv
    global FORCE_ALL
    FORCE_ALL      = "--force-all" in sys.argv

    log.info("=" * 60)
    if dashboard_only:
        log.info(f"IBBA Extractor | DASHBOARD-ONLY MODE | {NOW_STR}")
    else:
        log.info(f"IBBA Extractor | {TODAY} ({TODAY.strftime('%A')}) | {NOW_STR}")
        log.info(f"  Weekday: {is_weekday()} | Thursday: {is_thursday()} | 5th: {is_month_5th()} | Force: {FORCE_ALL}")
    log.info("=" * 60)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    ensure_schema(conn)

    errors = []

    if not dashboard_only:
        # S&E — daily
        try:
            run_se(conn)
        except Exception as e:
            log.error(f"[S&E] FAILED: {e}")
            errors.append(f"S&E: {e}")

        # Fuel — Thursdays
        try:
            run_fuel(conn)
        except Exception as e:
            log.error(f"[Fuel] FAILED: {e}")
            errors.append(f"Fuel: {e}")

        # Supply/Demand — 5th of month
        try:
            run_supply_demand(conn)
        except Exception as e:
            log.error(f"[Supply/Demand] FAILED: {e}")
            errors.append(f"Supply/Demand: {e}")

    # Regenerate dashboard with latest data
    try:
        generate_dashboard(conn)
    except Exception as e:
        log.error(f'[Dashboard] Generation failed: {e}')
        errors.append(f'Dashboard: {e}')

    summary(conn)
    conn.close()

    if errors:
        log.error(f"EXTRACTOR FINISHED WITH {len(errors)} ERROR(S):")
        for e in errors:
            log.error(f"  • {e}")
        sys.exit(1)
    else:
        log.info("All sections completed successfully.")


if __name__ == "__main__":
    main()
