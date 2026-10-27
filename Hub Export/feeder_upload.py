#!/usr/bin/env python3
"""
feeder_upload.py — send hub JSONs to the Dashboard Data Feeder
==============================================================
    python "Hub Export/feeder_upload.py" <file.json> [<file.json> ...]
    python "Hub Export/feeder_upload.py" --all

Each name is the logical filename the hub reads (metadata.ts dataFiles); FILES
maps it to where the export scripts write it. Every file is checked before
anything is sent: it must parse, carry schemaVersion 1 and an asOf, stay under
MAX_BYTES and contain nothing that looks like a credential (Mindassets files
are public).

Nothing is sent unless DASHBOARD_FEEDER_ENABLED is "true" and both
DASHBOARD_FEEDER_PROJECT_ID and DASHBOARD_FEEDER_API_KEY are set; otherwise the
run is a dry run that only prints the checks. Protocol (docs/dashboard-data-
feeder.md in the hub): POST initialize → signed PUT → POST complete, reporting
"failed" when the PUT does not go through.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_URL = "https://dashboard-data-feeder.vercel.app"
MAX_BYTES = 8 * 1024 * 1024
SUSPICIOUS = re.compile(r"rdf_[A-Za-z0-9]|service_role|eyJhbGciOi|supabase\.co|ghp_[A-Za-z0-9]|BEGIN [A-Z ]*PRIVATE KEY")

FILES = {
    "beer-prices.json": "Supermarket Prices/beer-prices.json",
    "bz-beef-export-spread.json": "Beef/BZ Tracker/bz-beef-export-spread.json",
    "bz-chicken-domestic-spread.json": "Chicken/BZ Tracker/bz-chicken-domestic-spread.json",
    "bz-chicken-export-spread.json": "Chicken/BZ Tracker/bz-chicken-export-spread.json",
    "bz-pork-export-spread.json": "Pork/BZ Tracker/bz-pork-export-spread.json",
    "cattle-cycle.json": "Beef/Cattle Cycle/cattle-cycle.json",
    "climate-capitals.json": "Climate Monitor/Hub/climate-capitals.json",
    "climate-capitals-era5.json": "Climate Monitor/Hub/climate-capitals-era5.json",
    "climate-farms.json": "Climate Monitor/Hub/climate-farms.json",
    "climate-farms-era5.json": "Climate Monitor/Hub/climate-farms-era5.json",
    "climate-regions-go.json": "Climate Monitor/Hub/climate-regions-go.json",
    "climate-regions-matopiba.json": "Climate Monitor/Hub/climate-regions-matopiba.json",
    "climate-regions-ms.json": "Climate Monitor/Hub/climate-regions-ms.json",
    "climate-regions-mt.json": "Climate Monitor/Hub/climate-regions-mt.json",
    "climate-regions-pr.json": "Climate Monitor/Hub/climate-regions-pr.json",
    "climate-regions-rs.json": "Climate Monitor/Hub/climate-regions-rs.json",
    "climate-sugar.json": "Climate Monitor/Hub/climate-sugar.json",
    "climate-sugar-era5.json": "Climate Monitor/Hub/climate-sugar-era5.json",
    "climate-sugar-regions.json": "Climate Monitor/Hub/climate-sugar-regions.json",
    "comps-table.json": "Market Watch/Comps Table/comps-table.json",
    "conab.json": "Agri Monitor/conab/conab.json",
    "cost-model.json": "Cost Model/cost-model.json",
    "crushing-spread.json": "Agri Monitor/crushing_spread/crushing-spread.json",
    "cvm.json": "CVM Buybacks/cvm.json",
    "imea.json": "Agri Monitor/imea/imea.json",
    "se-fuel-balance.json": "Sugar and Ethanol/se-fuel-balance.json",
    "se-parity.json": "Sugar and Ethanol/se-parity.json",
    "se-prices.json": "Sugar and Ethanol/se-prices.json",
    "secex.json": "Agri Monitor/secex/secex.json",
    "short-interest.json": "Market Watch/Short Interest/short-interest.json",
    "us-beef-tracker.json": "Beef/U.S. Tracker/us-beef-tracker.json",
    "us-chicken-tracker.json": "Chicken/U.S. Tracker/us-chicken-tracker.json",
    "us-pork-tracker.json": "Pork/U.S. Tracker/us-pork-tracker.json",
    "wasde-corn.json": "Wasde/wasde-corn.json",
    "wasde-cotton.json": "Wasde/wasde-cotton.json",
    "wasde-soybean.json": "Wasde/wasde-soybean.json",
}


def check(name):
    """The file's bytes and asOf, or SystemExit when it must not be published."""
    path = ROOT / FILES[name]
    if not path.is_file():
        raise SystemExit(f"[FEEDER] {name}: {FILES[name]} not found")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise SystemExit(f"[FEEDER] {name}: {len(raw) // 1024} KB is over the {MAX_BYTES // 1024 // 1024} MB limit")
    text = raw.decode("utf-8")
    doc = json.loads(text)
    if doc.get("schemaVersion") != 1 or not doc.get("asOf"):
        raise SystemExit(f"[FEEDER] {name}: missing schemaVersion 1 or asOf")
    hit = SUSPICIOUS.search(text)
    if hit:
        raise SystemExit(f"[FEEDER] {name}: looks like it contains a credential ({hit.group(0)[:12]}…) — not publishing")
    return path, raw, doc["asOf"]


