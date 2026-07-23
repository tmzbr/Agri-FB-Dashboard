"""
WASDE S&D Tracker -- script unico.

Extrai os dados de Supply & Demand do WASDE (USDA) pras commodities/paises
definidos em CONFIG abaixo, e grava a evolucao mes a mes num SQLite
(wasde.db) que o index.html le direto no browser.

Uso:
    python wasde.py monthly                  -> busca o mes/ano atual
    python wasde.py monthly 2026 7            -> busca um mes especifico
    python wasde.py backfill                  -> historico completo (2010-04 ate hoje)
    python wasde.py backfill --skip-zips       -> so 2021 em diante (sem baixar os zips)
    python wasde.py backfill --only-year 2023  -> so um ano especifico
    python wasde.py diagnostico                -> confere nomes de commodity/region no banco
"""

import argparse
import csv
import datetime
import io
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from contextlib import contextmanager

import requests
import pdfplumber


# =============================================================================
# CONFIG -- ajuste aqui pra mudar commodities/paises rastreados
# =============================================================================

# Nomes exatos como aparecem na coluna "Commodity" do CSV do WASDE
COMMODITIES = [
    "Corn",
    "Oilseed, Soybean",   # soja em grao (nao inclui farelo/oleo)
    "Cotton",
]

# Nomes exatos como aparecem na coluna "Region" do CSV do WASDE
# "Others" NAO fica salvo no banco -- e calculado dinamicamente (World menos
# a soma dos paises abaixo) via VIEW SQL.
REGIONS = [
    "World",
    "United States",
    "Brazil",
    "Argentina",
    "China",
    "India",
    "Pakistan",
    "European Union",
]
REGIONS_FOR_OTHERS_CALC = [r for r in REGIONS if r != "World"]

WASDE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(WASDE_DIR, "wasde.db")

BASE_MONTHLY_CSV_URL = "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-{year}-{month:02d}.csv"
BASE_MONTHLY_CSV_URL_V2 = "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-{year}-{month:02d}-V2.csv"
ZIP_2010_2015 = "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-2010-04-to-2015-12.zip"
ZIP_2016_2020 = "https://www.usda.gov/sites/default/files/documents/oce-wasde-report-data-2016-01-to-2020-12.zip"

# Fonte ESTAVEL pra atualizacao mensal (mirror da Mann Library/Cornell -- nao
# tem o bloqueio anti-bot que o www.usda.gov tem pra trafego de datacenter).
# A pagina sempre lista o release mais recente primeiro, entao nao precisa
# adivinhar URL por data -- so pega o primeiro link .xml da lista.
WASDE_LISTING_URL = "https://esmis.nal.usda.gov/publication/world-agricultural-supply-and-demand-estimates"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_RETRIES = 4
BACKOFF_SECONDS = 15
REQUEST_TIMEOUT = 60
# Pausa entre CADA requisicao (mesmo as que deram certo) durante o backfill,
# pra evitar disparar rate-limit/anti-bot do usda.gov ao fazer muitas
# chamadas em sequencia rapida.
SLEEP_BETWEEN_MONTHS = 5


# =============================================================================
# DB -- schema, parsing do CSV, upsert
# =============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS wasde_facts (
    wasde_number    INTEGER NOT NULL,
    report_title    TEXT    NOT NULL,
    release_date    TEXT,
    commodity       TEXT    NOT NULL,
    region          TEXT    NOT NULL,
    market_year     TEXT    NOT NULL,
    attribute       TEXT    NOT NULL,
    proj_est_flag   TEXT,
    value           REAL,
    unit            TEXT NOT NULL DEFAULT '',
    forecast_year   INTEGER,
    forecast_month  INTEGER,
    ingested_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (wasde_number, commodity, region, market_year, attribute, unit)
);
CREATE INDEX IF NOT EXISTS idx_wasde_number ON wasde_facts(wasde_number);
-- (indices por commodity/region/market_year foram removidos: nesse volume de
-- linhas (~100k) o SQLite escaneia rapido sem eles, e eles inflavam o .db em
-- ~7MB por pouco ganho de performance)

CREATE TABLE IF NOT EXISTS ingestion_log (
    year            INTEGER NOT NULL,
    month           INTEGER NOT NULL,
    wasde_number    INTEGER,
    status          TEXT NOT NULL,
    detail          TEXT,
    ingested_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (year, month)
);
"""

OTHERS_VIEW_TEMPLATE = """
DROP VIEW IF EXISTS wasde_facts_with_others;
CREATE VIEW wasde_facts_with_others AS
SELECT wasde_number, report_title, release_date, commodity, region, market_year,
       attribute, proj_est_flag, value, unit, forecast_year, forecast_month
FROM wasde_facts

UNION ALL

