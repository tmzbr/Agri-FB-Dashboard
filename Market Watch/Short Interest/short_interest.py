"""
Short Interest — monitor de posições de empréstimo de ativos (short interest)
das companhias cobertas.

Fonte:
  B3 · Boletim Diário do Mercado (BDI) · Empréstimos de Ativos — Posições em aberto
  Endpoint (tabela 553 = BTBLendingOpenPosition):
    POST https://arquivos.b3.com.br/bdi/table/BTBLendingOpenPosition/{date}/{date}/{page}/{take}
    Content-Type: application/json   body: {}
  Colunas: Data | Data | Código IF | Código ISIN | Empresa | Tipo de empréstimo |
           Mercado | Saldo em quantidade do ativo | Preço médio | Saldo em R$
  Filtramos Mercado == "Total" para os tickers cobertos. "Saldo em quantidade do
  ativo" é o número de ações emprestadas (= posição vendida/short do mercado).

  Ações em circulação (denominador do short interest % das ações totais):
    B3 · Empresas Listadas · GetListedSupplementCompany
    GET https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/
        GetListedSupplementCompany/{base64({issuingCompany, language})}
    → campo totalNumberShares

  A B3 mantém apenas ~21 pregões desta tabela (limitDate D-21), então o histórico
  é acumulado localmente: cada execução grava o snapshot do dia no SQLite.

Execução:
  python short_interest.py                # incremental (último pregão disponível)
  python short_interest.py --backfill 20  # tenta os últimos 20 pregões
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator

import requests

# ============================================================================
# CONFIGURAÇÃO
# ============================================================================

# ticker (Código IF na tabela B3)  →  issuingCompany (Empresas Listadas)
# O issuer é o ticker sem o dígito final (ABEV3 → ABEV), confirmado para os 13.
TICKERS: list[str] = [
    "MBRF3", "BEEF3", "JBSS3", "ABEV3", "MDIA3", "CAML3", "SLCE3",
    "TTEN3", "SMTO3", "JALL3", "SOJA3", "VITT3", "AGRO3",
]

SCRIPT_DIR = Path(__file__).resolve().parent
DB_PATH    = SCRIPT_DIR / "short_interest.db"
DASHBOARD_HTML = SCRIPT_DIR / "short_interest.html"

TABLE_NAME = "BTBLendingOpenPosition"
BDI_URL = (
    "https://arquivos.b3.com.br/bdi/table/"
    + TABLE_NAME
    + "/{date}/{date}/{page}/{take}"
)
SUPPLEMENT_URL = (
    "https://sistemaswebb3-listados.b3.com.br/listedCompaniesProxy/CompanyCall/"
    "GetListedSupplementCompany/{payload}"
)

HTTP_HEADERS = {
    "User-Agent": "short-interest-monitor/1.0 (github-actions)",
    "Accept": "application/json",
}
PAGE_TAKE = 500          # linhas por página no POST da tabela
MAX_LOOKBACK_DAYS = 15   # dias corridos para trás ao procurar o último pregão

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("si")


def issuer_of(ticker: str) -> str:
    """ABEV3 → ABEV (issuingCompany usado na API de Empresas Listadas)."""
    return re.sub(r"\d+$", "", ticker)


# ============================================================================
# SCHEMA
# ============================================================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    ticker       TEXT PRIMARY KEY,
    issuer       TEXT NOT NULL,
    nome         TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Um snapshot por (ticker, data). Acumula histórico do short interest.
CREATE TABLE IF NOT EXISTS short_interest (
    ticker        TEXT NOT NULL,
    data          TEXT NOT NULL,   -- YYYY-MM-DD (pregão de referência)
    qtd           REAL,            -- Saldo em quantidade do ativo (ações emprestadas)
    volume        REAL,            -- Saldo em R$
    preco_medio   REAL,            -- volume / qtd
    shares_out    REAL,            -- ações em circulação (totalNumberShares) no dia
    pct_shares    REAL,            -- qtd / shares_out * 100
    nome          TEXT,
    PRIMARY KEY (ticker, data)
);
CREATE INDEX IF NOT EXISTS idx_si_ticker ON short_interest(ticker);
CREATE INDEX IF NOT EXISTS idx_si_data   ON short_interest(data);
"""


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with db_conn() as conn:
        conn.executescript(SCHEMA)
        for t in TICKERS:
            conn.execute(
                "INSERT INTO companies(ticker, issuer) VALUES(?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET issuer=excluded.issuer",
                (t, issuer_of(t)),
            )


# ============================================================================
# HTTP
# ============================================================================

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HTTP_HEADERS)
    return s


