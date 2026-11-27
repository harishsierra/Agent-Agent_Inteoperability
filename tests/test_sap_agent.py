import httpx
import pytest
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_data_parts, new_text_message
from a2a.types import Role, SendMessageRequest, TaskState
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from po_agents.common.understanding import (
    build_extractor,
    llm_extractor,
    offline_extractor,
)
from po_agents.sap_joule_agent.server import build_app

URL = "http://sap-agent.test"


def connect(extractor=offline_extractor):
    transport = httpx.ASGITransport(app=build_app(extractor, URL))
    return httpx.AsyncClient(transport=transport, base_url=URL)


@pytest.fixture
async def http():
    async with connect() as client:
        yield client


async def test_v1_client_gets_structured_data(http):
    client = await create_client(URL, ClientConfig(streaming=False, httpx_client=http))
    message = new_text_message("Get PO 450001", role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)
    [response] = [r async for r in client.send_message(request)]
    assert response.task.status.state == TaskState.TASK_STATE_COMPLETED
    [data] = get_data_parts(response.task.artifacts[0].parts)
    assert data["po_number"] == "450001" and data["found"] is True
    # A general question gets the summary fields.
    assert data["requested_fields"] == [
        "supplier", "status", "quantity", "delivery_date", "delivery_status", "value"
    ]
    assert data["data"]["delivery_date"]["expected_delivery_date"] == "2026-11-28"
    assert data["data"]["quantity"]["total"] == {"units": 1000}


def v03(text, **ids):
    """A message/send request as an A2A 0.3 client sends it."""
    message = {
        "kind": "message",
        "messageId": f"m-{text}",
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        **ids,
    }
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "message/send",
        "params": {"message": message},
    }


async def test_v03_wire_format(http):
    task = (await http.post("/", json=v03("PO 450001"))).json()["result"]
    assert task["kind"] == "task" and task["id"] and task["contextId"]
    assert task["status"]["state"] == "completed"
    assert task["status"]["message"]["parts"][0]["text"] == (
        "PO 450001: supplier SUP001 (Demo Supplier Inc., Chicago, US); status Released; "
        "ordered quantity 1,000 units across 2 items; expected delivery between "
        "25 Nov 2026 and 28 Nov 2026 (complete by 28 Nov 2026); 1 of 2 items delivered; "
        "net value 10,700 USD."
    )


def text_of(task):
    return task["status"]["message"]["parts"][0]["text"]


@pytest.mark.parametrize("question,expected,fields", [
    ("What is the quantity of PO 450001?",
     "PO 450001: ordered quantity 1,000 units across 2 items.", ["quantity"]),
    ("Who is the supplier of PO 450001?",
     "PO 450001: supplier SUP001 (Demo Supplier Inc., Chicago, US).", ["supplier"]),
    ("When was PO 450001 ordered?", "PO 450001: ordered on 2 Nov 2026.", ["order_date"]),
    ("What is the price of PO 450001?", "PO 450001: net value 10,700 USD.", ["value"]),
])
async def test_only_the_requested_field_is_returned(http, question, expected, fields):
    task = (await http.post("/", json=v03(question))).json()["result"]
    assert text_of(task) == expected
    data = task["artifacts"][0]["parts"][0]["data"]
    assert data["requested_fields"] == fields
    assert list(data["data"]) == fields


async def test_unknown_po(http):
    task = (await http.post("/", json=v03("PO 999999"))).json()["result"]
    text = task["status"]["message"]["parts"][0]["text"]
    assert text == "PO 999999 was not found in SAP."


async def test_v03_input_required_then_resume(http):
    first = (await http.post("/", json=v03("hello"))).json()["result"]
    assert first["status"]["state"] == "input-required"
    ids = {"taskId": first["id"], "contextId": first["contextId"]}
    second = (await http.post("/", json=v03("450001", **ids))).json()["result"]
    assert second["id"] == first["id"]
    assert second["status"]["state"] == "completed"


async def test_agent_failure_is_reported_without_details():
    async def broken(text):
        raise RuntimeError("secret detail")

    async with connect(broken) as http:
        body = (await http.post("/", json=v03("PO 450001"))).text
    assert '"failed"' in body and "secret detail" not in body


@pytest.mark.parametrize("path", ["agent-card.json", "agent.json"])
async def test_agent_card(http, path):
    card = (await http.get(f"/.well-known/{path}")).json()
    assert [i["protocolVersion"] for i in card["supportedInterfaces"]] == ["1.0", "0.3"]
    # 0.3 clients read these top-level fields.
    assert card["url"] == URL
    assert card["protocolVersion"].startswith("0.3")


class ToolModel(GenericFakeChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.parametrize(
    "model_says,expected",
    [("450001", "450001"), ("450002", None), ("45000", None), (None, None)],
)
async def test_llm_po_must_be_in_user_text(model_says, expected):
    call = {"name": "PoRequest", "args": {"po_number": model_says, "fields": ["delivery_date"]}, "id": "1"}
    model = ToolModel(messages=iter([AIMessage("", tool_calls=[call])]))
    query = await llm_extractor(model)("Will PO 450001 arrive?")
    assert query.po_number == expected
    assert query.fields == ["delivery_date"]


async def test_llm_all_expands_to_every_field():
    call = {"name": "PoRequest", "args": {"po_number": "450001", "fields": ["all"]}, "id": "1"}
    model = ToolModel(messages=iter([AIMessage("", tool_calls=[call])]))
    query = await llm_extractor(model)("Everything about PO 450001")
    assert "all" not in query.fields and len(query.fields) > 10


def test_missing_model_config(monkeypatch):
    monkeypatch.setenv("LLM_MODE", "aicore")
    monkeypatch.delenv("AICORE_MODEL", raising=False)
    with pytest.raises(ValueError, match="AICORE_MODEL"):
        build_extractor()


AUTH = ("Admin", "Admin@123")


def connect_with_auth(**kwargs):
    app = build_app(offline_extractor, URL, credentials=AUTH)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=URL, **kwargs
    )


@pytest.mark.parametrize(
    "auth", [None, ("Admin", "wrong"), ("admin", "Admin@123")]
)
async def test_basic_auth_rejects_bad_credentials(auth):
    async with connect_with_auth(auth=auth) as http:
        response = await http.post("/", json=v03("PO 450001"))
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic")


async def test_basic_auth_accepts_valid_credentials():
    async with connect_with_auth(auth=AUTH) as http:
        task = (await http.post("/", json=v03("PO 450001"))).json()["result"]
    assert task["status"]["state"] == "completed"


async def test_agent_card_is_public_and_declares_basic_auth():
    async with connect_with_auth() as http:
        card = (await http.get("/.well-known/agent-card.json")).json()
    scheme = card["securitySchemes"]["basicAuth"]
    assert scheme["httpAuthSecurityScheme"]["scheme"] == "Basic"


async def test_no_credentials_means_open_endpoint_and_no_security_in_card(http):
    card = (await http.get("/.well-known/agent-card.json")).json()
    assert "securitySchemes" not in card
    task = (await http.post("/", json=v03("PO 450001"))).json()["result"]
    assert task["status"]["state"] == "completed"