SELECT
    w.wasde_number, w.report_title, w.release_date, w.commodity,
    'Others' AS region, w.market_year, w.attribute, w.proj_est_flag,
    (w.value - COALESCE((
        SELECT SUM(w2.value) FROM wasde_facts w2
        WHERE w2.wasde_number = w.wasde_number AND w2.commodity = w.commodity
          AND w2.market_year = w.market_year AND w2.attribute = w.attribute
          AND w2.unit = w.unit
          AND w2.region IN ({others_regions})
    ), 0)) AS value,
    w.unit, w.forecast_year, w.forecast_month
FROM wasde_facts w
WHERE w.region = 'World';
"""


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def ensure_schema():
    others_regions_sql = ",".join(f"'{r}'" for r in REGIONS_FOR_OTHERS_CALC)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(OTHERS_VIEW_TEMPLATE.format(others_regions=others_regions_sql))


def upsert_rows(rows):
    if not rows:
        return 0
    with get_conn() as conn:
        conn.executemany(
            """
            INSERT INTO wasde_facts (
                wasde_number, report_title, release_date, commodity, region,
                market_year, attribute, proj_est_flag, value, unit,
                forecast_year, forecast_month
            ) VALUES (
                :wasde_number, :report_title, :release_date, :commodity, :region,
                :market_year, :attribute, :proj_est_flag, :value, :unit,
                :forecast_year, :forecast_month
            )
            ON CONFLICT(wasde_number, commodity, region, market_year, attribute, unit)
            DO UPDATE SET
                value = excluded.value,
                proj_est_flag = excluded.proj_est_flag,
                release_date = excluded.release_date,
                report_title = excluded.report_title,
                ingested_at = datetime('now')
            """,
            rows,
        )
    return len(rows)


def log_ingestion(year, month, wasde_number, status, detail=""):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO ingestion_log (year, month, wasde_number, status, detail)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(year, month) DO UPDATE SET
                wasde_number=excluded.wasde_number, status=excluded.status,
                detail=excluded.detail, ingested_at=datetime('now')
            """,
            (year, month, wasde_number, status, detail),
        )


def already_ingested(year, month):
    with get_conn() as conn:
        cur = conn.execute("SELECT status FROM ingestion_log WHERE year=? AND month=?", (year, month))
        row = cur.fetchone()
        return row is not None and row[0] == "ok"


# =============================================================================
# NORMALIZACAO DE ATRIBUTOS -- o WASDE mudou grafia/capitalizacao de alguns
# nomes de atributo ao longo dos 16+ anos de historico (ex: "Ending Stocks"
# vs "Ending stocks", "Production" vs "Production 2/"). Isso normaliza tudo
# pra um nome canonico, senao a mesma serie historica "quebra" visualmente
# nos anos que mudaram a grafia.
# =============================================================================

CANONICAL_ATTRIBUTES = {
    "area harvested": "Area Harvested", "area planted": "Area Planted",
    "avg. farm price": "Avg. Farm Price", "avg. farm price - high": "Avg. Farm Price - High",
    "avg. farm price - low": "Avg. Farm Price - Low",
    "beginning stocks": "Beginning Stocks", "ccc inventory": "CCC Inventory",
    "domestic feed": "Domestic Feed", "domestic total": "Domestic Total", "domestic, total": "Domestic, Total",
    "domestic use": "Domestic Use", "domestic crush": "Domestic Crush",
    "ending stocks": "Ending Stocks", "ethanol & by-products": "Ethanol & By-products",
    "ethanol for fuel": "Ethanol for Fuel",
    "exports": "Exports", "exports, total": "Exports, Total", "exports - other": "Exports - Other",
    "feed and residual": "Feed and Residual", "food, seed & industrial": "Food, Seed & Industrial",
    "free stocks": "Free Stocks", "imports": "Imports", "imports - other": "Imports - Other",
    "outstanding loans": "Outstanding Loans", "production": "Production",
    "supply, total": "Supply, Total", "total supply": "Total Supply",
    "use, total": "Use, Total", "total use": "Total Use",
    "yield per harvested acre": "Yield per Harvested Acre",
    "crushings": "Crushings", "residual": "Residual", "seed": "Seed",
    "harvested": "Harvested", "planted": "Planted", "output": "Output",
    "loss": "Loss", "trade": "Trade", "unaccounted": "Unaccounted",
    "deliveries": "Deliveries",
    "florida": "Florida", "food": "Food", "hawaii": "Hawaii",
    "high-tier tariff/other": "High-tier Tariff/Other", "louisiana": "Louisiana",
    "mexico": "Mexico", "miscellaneous": "Miscellaneous", "non-program": "Non-program",
    "other": "Other", "other program": "Other Program", "stocks to use ratio": "Stocks to Use Ratio",
    "trq": "TRQ", "texas": "Texas",
}


def normalize_attribute(raw):
    if raw is None:
        return raw
    s = raw.strip()
    s = re.sub(r'\s+\d+/\s*$', '', s)   # "X 2/" -> "X"
    s = re.sub(r'\s*/\d+\s*$', '', s)   # "X /2" -> "X"
    s = re.sub(r'\s+', ' ', s).strip()
    return CANONICAL_ATTRIBUTES.get(s.lower(), s)


