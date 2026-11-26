"""Purchase order data contract."""
from datetime import date

from pydantic import BaseModel, Field


class PurchaseOrder(BaseModel):
    po_number: str
    supplier: str
    expected_delivery_date: date
    ordered_quantity: int = Field(ge=0)
    unit: str


def format_date(value: date) -> str:
    return f"{value.day} {value:%b %Y}"
