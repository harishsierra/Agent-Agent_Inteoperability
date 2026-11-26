"""A2A server for the SAP agent. Run: python -m po_agents.sap_joule_agent.server"""
import base64
import binascii
import os
import secrets

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

from po_agents.common.understanding import PoExtractor, build_extractor
from po_agents.sap_joule_agent.executor import SapAgentExecutor
from po_agents.sap_joule_agent.graph import build_sap_graph


def build_agent_card(url: str) -> AgentCard:
    return AgentCard(
        name="SAP Purchase Order Agent",
        description="Retrieves purchase order details from SAP.",
        version="1.0.0",
        # 0.3 is the version Joule speaks; 1.0 is the current A2A version.
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0"),
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="0.3"),
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        # Tells clients to send HTTP Basic credentials on A2A calls.
        security_schemes={
            "basicAuth": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="Basic")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"basicAuth": StringList()})],
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="get_purchase_order",
                name="Get purchase order",
                description="Returns supplier, quantity and expected delivery date.",
                tags=["sap", "purchase-order"],
                examples=["Get the details of purchase order 450001"],
            )
        ],
    )


class BasicAuthMiddleware:
    """Requires HTTP Basic credentials on every path except the public agent card."""

    def __init__(self, app, username: str, password: str):
        self.app = app
        self.expected = f"{username}:{password}".encode()

    def _authorized(self, scope) -> bool:
        header = dict(scope["headers"]).get(b"authorization", b"")
        scheme, _, encoded = header.partition(b" ")
        if scheme.lower() != b"basic":
            return False
        try:
            supplied = base64.b64decode(encoded.strip(), validate=True)
        except (binascii.Error, ValueError):
            return False
        return secrets.compare_digest(supplied, self.expected)

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["path"].startswith("/.well-known/")
            or self._authorized(scope)
        ):
            await self.app(scope, receive, send)
            return
        response = JSONResponse(
            {"error": "Unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="sap-po-agent"'},
        )
        await response(scope, receive, send)


def build_app(
    extract_po: PoExtractor,
    url: str,
    credentials: tuple[str, str] | None = None,
) -> Starlette:
    """credentials=(username, password) turns on HTTP Basic auth."""
    card = build_agent_card(url)
    handler = DefaultRequestHandler(
        agent_executor=SapAgentExecutor(build_sap_graph(extract_po)),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    middleware = []
    if credentials:
        username, password = credentials
        middleware.append(
            Middleware(BasicAuthMiddleware, username=username, password=password)
        )
    return Starlette(
        routes=[
            *create_agent_card_routes(card),
            # Older well-known path, for clients that still look there.
            *create_agent_card_routes(card, card_url="/.well-known/agent.json"),
            *create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
        ],
        middleware=middleware,
    )


def main() -> None:
    load_dotenv()
    host = os.environ.get("SAP_AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("SAP_AGENT_PORT", "9001"))
    url = os.environ.get("SAP_AGENT_URL", f"http://{host}:{port}")
    username = os.environ.get("SAP_AGENT_USERNAME")
    password = os.environ.get("SAP_AGENT_PASSWORD")
    if not username or not password:
        raise SystemExit("Set SAP_AGENT_USERNAME and SAP_AGENT_PASSWORD in .env")
    app = build_app(build_extractor(), url, credentials=(username, password))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