def parse_csv_text_to_rows(csv_text):
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = []
    wasde_number = None

    for r in reader:
        commodity = (r.get("Commodity") or "").strip()
        region = (r.get("Region") or "").strip()
        if commodity not in COMMODITIES or region not in REGIONS:
            continue
        if not (r.get("MarketYear") or "").strip():
            continue  # pula linhas de "Reliability of Projections"

        try:
            value = float(r["Value"]) if r.get("Value") not in (None, "") else None
        except ValueError:
            value = None
        try:
            wn = int(r["WasdeNumber"])
            wasde_number = wn
        except (ValueError, TypeError, KeyError):
            wn = None

        rows.append({
            "wasde_number": wn,
            "report_title": (r.get("ReportDate") or "").strip(),
            "release_date": (r.get("ReleaseDate") or "").strip(),
            "commodity": commodity,
            "region": region,
            "market_year": (r.get("MarketYear") or "").strip(),
            "attribute": normalize_attribute((r.get("Attribute") or "").strip()),
            "proj_est_flag": (r.get("ProjEstFlag") or "").strip(),
            "value": value,
            "unit": (r.get("Unit") or "").strip(),
            "forecast_year": int(r["ForecastYear"]) if (r.get("ForecastYear") or "").strip() else None,
            "forecast_month": int(r["ForecastMonth"]) if (r.get("ForecastMonth") or "").strip() else None,
        })

    return rows, wasde_number


# =============================================================================
# PARSER XML -- fonte estavel (Mann Library/Cornell), usada na atualizacao
# mensal daqui pra frente. Formato bem mais aninhado que o CSV (e um export
# de SQL Server Reporting Services), mas foi validado byte-a-byte contra o
# CSV oficial (292 valores comparados, 0 divergencias).
# =============================================================================

XML_COMMODITY_TITLES = {
    "World Corn Supply and Use": "Corn",
    "World Soybean Supply and Use": "Oilseed, Soybean",
    "World Cotton Supply and Use": "Cotton",
}
XML_US_ONLY_TITLES = set()  # nenhuma commodity atual usa o parsing "so US" (era so o Sugar)


def _xml_clean_region(raw):
    return re.sub(r'\s+', ' ', re.sub(r'\s*\d+/\s*$', '', raw)).strip()


def _xml_clean_attribute(raw):
    return normalize_attribute(re.sub(r'\s+', ' ', raw).strip())


def _xml_clean_market_year(raw):
    raw = raw.strip()
    m = re.match(r'([\d/]+)\s*(Est\.|Proj\.)?', raw)
    return (m.group(1), m.group(2) or "") if m else (raw, "")


def _xml_parse_value(raw):
    if raw is None:
        return None
    raw = raw.replace(',', '').strip()
    try:
        return float(raw)
    except ValueError:
        return None


