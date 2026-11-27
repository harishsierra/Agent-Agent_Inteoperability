"""SAP OData connector, tested against a mocked S/4HANA response."""
from datetime import date
from decimal import Decimal

import httpx
import pytest

from po_agents.common import repository


def odata_po(items):
    return {"d": {"PurchaseOrder": "4500000001", "Supplier": "17300001",
                  "to_PurchaseOrderItem": {"results": items}}}


def item(qty, unit, *dates, deleted="", no="10", price="10.00", delivered=False):
    lines = [{"ScheduleLineDeliveryDate": d} for d in dates]
    return {"PurchaseOrderItem": no, "PurchaseOrderItemText": f"Item {no}",
            "Material": f"MAT-{no}", "NetPriceAmount": price, "NetPriceQuantity": "1",
            "IsCompletelyDelivered": delivered,
            "OrderQuantity": qty, "PurchaseOrderQuantityUnit": unit,
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
    assert (po.po_number, po.supplier) == ("4500000001", "17300001")
    assert po.quantity_by_unit == {"PC": 1000}
    assert po.expected_delivery_date == date(2026, 11, 28)
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


def test_parses_header_items_and_skips_deleted():
    payload = odata_po([
        item("600", "PC", NOV28, no="10", price="2.00", delivered=True),
        item("400.5", "PC", DEC05, no="20", price="1.00"),
        item("999", "PC", DEC05, no="30", deleted="L"),
    ])["d"]
    payload.update({"PurchasingProcessingStatus": "05", "DocumentCurrency": "USD",
                    "PaymentTerms": "0002", "CashDiscount1Days": "14",
                    "CashDiscount1Percent": "2.000", "AddressName": "Acme",
                    "PurchaseOrderDate": NOV28})
    po = repository.parse_odata_purchase_order(payload)
    assert [i.item for i in po.items] == ["10", "20"]
    assert po.quantity_by_unit == {"PC": Decimal("1000.5")}
    assert po.expected_delivery_date == date(2026, 12, 5)
    assert po.net_value == Decimal("1600.5")
    assert (po.status_text, po.currency, po.cash_discount_days) == ("Released", "USD", 14)
    assert po.order_date == date(2026, 11, 28)


def test_mixed_units_are_totalled_separately():
    po = repository.parse_odata_purchase_order(odata_po([
        item("10", "KG", NOV28, no="10"), item("5", "PC", DEC05, no="20"),
    ])["d"])
    assert po.quantity_by_unit == {"KG": 10, "PC": 5}


async def test_no_connectivity_binding_means_direct_call(monkeypatch):
    monkeypatch.delenv("VCAP_SERVICES", raising=False)
    assert await repository._connectivity_proxy() is None


VCAP = {
    "connectivity": [{"credentials": {
        "clientid": "conn-id", "clientsecret": "conn-secret",
        "token_service_url": "https://sub.authentication.us10.hana.ondemand.com",
        "onpremise_proxy_host": "connectivityproxy.internal",
        "onpremise_proxy_http_port": "20003",
    }}],
    "destination": [{"credentials": {
        "clientid": "dest-id", "clientsecret": "dest-secret",
        "url": "https://sub.authentication.us10.hana.ondemand.com",
        "uri": "https://destination-configuration.cfapps.us10.hana.ondemand.com",
    }}],
}

S4_DESTINATION = {"destinationConfiguration": {
    "Name": "S4", "Type": "HTTP", "URL": "http://s4h-virtual:50000",
    "Authentication": "BasicAuthentication", "ProxyType": "OnPremise",
    "User": "sapuser", "Password": "sappass",
    "sap-client": "100", "CloudConnectorLocationId": "LOC1",
}}


@pytest.fixture
def btp(monkeypatch):
    """Cloud Foundry with Connectivity + Destination bound and a fake BTP + SAP."""
    import json
    monkeypatch.setenv("VCAP_SERVICES", json.dumps(VCAP))
    monkeypatch.setenv("SAP_MODE", "odata")
    monkeypatch.setenv("SAP_DESTINATION", "S4")
    monkeypatch.setattr(repository, "_tokens", {})
    monkeypatch.setattr(repository, "_destinations", {})
    state = {"requests": [], "destination": S4_DESTINATION}

    def handler(request):
        state["requests"].append(request)
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if "/destination-configuration/" in request.url.path:
            return httpx.Response(200, json=state["destination"])
        return httpx.Response(200, json=odata_po([item("490.000", "PC", NOV28)]))

    monkeypatch.setattr(repository, "_transport", httpx.MockTransport(handler))
    return state


async def test_destination_supplies_sap_connection(btp):
    target = await repository._sap_target()
    assert target.base_url == "http://s4h-virtual:50000/sap/opu/odata/sap/API_PURCHASEORDER_PROCESS_SRV"
    assert target.auth == ("sapuser", "sappass")
    assert (target.sap_client, target.location_id, target.on_premise) == ("100", "LOC1", True)
    await repository._sap_target()  # cached: no second lookup
    lookups = [r for r in btp["requests"] if "/destination-configuration/" in r.url.path]
    assert len(lookups) == 1
    assert lookups[0].url.path.endswith("/destinations/S4")


async def test_po_lookup_through_destination(btp, monkeypatch):
    proxied_for = []

    async def fake_proxy(location_id=None):
        proxied_for.append(location_id)
        return None  # the real proxy would bypass the mock transport

    monkeypatch.setattr(repository, "_connectivity_proxy", fake_proxy)
    po = await repository.get_purchase_order("4500000001")
    assert proxied_for == ["LOC1"]  # OnPremise destination goes via Cloud Connector
    assert po.quantity_by_unit == {"PC": 490}
    sap_call = btp["requests"][-1]
    assert sap_call.url.host == "s4h-virtual"
    assert sap_call.url.params["sap-client"] == "100"


async def test_connectivity_proxy_uses_destination_location(btp):
    proxy = await repository._connectivity_proxy("LOC1")
    assert str(proxy.url) == "http://connectivityproxy.internal:20003"
    assert proxy.headers["Proxy-Authorization"] == "Bearer tok"
    assert proxy.headers["SAP-Connectivity-SCC-Location_ID"] == "LOC1"


async def test_unsupported_destination_auth_is_reported(btp):
    btp["destination"] = {"destinationConfiguration": {
        **S4_DESTINATION["destinationConfiguration"],
        "Authentication": "PrincipalPropagation"}}
    with pytest.raises(ValueError, match="PrincipalPropagation"):
        await repository._sap_target()
