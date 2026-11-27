"""What a user can ask about a purchase order, and how each answer is built.

Every answer is built here from SAP data. The LLM only chooses field names
from this catalog; it never writes any of the facts.
"""
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from po_agents.common.models import PurchaseOrder, format_date, format_number


@dataclass(frozen=True)
class Field:
    key: str
    description: str  # shown to the LLM
    render: Callable[[PurchaseOrder], tuple[str, Any]]  # (text, structured data)


def _quantity(po):
    if not po.items:
        return "no items in SAP", None
    totals = po.quantity_by_unit
    text = " + ".join(f"{format_number(q)} {unit}" for unit, q in totals.items())
    if len(po.items) > 1:
        text += f" across {len(po.items)} items"
    data = {
        "total": {unit: float(q) for unit, q in totals.items()},
        "items": [
            {"item": i.item, "description": i.description, "quantity": float(i.quantity), "unit": i.unit}
            for i in po.items
        ],
    }
    return f"ordered quantity {text}", data


def _delivery_date(po):
    dates = sorted({d for i in po.items for d in i.delivery_dates})
    if not dates:
        return "no delivery date in SAP", None
    if len(dates) == 1:
        text = f"expected delivery {format_date(dates[0])}"
    else:
        text = (
            f"expected delivery between {format_date(dates[0])} and "
            f"{format_date(dates[-1])} (complete by {format_date(dates[-1])})"
        )
    data = {
        "expected_delivery_date": dates[-1].isoformat(),
        "items": [
            {"item": i.item, "delivery_dates": [d.isoformat() for d in i.delivery_dates]}
            for i in po.items
        ],
    }
    return text, data


def _delivery_status(po):
    if not po.items:
        return "no items in SAP", None
    done = [i for i in po.items if i.completely_delivered]
    if len(done) == len(po.items):
        text = "fully delivered"
    elif not done:
        text = "not yet delivered"
    else:
        text = f"{len(done)} of {len(po.items)} items delivered"
    open_items = [i.item for i in po.items if not i.completely_delivered]
    return text, {"delivered_items": len(done), "total_items": len(po.items), "open_items": open_items}


def _invoice_status(po):
    if not po.items:
        return "no items in SAP", None
    done = [i for i in po.items if i.finally_invoiced]
    if len(done) == len(po.items):
        text = "fully invoiced"
    elif not done:
        text = "not yet fully invoiced"
    else:
        text = f"{len(done)} of {len(po.items)} items finally invoiced"
    return text, {"finally_invoiced_items": len(done), "total_items": len(po.items)}


def _status(po):
    text = f"status {po.status_text}" if po.status_text else "no status in SAP"
    return text, {"code": po.processing_status, "text": po.status_text}


def _supplier(po):
    text = f"supplier {po.supplier}"
    place = ", ".join(p for p in (po.supplier_city, po.supplier_country) if p)
    if po.supplier_name and po.supplier_name != po.supplier:
        text += f" ({po.supplier_name}{', ' + place if place else ''})"
    elif place:
        text += f" ({place})"
    return text, {
        "id": po.supplier, "name": po.supplier_name,
        "city": po.supplier_city, "country": po.supplier_country,
    }


def _value(po):
    value = po.net_value
    if value is None:
        return "no price in SAP", None
    text = f"net value {format_number(value)} {po.currency or ''}".rstrip()
    data = {
        "net_value": float(value),
        "currency": po.currency,
        "items": [
            {"item": i.item, "net_price": float(i.net_price) if i.net_price is not None else None,
             "per": float(i.price_quantity), "unit": i.unit,
             "net_value": float(i.net_value) if i.net_value is not None else None}
            for i in po.items
        ],
    }
    return text, data


def _items(po):
    if not po.items:
        return "no items in SAP", []
    lines = []
    for i in po.items:
        name = i.description or i.material or "item"
        lines.append(f"{i.item}: {name} ({format_number(i.quantity)} {i.unit})")
    text = f"{len(po.items)} item{'s' if len(po.items) != 1 else ''}: " + "; ".join(lines)
    data = [
        {"item": i.item, "material": i.material, "description": i.description,
         "quantity": float(i.quantity), "unit": i.unit}
        for i in po.items
    ]
    return text, data


