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


def build_compact_dataset(static_data: dict, dynamic_index: dict) -> dict:
    locations_out = []
    operator_ids = set()
    standard_counts = {}
    total_evses = 0
    total_connectors = 0

    for loc in static_data.get("Locations", []):
        if loc.get("publish") is False:
            continue

        lat = to_float(loc.get("coordinates", {}).get("latitude"))
        lng = to_float(loc.get("coordinates", {}).get("longitude"))
        if lat is None or lng is None:
            continue

        party_id = loc.get("party_id")
        operator_ids.add(party_id)

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

            evses_out.append({
                "uid": uid,
                "status": status,
                "connectors": connectors_out,
            })
            total_evses += 1

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

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "locations": len(locations_out),
            "evses": total_evses,
            "connectors": total_connectors,
        },
        "operators": sorted(o for o in operator_ids if o),
        "connector_standards": sorted(standard_counts.keys()),
        "locations": locations_out,
    }


def push_to_kv(payload: dict) -> None:
    account_id = os.environ["CF_ACCOUNT_ID"]
    api_token = os.environ["CF_API_TOKEN"]
    namespace_id = os.environ["CF_KV_NAMESPACE_ID"]

    url = (
        f"https://api.cloudflare.com/client/v4/accounts/{account_id}"
        f"/storage/kv/namespaces/{namespace_id}/values/{KV_KEY}"
    )
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    print(f"Payload size: {len(body) / 1024:.1f} KB")

    resp = requests.put(
        url,
        headers={"Authorization": f"Bearer {api_token}"},
        files={"value": (KV_KEY, body, "application/json")},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    result = resp.json()
    if not result.get("success"):
        raise RuntimeError(f"Cloudflare KV write failed: {result}")


def main():
    print("Fetching static dataset...")
    static_data = fetch_zip_json(STATIC_URL)
    print(f"  {len(static_data.get('Locations', []))} locations")

    print("Fetching dynamic dataset...")
    dynamic_data = fetch_zip_json(DYNAMIC_URL)
    dynamic_index = build_dynamic_index(dynamic_data)
    print(f"  {len(dynamic_index)} live EVSE statuses")

    print("Merging...")
    dataset = build_compact_dataset(static_data, dynamic_index)
    print(f"  {dataset['counts']}")

    print("Pushing to Cloudflare KV...")
    push_to_kv(dataset)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
