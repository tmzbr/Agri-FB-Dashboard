#!/usr/bin/env python3
"""
extractor_chicken_bz_domestic.py — Brazil Chicken DOMESTIC Spread Tracker
==========================================================================
Builds / refreshes  chicken_bz_domestic.db  (the "Domestic" tab of the BZ
Chicken Spread Tracker; the "Exports" tab reads chicken_bz.db).

Same spread construction as the export tracker (extractor_chicken_bz.py),
with a single change: the price leg is the domestic wholesale price of
chilled chicken (CEPEA "Frango Resfriado – Estado SP", R$/kg) instead of the
SECEX export price converted to BRL.

  spread = (frango_brl_kg − grain_brl_kg) / frango_brl_kg

DATA SOURCES
  • CEPEA Frango Resfriado SP (R$/kg + US$/kg, daily)
        primary  → full-history xls, https://cepea.org.br/br/indicador/series/frango.aspx?id=130
                   (id=181 is Frango CONGELADO — do not use). Re-downloaded
                   every run, so CEPEA revisions to past days are picked up.
        fallback → https://cepea.org.br/br/indicador/frango.aspx, the table
                   under "FRANGO RESFRIADO" (last 15 trading days in the HTML)
  • Grain basket (corn 66% + soy PNA 34%, R$/sc60kg, 2-month lag)
        → copied from ./chicken_bz.db (_cepea_grain_raw), which the export
          tracker scrapes every weekday. Basket weights, lag and the monthly
          grain maths are imported from extractor_chicken_bz so both tabs
          stay identical at monthly granularity.

GRAIN COST BY GRANULARITY
  monthly  = export tracker rule: average basket of month (M − lag)
  daily    = average basket over the 30 days ending on (d − lag months).
             A monthly figure would make daily/weekly lines step at every
             turn of the month; the rolling window gives the same level
             without the steps.
  weekly   = mean of the daily values of the week.

USAGE
  pip install requests xlrd
  python extractor_chicken_bz_domestic.py                 # download + rebuild
  python extractor_chicken_bz_domestic.py --xls PATH      # load a local xls instead

OUTPUT TABLES
  _cepea_frango_raw  dt, brl_kg, usd_kg
  _cepea_grain_raw   dt, corn_brl_sc, soy_brl_sc   (copy of chicken_bz.db)
  daily    dt, frango_brl_kg, grain_brl_kg, spread
  weekly   start_date (Mon), end_date (Sun), n_days, frango_brl_kg, grain_brl_kg, spread
  monthly  period, year, month, n_days, frango_brl_kg, grain_brl_kg, spread
"""

import bisect, re, sqlite3, sys, time
import urllib.request
from calendar import monthrange
from datetime import datetime, date, timedelta
from pathlib import Path

HERE       = Path(__file__).parent
DB_PATH    = HERE / "chicken_bz_domestic.db"
EXPORT_DB  = HERE / "chicken_bz.db"
SERIES_URL = "https://cepea.org.br/br/indicador/series/frango.aspx?id=130"
PAGE_URL   = "https://cepea.org.br/br/indicador/frango.aspx"
GRAIN_WINDOW_DAYS = 30

sys.path.insert(0, str(HERE))
from extractor_chicken_bz import (  # noqa: E402
    _grain_cost_brl_kg, GRAIN_LAG, CORN_WEIGHT, SOY_WEIGHT)

