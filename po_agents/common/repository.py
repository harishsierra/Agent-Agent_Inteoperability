"""JSON stand-in for the SAP system."""
import json
from pathlib import Path

from po_agents.common.models import PurchaseOrder

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


def _find(filename: str, po_number: str) -> dict | None:
    records = json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
    return records.get(po_number)


def get_purchase_order(po_number: str) -> PurchaseOrder | None:
    """Replace this body to read from a real SAP system."""
    record = _find("sap_purchase_orders.json", po_number)
    return PurchaseOrder.model_validate(record) if record else None
