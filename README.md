# SAP Purchase Order agent for Joule (A2A)

```
Copilot agent --A2A--> SAP Agent Gateway -> Joule capability --A2A 0.3--> this agent -> SAP PO data
(existing)             (existing)           (joule/)                      (po_agents/)
```

This repository holds only the SAP side. The Copilot agent owns logistics data
and the delay comparison; this agent returns PO, supplier, quantity and expected
delivery date.

The LLM (through SAP AI Core) only identifies the PO number, and the number is
rejected unless it appears in the user's text. All facts come from PO data.

## Run

```powershell
venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env     # fill in AI Core values, or set LLM_MODE=offline
venv\Scripts\python -m po_agents.sap_joule_agent.server
venv\Scripts\python -m pytest
```

## Before deploying

- Confirm with SAP that Agent Gateway can invoke a Joule capability that calls a
  code-based agent. SAP's pages (October 2026) confirm SAP-delivered and Joule
  Studio agents, and say communication with self-hosted agents through Agent
  Gateway is not yet supported.
- PO data comes from SAP S/4HANA (`SAP_MODE=odata`, OData service
  `API_PURCHASEORDER_PROCESS_SRV`) or from `data/sap_purchase_orders.json`
  (`SAP_MODE=json`). See `po_agents/common/repository.py`.
- The A2A endpoint requires **HTTP Basic auth** (`SAP_AGENT_USERNAME` /
  `SAP_AGENT_PASSWORD` in `.env`); the agent card stays public. Basic auth sends
  the password with every request, so only expose the agent over HTTPS, and
  consider IAS/JWT for production. Tasks are held in memory.
- The files in `joule/` follow SAP's CodeJam and have not been linted, compiled or
  deployed.

## Deploy the Joule capability

1. Host the agent at an HTTPS URL that Joule can reach.
2. Create the BTP destination `PO_AGENT_A2A` in the Joule subaccount: type HTTP,
   URL = agent base URL, proxy type Internet.
3. Deploy a standalone test assistant:

```powershell
npm install -g @sap/joule-studio-cli
joule login
cd joule
joule deploy -c -n "po_status_assistant"
joule launch "po_status_assistant"
```

`-n` creates a test assistant and does not affect the production Joule instance.

## References

- https://github.com/SAP-samples/codejam-code-based-agents/blob/main/exercises/Python-LangGraph/09-integrate-agent-into-joule.md
- https://github.com/SAP-samples/btp-joule-a2a-pro-code-agent
- https://architecture.learning.sap.com/docs/ref-arch/fe0d1d
- https://architecture.learning.sap.com/news/2026/10/27/a2a-agent-gateway-interoperability
