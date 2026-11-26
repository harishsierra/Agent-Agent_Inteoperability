"""Purchase order lookup: SAP S/4HANA OData (SAP_MODE=odata) or a local JSON file (SAP_MODE=json)."""
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from po_agents.common.models import PurchaseOrder

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# SAP PO numbers are up to 10 digits. Checked before the number goes into a URL.
PO_NUMBER = re.compile(r"^\d{1,10}$")
# OData V2 JSON dates look like /Date(1764288000000)/ or /Date(1764288000000+0000)/
ODATA_DATE = re.compile(r"/Date\((-?\d+)(?:[+-]\d+)?\)/")

# Tests replace this with httpx.MockTransport; None means the real network.
_transport: httpx.AsyncBaseTransport | None = None


async def get_purchase_order(po_number: str) -> PurchaseOrder | None:
    mode = os.environ.get("SAP_MODE", "json")
    if mode == "json":
        return _from_json_file(po_number)
    if mode == "odata":
        return await _from_odata(po_number)
    raise ValueError(f"SAP_MODE must be 'json' or 'odata', not {mode!r}")


def _from_json_file(po_number: str) -> PurchaseOrder | None:
    path = DATA_DIR / "sap_purchase_orders.json"
    record = json.loads(path.read_text(encoding="utf-8")).get(po_number)
    return PurchaseOrder.model_validate(record) if record else None


PO_SERVICE_PATH = "/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"


@dataclass
class SapTarget:
    base_url: str
    auth: tuple[str, str] | None
    sap_client: str | None
    location_id: str | None
    on_premise: bool


async def _sap_target() -> SapTarget:
    """SAP connection details: from a BTP destination when SAP_DESTINATION is set
    (on Cloud Foundry), otherwise from SAP_ODATA_BASE_URL / SAP_USERNAME / ... (local)."""
    if name := os.environ.get("SAP_DESTINATION"):
        return await _target_from_destination(name)
    return SapTarget(
        base_url=_required("SAP_ODATA_BASE_URL").rstrip("/"),
        auth=(_required("SAP_USERNAME"), _required("SAP_PASSWORD")),
        sap_client=os.environ.get("SAP_CLIENT") or None,
        location_id=os.environ.get("SAP_CC_LOCATION_ID") or None,
        on_premise=True,
    )


async def _from_odata(po_number: str) -> PurchaseOrder | None:
    if not PO_NUMBER.match(po_number):
        return None
    target = await _sap_target()
    params = {
        "$expand": "to_PurchaseOrderItem/to_ScheduleLine",
        "$format": "json",
    }
    if target.sap_client:
        params["sap-client"] = target.sap_client
    verify = os.environ.get("SAP_VERIFY_TLS", "true").lower() != "false"
    proxy = await _connectivity_proxy(target.location_id) if target.on_premise else None

    async with httpx.AsyncClient(
        auth=target.auth, verify=verify, timeout=30, transport=_transport, proxy=proxy
    ) as http:
        response = await http.get(
            f"{target.base_url}/A_PurchaseOrder('{po_number}')", params=params
        )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return parse_odata_purchase_order(response.json()["d"])


def parse_odata_purchase_order(header: dict) -> PurchaseOrder | None:
    """Maps an A_PurchaseOrder entity (with items and schedule lines) to PurchaseOrder.

    A PO can have several items. Quantities are added up when every item uses the
    same unit (otherwise the first item is used), and the latest schedule-line date
    is taken as the expected delivery date.
    """
    items = [
        item
        for item in header.get("to_PurchaseOrderItem", {}).get("results", [])
        # "L" marks a deleted item, "S" a blocked one.
        if not item.get("PurchasingDocumentDeletionCode")
    ]
    if not items:
        return None

    units = {item["PurchaseOrderQuantityUnit"] for item in items}
    counted = items if len(units) == 1 else items[:1]
    quantity = sum(Decimal(item["OrderQuantity"]) for item in counted)

    dates = [
        _odata_date(line["ScheduleLineDeliveryDate"])
        for item in counted
        for line in item.get("to_ScheduleLine", {}).get("results", [])
        if line.get("ScheduleLineDeliveryDate")
    ]
    if not dates:
        return None

    return PurchaseOrder(
        po_number=header["PurchaseOrder"],
        supplier=header["Supplier"],
        ordered_quantity=int(quantity) if quantity == quantity.to_integral() else float(quantity),
        unit=counted[0]["PurchaseOrderQuantityUnit"],
        expected_delivery_date=max(dates),
    )


