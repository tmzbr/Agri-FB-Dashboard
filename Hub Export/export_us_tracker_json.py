#!/usr/bin/env python3
"""
export_us_tracker_json.py — U.S. packer trackers → JSON for the research hub
============================================================================
    python "Hub Export/export_us_tracker_json.py" beef|chicken|pork

Writes us-<protein>-tracker.json next to the tracker DB for the Research
Dashboard Hub. Rows keep the DB column names (snake_case) so the hub reads the
same fields the legacy pages read; the hub recomputes every spread itself
because the analyst moves the weights, the lag or the producer type.

CONTRACT (schemaVersion 1)
  { "schemaVersion": 1, "asOf": "2026-10-16", "protein": "beef",
    "quarterly": [ {...one object per quarter...} ],
    "weekly":    [ {...one object per report...} ] }

  beef     quarterly: quarter, quarter_start, choice, select_, ct150_steer, ct150_all,
                      ks_avg, ne_avg, ks_est, ne_est, mbrf_gm, jbs_gm  ·  weekly: week_ending + prices
                      (ks_est / ne_est = CT150-delta estimate for confidential KS / NE weeks)
  chicken  quarterly: quarter, year_q, breast, leg_qtrs, wings, tenders, sbm, corn,
                      fc_spot, ppc_us_gm, ppc_cnl_gm  ·  weekly: report_date + parts, sbm, corn
  pork     quarterly: quarter, year_q, carcass, hog_price, feed_grain_spot,
                      spread_non_integrated  ·  weekly: report_date, carcass, hog_price,
                      spread_non_integrated
  asOf = the newest weekly date (data, not the clock).
"""

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

TRACKERS = {
    "beef": ("Beef/U.S. Tracker/beef.db",
             "SELECT quarter, quarter_start, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg, "
             "ks_est, ne_est, mbrf_gm, jbs_gm FROM beef_quarterly ORDER BY quarter_start",
             "SELECT week_ending, choice, select_, ct150_steer, ct150_all, ks_avg, ne_avg, ks_est, ne_est "
             "FROM beef_weekly ORDER BY week_ending", "week_ending"),
    "chicken": ("Chicken/U.S. Tracker/chicken.db",
                "SELECT quarter, year_q, breast, leg_qtrs, wings, tenders, sbm, corn, fc_spot, "
                "ppc_us_gm, ppc_cnl_gm FROM quarterly ORDER BY year_q",
                "SELECT report_date, breast, leg_qtrs, wings, tenders, sbm, corn "
                "FROM weekly ORDER BY report_date", "report_date"),
    "pork": ("Pork/U.S. Tracker/pork_us.db",
             "SELECT quarter, year_q, carcass, hog_price, feed_grain_spot, spread_non_integrated "
             "FROM quarterly ORDER BY year_q",
             "SELECT report_date, carcass, hog_price, spread_non_integrated "
             "FROM weekly ORDER BY report_date", "report_date"),
}


def clean(row):
    # Full precision on purpose: the hub rounds for display exactly like the
    # legacy pages, and pre-rounding here moves half-cent values (355.849… → 355.85).
    return dict(row)


def export(protein):
    db_rel, q_sql, w_sql, date_col = TRACKERS[protein]
    db_path = ROOT / db_rel
    out_path = db_path.parent / f"us-{protein}-tracker.json"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    quarterly = [clean(dict(r)) for r in conn.execute(q_sql)]
    weekly = [clean(dict(r)) for r in conn.execute(w_sql)]
    conn.close()
    if not quarterly:
        raise SystemExit(f"[EXPORT] {protein}: quarterly is empty — refusing to publish")
    as_of = weekly[-1][date_col] if weekly else quarterly[-1]["quarter"]
    doc = {"schemaVersion": 1, "asOf": as_of, "protein": protein, "quarterly": quarterly, "weekly": weekly}
    out_path.write_text(json.dumps(doc, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"[EXPORT] {protein}: {len(quarterly)} quarters, {len(weekly)} weekly rows"
          f" → {out_path.name} = {out_path.stat().st_size // 1024} KB (asOf {as_of})")


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in TRACKERS:
        raise SystemExit(f"usage: {Path(__file__).name} {'|'.join(TRACKERS)}")
    export(sys.argv[1])
