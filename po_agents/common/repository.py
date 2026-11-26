"""Purchase order lookup: SAP S/4HANA OData (SAP_MODE=odata) or a local JSON file (SAP_MODE=json)."""
import json
import os
import re
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


async def _from_odata(po_number: str) -> PurchaseOrder | None:
    if not PO_NUMBER.match(po_number):
        return None
    base_url = _required("SAP_ODATA_BASE_URL").rstrip("/")
    auth = (_required("SAP_USERNAME"), _required("SAP_PASSWORD"))
    params = {
        "$expand": "to_PurchaseOrderItem/to_ScheduleLine",
        "$format": "json",
    }
    if client := os.environ.get("SAP_CLIENT"):
        params["sap-client"] = client
    verify = os.environ.get("SAP_VERIFY_TLS", "true").lower() != "false"

    async with httpx.AsyncClient(
        auth=auth, verify=verify, timeout=30, transport=_transport
    ) as http:
        response = await http.get(
            f"{base_url}/A_PurchaseOrder('{po_number}')", params=params
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


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is not set. See .env.example.")
    return value
