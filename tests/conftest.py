import pytest


@pytest.fixture(autouse=True)
def local_po_data(monkeypatch):
    """Tests read data/sap_purchase_orders.json unless a test points them at SAP."""
    monkeypatch.setenv("SAP_MODE", "json")
