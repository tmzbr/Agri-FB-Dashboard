#!/usr/bin/env python3
"""
extractor_cattle_cycle.py — Cattle Cycle tracker (Brazil tab)
=============================================================
Builds / refreshes  cattle_cycle.db.

SOURCE
  IBGE — Pesquisa Trimestral do Abate de Animais, SIDRA table 1092
  https://sidra.ibge.gov.br/tabela/1092
    level      Brasil (N1)
    variables  284 Animais abatidos (cabeças) · 285 Peso total das carcaças (kg)
    months     c12716: 115233/115234/115235 = 1st/2nd/3rd month of the quarter
    herd type  c18 (all): Bois, Vacas, Novilhos, Novilhas, Vitelos e vitelas, Total
    inspection c12529 = 118225 Total (IBGE headline)

  The table is quarterly but carries each month of the quarter, so the
  series is monthly from Jan/1997. The whole history (~2 MB) is downloaded
  every run so IBGE revisions are always picked up.

CYCLE INDICATOR
  female_share = (Vacas + Novilhas) / (Vacas + Novilhas + Bois + Novilhos)
  (heads; calves — "Vitelos e vitelas" — are excluded, and are mostly not
  published by IBGE anyway: "...")

  If IBGE ever suppresses Novilhas ("X"), heifers are taken as the residual
  total − bois − vacas − novilhos (flag novilhas_imputed = 1). The Total
  series has no suppressed cells today.

USAGE
  pip install requests
  python extractor_cattle_cycle.py

OUTPUT TABLES
  br_slaughter_raw  period (YYYY-MM), herd, heads, carcass_kg
                    herd ∈ total, bois, vacas, novilhos, novilhas, vitelos
  br_monthly        period, year, month, heads_* / carcass_kg_* per herd,
                    female_heads, male_heads, female_share, updated_at
"""

import sqlite3, sys, time
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing: pip install requests")

DB_PATH = Path(__file__).parent / "cattle_cycle.db"

SIDRA_URL = ("https://apisidra.ibge.gov.br/values/t/1092/n1/all/v/284,285/p/all"
             "/c12716/115233,115234,115235/c18/all/c12529/118225")

MONTH_IN_QUARTER = {"115233": 1, "115234": 2, "115235": 3}
HERD = {"992": "total", "55": "bois", "56": "vacas",
        "111734": "novilhos", "111735": "novilhas", "57": "vitelos"}
HERDS = ["total", "bois", "vacas", "novilhos", "novilhas", "vitelos"]


def init_db(conn):
    cols = ",\n        ".join(f"heads_{h} REAL, carcass_kg_{h} REAL" for h in HERDS)
    # Earlier versions held other inspection types — the DB is fully rebuilt
    # from SIDRA, so drop any old layout.
    raw_cols = [r[1] for r in conn.execute("PRAGMA table_info(br_slaughter_raw)")]
    if "inspection" in raw_cols:
        conn.executescript("DROP TABLE br_slaughter_raw; DROP TABLE IF EXISTS br_monthly;")
    conn.executescript(f"""
    CREATE TABLE IF NOT EXISTS br_slaughter_raw (
        period      TEXT,     -- YYYY-MM
        herd        TEXT,
        heads       REAL,
        carcass_kg  REAL,
        PRIMARY KEY (period, herd)
    );
    CREATE TABLE IF NOT EXISTS br_monthly (
        period       TEXT PRIMARY KEY,
        year         INTEGER,
        month        INTEGER,
        {cols},
        female_heads REAL,    -- vacas + novilhas
        male_heads   REAL,    -- bois + novilhos
        female_share REAL,    -- female / (female + male)
        novilhas_imputed INTEGER,  -- 1 = heifers = residual (IBGE suppressed)
        updated_at   TEXT
    );
    """)
    conn.commit()


def _num(v):
    # SIDRA placeholders: "-" zero, "..." not available, "X" confidential
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_sidra(conn):
    data = None
    for attempt in range(4):
        try:
            r = requests.get(SIDRA_URL, timeout=120)
            r.raise_for_status()
            data = r.json()
            break
        except Exception as e:
            print(f"  [SIDRA] attempt {attempt + 1} failed: {e}")
            time.sleep(5 * 2 ** attempt)
    if not data or len(data) < 2:
        sys.exit("  ✗ SIDRA 1092: no data retrieved")

    vals = {}   # (period, herd) → [heads, kg]
    for row in data[1:]:
        q = row["D3C"]                       # YYYYQQ, e.g. 202602
        m = (int(q[4:]) - 1) * 3 + MONTH_IN_QUARTER[row["D4C"]]
        key = (f"{q[:4]}-{m:02d}", HERD[row["D5C"]])
        slot = vals.setdefault(key, [None, None])
        slot[0 if row["D2C"] == "284" else 1] = _num(row["V"])

    periods = sorted({p for p, _ in vals})
    print(f"  [SIDRA] {len(vals)} rows · {periods[0]} → {periods[-1]}")

    # The workflow polls daily around IBGE's release dates — leave the DB
    # file untouched (so nothing gets committed) unless IBGE changed something.
    new = {(p, h, v[0], v[1]) for (p, h), v in vals.items()}
    old = set(conn.execute("SELECT period, herd, heads, carcass_kg FROM br_slaughter_raw"))
    if new == old:
        print("  [SIDRA] no change since last run — nothing to update.")
        return False

    conn.executemany(
        "INSERT OR REPLACE INTO br_slaughter_raw(period, herd, heads, carcass_kg) VALUES(?,?,?,?)",
        sorted(new))
    conn.commit()
    print(f"  [SIDRA] {len(new - old)} new/revised rows")
    return True


def materialise(conn):
    now = datetime.utcnow().isoformat()
    raw = {}
    for p, h, heads, kg in conn.execute("SELECT period, herd, heads, carcass_kg FROM br_slaughter_raw"):
        raw.setdefault(p, {})[h] = (heads, kg)

    rows = []
    for p in sorted(raw):
        d = raw[p]
        get = lambda h, i: d.get(h, (None, None))[i]
        # Months where IBGE has not published yet come back as all-NULL — skip.
        if get("total", 0) is None:
            continue
        parts = [get(h, 0) for h in ("vacas", "novilhas", "bois", "novilhos")]
        imputed = 0
        if parts[1] is None and None not in (parts[0], parts[2], parts[3]):
            parts[1] = get("total", 0) - parts[0] - parts[2] - parts[3]
            imputed = 1
        if None in parts:
            fem = male = share = None
        else:
            fem, male = parts[0] + parts[1], parts[2] + parts[3]
            share = fem / (fem + male) if fem + male else None
        rec = [p, int(p[:4]), int(p[5:])]
        for h in HERDS:
            rec += [parts[1] if h == "novilhas" else get(h, 0), get(h, 1)]
        rows.append(rec + [fem, male, share, imputed, now])

    conn.execute("DELETE FROM br_monthly")
    conn.executemany(f"INSERT INTO br_monthly VALUES({','.join('?' * len(rows[0]))})", rows)
    conn.commit()
    print(f"  [MAT] br_monthly = {len(rows)} months")
    for r in rows[-3:]:
        print(f"        {r[0]}  total {r[3]:>12,.0f} hd   female share {r[-3]:.1%}")


def main():
    print(f"[DB] Opening {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)
    if fetch_sidra(conn):
        materialise(conn)
        conn.execute("VACUUM")
    conn.close()
    print(f"\n✓ Done. {DB_PATH.name} = {DB_PATH.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