def parse_wasde_xml(xml_text):
    """Recebe o XML de um release do WASDE (formato Mann Library/Cornell) e
    retorna (rows, wasde_number) no mesmo formato de parse_csv_text_to_rows."""
    root = ET.fromstring(xml_text)
    dedup = {}

    for sr in root:
        report_el = sr.find('Report')
        if report_el is None:
            continue
        title_raw = report_el.get('sub_report_title', '').strip()
        title = re.sub(r'\s+1/\s*$', '', title_raw).strip()
        title = re.sub(r"\s*\(Cont'd\.\)\s*$", '', title).strip()

        commodity = XML_COMMODITY_TITLES.get(title)
        if commodity is None:
            continue
        is_us_only = title in XML_US_ONLY_TITLES

        page_title = report_el.get('page_title', '')
        wm = re.search(r'WASDE\s*-\s*(\d+)', page_title)
        wasde_number = int(wm.group(1)) if wm else None
        report_month = report_el.get('Report_Month', '').strip()
        unit = report_el.get('sub_report_subtitle', '').strip('() ')

        for matrix in report_el:
            if not re.match(r'matrix\d+$', matrix.tag):
                continue

            if is_us_only:
                # tabelas "so US" nao tem dimensao de regiao no XML -- o
                # numero de sufixo interno (attributeN) e descoberto
                # dinamicamente pois nem sempre bate com o N do <matrixN>
                attr_suffixes = {m.group(1) for el in matrix.iter()
                                  for m in [re.match(r'attribute(\d+)$', el.tag)] if m}
                for n in attr_suffixes:
                    for attr_el in matrix.iter(f'attribute{n}'):
                        attribute = _xml_clean_attribute(attr_el.get(f'attribute{n}', ''))
                        for yr_el in attr_el.iter():
                            yr_raw = yr_el.attrib.get(f'market_year{n}')
                            if yr_raw is None:
                                continue
                            market_year, proj_flag = _xml_clean_market_year(yr_raw)
                            cell = next((c for c in yr_el.iter('Cell') if f'cell_value{n}' in c.attrib), None)
                            value = _xml_parse_value(cell.get(f'cell_value{n}')) if cell is not None else None
                            key = (wasde_number, commodity, 'United States', market_year, attribute)
                            if value is None and key in dedup:
                                continue
                            dedup[key] = {
                                "wasde_number": wasde_number, "report_title": report_month,
                                "release_date": "", "commodity": commodity, "region": "United States",
                                "market_year": market_year, "attribute": attribute,
                                "proj_est_flag": proj_flag, "value": value, "unit": unit,
                                "forecast_year": None, "forecast_month": None,
                            }
            else:
                # o sufixo interno (regionN/attributeN/cell_valueN) nem sempre
                # bate com o numero da propria tag <matrixN> -- descobre
                # dinamicamente escaneando por chaves 'regionN' de verdade,
                # igual ja fazemos no ramo US-only acima.
                region_ns = {mm.group(1) for el in matrix.iter() for k in el.attrib
                             for mm in [re.match(r'region(\d+)$', k)] if mm}
                for n in region_ns:
                    market_year_raw = matrix.get(f'region_header{n}', '')
                    market_year, proj_flag = _xml_clean_market_year(market_year_raw)
                    region_key, attr_key, value_key = f'region{n}', f'attribute{n}', f'cell_value{n}'

                    for region_el in matrix.iter():
                        region_raw = region_el.attrib.get(region_key)
                        if region_raw is None:
                            continue
                        region = _xml_clean_region(region_raw)
                        if region not in REGIONS:
                            continue
                        for attr_el in region_el.iter():
                            attr_raw = attr_el.attrib.get(attr_key)
                            if attr_raw is None:
                                continue
                            attribute = _xml_clean_attribute(attr_raw)
                            cell = next((c for c in attr_el.iter('Cell') if value_key in c.attrib), None)
                            value = _xml_parse_value(cell.get(value_key)) if cell is not None else None
                            key = (wasde_number, commodity, region, market_year, attribute)
                            if value is None and key in dedup:
                                continue  # nao deixa uma celula "filler" sobrescrever um valor real ja achado
                            dedup[key] = {
                                "wasde_number": wasde_number, "report_title": report_month,
                                "release_date": "", "commodity": commodity, "region": region,
                                "market_year": market_year, "attribute": attribute,
                                "proj_est_flag": proj_flag, "value": value, "unit": unit,
                                "forecast_year": None, "forecast_month": None,
                            }

    rows = list(dedup.values())
    wasde_number = rows[0]["wasde_number"] if rows else None
    return rows, wasde_number


# =============================================================================
# FETCH -- busca de 1 mes especifico (usado pelo Actions todo mes)
# =============================================================================

def _get_with_retries(url):
    session = requests.Session()
    session.headers.update(HEADERS)
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.text
            if resp.status_code == 404:
                return None
            last_exc = RuntimeError(f"HTTP {resp.status_code} em {url}")
        except requests.RequestException as e:
            last_exc = e

        if attempt < MAX_RETRIES:
            wait = BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"  tentativa {attempt} falhou ({last_exc}); aguardando {wait}s...")
            time.sleep(wait)

    raise RuntimeError(f"Falhou apos {MAX_RETRIES} tentativas em {url}: {last_exc}")


def fetch_latest_release():
    """Descobre e baixa o release mais recente do WASDE via o mirror da Mann
    Library/Cornell (esmis.nal.usda.gov) -- fonte estavel, sem o bloqueio
    anti-bot que o www.usda.gov tem pra trafego de datacenter/Actions.
    A pagina de listagem sempre traz o release mais recente primeiro.
    Retorna (rows, wasde_number, report_month_str).
    """
    print(f"Buscando lista de releases em {WASDE_LISTING_URL} ...")
    html = _get_with_retries(WASDE_LISTING_URL)
    if html is None:
        raise RuntimeError("Nao consegui acessar a pagina de listagem do WASDE")

    m = re.search(r'/sites/default/release-files/(\d+)/(wasde\d{4}(?:v\d+)?)\.xml', html)
    if not m:
        raise RuntimeError("Nao encontrei nenhum link .xml na pagina de listagem")

    xml_url = f"https://esmis.nal.usda.gov/sites/default/release-files/{m.group(1)}/{m.group(2)}.xml"
    print(f"Release mais recente encontrado: {xml_url}")
    xml_text = _get_with_retries(xml_url)
    if xml_text is None:
        raise RuntimeError(f"Encontrei o link {xml_url} mas nao consegui baixar")

    rows, wasde_number = parse_wasde_xml(xml_text)
    report_month = rows[0]["report_title"] if rows else ""
    return rows, wasde_number, report_month


def fetch_month(year, month):
    """Fallback: busca o CSV de um mes especifico direto do www.usda.gov.
    Mantido para reprocessamento de meses antigos especificos; a atualizacao
    mensal normal usa fetch_latest_release() (fonte estavel)."""
    url = BASE_MONTHLY_CSV_URL.format(year=year, month=month)
    print(f"Buscando {url} ...")
    text = _get_with_retries(url)

    if text is None:
        url_v2 = BASE_MONTHLY_CSV_URL_V2.format(year=year, month=month)
        print(f"  {url} nao encontrado (404); tentando {url_v2} ...")
        text = _get_with_retries(url_v2)

    if text is None:
        raise FileNotFoundError(f"Nenhum CSV encontrado para {year}-{month:02d}")

    return parse_csv_text_to_rows(text)


