"""Purchase order data contract: the PO header and all of its items."""
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field

# PurchasingProcessingStatus (SAP domain MEPROCSTATE)
PROCESSING_STATUS = {
    "01": "In process",
    "02": "Active",
    "03": "In release",
    "04": "Partially released",
    "05": "Released",
    "08": "Rejected",
}


class PurchaseOrderItem(BaseModel):
    item: str
    material: str | None = None
    description: str | None = None
    quantity: Decimal = Field(ge=0)
    unit: str
    net_price: Decimal | None = None
    # Quantity the net price refers to (price per 1, per 100, ...).
    price_quantity: Decimal = Decimal(1)
    currency: str | None = None
    plant: str | None = None
    storage_location: str | None = None
    delivery_dates: list[date] = []
    completely_delivered: bool = False
    finally_invoiced: bool = False
    delivery_address: str | None = None

    @property
    def net_value(self) -> Decimal | None:
        if self.net_price is None or not self.price_quantity:
            return None
        return self.net_price / self.price_quantity * self.quantity


class PurchaseOrder(BaseModel):
    po_number: str
    supplier: str
    supplier_name: str | None = None
    supplier_city: str | None = None
    supplier_country: str | None = None
    order_date: date | None = None
    created_by: str | None = None
    company_code: str | None = None
    purchasing_organization: str | None = None
    purchasing_group: str | None = None
    po_type: str | None = None
    processing_status: str | None = None
    currency: str | None = None
    payment_terms: str | None = None
    cash_discount_days: int | None = None
    cash_discount_percent: Decimal | None = None
    incoterms: str | None = None
    incoterms_location: str | None = None
    items: list[PurchaseOrderItem] = []

    @property
    def status_text(self) -> str | None:
        if not self.processing_status:
            return None
        return PROCESSING_STATUS.get(self.processing_status, self.processing_status)

    @property
    def expected_delivery_date(self) -> date | None:
        """Latest schedule-line date over all items: when the whole PO is delivered."""
        dates = [d for item in self.items for d in item.delivery_dates]
        return max(dates) if dates else None

    @property
    def quantity_by_unit(self) -> dict[str, Decimal]:
        totals: dict[str, Decimal] = {}
        for item in self.items:
            totals[item.unit] = totals.get(item.unit, Decimal(0)) + item.quantity
        return totals

    @property
    def net_value(self) -> Decimal | None:
        values = [item.net_value for item in self.items]
        return sum(values, Decimal(0)) if values and None not in values else None


def format_date(value: date) -> str:
    return f"{value.day} {value:%b %Y}"


def format_number(value: Decimal) -> str:
    """1,000 for whole numbers, 1,730.19 otherwise."""
    value = value.normalize()
    if value == value.to_integral():
        return f"{int(value):,}"
    return f"{value:,.2f}" if value.as_tuple().exponent >= -2 else f"{value:,}"