def _num_br(s) -> float | None:
    """'15.763.664.889' → 15763664889.0 ; '1.020,32' → 1020.32"""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fetch_lending_page(sess: requests.Session, day: str, page: int,
                       take: int = PAGE_TAKE, retries: int = 3) -> dict | None:
    url = BDI_URL.format(date=day, page=page, take=take)
    for attempt in range(retries):
        try:
            r = sess.post(url, data="{}",
                          headers={"Content-Type": "application/json"}, timeout=60)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json"):
                return r.json()
            return None
        except requests.RequestException as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log.warning("  falha ao buscar %s p%d: %s", day, page, e)
                return None
    return None


def fetch_lending_all(sess: requests.Session, day: str) -> list[list]:
    """Retorna todas as linhas (values) da tabela de empréstimos para o dia."""
    first = fetch_lending_page(sess, day, 1)
    if not first or "table" not in first:
        return []
    tbl = first["table"]
    rows = list(tbl.get("values") or [])
    page_count = int(tbl.get("pageCount") or 1)
    for p in range(2, page_count + 1):
        nxt = fetch_lending_page(sess, day, p)
        if nxt and nxt.get("table"):
            rows.extend(nxt["table"].get("values") or [])
    return rows


def find_latest_lending_date(sess: requests.Session) -> str | None:
    """Anda para trás a partir de hoje até achar um pregão com dados."""
    today = date.today()
    for i in range(MAX_LOOKBACK_DAYS):
        d = today - timedelta(days=i)
        if d.weekday() >= 5:  # sáb/dom
            continue
        day = d.isoformat()
        page = fetch_lending_page(sess, day, 1, take=1)
        if page and page.get("table", {}).get("values"):
            return day
    return None


def fetch_shares_out(sess: requests.Session, issuer: str) -> dict:
    payload = base64.b64encode(
        json.dumps({"issuingCompany": issuer, "language": "pt-br"}).encode()
    ).decode()
    url = SUPPLEMENT_URL.format(payload=payload)
    try:
        r = sess.get(url, timeout=45)
        r.raise_for_status()
        data = r.json()
        # a API às vezes devolve o array como string JSON (dupla codificação)
        if isinstance(data, str):
            data = json.loads(data)
        c = (data or [{}])[0]
        if not isinstance(c, dict):
            raise ValueError("payload inesperado")
        return {
            "total":     _num_br(c.get("totalNumberShares")),
            "common":    _num_br(c.get("numberCommonShares")),
            "preferred": _num_br(c.get("numberPreferredShares")),
            "name":      (c.get("tradingName") or "").strip() or None,
        }
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        log.warning("  shares_out %s falhou: %s", issuer, e)
        return {"total": None, "common": None, "preferred": None, "name": None}


# ============================================================================
# INGESTÃO
# ============================================================================

# Índices das colunas na tabela BTBLendingOpenPosition
C_COD_IF   = 2   # Código IF (ticker)
C_ISIN     = 3
C_EMPRESA  = 4
C_TIPO     = 5   # Tipo de empréstimo (classe: ON / PN / DRN ...)
C_MERCADO  = 6   # Mercado (Total | Neg. Eletrônica D+1 | Registro)
C_QTD      = 7   # Saldo em quantidade do ativo
C_PRECO    = 8   # Preço médio
C_VOLUME   = 9   # Saldo em R$


def ingest_day(sess: requests.Session, conn: sqlite3.Connection, day: str) -> int:
    log.info("Empréstimos de ativos — pregão %s", day)
    rows = fetch_lending_all(sess, day)
    if not rows:
        log.warning("  sem dados para %s", day)
        return 0

    covered = set(TICKERS)
    # Somar linhas Mercado=Total por ticker (cobre múltiplas classes ON/PN).
    agg: dict[str, dict] = {}
    for r in rows:
        cod = r[C_COD_IF]
        if cod not in covered:
            continue
        if str(r[C_MERCADO]).strip().lower() != "total":
            continue
        a = agg.setdefault(cod, {"qtd": 0.0, "volume": 0.0, "nome": r[C_EMPRESA]})
        a["qtd"]    += float(r[C_QTD] or 0)
        a["volume"] += float(r[C_VOLUME] or 0)

    if not agg:
        log.warning("  nenhum ticker coberto com posição em aberto em %s", day)
        return 0

    n = 0
    for ticker, a in agg.items():
        issuer = issuer_of(ticker)
        so = fetch_shares_out(sess, issuer)
        shares_out = so["total"]
        qtd = a["qtd"]
        vol = a["volume"]
        pct = round(qtd / shares_out * 100, 4) if (shares_out and shares_out > 0) else None
        preco = round(vol / qtd, 4) if qtd else None
        nome = a["nome"] or so["name"] or ticker
        conn.execute(
            """
            INSERT INTO short_interest
                (ticker, data, qtd, volume, preco_medio, shares_out, pct_shares, nome)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(ticker, data) DO UPDATE SET
                qtd=excluded.qtd, volume=excluded.volume,
                preco_medio=excluded.preco_medio, shares_out=excluded.shares_out,
                pct_shares=excluded.pct_shares, nome=excluded.nome
            """,
            (ticker, day, round(qtd), round(vol, 2), preco, shares_out, pct, nome),
        )
        conn.execute(
            "UPDATE companies SET nome=? WHERE ticker=?", (nome, ticker)
        )
        n += 1
        log.info("  %-6s  %14s ações  (%.2f%% do capital)  R$ %s",
                 ticker, f"{round(qtd):,}", pct or 0, f"{round(vol):,}")
    return n


