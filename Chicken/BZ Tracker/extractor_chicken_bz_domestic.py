#!/usr/bin/env python3
"""
extractor_chicken_bz_domestic.py — Brazil Chicken DOMESTIC Spread Tracker
==========================================================================
Builds / refreshes  chicken_bz_domestic.db.

Same spread construction as the export tracker (extractor_chicken_bz.py),
with a single change: the price leg is the domestic wholesale price of
chilled chicken (CEPEA "Frango Resfriado – Estado SP", R$/kg) instead of the
SECEX export price converted to BRL.

  spread = (frango_brl_kg − grain_brl_kg) / frango_brl_kg      (margin %)

DATA SOURCES
  • CEPEA Frango Resfriado SP (R$/kg, daily)
        history → CEPEA xls download ("Série de preços", id=181), via --xls
        daily   → https://cepea.org.br/br/indicador/frango.aspx
                  table #imagenet-indicador2 (last 15 trading days)
  • Grain basket (corn 66% + soy PNA 34%, R$/sc60kg, 2-month lag)
        → copied from ../chicken_bz.db (_cepea_grain_raw), which the export
          tracker scrapes every weekday. The basket/lag maths is imported
          from extractor_chicken_bz so both trackers stay identical.

USAGE
  pip install requests               (+ xlrd only for --xls)
  python extractor_chicken_bz_domestic.py                 # scrape + rebuild
  python extractor_chicken_bz_domestic.py --xls PATH      # also load CEPEA xls history

OUTPUT TABLES
  _cepea_frango_raw  dt, brl_kg, usd_kg          raw daily CEPEA (usd only from xls)
  _cepea_grain_raw   dt, corn_brl_sc, soy_brl_sc copy of chicken_bz.db
  daily    dt, frango_brl_kg, grain_brl_kg, spread
  weekly   start_date (Mon), end_date (Sun), n_days, frango_brl_kg, grain_brl_kg, spread
  monthly  period, year, month, n_days, frango_brl_kg, grain_brl_kg, spread
"""

import re, sqlite3, sys, time
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

HERE       = Path(__file__).parent
DB_PATH    = HERE / "chicken_bz_domestic.db"
EXPORT_DB  = HERE / "chicken_bz.db"
FRANGO_URL = "https://cepea.org.br/br/indicador/frango.aspx"

sys.path.insert(0, str(HERE))
from extractor_chicken_bz import _grain_cost_brl_kg, GRAIN_LAG  # noqa: E402