_MONTH_NAMES = {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6,
    "July": 7, "August": 8, "September": 9, "October": 10, "November": 11, "December": 12,
}


def cmd_monthly(year=None, month=None):
    ensure_schema()

    if year is None and month is None:
        # modo padrao (usado pelo Actions todo mes): pega sempre o release
        # mais recente disponivel via a fonte estavel, sem precisar adivinhar
        # data nenhuma.
        try:
            rows, wasde_number, report_month = fetch_latest_release()
            m = re.match(r'(\w+)\s+(\d{4})', report_month)
            if m:
                month_num = _MONTH_NAMES.get(m.group(1))
                year_num = int(m.group(2))
            else:
                today = datetime.date.today()
                year_num, month_num = today.year, today.month

            n = upsert_rows(rows)
            log_ingestion(year_num, month_num, wasde_number, "ok", f"{n} linhas (fonte estavel)")
            print(f"OK: {n} linhas gravadas (WasdeNumber={wasde_number}) para {report_month}")
            return True
        except Exception as e:
            print(f"ERRO: {e}")
            raise

    # modo explicito (usado pelo backfill / reprocessamento de mes especifico):
    # busca o CSV antigo direto do www.usda.gov pra aquele ano-mes exato
    today = datetime.date.today()
    year = year or today.year
    month = month or today.month
    try:
        rows, wasde_number = fetch_month(year, month)
        n = upsert_rows(rows)
        log_ingestion(year, month, wasde_number, "ok", f"{n} linhas")
        print(f"OK: {n} linhas gravadas (WasdeNumber={wasde_number}) para {year}-{month:02d}")
        return True
    except FileNotFoundError as e:
        log_ingestion(year, month, None, "not_found", str(e))
        print(f"AVISO: {e} (relatorio desse mes pode ainda nao ter saido)")
        return False
    except Exception as e:
        log_ingestion(year, month, None, "error", str(e))
        print(f"ERRO: {e}")
        raise


# =============================================================================
# BACKFILL -- historico completo (rodar 1x)
# =============================================================================

def _download_zip_bytes(url):
    print(f"Baixando {url} ...")
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    return resp.content


def _process_zip(url):
    try:
        content = _download_zip_bytes(url)
    except Exception as e:
        print(f"ERRO baixando {url}: {e}")
        return 0

    total_rows = 0
    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        csv_names = [n for n in zf.namelist() if n.lower().endswith(".csv")]
        print(f"  {len(csv_names)} arquivo(s) CSV encontrados dentro do zip")
        for name in csv_names:
            print(f"  processando {name} ...")
            with zf.open(name) as f:
                text = f.read().decode("utf-8", errors="replace")
            rows, wasde_number = parse_csv_text_to_rows(text)
            n = upsert_rows(rows)
            total_rows += n
            print(f"    {n} linhas gravadas (WasdeNumber={wasde_number})")
    return total_rows


def _month_range(start_year, start_month, end_year, end_month):
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


def cmd_backfill(skip_zips=False, only_year=None, max_minutes=170):
    """
    max_minutes: orcamento de tempo do backfill. Ao atingir esse limite, para
    de forma limpa (em vez de ser matado no meio pelo timeout do Actions job,
    o que faria perder TODO o progresso porque o commit so roda depois do
    script terminar). Como e idempotente, rodar de novo continua de onde
    parou (already_ingested pula o que ja deu certo).
    """
    ensure_schema()
    grand_total = 0
    failures = []
    start_time = time.time()

    if not skip_zips:
        print("=" * 60)
        print("ETAPA 1/2: ZIPs consolidados (abr/2010 a dez/2020)")
        print("=" * 60)
        grand_total += _process_zip(ZIP_2010_2015)
        grand_total += _process_zip(ZIP_2016_2020)

    print("=" * 60)
    print("ETAPA 2/2: CSVs mensais individuais (jan/2021 em diante)")
    print("=" * 60)

    today = datetime.date.today()
    start_year = only_year or 2021
    end_year = only_year or today.year
    end_month = 12 if only_year else today.month

    for year, month in _month_range(start_year, 1, end_year, end_month):
        elapsed_min = (time.time() - start_time) / 60
        if elapsed_min > max_minutes:
            print(f"\nORCAMENTO DE TEMPO ({max_minutes} min) atingido -- parando de forma limpa.")
            print(f"Faltou processar a partir de {year}-{month:02d}. Rode 'backfill' de novo")
            print("(vai pular automaticamente tudo que ja deu certo) pra continuar.")
            failures.append((year, month, "orcamento de tempo atingido -- rode de novo"))
            break

        if already_ingested(year, month):
            print(f"{year}-{month:02d}: ja processado, pulando")
            continue
        print(f"{year}-{month:02d}: buscando...")
        try:
            ok = cmd_monthly(year, month)
            if not ok:
                failures.append((year, month, "not_found"))
        except Exception as e:
            print(f"  FALHOU: {e}")
            failures.append((year, month, str(e)))

        time.sleep(SLEEP_BETWEEN_MONTHS)

    print("=" * 60)
    print(f"CONCLUIDO. Total de linhas gravadas nesta rodada: {grand_total}")
    if failures:
        print(f"\n{len(failures)} mes(es) com problema (rode o script de novo mais tarde):")
        for y, m, reason in failures:
            print(f"  - {y}-{m:02d}: {reason}")
    else:
        print("Nenhuma falha registrada.")


