"""LangGraph workflow of the SAP agent: understand -> look up in SAP."""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from po_agents.common.models import PurchaseOrder
from po_agents.common.repository import get_purchase_order
from po_agents.common.understanding import PoExtractor


class SapState(TypedDict, total=False):
    request: str
    po_number: str | None
    # Fields the user asked about; empty means a general question (summary).
    fields: list[str]
    purchase_order: PurchaseOrder | None


def build_sap_graph(extract_po: PoExtractor):
    async def understand(state: SapState) -> SapState:
        query = await extract_po(state["request"])
        return {"po_number": query.po_number, "fields": query.fields}

    async def lookup(state: SapState) -> SapState:
        return {"purchase_order": await get_purchase_order(state["po_number"])}

    def after_understand(state: SapState) -> str:
        return "lookup" if state["po_number"] else END

    graph = StateGraph(SapState)
    graph.add_node("understand", understand)
    graph.add_node("lookup", lookup)
    graph.add_edge(START, "understand")
    graph.add_conditional_edges("understand", after_understand, ["lookup", END])
    graph.add_edge("lookup", END)
    return graph.compile()
