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
import sqlite3
import sys
import time
import zipfile
from contextlib import contextmanager

import requests


# =============================================================================
# CONFIG -- ajuste aqui pra mudar commodities/paises rastreados
# =============================================================================

# Nomes exatos como aparecem na coluna "Commodity" do CSV do WASDE
COMMODITIES = [
    "Corn",
    "Oilseed, Soybean",   # soja em grao (nao inclui farelo/oleo)
    "Cotton",
    "Sugar",
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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/csv,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
MAX_RETRIES = 3
BACKOFF_SECONDS = 8
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
    unit            TEXT,
    forecast_year   INTEGER,
    forecast_month  INTEGER,
    ingested_at     TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (wasde_number, commodity, region, market_year, attribute)
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
            ON CONFLICT(wasde_number, commodity, region, market_year, attribute)
            DO UPDATE SET
                value = excluded.value, unit = excluded.unit,
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
            "report_title": (r.get("ReportTitle") or "").strip(),
            "release_date": (r.get("ReleaseDate") or "").strip(),
            "commodity": commodity,
            "region": region,
            "market_year": (r.get("MarketYear") or "").strip(),
            "attribute": (r.get("Attribute") or "").strip(),
            "proj_est_flag": (r.get("ProjEstFlag") or "").strip(),
            "value": value,
            "unit": (r.get("Unit") or "").strip(),
            "forecast_year": int(r["ForecastYear"]) if (r.get("ForecastYear") or "").strip() else None,
            "forecast_month": int(r["ForecastMonth"]) if (r.get("ForecastMonth") or "").strip() else None,
        })

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
            resp = session.get(url, timeout=30)
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


def fetch_month(year, month):
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


def cmd_monthly(year=None, month=None):
    today = datetime.date.today()
    year = year or today.year
    month = month or today.month

    ensure_schema()
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
    print("  SELECT DISTINCT commodity FROM wasde_facts WHERE commodity LIKE '%Sugar%';")
    print("e ajuste as listas COMMODITIES/REGIONS no topo deste arquivo.")

    conn.close()


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

    args = parser.parse_args()

    if args.command == "monthly":
        cmd_monthly(args.year, args.month)
    elif args.command == "backfill":
        cmd_backfill(skip_zips=args.skip_zips, only_year=args.only_year, max_minutes=args.max_minutes)
    elif args.command == "diagnostico":
        cmd_diagnostico()


if __name__ == "__main__":
    main()
