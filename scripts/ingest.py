#!/usr/bin/env python3
"""
EV Charging Platform - ingestion script.

Downloads the static + dynamic OCPI-format datasets published by the Greek
Ministry of Infrastructure & Transport (electrokinisi.yme.gov.gr), merges
them into one compact JSON document optimized for the frontend map, and
pushes the result into a Cloudflare Workers KV namespace via the Cloudflare
REST API.

Run on a schedule by .github/workflows/ingest.yml (GitHub Actions).

Required environment variables:
  CF_ACCOUNT_ID     - Cloudflare account ID
  CF_API_TOKEN      - Cloudflare API token with "Workers KV Storage: Edit" permission
  CF_KV_NAMESPACE_ID - ID of the KV namespace to write into
"""
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone

import requests

STATIC_URL = "https://electrokinisi.yme.gov.gr/public/static_files/GR.IDRO.static.data.latest.json.zip"
DYNAMIC_URL = "https://electrokinisi.yme.gov.gr/public/static_files/GR.IDRO.dynamic.data.latest.json.zip"
KV_KEY = "chargers"
KV_HISTORY_KEY = "usage_history"
KV_EVSE_USAGE_KEY = "evse_usage"
KV_TESLA_KEY = "tesla_chargers"  # written separately (once/day) by scripts/ingest_tesla.py
SAMPLE_INTERVAL_MINUTES = 10  # must match the cron schedule in .github/workflows/ingest.yml

HTTP_TIMEOUT = 60


def fetch_zip_json(url: str) -> dict:
    resp = requests.get(url, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        # There should be exactly one .json file inside
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError(f"No JSON file found inside zip from {url}")
        with zf.open(names[0]) as f:
            raw = f.read().decode("utf-8-sig")
            return json.loads(raw)


def build_dynamic_index(dynamic_data: dict) -> dict:
    """Map evse uid -> {status, last_updated}."""
    index = {}
    for loc in dynamic_data.get("Locations", []):
        for evse in loc.get("evses", []):
            uid = evse.get("uid")
            if uid:
                index[uid] = {
                    "status": evse.get("status", "UNKNOWN"),
                    "last_updated": evse.get("last_updated"),
                }
    return index


def to_float(value):
    try:
        return round(float(value), 6)
    except (TypeError, ValueError):
        return None


def build_compact_dataset(static_data: dict, dynamic_index: dict, usage_state: dict | None = None) -> dict:
    from collections import Counter

    locations_out = []
    operator_ids = set()
    operator_name_votes = {}  # party_id -> Counter of operator.name seen
    standard_counts = {}
    total_evses = 0
    total_connectors = 0
    available_evses = 0
    charging_evses = 0

    for loc in static_data.get("Locations", []):
        if loc.get("publish") is False:
            continue

        lat = to_float(loc.get("coordinates", {}).get("latitude"))
        lng = to_float(loc.get("coordinates", {}).get("longitude"))
        if lat is None or lng is None:
            continue

        party_id = loc.get("party_id")
        operator_ids.add(party_id)

        op_name = (loc.get("operator") or {}).get("name")
        if party_id and op_name:
            operator_name_votes.setdefault(party_id, Counter())[op_name] += 1

        evses_out = []
        loc_max_kw = 0
        for evse in loc.get("evses", []):
            uid = evse.get("uid")
            dyn = dynamic_index.get(uid, {})
            status = dyn.get("status") or evse.get("status", "UNKNOWN")

            connectors_out = []
            for c in evse.get("connectors", []):
                standard = c.get("standard")
                power_w = c.get("max_electric_power") or 0
                kw = round(power_w / 1000, 1) if power_w else None
                if kw and kw > loc_max_kw:
                    loc_max_kw = kw
                if standard:
                    standard_counts[standard] = standard_counts.get(standard, 0) + 1
                connectors_out.append({
                    "standard": standard,
                    "power_type": c.get("power_type"),
                    "kw": kw,
                })
                total_connectors += 1

            evse_record = {
                "uid": uid,
                "status": status,
                "connectors": connectors_out,
            }
            if usage_state:
                usage = usage_estimate_for(usage_state, uid)
                if usage:
                    evse_record["usage"] = usage
            evses_out.append(evse_record)
            total_evses += 1
            if status == "AVAILABLE":
                available_evses += 1
            elif status == "CHARGING":
                charging_evses += 1

        locations_out.append({
            "id": loc.get("id"),
            "name": loc.get("name"),
            "operator": party_id,
            "address": loc.get("address"),
            "city": loc.get("city"),
            "lat": lat,
            "lng": lng,
            "parking_type": loc.get("parking_type"),
            "max_kw": loc_max_kw or None,
            "evses": evses_out,
        })

    operator_names = {
        pid: votes.most_common(1)[0][0]
        for pid, votes in operator_name_votes.items()
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "locations": len(locations_out),
            "evses": total_evses,
            "connectors": total_connectors,
            "available_evses": available_evses,
            "charging_evses": charging_evses,
        },
        "operators": sorted(o for o in operator_ids if o),
        "operator_names": operator_names,
        "connector_standards": sorted(standard_counts.keys()),
        "locations": locations_out,
    }


