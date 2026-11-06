#!/usr/bin/env python3
"""
Live 911 dispatch poller for a public Power BI dashboard — a generic template.

Which dashboard and which towns come from the private config ("dispatch" section of
the BRIEF_CONFIG secret or a local config.json). The feed only covers a short
rolling window, so GitHub Actions runs this every 15 minutes; matching calls are
saved to an encrypted file that brief.py reads for the morning email.

Usage:
  python dispatch.py          # check once, save any new local calls
  python dispatch.py --show   # also print every call (local testing only)
"""
import base64
import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from common import IN_ACTIONS, load_config, load_state, log, save_state

_CFG = load_config().get("dispatch", {})
REPORT_R = _CFG.get("report_r", "")          # the r= value from the dashboard link
ENTITY = _CFG.get("entity", "")              # the dataset table name
LOCAL_MUNIS = _CFG.get("local_munis", [])
IGNORE_TYPES = _CFG.get("ignore_types", [])  # call types to skip, case-insensitive substring
API = "https://wabi-us-gov-virginia-api.analysis.usgovcloudapi.net/public/reports"

STATE_FILE = "dispatches.json"
KEEP_DAYS = 3
TZ = ZoneInfo("America/New_York")

CATEGORY = {"PD": "Police/Traffic", "ED": "EMS", "FD": "Fire"}

# Only block-level location fields — the same detail the county shows publicly.
# External_Lat/Lon are the block-level points the county plots on its own public map.
COLUMNS = ["Incident_Number", "Incident_Type", "Dispatch_Time", "ExternalLocWMun",
           "Municipality_Name", "Station", "External_Lat", "External_Lon"]


def resource_key() -> str:
    pad = "=" * (-len(REPORT_R) % 4)
    return json.loads(base64.b64decode(REPORT_R + pad))["k"]


def model_id(session, key) -> int:
    r = session.get(f"{API}/{key}/modelsAndExploration?preferReadOnlySession=true", timeout=30)
    r.raise_for_status()
    return r.json()["models"][0]["id"]


def query(session, key, mid) -> dict:
    select = [{"Column": {"Expression": {"SourceRef": {"Source": "u"}}, "Property": c}, "Name": f"u.{c}"}
              for c in COLUMNS]
    body = {
        "version": "1.0.0",
        "queries": [{"Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
            "Query": {"Version": 2, "From": [{"Name": "u", "Entity": ENTITY, "Type": 0}], "Select": select},
            "Binding": {"Primary": {"Groupings": [{"Projections": list(range(len(COLUMNS)))}]},
                        "DataReduction": {"DataVolume": 3, "Primary": {"Window": {"Count": 1000}}},
                        "Version": 1}}}]}}],
        "cancelQueries": [], "modelId": mid,
    }
    r = session.post(f"{API}/querydata?synchronous=true", json=body, timeout=60)
    r.raise_for_status()
    return r.json()


def decode(resp: dict) -> list:
    """Decode Power BI's compressed row format (DSR) into a list of dicts."""
    ds = resp["results"][0]["result"]["data"]["dsr"]["DS"][0]
    dicts = ds.get("ValueDicts", {})
    rows = ds["PH"][0].get("DM0", [])
    if not rows:
        return []
    schema = rows[0]["S"]
    n = len(schema)
    out, prev = [], [None] * n
    for row in rows:
        vals = iter(row.get("C", []))
        repeat, null = row.get("R", 0), row.get("Ø", 0)
        cur = []
        for i, col in enumerate(schema):
            if repeat >> i & 1:
                v = prev[i]
            elif null >> i & 1:
                v = None
            else:
                v = next(vals)
                if "DN" in col and isinstance(v, int):
                    v = dicts[col["DN"]][v]
            cur.append(v)
        prev = cur
        out.append(dict(zip(COLUMNS, cur)))
    return out


def map_link(lat, lon) -> str:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={lat:.5f},{lon:.5f}"


def clean(row: dict) -> dict:
    num = (row.get("Incident_Number") or "").strip()
    return dict(
        map=map_link(row.get("External_Lat"), row.get("External_Lon")),
        number=num,
        category=CATEGORY.get(num[:2].upper(), "Other"),
        type=" ".join((row.get("Incident_Type") or "").split()),
        time=(row.get("Dispatch_Time") or "").strip(),
        location=" ".join((row.get("ExternalLocWMun") or "").split()),
        muni=(row.get("Municipality_Name") or "").strip().upper(),
        station=(row.get("Station") or "").strip(),
    )


def run():
    if not (REPORT_R and ENTITY and LOCAL_MUNIS):
        sys.exit("dispatch section of the config is incomplete.")
    s = requests.Session()
    key = resource_key()
    s.headers.update({"X-PowerBI-ResourceKey": key, "User-Agent": "LocalNewsBrief/1.0"})
    calls = [clean(r) for r in decode(query(s, key, model_id(s, key)))]
    calls = [c for c in calls if c["number"]]

    if "--show" in sys.argv and not IN_ACTIONS:
        for c in sorted(calls, key=lambda c: c["time"]):
            print(f"{c['time']}  {c['category']:<14} {c['type']:<38} {c['location']}")
    log(f"{len(calls)} calls on the feed.")

    state = load_state(STATE_FILE, {"incidents": {}})
    incidents = state.setdefault("incidents", {})

    local = {m.upper() for m in LOCAL_MUNIS}
    new = 0
    for c in calls:
        if c["muni"] not in local or c["number"] in incidents:
            continue
        if any(t.lower() in c["type"].lower() for t in IGNORE_TYPES):
            continue
        incidents[c["number"]] = c
        new += 1
        log("New local call.", f"{c['time']} {c['type']} — {c['location']}")

    cutoff = (datetime.now(TZ) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    pruned = {k: v for k, v in incidents.items() if v.get("time", "")[:10] >= cutoff}
    if new or len(pruned) != len(incidents):
        state["incidents"] = pruned
        save_state(STATE_FILE, state)
    log(f"{new} new local calls saved." if new else "No new local calls.")


def main():
    try:
        run()
    except SystemExit:
        raise
    except Exception as e:
        # Keep public logs free of URLs/keys that a full traceback would show.
        if IN_ACTIONS:
            sys.exit(f"Feed check failed ({e.__class__.__name__}).")
        raise


if __name__ == "__main__":
    main()
