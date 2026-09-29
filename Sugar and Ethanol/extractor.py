#!/usr/bin/env python3
"""
extractor.py — Agri Monitor · Unified Daily Extractor
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

# ── Supply/Demand windows ───────────────────────────────────────────────────
# ANP "vendas" (sales) is published on the last business day of month M+1 for
# month M's data — confirmed empirically (Mar->30/04, Apr->29/05, May->30/06,
# Jun->31/07, all landing on the last weekday on/before the calendar month-end).
# Window = last days of the month + first few days of the next month as a
# safety net in case publication slips.
def is_vendas_window() -> bool:
    return FORCE_ALL or TODAY.day >= 28 or TODAY.day <= 3

# ANP "produção" (biofuel production) has no confirmed fixed publish day —
# observed fills ranged from the 16th to the 24th of the month. Check weekly
# (Fridays) across that broader window until a tighter pattern is confirmed.
def is_producao_window() -> bool:
    return FORCE_ALL or (12 <= TODAY.day <= 28 and TODAY.weekday() == 4)


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

    # yfinance's `end` is exclusive of the date itself, so end=TODAY always
    # skips today's own close — even after market close, even hours after
    # the run started. Using TODAY+1 lets today's close (if already settled
    # by run time) be captured the same evening instead of the next run.
    end = (TODAY + timedelta(days=1)).strftime("%Y-%m-%d")
    df = yf.Ticker("SB=F").history(start=start, end=end, auto_adjust=False)
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

    results = {}

    if is_vendas_window():
        results["vendas"] = ingest_vendas(conn)
    else:
        log.info("[vendas] Outside publication window (day 28-31 or 1-3) — skipping.")
        results["vendas"] = {"skipped": True}

    if is_producao_window():
        results["producao"] = ingest_producao(conn)
    else:
        log.info("[producao] Outside publication window (Fridays, day 12-28) — skipping.")
        results["producao"] = {"skipped": True}

    return results


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
        log.info(f"Agri Extractor | DASHBOARD-ONLY MODE | {NOW_STR}")
    else:
        log.info(f"Agri Extractor | {TODAY} ({TODAY.strftime('%A')}) | {NOW_STR}")
        log.info(f"  Weekday: {is_weekday()} | Thursday: {is_thursday()} | "
                 f"Vendas window: {is_vendas_window()} | Producao window: {is_producao_window()} | "
                 f"Force: {FORCE_ALL}")
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
