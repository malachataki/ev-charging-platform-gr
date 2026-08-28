#!/usr/bin/env python3
"""
Tesla Supercharger ingestion (Greece).

Tesla's charging network is NOT part of the official Greek government registry
(electrokinisi.yme.gov.gr) that scripts/ingest.py reads from — verified by
searching the raw government dataset for any Tesla-branded operator or
Tesla-specific connector type: there are none. So Tesla locations have to come
from a different source: Open Charge Map (https://openchargemap.org), a free,
community-maintained, public API.

This is intentionally a SEPARATE, much slower-cadence job (once a day) from the
main scripts/ingest.py (every 10 minutes): Tesla station *locations* barely
change day to day, so there is no reason to hit Open Charge Map's API more
often, and doing so respects their fair-use limits.

Honesty note: Open Charge Map does not provide a reliable live-occupancy feed
for Tesla Superchargers, so we only ever show static location + connector
info here — never a fabricated "available now" count. We also only *infer*
whether a station is likely open to non-Tesla vehicles, from the connector
types Open Charge Map lists (CCS/Type 2 = physically compatible with most
non-Tesla EVs). This is a best-effort signal, not a guarantee — actual access
is controlled by Tesla's own app and can differ. The frontend must present it
as "probably", never as a confirmed fact.

Required environment variables:
  OCM_API_KEY         - free API key from https://openchargemap.org (Profile -> Apps/API Keys)
  CF_ACCOUNT_ID       - Cloudflare account ID
  CF_API_TOKEN        - Cloudflare API token with "Workers KV Storage: Edit" permission
  CF_KV_NAMESPACE_ID  - ID of the KV namespace to write into
"""
import json
import os
import sys

import requests

OCM_URL = "https://api.openchargemap.io/v3/poi/"
KV_TESLA_KEY = "tesla_chargers"
HTTP_TIMEOUT = 60


def fetch_ocm_greece() -> list:
    api_key = os.environ["OCM_API_KEY"]
    params = {
        "output": "json",
        "countrycode": "GR",
        "maxresults": 500,
        # compact=true strips out OperatorInfo/ConnectionType (leaves bare numeric IDs
        # instead) - we need the human-readable titles to identify Tesla and connector
        # types, so we deliberately do NOT set compact=true here.
        "verbose": "false",
    }
    resp = requests.get(OCM_URL, params=params, headers={"X-API-Key": api_key}, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Open Charge Map response shape: {type(data)}")
    return data


def is_tesla(poi: dict) -> bool:
    operator = ((poi.get("OperatorInfo") or {}).get("Title") or "")
    return "tesla" in operator.lower()


def classify_compat(connections: list) -> str:
    """Best-effort inference only - see module docstring."""
    has_ccs_or_type2 = False
    has_tesla_proprietary = False
    for c in connections:
        ctype = ((c.get("ConnectionType") or {}).get("Title") or "").lower()
        if "ccs" in ctype or "type 2" in ctype or "mennekes" in ctype:
            has_ccs_or_type2 = True
        elif "tesla" in ctype:
            has_tesla_proprietary = True
    if has_ccs_or_type2:
        return "likely_open"
    if has_tesla_proprietary:
        return "tesla_only"
    return "unknown"


def build_tesla_dataset(pois: list) -> list:
    out = []
    for poi in pois:
        if not is_tesla(poi):
            continue
        addr = poi.get("AddressInfo") or {}
        lat = addr.get("Latitude")
        lng = addr.get("Longitude")
        if lat is None or lng is None:
            continue

        connections = poi.get("Connections") or []
        conn_out = [
            {
                "type": ((c.get("ConnectionType") or {}).get("Title") or "").strip() or None,
                "kw": c.get("PowerKW"),
                "qty": c.get("Quantity") or 1,
            }
            for c in connections
        ]

        out.append({
            "id": poi.get("ID"),
            "name": addr.get("Title"),
            "address": addr.get("AddressLine1"),
            "city": addr.get("Town"),
            "lat": lat,
            "lng": lng,
            "connections": conn_out,
            "compat": classify_compat(connections),
        })
    return out


def _kv_base_url(key: str) -> str:
    account_id = os.environ["CF_ACCOUNT_ID"]
    namespace_id = os.environ["CF_KV_NAMESPACE_ID"]
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces/{namespace_id}/values/{key}"


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


def main():
    print("Fetching Tesla charging locations from Open Charge Map...")
    pois = fetch_ocm_greece()
    print(f"  {len(pois)} total POIs returned for GR")

    tesla_locations = build_tesla_dataset(pois)
    print(f"  {len(tesla_locations)} Tesla locations found")

    print("Pushing to Cloudflare KV...")
    kv_put_json(KV_TESLA_KEY, tesla_locations)
    print("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