# =============================================================================
# DIAGNOSTICO -- confere nomes de commodity/region contra o banco real
# =============================================================================

def cmd_diagnostico():
    conn = sqlite3.connect(DB_PATH)

    print("=== Commodities distintas no banco (compare com COMMODITIES) ===")
    for row in conn.execute("SELECT DISTINCT commodity, COUNT(*) FROM wasde_facts GROUP BY commodity ORDER BY commodity"):
        marker = "OK" if row[0] in COMMODITIES else "  "
        print(f"  [{marker}] {row[0]:30s} ({row[1]} linhas)")

    print("\n=== Regions distintas no banco (compare com REGIONS) ===")
    for row in conn.execute("SELECT DISTINCT region, COUNT(*) FROM wasde_facts GROUP BY region ORDER BY region"):
        marker = "OK" if row[0] in REGIONS else "  "
        print(f"  [{marker}] {row[0]:30s} ({row[1]} linhas)")

    print("\nSe algo esperado nao aparecer marcado [OK], o nome pode ter mudado")
    print("de grafia em algum ano -- confira com uma query tipo:")
    print("  SELECT DISTINCT commodity FROM wasde_facts WHERE commodity LIKE '%Corn%';")
    print("e ajuste as listas COMMODITIES/REGIONS no topo deste arquivo.")

    conn.close()


def cmd_ingest_file(path):
    """Ingere um CSV ja baixado manualmente (fallback quando o usda.gov bloqueia
    requisicoes automatizadas). O nome do arquivo precisa conter YYYY-MM em
    algum lugar (ex: oce-wasde-report-data-2026-08.csv) pra logar o mes certo."""
    ensure_schema()
    fname = os.path.basename(path)
    text = open(path, encoding="utf-8", errors="replace").read()
    rows, wn = parse_csv_text_to_rows(text)
    n = upsert_rows(rows)

    m = re.search(r"(\d{4})-(\d{2})(-V2)?\.csv", fname)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        log_ingestion(y, mo, wn, "ok", f"{n} linhas (upload manual)")
        print(f"OK: {n} linhas gravadas (WasdeNumber={wn}) para {y}-{mo:02d}")
    else:
        print(f"OK: {n} linhas gravadas (WasdeNumber={wn}) -- nao consegui")
        print("identificar ano/mes pelo nome do arquivo, ingestion_log nao foi atualizado")


# =============================================================================
# WAP (World Agricultural Production) -- fonte complementar pra Area Harvested
# e Yield por pais (o WASDE so tem isso pra United States). O WAP e um circular
# mensal do FAS, publicado no MESMO dia do WASDE, arquivado no mesmo mirror
# estavel (Mann Library/Cornell). So existe em PDF, entao usamos pdfplumber
# pra extrair as tabelas de Area/Yield/Production por pais.
# =============================================================================

WAP_LISTING_URL = "https://esmis.nal.usda.gov/publication/world-agricultural-production"
WAP_CURRENT_URL = "https://apps.fas.usda.gov/psdonline/circulars/production.pdf"
WAP_TABLE_TITLES = {
    "Corn": "Corn Area, Yield, and Production",
    "Oilseed, Soybean": "Soybean Area, Yield, and Production",
    "Cotton": "Cotton Area, Yield, and Production",
}
WAP_YIELD_UNIT = {
    "Corn": "Metric Tons per Hectare",
    "Oilseed, Soybean": "Metric Tons per Hectare",
    "Cotton": "Kilograms per Hectare",
}
WAP_ROW_RE = re.compile(r'^([A-Za-z][A-Za-z ,\.\-]*?)\s+((?:-?[\d,]+(?:\.\d+)?\s+){9,15}-?[\d,]+(?:\.\d+)?)$')


def _wap_extract_years(text):
    """Acha os 2 rotulos de safra (prelim + atual) no cabecalho da tabela."""
    lines = text.split("\n")
    prelim_year, current_year = None, None
    for line in lines[:8]:
        matches = re.findall(r'\d{4}/\d{2}', line)
        if len(matches) >= 2 and current_year is None and "Proj." in "".join(lines[:5]):
            # linha tipo "2023/24 2024/25 Jul Aug ..." -> prelim = 2o valor distinto
            uniq = list(dict.fromkeys(matches))
            if len(uniq) >= 2:
                prelim_year = uniq[1]
        if "Proj." in line:
            m2 = re.findall(r'\d{4}/\d{2}', line)
            if m2:
                current_year = list(dict.fromkeys(m2))[0]
    return prelim_year, current_year