def init_db(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS _cepea_frango_raw (
        dt      TEXT PRIMARY KEY,
        brl_kg  REAL,
        usd_kg  REAL
    );
    CREATE TABLE IF NOT EXISTS _cepea_grain_raw (
        dt          TEXT PRIMARY KEY,
        corn_brl_sc REAL,
        soy_brl_sc  REAL
    );
    CREATE TABLE IF NOT EXISTS daily (
        dt            TEXT PRIMARY KEY,
        frango_brl_kg REAL,
        grain_brl_kg  REAL,   -- grain basket BRL/kg (2-mo lag)
        spread        REAL    -- (frango - grain) / frango
    );
    CREATE TABLE IF NOT EXISTS weekly (
        start_date    TEXT PRIMARY KEY,   -- Monday
        end_date      TEXT,               -- Sunday
        n_days        INTEGER,
        frango_brl_kg REAL,
        grain_brl_kg  REAL,
        spread        REAL,
        updated_at    TEXT
    );
    CREATE TABLE IF NOT EXISTS monthly (
        period        TEXT PRIMARY KEY,
        year          INTEGER,
        month         INTEGER,
        n_days        INTEGER,
        frango_brl_kg REAL,
        grain_brl_kg  REAL,
        spread        REAL,
        updated_at    TEXT
    );
    """)
    conn.commit()


# ══════════════════════════════════════════════════════════════════════════════
# CEPEA FRANGO — HISTORY (xls)
# ══════════════════════════════════════════════════════════════════════════════
def load_frango_xls(conn, path):
    """Load the CEPEA 'Série de preços' xls (Data | À vista R$ | À vista US$)."""
    import xlrd
    # CEPEA's xls exports have a malformed OLE directory — xlrd refuses them
    # unless told to ignore it.
    book = xlrd.open_workbook(path, ignore_workbook_corruption=True)
    sh = book.sheet_by_index(0)
    if "RESFRIADO" not in str(sh.cell_value(0, 0)).upper():
        sys.exit(f"  ✗ {path} is not the Frango Resfriado series: {sh.cell_value(0, 0)!r}")
    rows = []
    for r in range(sh.nrows):
        dt, brl, usd = (sh.row_values(r) + ["", "", ""])[:3]
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})$", str(dt).strip())
        if not m or not isinstance(brl, float):
            continue
        rows.append((f"{m.group(3)}-{m.group(2)}-{m.group(1)}", brl,
                     usd if isinstance(usd, float) else None))
    conn.executemany(
        "INSERT OR REPLACE INTO _cepea_frango_raw(dt, brl_kg, usd_kg) VALUES(?,?,?)", rows)
    conn.commit()
    print(f"  [XLS] {len(rows)} rows loaded ({rows[0][0]} → {rows[-1][0]})")


# ══════════════════════════════════════════════════════════════════════════════
# CEPEA FRANGO — DAILY SCRAPE
# ══════════════════════════════════════════════════════════════════════════════
def fetch_frango_daily(conn):
    """
    Scrape the Frango Resfriado table from cepea.org.br. The page shows one row
    and hides the other 14 behind "Mais valores" — all 15 are in the HTML, so a
    run every 2–3 days always overlaps the previous one.
    """
    hdrs = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept":          "text/html,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer":         "https://cepea.org.br/",
    }
    html = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(FRANGO_URL, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", errors="replace")
            break
        except Exception as e:
            print(f"  [CEPEA] attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)
    if html is None:
        print("  [CEPEA] Frango: no data retrieved.")
        return 0

    # Locate the table that follows the "FRANGO RESFRIADO" heading (the page
    # also carries "FRANGO CONGELADO", which must not be picked up).
    i = html.upper().find("FRANGO RESFRIADO CEPEA")
    m = re.search(r"<table[^>]*>(.*?)</table>", html[i:], re.S) if i >= 0 else None
    if not m:
        print("  [CEPEA] Frango Resfriado table not found — page layout changed?")
        return 0

    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 2:
            continue
        d = re.match(r"(\d{2})/(\d{2})/(\d{4})$", cells[0])
        if not d:
            continue
        try:
            price = float(cells[1].replace(".", "").replace(",", "."))
        except ValueError:
            continue
        rows.append((f"{d.group(3)}-{d.group(2)}-{d.group(1)}", price))

    # Keep any usd_kg already loaded from the xls for that date.
    conn.executemany(
        """INSERT INTO _cepea_frango_raw(dt, brl_kg) VALUES(?,?)
           ON CONFLICT(dt) DO UPDATE SET brl_kg = excluded.brl_kg""", rows)
    conn.commit()
    span = f"{min(r[0] for r in rows)} → {max(r[0] for r in rows)}" if rows else "—"
    print(f"  [CEPEA] Frango Resfriado: {len(rows)} days scraped ({span})")
    return len(rows)


# ══════════════════════════════════════════════════════════════════════════════
# GRAIN — copy from the export tracker
# ══════════════════════════════════════════════════════════════════════════════
def sync_grain(conn):
    if not EXPORT_DB.exists():
        print(f"  [GRAIN] {EXPORT_DB.name} not found — keeping existing grain rows.")
        return
    conn.execute("ATTACH DATABASE ? AS bz", (str(EXPORT_DB),))
    n = conn.execute("SELECT COUNT(*) FROM bz._cepea_grain_raw").fetchone()[0]
    if n:
        conn.execute("DELETE FROM main._cepea_grain_raw")
        conn.execute("INSERT INTO main._cepea_grain_raw SELECT dt, corn_brl_sc, soy_brl_sc "
                     "FROM bz._cepea_grain_raw")
    conn.commit()
    conn.execute("DETACH DATABASE bz")
    last = conn.execute("SELECT MAX(dt) FROM _cepea_grain_raw").fetchone()[0]
    print(f"  [GRAIN] {n} rows copied from {EXPORT_DB.name} (last {last})")


# ══════════════════════════════════════════════════════════════════════════════
# MATERIALISE
# ══════════════════════════════════════════════════════════════════════════════
def _spread(frango, grain):
    if frango is None or grain is None or frango <= 0:
        return None
    return (frango - grain) / frango


def materialise(conn):
    now = datetime.utcnow().isoformat()
    raw = conn.execute(
        "SELECT dt, brl_kg FROM _cepea_frango_raw WHERE brl_kg IS NOT NULL ORDER BY dt").fetchall()

    grain_cache = {}
    def grain(y, m):
        if (y, m) not in grain_cache:
            grain_cache[(y, m)] = _grain_cost_brl_kg(conn, y, m)
        return grain_cache[(y, m)]

    # ── daily ────────────────────────────────────────────────────────────────
    daily = []
    for dt, px in raw:
        g = grain(int(dt[:4]), int(dt[5:7]))
        daily.append((dt, px, g, _spread(px, g)))
    conn.execute("DELETE FROM daily")
    conn.executemany("INSERT INTO daily VALUES(?,?,?,?)", daily)

    # ── weekly (Mon–Sun) ─────────────────────────────────────────────────────
    # Grain = mean of the daily lagged grain values, so a week straddling two
    # months blends both months' basket like the price leg does.
    weeks = {}
    for dt, px, g, _ in daily:
        d = date.fromisoformat(dt)
        weeks.setdefault(d - timedelta(days=d.weekday()), []).append((px, g))
    wrows = []
    for mon, vals in sorted(weeks.items()):
        px = sum(v[0] for v in vals) / len(vals)
        gs = [v[1] for v in vals if v[1] is not None]
        g = sum(gs) / len(gs) if gs else None
        wrows.append((mon.isoformat(), (mon + timedelta(days=6)).isoformat(),
                      len(vals), px, g, _spread(px, g), now))
    conn.execute("DELETE FROM weekly")
    conn.executemany("INSERT INTO weekly VALUES(?,?,?,?,?,?,?)", wrows)

    # ── monthly ──────────────────────────────────────────────────────────────
    months = {}
    for dt, px, _, _ in daily:
        months.setdefault(dt[:7], []).append(px)
    mrows = []
    for ym, pxs in sorted(months.items()):
        y, m = int(ym[:4]), int(ym[5:7])
        px = sum(pxs) / len(pxs)
        g = grain(y, m)
        mrows.append((ym, y, m, len(pxs), px, g, _spread(px, g), now))
    conn.execute("DELETE FROM monthly")
    conn.executemany("INSERT INTO monthly VALUES(?,?,?,?,?,?,?,?)", mrows)
    conn.commit()

    print(f"  [MAT] daily={len(daily)}  weekly={len(wrows)}  monthly={len(mrows)}  "
          f"(grain lag {GRAIN_LAG}m)")
    for r in mrows[-3:]:
        sp = f"{r[6]:.1%}" if r[6] is not None else "—"
        gr = f"{r[5]:.3f}" if r[5] is not None else "—"
        print(f"        {r[0]}  frango {r[4]:.2f}  grain {gr} R$/kg  spread {sp}  ({r[3]}d)")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Refresh chicken_bz_domestic.db")
    ap.add_argument("--xls", metavar="PATH",
                    help="CEPEA Frango Resfriado xls (Série de preços) to load as history")
    args = ap.parse_args()

    print(f"[DB] Opening {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    if args.xls:
        load_frango_xls(conn, args.xls)
    fetch_frango_daily(conn)
    sync_grain(conn)
    materialise(conn)

    conn.execute("VACUUM")
    conn.close()
    print(f"\n✓ Done. {DB_PATH.name} = {DB_PATH.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