def _order_date(po):
    if not po.order_date:
        return "no order date in SAP", None
    return f"ordered on {format_date(po.order_date)}", po.order_date.isoformat()


def _payment_terms(po):
    if not po.payment_terms:
        return "no payment terms in SAP", None
    text = f"payment terms {po.payment_terms}"
    if po.cash_discount_percent and po.cash_discount_days:
        text += (
            f" ({format_number(po.cash_discount_percent)}% discount within "
            f"{po.cash_discount_days} days)"
        )
    return text, {
        "code": po.payment_terms,
        "cash_discount_percent": float(po.cash_discount_percent) if po.cash_discount_percent else None,
        "cash_discount_days": po.cash_discount_days,
    }


def _incoterms(po):
    if not po.incoterms:
        return "no Incoterms in SAP", None
    text = f"Incoterms {po.incoterms}"
    if po.incoterms_location:
        text += f" {po.incoterms_location}"
    return text, {"code": po.incoterms, "location": po.incoterms_location}


def _plant(po):
    places = sorted({(i.plant, i.storage_location) for i in po.items if i.plant})
    if not places:
        return "no plant in SAP", None
    text = "; ".join(f"plant {p}" + (f", storage location {s}" if s else "") for p, s in places)
    return text, [{"plant": p, "storage_location": s} for p, s in places]


def _delivery_address(po):
    addresses = sorted({i.delivery_address for i in po.items if i.delivery_address})
    if not addresses:
        return "no delivery address in SAP", None
    return "deliver to " + " | ".join(addresses), addresses


def _organization(po):
    parts = [
        f"company code {po.company_code}" if po.company_code else "",
        f"purchasing organization {po.purchasing_organization}" if po.purchasing_organization else "",
        f"purchasing group {po.purchasing_group}" if po.purchasing_group else "",
    ]
    text = ", ".join(p for p in parts if p) or "no organization data in SAP"
    return text, {
        "company_code": po.company_code,
        "purchasing_organization": po.purchasing_organization,
        "purchasing_group": po.purchasing_group,
    }


def _created_by(po):
    if not po.created_by:
        return "no creator in SAP", None
    return f"created by {po.created_by}", po.created_by


def _po_type(po):
    if not po.po_type:
        return "no document type in SAP", None
    return f"document type {po.po_type}", po.po_type


FIELDS: dict[str, Field] = {
    f.key: f
    for f in [
        Field("supplier", "Supplier / vendor of the PO.", _supplier),
        Field("quantity", "Ordered quantity (total and per item).", _quantity),
        Field("delivery_date", "Expected / scheduled delivery date, when it will arrive.", _delivery_date),
        Field("delivery_status", "Whether the goods have been delivered / received, open items.", _delivery_status),
        Field("status", "Processing / approval / release status of the PO.", _status),
        Field("value", "Price, net value, total amount or cost of the PO.", _value),
        Field("items", "Line items: materials, products, descriptions.", _items),
        Field("order_date", "Date the PO was created / ordered.", _order_date),
        Field("payment_terms", "Payment terms and cash discount.", _payment_terms),
        Field("incoterms", "Incoterms / shipping terms.", _incoterms),
        Field("plant", "Receiving plant and storage location.", _plant),
        Field("delivery_address", "Ship-to / delivery address.", _delivery_address),
        Field("organization", "Company code, purchasing organization, purchasing group.", _organization),
        Field("invoice_status", "Whether the PO has been invoiced.", _invoice_status),
        Field("created_by", "User who created the PO.", _created_by),
        Field("po_type", "PO document type.", _po_type),
    ]
}

# Answer to a general question such as "details of PO 4500000001".
SUMMARY = ["supplier", "status", "quantity", "delivery_date", "delivery_status", "value"]
# "status of PO ..." usually means: is it released, delivered, and when will it arrive.
STATUS_BUNDLE = ["status", "delivery_status", "delivery_date"]


def answer(po: PurchaseOrder, fields: list[str]) -> tuple[str, dict]:
    """Builds the reply text and structured data for the requested fields only."""
    keys = [k for k in dict.fromkeys(fields) if k in FIELDS] or SUMMARY
    texts, data = [], {}
    for key in keys:
        text, value = FIELDS[key].render(po)
        texts.append(text)
        data[key] = value
    return f"PO {po.po_number}: " + "; ".join(texts) + ".", data