def _kv_base_url(key: str) -> str:
    account_id = os.environ["CF_ACCOUNT_ID"]
    namespace_id = os.environ["CF_KV_NAMESPACE_ID"]
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"


def kv_get_json(key: str):
    api_token = os.environ["CF_API_TOKEN"]
    resp = requests.get(_kv_base_url(key), headers={"Authorization": f"Bearer {api_token}"}, timeout=HTTP_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def kv_put_json(key: str, payload) -> None:
    api_token = os.environ["CF_API_TOKEN"]
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    print(f"  {key} payload size: {len(body) / 1024:.1f} KB")

    resp = requests.put(
        _kv_base_url(key),
        headers={"Authorization": f"Bearer {api_token}"},
        files={"value": (key, body, "application/json")},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("success"):
        raise RuntimeError(f"Cloudflare KV write failed for {key}: {result}")


def push_to_kv(payload: dict) -> None:
    kv_put_json(KV_KEY, payload)


def append_usage_history(dataset: dict) -> None:
    """Append one lightweight snapshot to the rolling usage-history KV entry.

    This is the seed for a future *real* "typical usage" feature: we do not
    fabricate any historical trend, we just start honestly recording what
    actually happens, one snapshot at a time. Kept indefinitely (no trimming)
    so history is never lost.
    """
    history = kv_get_json(KV_HISTORY_KEY)
    if not isinstance(history, list):
        history = []

    counts = dataset["counts"]
    history.append({
        "t": dataset["generated_at"],
        "total": counts["evses"],
        "charging": counts["charging_evses"],
        "available": counts["available_evses"],
    })

    kv_put_json(KV_HISTORY_KEY, history)


def update_evse_usage(dynamic_index: dict, now_iso: str) -> dict:
    """Maintain a per-EVSE running tally of observed samples, so we can estimate
    real usage hours per charger. This is a genuine measurement built from actual
    snapshots (one every SAMPLE_INTERVAL_MINUTES) — not a guess — but it IS a
    sampled estimate: a charging session shorter than the sampling interval can be
    missed or under/over-counted, and it only covers time since "since" below.

    Stored state shape:
      {"since": "<iso timestamp of first run>",
       "evses": {"<uid>": {"s": <samples seen>, "c": <samples seen CHARGING>}}}
    """
    state = kv_get_json(KV_EVSE_USAGE_KEY)
    if not isinstance(state, dict) or "evses" not in state:
        state = {"since": now_iso, "evses": {}}

    evses = state["evses"]
    for uid, info in dynamic_index.items():
        entry = evses.setdefault(uid, {"s": 0, "c": 0})
        entry["s"] += 1
        if info.get("status") == "CHARGING":
            entry["c"] += 1

    kv_put_json(KV_EVSE_USAGE_KEY, state)
    return state


def usage_estimate_for(evse_usage_state: dict, uid: str) -> dict | None:
    entry = evse_usage_state.get("evses", {}).get(uid)
    if not entry or entry["s"] == 0:
        return None

    hours_since_tracking = round(entry["c"] * SAMPLE_INTERVAL_MINUTES / 60, 1)
    fraction_charging = round(entry["c"] / entry["s"], 3)
    return {
        "since": evse_usage_state.get("since"),
        "samples": entry["s"],
        "hours_charging": hours_since_tracking,
        "fraction_charging": fraction_charging,
    }


def main():
    print("Fetching static dataset...")
    static_data = fetch_zip_json(STATIC_URL)
    print(f"  {len(static_data.get('Locations', []))} locations")

    print("Fetching dynamic dataset...")
    dynamic_data = fetch_zip_json(DYNAMIC_URL)
    dynamic_index = build_dynamic_index(dynamic_data)
    print(f"  {len(dynamic_index)} live EVSE statuses")

    now_iso = datetime.now(timezone.utc).isoformat()

    print("Updating per-EVSE usage counters...")
    usage_state = update_evse_usage(dynamic_index, now_iso)
    print(f"  tracking {len(usage_state['evses'])} EVSEs since {usage_state['since']}")

    print("Merging...")
    dataset = build_compact_dataset(static_data, dynamic_index, usage_state)
    print(f"  {dataset['counts']}")

    # Tesla locations are refreshed separately (once/day, see ingest_tesla.py) since they
    # rarely change and come from a different source (Open Charge Map). We just splice in
    # whatever is already in KV here - this is a cheap read, no extra Open Charge Map calls.
    tesla_locations = kv_get_json(KV_TESLA_KEY)
    if isinstance(tesla_locations, list):
        dataset["tesla_locations"] = tesla_locations
        print(f"  {len(tesla_locations)} Tesla locations included")

    print("Pushing to Cloudflare KV...")
    push_to_kv(dataset)

    print("Appending usage history snapshot...")
    append_usage_history(dataset)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
