"""Purchase order data contract."""
from datetime import date

from pydantic import BaseModel, Field


class PurchaseOrder(BaseModel):
    po_number: str
    supplier: str
    expected_delivery_date: date
    # SAP quantities are decimals; whole numbers stay int.
    ordered_quantity: int | float = Field(ge=0)
    unit: str


def format_date(value: date) -> str:
    return f"{value.day} {value:%b %Y}"