def parse_wap_pdf(pdf_bytes, report_title):
    """Extrai Area Harvested e Yield (World + paises rastreados) das tabelas
    de Corn/Soybean/Cotton de um PDF do WAP. Retorna lista de rows no mesmo
    formato usado por upsert_rows (sem wasde_number ainda -- isso e resolvido
    depois via lookup no proprio wasde_facts pelo report_title)."""
    rows = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            commodity = None
            for cmdty, title in WAP_TABLE_TITLES.items():
                if title in text:
                    commodity = cmdty
                    break
            if commodity is None:
                continue

            prelim_year, current_year = _wap_extract_years(text)
            if not current_year:
                continue

            for line in text.split("\n"):
                line = line.strip()
                m = WAP_ROW_RE.match(line)
                if not m:
                    continue
                region = m.group(1).strip()
                if region not in REGIONS:
                    continue
                nums_raw = m.group(2).split()
                if len(nums_raw) == 16:
                    prelim_idx, current_idx = 1, 3
                elif len(nums_raw) == 11:
                    # maio: safra nova so tem 1 coluna (sem par mes-anterior/atual)
                    prelim_idx, current_idx = 1, 2
                else:
                    continue
                nums = [float(x.replace(",", "")) for x in nums_raw]
                n_cols = 4 if len(nums_raw) == 16 else 3

                yield_unit = WAP_YIELD_UNIT[commodity]
                for col_idx, market_year in [(prelim_idx, prelim_year), (current_idx, current_year)]:
                    if not market_year:
                        continue
                    area_v = nums[col_idx]
                    yield_v = nums[col_idx + n_cols]
                    rows.append({
                        "report_title": report_title, "release_date": "",
                        "commodity": commodity, "region": region, "market_year": market_year,
                        "attribute": "Area Harvested", "proj_est_flag": "",
                        "value": area_v, "unit": "Million Hectares",
                        "forecast_year": None, "forecast_month": None,
                    })
                    rows.append({
                        "report_title": report_title, "release_date": "",
                        "commodity": commodity, "region": region, "market_year": market_year,
                        "attribute": "Yield per Harvested Acre", "proj_est_flag": "",
                        "value": yield_v, "unit": yield_unit,
                        "forecast_year": None, "forecast_month": None,
                    })
    return rows


def fetch_wap_releases_page(page_num=None):
    """Baixa uma pagina da listagem do WAP e retorna [(pdf_url, date_str), ...]
    em ordem (mais recente primeiro na pagina 0), sem duplicatas."""
    url = WAP_LISTING_URL if page_num is None else f"{WAP_LISTING_URL}?page={page_num}"
    html = _get_with_retries(url)
    if html is None:
        return []
    out = []
    seen = set()
    for row in re.findall(r'<tr>.*?</tr>', html, re.DOTALL):
        dm = re.search(r'datetime="(\d{4}-\d{2}-\d{2})T', row)
        hm = re.search(r'<a href="(/sites/default/release-files/[^"]+\.pdf)"', row)
        if not dm or not hm:
            continue
        iso_date = dm.group(1)
        if iso_date in seen:
            continue
        seen.add(iso_date)
        dt = datetime.datetime.strptime(iso_date, "%Y-%m-%d")
        out.append((f"https://esmis.nal.usda.gov{hm.group(1)}", dt.strftime("%b %d %Y")))
    return out


def _wap_date_to_report_title(date_str):
    """'Aug 12 2025' -> 'August 2025'"""
    dt = datetime.datetime.strptime(date_str, "%b %d %Y")
    return dt.strftime("%B %Y")


def cmd_wap_ingest_release(pdf_url, report_title):
    print(f"  Baixando {pdf_url} ...")
    resp_text = None
    session = requests.Session()
    session.headers.update(HEADERS)
    r = session.get(pdf_url, timeout=60)
    if r.status_code != 200:
        print(f"  ERRO: HTTP {r.status_code}")
        return 0
    rows = parse_wap_pdf(r.content, report_title)

    # resolve o wasde_number correspondente via o proprio wasde_facts (WAP e
    # WASDE saem no mesmo dia, entao devem compartilhar o mesmo numero)
    with get_conn() as conn:
        wn_row = conn.execute(
            "SELECT DISTINCT wasde_number FROM wasde_facts WHERE report_title=? LIMIT 1", (report_title,)
        ).fetchone()
    wasde_number = wn_row[0] if wn_row else None
    if wasde_number is None:
        print(f"  AVISO: nao achei WasdeNumber pra '{report_title}' no wasde_facts -- pulando")
        return 0

    for row in rows:
        row["wasde_number"] = wasde_number
    n = upsert_rows(rows)
    print(f"  OK: {n} linhas (WasdeNumber={wasde_number}, {report_title})")
    return n


