"""SAP OData connector, tested against a mocked S/4HANA response."""
from datetime import date

import httpx
import pytest

from po_agents.common import repository


def odata_po(items):
    return {"d": {"PurchaseOrder": "4500000001", "Supplier": "17300001",
                  "to_PurchaseOrderItem": {"results": items}}}


def item(qty, unit, *dates, deleted=""):
    lines = [{"ScheduleLineDeliveryDate": d} for d in dates]
    return {"OrderQuantity": qty, "PurchaseOrderQuantityUnit": unit,
            "PurchasingDocumentDeletionCode": deleted,
            "to_ScheduleLine": {"results": lines}}


# 28 Nov 2026 and 5 Dec 2026 as OData V2 dates
NOV28 = "/Date(1795824000000)/"
DEC05 = "/Date(1796428800000)/"


@pytest.fixture
def sap(monkeypatch):
    """Points the repository at a fake SAP and records the requests."""
    monkeypatch.setenv("SAP_MODE", "odata")
    monkeypatch.setenv("SAP_ODATA_BASE_URL", "http://sap.test/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV")
    monkeypatch.setenv("SAP_CLIENT", "100")
    monkeypatch.setenv("SAP_USERNAME", "user")
    monkeypatch.setenv("SAP_PASSWORD", "pass")
    state = {"requests": [], "response": httpx.Response(404)}

    def handler(request):
        state["requests"].append(request)
        return state["response"]

    monkeypatch.setattr(repository, "_transport", httpx.MockTransport(handler))
    return state


async def test_reads_po_from_sap(sap):
    sap["response"] = httpx.Response(200, json=odata_po([item("1000.000", "PC", NOV28)]))
    po = await repository.get_purchase_order("4500000001")
    assert po.model_dump() == {
        "po_number": "4500000001", "supplier": "17300001",
        "ordered_quantity": 1000, "unit": "PC",
        "expected_delivery_date": date(2026, 11, 28),
    }
    [request] = sap["requests"]
    assert request.url.path.endswith("/A_PurchaseOrder('4500000001')")
    assert request.url.params["sap-client"] == "100"
    assert request.url.params["$expand"] == "to_PurchaseOrderItem/to_ScheduleLine"
    assert request.headers["authorization"].startswith("Basic ")


async def test_unknown_po_is_none(sap):
    assert await repository.get_purchase_order("4500009999") is None


async def test_non_numeric_po_never_reaches_sap(sap):
    assert await repository.get_purchase_order("1')/x") is None
    assert sap["requests"] == []


async def test_sap_error_is_raised(sap):
    sap["response"] = httpx.Response(401)
    with pytest.raises(httpx.HTTPStatusError):
        await repository.get_purchase_order("4500000001")


def test_multi_item_sums_quantity_and_takes_latest_date():
    po = repository.parse_odata_purchase_order(odata_po([
        item("600", "PC", NOV28),
        item("400.5", "PC", DEC05),
        item("999", "PC", DEC05, deleted="L"),
    ])["d"])
    assert po.ordered_quantity == 1000.5
    assert po.expected_delivery_date == date(2026, 12, 5)


def test_mixed_units_use_first_item():
    po = repository.parse_odata_purchase_order(odata_po([
        item("10", "KG", NOV28), item("5", "PC", DEC05),
    ])["d"])
    assert (po.ordered_quantity, po.unit) == (10, "KG")
    assert po.expected_delivery_date == date(2026, 11, 28)