def api_post(base, key, path, payload=None):
    request = Request(f"{base}{path}", data=json.dumps(payload or {}).encode(), method="POST",
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def upload(base, project, key, name, path, raw):
    uploads = f"/api/projects/{project}/files/uploads"
    init = api_post(base, key, uploads, {
        "name": name, "sourceFileName": path.name, "contentType": "application/json", "sizeBytes": len(raw),
        "sourceModifiedAt": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
    })
    update_id, target = init["update"]["id"], init["upload"]
    try:
        with urlopen(Request(target["url"], data=raw, method="PUT", headers=target["requiredHeaders"]), timeout=300):
            pass
    except Exception as error:
        reason = {"reason": "storage_rejected", "storageStatus": error.code} if isinstance(error, HTTPError) else {"reason": "network_error"}
        try:
            api_post(base, key, f"{uploads}/{update_id}/failed", reason)
        except Exception:
            pass
        raise
    return api_post(base, key, f"{uploads}/{update_id}/complete")["update"]["revision"]


def main(args):
    names = sorted(FILES) if args == ["--all"] else args
    unknown = [n for n in names if n not in FILES]
    if not names or unknown:
        raise SystemExit(f"usage: {Path(__file__).name} --all | <file.json> …  (unknown: {', '.join(unknown) or 'none'})")
    checked = [(n, *check(n)) for n in names]

    base = (os.getenv("DASHBOARD_FEEDER_URL") or DEFAULT_URL).rstrip("/")
    project, key = os.getenv("DASHBOARD_FEEDER_PROJECT_ID"), os.getenv("DASHBOARD_FEEDER_API_KEY")
    live = os.getenv("DASHBOARD_FEEDER_ENABLED", "").lower() == "true" and project and key
    for name, path, raw, as_of in checked:
        if not live:
            print(f"[FEEDER] dry run — {name}: {len(raw) // 1024} KB, asOf {as_of}, checks passed")
            continue
        revision = upload(base, project, key, name, path, raw)
        print(f"[FEEDER] {name}: {len(raw) // 1024} KB, asOf {as_of} → revision {revision}")
    if not live:
        print("[FEEDER] nothing sent (set DASHBOARD_FEEDER_ENABLED=true plus the project ID and API key to upload)")


if __name__ == "__main__":
    main(sys.argv[1:])