def _wap_report_title_from_pdf_text(text):
    """Acha 'Month Year' logo apos 'Circular Series' / 'WAP MM-YY' no topo do PDF."""
    m = re.search(r'Circular Series\s*\n?\s*WAP\s*[\d-]+\s*\n?\s*([A-Za-z]+ \d{4})', text)
    return m.group(1) if m else None


def cmd_wap_monthly():
    """Busca o release mais recente do WAP direto da URL 'atual' (que a propria
    USDA mantem sempre com o mes mais novo -- diferente do mirror da Cornell,
    que fica alguns meses atrasado)."""
    ensure_schema()
    print(f"Buscando {WAP_CURRENT_URL} ...")
    session = requests.Session()
    session.headers.update(HEADERS)
    r = session.get(WAP_CURRENT_URL, timeout=60)
    if r.status_code != 200:
        print(f"ERRO: HTTP {r.status_code}")
        return
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        first_page_text = pdf.pages[0].extract_text() or ""
    report_title = _wap_report_title_from_pdf_text(first_page_text)
    if not report_title:
        print("ERRO: nao consegui identificar o mes do relatorio no PDF")
        return
    print(f"Release atual do WAP: {report_title}")

    rows = parse_wap_pdf(r.content, report_title)
    with get_conn() as conn:
        wn_row = conn.execute(
            "SELECT DISTINCT wasde_number FROM wasde_facts WHERE report_title=? LIMIT 1", (report_title,)
        ).fetchone()
    wasde_number = wn_row[0] if wn_row else None
    if wasde_number is None:
        print(f"AVISO: nao achei WasdeNumber pra '{report_title}' no wasde_facts ainda")
        print("(rode 'python wasde.py monthly' primeiro nesse mes, pra ter o WASDE gravado)")
        return
    for row in rows:
        row["wasde_number"] = wasde_number
    n = upsert_rows(rows)
    print(f"OK: {n} linhas (WasdeNumber={wasde_number}, {report_title})")


def cmd_wap_backfill(max_pages=30, max_minutes=170, start_page=0):
    ensure_schema()
    start_time = time.time()
    total = 0
    page = start_page
    while page < start_page + max_pages:
        elapsed = (time.time() - start_time) / 60
        if elapsed > max_minutes:
            print(f"Orcamento de tempo ({max_minutes}min) atingido na pagina {page}. Rode de novo pra continuar.")
            break
        releases = fetch_wap_releases_page(page)
        if not releases:
            print(f"Pagina {page}: vazia, parando.")
            break
        print(f"--- Pagina {page}: {len(releases)} releases ---")
        for pdf_url, date_str in releases:
            report_title = _wap_date_to_report_title(date_str)
            dt = datetime.datetime.strptime(date_str, "%b %d %Y")
            if dt.year < 2010 or (dt.year == 2010 and dt.month < 4):
                print(f"{report_title}: antes de abr/2010, parando backfill.")
                return
            print(f"{report_title}:")
            n = cmd_wap_ingest_release(pdf_url, report_title)
            total += n
            time.sleep(1.5)
        page += 1
    print(f"CONCLUIDO. Total de linhas gravadas: {total}")


# =============================================================================
# CLI
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_monthly = sub.add_parser("monthly", help="busca um mes (padrao: mes atual)")
    p_monthly.add_argument("year", nargs="?", type=int, default=None)
    p_monthly.add_argument("month", nargs="?", type=int, default=None)

    p_backfill = sub.add_parser("backfill", help="busca todo o historico")
    p_backfill.add_argument("--skip-zips", action="store_true")
    p_backfill.add_argument("--only-year", type=int, default=None)
    p_backfill.add_argument("--max-minutes", type=int, default=170,
                             help="para de forma limpa apos esse tempo (default: 170min)")

    sub.add_parser("diagnostico", help="confere nomes de commodity/region no banco")

    p_ingest = sub.add_parser("ingest-file", help="processa um CSV ja baixado manualmente")
    p_ingest.add_argument("path")

    sub.add_parser("wap-monthly", help="busca o release mais recente do WAP (Area Harvested/Yield por pais)")
    p_wap_backfill = sub.add_parser("wap-backfill", help="busca todo o historico do WAP desde abr/2010")
    p_wap_backfill.add_argument("--max-pages", type=int, default=30)
    p_wap_backfill.add_argument("--max-minutes", type=int, default=170)
    p_wap_backfill.add_argument("--start-page", type=int, default=0)

    args = parser.parse_args()

    if args.command == "monthly":
        cmd_monthly(args.year, args.month)
    elif args.command == "backfill":
        cmd_backfill(skip_zips=args.skip_zips, only_year=args.only_year, max_minutes=args.max_minutes)
    elif args.command == "diagnostico":
        cmd_diagnostico()
    elif args.command == "ingest-file":
        cmd_ingest_file(args.path)
    elif args.command == "wap-monthly":
        cmd_wap_monthly()
    elif args.command == "wap-backfill":
        cmd_wap_backfill(max_pages=args.max_pages, max_minutes=args.max_minutes, start_page=args.start_page)


if __name__ == "__main__":
    main()