# ============================================================================
# DASHBOARD
# ============================================================================

def _replace_block(html: str, name: str, new_val: str) -> str:
    """Substitui o valor de `const NAME = <value>` (objeto, array ou string)."""
    m = re.search(rf"(const {re.escape(name)}\s*=\s*)", html)
    if not m:
        log.warning("Constante '%s' não encontrada no HTML — pulando", name)
        return html
    start = m.end()
    if start >= len(html):
        return html
    first = html[start]
    if first in "{[":
        depth = 0
        i = start
        while i < len(html):
            c = html[i]
            if c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        end = i + 1
    elif first in "\"'":
        i = start + 1
        while i < len(html):
            if html[i] == "\\":
                i += 2
                continue
            if html[i] == first:
                break
            i += 1
        end = i + 1
    else:
        log.warning("Valor inesperado para '%s' — pulando", name)
        return html
    return html[:start] + new_val + html[end:]


def build_dashboard(conn: sqlite3.Connection) -> None:
    if not DASHBOARD_HTML.exists():
        log.warning("Template HTML não encontrado: %s — pulando", DASHBOARD_HTML)
        return

    # SI_SERIES: histórico diário por ticker
    series: dict[str, list] = {}
    for r in conn.execute(
        "SELECT ticker, data, qtd, volume, pct_shares "
        "FROM short_interest ORDER BY ticker, data"
    ):
        series.setdefault(r["ticker"], []).append({
            "d":   r["data"],
            "q":   round(r["qtd"] or 0),
            "v":   round(r["volume"] or 0, 2),
            "pct": round(r["pct_shares"], 4) if r["pct_shares"] is not None else None,
        })

    # SI_LATEST: último snapshot + variação diária (DoD)
    latest: dict[str, dict] = {}
    company_names: dict[str, str] = {}
    last_date = ""
    for ticker, rows in series.items():
        if not rows:
            continue
        cur = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else None
        rec = conn.execute(
            "SELECT nome, shares_out, preco_medio FROM short_interest "
            "WHERE ticker=? AND data=?", (ticker, cur["d"])
        ).fetchone()
        nome = rec["nome"] if rec else ticker
        company_names[ticker] = nome
        latest[ticker] = {
            "d":         cur["d"],
            "q":         cur["q"],
            "v":         cur["v"],
            "pct":       cur["pct"],
            "shares_out": rec["shares_out"] if rec else None,
            "preco":     rec["preco_medio"] if rec else None,
            "dod_q":     (cur["q"] - prev["q"]) if prev else None,
            "dod_pct":   (round((cur["q"] - prev["q"]) / prev["q"] * 100, 2)
                          if prev and prev["q"] else None),
            "nome":      nome,
        }
        if cur["d"] > last_date:
            last_date = cur["d"]

    js = lambda obj: json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    html = _replace_block(html, "SI_SERIES",     js(series))
    html = _replace_block(html, "SI_LATEST",     js(latest))
    html = _replace_block(html, "COMPANY_NAMES", js(company_names))
    html = _replace_block(html, "LAST_UPDATE",   js(last_date))
    DASHBOARD_HTML.write_text(html, encoding="utf-8")
    log.info("Dashboard atualizado: %s (%d bytes)", DASHBOARD_HTML.name, len(html))


# ============================================================================
# MAIN
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Short Interest — B3 empréstimo de ativos")
    parser.add_argument("--backfill", type=int, default=0,
                        help="Tenta os últimos N pregões (dias corridos) e grava todos com dados")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Pula a atualização do dashboard HTML")
    args = parser.parse_args()

    init_db()
    sess = _session()

    with db_conn() as conn:
        if args.backfill > 0:
            today = date.today()
            days = []
            for i in range(args.backfill):
                d = today - timedelta(days=i)
                if d.weekday() < 5:
                    days.append(d.isoformat())
            total = 0
            for day in days:
                total += ingest_day(sess, conn, day)
            log.info("Backfill: %d registros gravados", total)
        else:
            day = find_latest_lending_date(sess)
            if not day:
                log.error("Nenhum pregão com dados encontrado nos últimos %d dias.",
                          MAX_LOOKBACK_DAYS)
                return
            ingest_day(sess, conn, day)

        if not args.no_dashboard:
            build_dashboard(conn)

    log.info("Concluído. DB: %s", DB_PATH)


if __name__ == "__main__":
    main()