# CEPEA answers 403 to non-browser User-Agents.
_HDRS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept":          "*/*",
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Referer":         "https://cepea.org.br/",
}


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
        grain_brl_kg  REAL,   -- 30-day basket avg ending (dt − lag months), BRL/kg
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
        grain_brl_kg  REAL,   -- basket of month (M − lag), same as export tracker
        spread        REAL,
        updated_at    TEXT
    );
    """)
    conn.commit()


def _download(url):
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=_HDRS)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read()
        except Exception as e:
            print(f"  [CEPEA] {url[-40:]} attempt {attempt + 1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# CEPEA FRANGO — full series (xls)
# ══════════════════════════════════════════════════════════════════════════════
def load_frango_xls(conn, path=None, content=None):
    """Load a CEPEA 'Série de preços' xls (Data | À vista R$ | À vista US$).
    Returns the number of rows loaded (0 if the file is not the right series)."""
    import xlrd
    # CEPEA's xls files have a malformed OLE directory — xlrd refuses them
    # unless told to ignore it.
    book = xlrd.open_workbook(filename=path, file_contents=content,
                              ignore_workbook_corruption=True)
    sh = book.sheet_by_index(0)
    title = str(sh.cell_value(0, 0)).upper()
    if "RESFRIADO" not in title:
        print(f"  ✗ xls is not the Frango Resfriado series: {title!r}")
        return 0
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
    return len(rows)


def fetch_frango_series(conn):
    content = _download(SERIES_URL)
    if not content or not content.startswith(b"\xd0\xcf\x11\xe0"):   # OLE2 magic
        print("  [CEPEA] series download failed or not an xls.")
        return 0
    try:
        return load_frango_xls(conn, content=content)
    except Exception as e:
        print(f"  [CEPEA] could not parse series xls: {e}")
        return 0


# ══════════════════════════════════════════════════════════════════════════════
# CEPEA FRANGO — page scrape (fallback)
# ══════════════════════════════════════════════════════════════════════════════
def fetch_frango_page(conn):
    raw = _download(PAGE_URL)
    if raw is None:
        return 0
    html = raw.decode("utf-8", errors="replace")
    # The page also carries "FRANGO CONGELADO" — take the table after RESFRIADO.
    i = html.upper().find("FRANGO RESFRIADO CEPEA")
    m = re.search(r"<table[^>]*>(.*?)</table>", html[i:], re.S) if i >= 0 else None
    if not m:
        print("  [CEPEA] Frango Resfriado table not found — page layout changed?")
        return 0
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(1), re.S):
        cells = [re.sub(r"<[^>]+>", "", c).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        d = re.match(r"(\d{2})/(\d{2})/(\d{4})$", cells[0]) if len(cells) >= 2 else None
        if not d:
            continue
        try:
            price = float(cells[1].replace(".", "").replace(",", "."))
        except ValueError:
            continue
        rows.append((f"{d.group(3)}-{d.group(2)}-{d.group(1)}", price))
    conn.executemany(
        """INSERT INTO _cepea_frango_raw(dt, brl_kg) VALUES(?,?)
           ON CONFLICT(dt) DO UPDATE SET brl_kg = excluded.brl_kg""", rows)
    conn.commit()
    span = f"{min(r[0] for r in rows)} → {max(r[0] for r in rows)}" if rows else "—"
    print(f"  [CEPEA] page fallback: {len(rows)} days ({span})")
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


def _shift_months(d, months):
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def _daily_grain_fn(conn):
    """Return f(date) → basket BRL/kg averaged over the GRAIN_WINDOW_DAYS days
    ending (date − GRAIN_LAG months). Falls back to the last available day."""
    g = conn.execute("""SELECT dt, corn_brl_sc, soy_brl_sc FROM _cepea_grain_raw
                        WHERE corn_brl_sc IS NOT NULL AND soy_brl_sc IS NOT NULL
                        ORDER BY dt""").fetchall()
    dts = [r[0] for r in g]
    pc, ps = [0.0], [0.0]
    for _, c, s in g:
        pc.append(pc[-1] + c)
        ps.append(ps[-1] + s)

    def f(d):
        anchor = _shift_months(d, GRAIN_LAG)
        hi = bisect.bisect_right(dts, anchor.isoformat())
        lo = bisect.bisect_right(dts, (anchor - timedelta(days=GRAIN_WINDOW_DAYS)).isoformat())
        if hi == 0:
            return None
        if lo == hi:            # no quote inside the window → last one before it
            lo = hi - 1
        n = hi - lo
        corn, soy = (pc[hi] - pc[lo]) / n, (ps[hi] - ps[lo]) / n
        return (CORN_WEIGHT * corn + SOY_WEIGHT * soy) / 60.0
    return f


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
    daily_grain = _daily_grain_fn(conn)

    # ── daily ────────────────────────────────────────────────────────────────
    daily = []
    for dt, px in raw:
        g = daily_grain(date.fromisoformat(dt))
        daily.append((dt, px, g, _spread(px, g)))
    conn.execute("DELETE FROM daily")
    conn.executemany("INSERT INTO daily VALUES(?,?,?,?)", daily)

    # ── weekly (Mon–Sun) ─────────────────────────────────────────────────────
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

    # ── monthly (export-tracker grain rule) ──────────────────────────────────
    months = {}
    for dt, px, _, _ in daily:
        months.setdefault(dt[:7], []).append(px)
    mrows = []
    for ym, pxs in sorted(months.items()):
        y, m = int(ym[:4]), int(ym[5:7])
        px = sum(pxs) / len(pxs)
        g = _grain_cost_brl_kg(conn, y, m)
        mrows.append((ym, y, m, len(pxs), px, g, _spread(px, g), now))
    conn.execute("DELETE FROM monthly")
    conn.executemany("INSERT INTO monthly VALUES(?,?,?,?,?,?,?,?)", mrows)
    conn.commit()

    print(f"  [MAT] daily={len(daily)}  weekly={len(wrows)}  monthly={len(mrows)}  "
          f"(grain lag {GRAIN_LAG}m)")
    for r in mrows[-3:]:
        sp = f"{r[6]:.3f}×" if r[6] is not None else "—"
        gr = f"{r[5]:.3f}" if r[5] is not None else "—"
        print(f"        {r[0]}  frango {r[4]:.2f}  grain {gr} R$/kg  spread {sp}  ({r[3]}d)")


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Refresh chicken_bz_domestic.db")
    ap.add_argument("--xls", metavar="PATH",
                    help="load a local CEPEA Frango Resfriado xls instead of downloading it")
    args = ap.parse_args()

    print(f"[DB] Opening {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    loaded = load_frango_xls(conn, path=args.xls) if args.xls else fetch_frango_series(conn)
    if not loaded:
        fetch_frango_page(conn)
    sync_grain(conn)
    materialise(conn)

    conn.execute("VACUUM")
    conn.close()
    print(f"\n✓ Done. {DB_PATH.name} = {DB_PATH.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