def _odata_date(value: str) -> date:
    match = ODATA_DATE.fullmatch(value)
    if not match:
        return date.fromisoformat(value[:10])
    millis = int(match.group(1))
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).date()


# ---- SAP BTP services (Cloud Foundry). Locally, without VCAP_SERVICES, none are used.

# OAuth tokens per BTP service ("connectivity", "destination").
_tokens: dict[str, tuple[str, float]] = {}
# Destination lookups, cached briefly so every PO request does not re-read them.
_destinations: dict[str, tuple[SapTarget, float]] = {}
DESTINATION_CACHE_SECONDS = 300


def _service_credentials(service: str) -> dict | None:
    bound = json.loads(os.environ.get("VCAP_SERVICES", "{}")).get(service)
    return bound[0]["credentials"] if bound else None


async def _service_token(service: str, creds: dict) -> str:
    token, expires = _tokens.get(service, ("", 0.0))
    if time.time() < expires:
        return token
    token_url = creds.get("token_service_url") or creds["url"]
    async with httpx.AsyncClient(timeout=30, transport=_transport) as http:
        response = await http.post(
            token_url.rstrip("/") + "/oauth/token",
            data={"grant_type": "client_credentials"},
            auth=(creds["clientid"], creds["clientsecret"]),
        )
    response.raise_for_status()
    body = response.json()
    # Renew a minute early.
    _tokens[service] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)) - 60)
    return body["access_token"]


async def _target_from_destination(name: str) -> SapTarget:
    """Reads URL, user, password, sap-client and Cloud Connector location from a
    BTP destination through the bound Destination service."""
    cached = _destinations.get(name)
    if cached and time.time() < cached[1]:
        return cached[0]

    creds = _service_credentials("destination")
    if not creds:
        raise ValueError("SAP_DESTINATION is set but no Destination service is bound.")
    token = await _service_token("destination", creds)
    async with httpx.AsyncClient(timeout=30, transport=_transport) as http:
        response = await http.get(
            f"{creds['uri'].rstrip('/')}/destination-configuration/v1/destinations/{name}",
            headers={"Authorization": f"Bearer {token}"},
        )
    if response.status_code == 404:
        raise ValueError(f"BTP destination {name!r} was not found.")
    response.raise_for_status()
    config = response.json()["destinationConfiguration"]

    auth_type = config.get("Authentication", "NoAuthentication")
    if auth_type == "BasicAuthentication":
        auth = (config["User"], config["Password"])
    elif auth_type == "NoAuthentication":
        auth = None
    else:
        raise ValueError(
            f"Destination {name!r} uses {auth_type}; only BasicAuthentication "
            "and NoAuthentication are supported."
        )

    url = config["URL"].rstrip("/")
    # The destination may point at the host or directly at the OData service.
    if "/sap/opu/odata" not in url:
        url += PO_SERVICE_PATH

    target = SapTarget(
        base_url=url,
        auth=auth,
        sap_client=config.get("sap-client") or os.environ.get("SAP_CLIENT") or None,
        location_id=config.get("CloudConnectorLocationId") or None,
        on_premise=config.get("ProxyType", "Internet") == "OnPremise",
    )
    _destinations[name] = (target, time.time() + DESTINATION_CACHE_SECONDS)
    return target


async def _connectivity_proxy(location_id: str | None = None) -> httpx.Proxy | None:
    """Routes SAP calls through the Cloud Connector when a Connectivity service
    is bound (Cloud Foundry). Locally, SAP is called directly."""
    creds = _service_credentials("connectivity")
    if not creds:
        return None
    token = await _service_token("connectivity", creds)
    headers = {"Proxy-Authorization": f"Bearer {token}"}
    if location_id:
        headers["SAP-Connectivity-SCC-Location_ID"] = location_id
    port = creds.get("onpremise_proxy_http_port") or creds["onpremise_proxy_port"]
    return httpx.Proxy(f"http://{creds['onpremise_proxy_host']}:{port}", headers=headers)


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is not set. See .env.example.")
    return value
