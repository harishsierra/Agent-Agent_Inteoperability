"""Bridges A2A requests to the SAP LangGraph workflow."""
import logging

from a2a.helpers import new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater

from po_agents.common.fields import FIELDS, SUMMARY, answer

logger = logging.getLogger(__name__)


class SapAgentExecutor(AgentExecutor):
    def __init__(self, graph) -> None:
        self._graph = graph

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        # Joule displays status.message.parts[0].text, so every outcome sets it.
        def say(text: str):
            return updater.new_agent_message([new_text_part(text)])

        await updater.start_work()
        try:
            state = await self._graph.ainvoke({"request": context.get_user_input()})
        except Exception:
            logger.exception("SAP agent failed on task %s", task.id)
            await updater.failed(say("The SAP agent could not process the request."))
            return

        po_number = state["po_number"]
        if po_number is None:
            await updater.requires_input(
                say("Which purchase order? Please give one PO number.")
            )
            return

        po = state["purchase_order"]
        if po is None:
            text, data = f"PO {po_number} was not found in SAP.", None
            fields = state.get("fields") or []
        else:
            fields = [f for f in state.get("fields") or [] if f in FIELDS] or SUMMARY
            text, data = answer(po, fields)
        await updater.add_artifact(
            [
                new_data_part(
                    {
                        "po_number": po_number,
                        "found": po is not None,
                        "requested_fields": fields,
                        # Only the fields the user asked about.
                        "data": data,
                    }
                )
            ],
            name="purchase_order",
        )
        await updater.complete(say(text))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id, context.context_id).cancel()
