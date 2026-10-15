"""Fechamento diário das empresas da Comps Table → Supabase (comps_price).

Lê os símbolos de comps_company (ABEV3.SA, JBS, ...), baixa os últimos
pregões no Yahoo Finance e faz upsert de (símbolo, data, fechamento). Grava
a janela inteira, não só o último dia: se o job falhar um dia, o seguinte
preenche o buraco, e o front sempre usa o fechamento mais recente.

Roda antes da abertura (comps_prices.yml), então o último candle completo
é o D-1. Se o Yahoo falhar para um símbolo, tenta o Google Finance, que
só dá o último preço (gravado com a data de ontem útil).

Uso:
    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... python comps_prices.py [--days 10]
"""

import argparse
import datetime as dt
import os
import re
import sys
import time

import requests

# UA curto de propósito: o Yahoo devolve 429 tanto para "python-requests"
# quanto para um UA completo de Chrome sem os cookies que o acompanham
# (testado em 15/10/2026); "Mozilla/5.0" puro passa.
UA = {"User-Agent": "Mozilla/5.0"}


def sb_headers(key):
    return {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def symbols(url, key):
    r = requests.get(f"{url}/rest/v1/comps_company?select=price_symbol,currency",
                     headers=sb_headers(key), timeout=30)
    r.raise_for_status()
    return {row["price_symbol"]: row["currency"] for row in r.json() if row.get("price_symbol")}


def yahoo(symbol, days):
    last = None
    for host in ("query1", "query2"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                             params={"range": f"{days}d", "interval": "1d"}, headers=UA, timeout=30)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
            tz = dt.timezone(dt.timedelta(seconds=res["meta"].get("gmtoffset", 0)))
            today = dt.datetime.now(tz).date()
            out = []
            for ts, close in zip(res.get("timestamp") or [], res["indicators"]["quote"][0]["close"]):
                d = dt.datetime.fromtimestamp(ts, tz).date()
                # Candle de hoje ainda está aberto se o job rodar no pregão.
                if close is None or d >= today:
                    continue
                out.append((d.isoformat(), round(float(close), 4)))
            if out:
                return out, res["meta"].get("currency")
        except Exception as e:  # noqa: BLE001 — tenta o próximo host/fonte
            last = e
            time.sleep(2)
    raise RuntimeError(f"yahoo {symbol}: {last}")


def google(symbol):
    # ABEV3.SA → ABEV3:BVMF ; JBS → JBS:NYSE
    q = f"{symbol[:-3]}:BVMF" if symbol.endswith(".SA") else f"{symbol}:NYSE"
    r = requests.get(f"https://www.google.com/finance/quote/{q}", headers=UA, timeout=30)
    r.raise_for_status()
    m = re.search(r'data-last-price="([\d.]+)"', r.text)
    if not m:
        raise RuntimeError(f"google {q}: preço não encontrado")
    d = dt.date.today() - dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return [(d.isoformat(), float(m.group(1)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    args = ap.parse_args()

    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

    rows, failed = [], []
    for sym, ccy in symbols(url, key).items():
        try:
            closes, yccy = yahoo(sym, args.days)
            src = "yahoo"
        except Exception as e:  # noqa: BLE001
            print(f"  ! {e} — tentando Google Finance")
            try:
                closes, yccy, src = google(sym), ccy, "google"
            except Exception as e2:  # noqa: BLE001
                print(f"  ! {e2}")
                failed.append(sym)
                continue
        print(f"  {sym:10s} {closes[-1][0]} {closes[-1][1]:>10.2f} {yccy or ccy} ({src})")
        rows += [{"price_symbol": sym, "date": d, "close": c, "currency": yccy or ccy, "source": src}
                 for d, c in closes]

    if rows:
        r = requests.post(f"{url}/rest/v1/comps_price?on_conflict=price_symbol,date",
                          headers={**sb_headers(key), "Prefer": "resolution=merge-duplicates"},
                          json=rows, timeout=60)
        r.raise_for_status()
    print(f"{len(rows)} fechamentos gravados; falhas: {failed or 'nenhuma'}")
    # Falha parcial não derruba o job (o front cai no último preço salvo),
    # mas falha total sim — aí é a fonte que mudou.
    if failed and not rows:
        sys.exit(1)


if __name__ == "__main__":
    main()
