"""Durable, explicitly authorized shipping followups over simulated carrier events.

Each event, ticket transition and local notification commits in one SQLite transaction.
The executor has read-only tools; no external message or payment is sent here.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import replace
from pathlib import Path

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from support_agent.graph import build_graph
from support_agent.i18n import translate
from support_agent.policies_kb import PolicyBase
from support_agent.policy import PolicyConfig
from support_agent.store import Store
from support_agent.tools import make_tools


class FollowupError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class FollowupService:
    def __init__(
        self, store: Store, model: BaseChatModel, data_dir: Path, db_path: str | None, mode: str = "observe"
    ) -> None:
        if mode not in {"observe", "auto"}:
            raise ValueError("followup mode must be observe or auto")
        self.mode = mode
        self.store = store
        self.model = model
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS followup_tickets (
                    id TEXT PRIMARY KEY, customer_id TEXT NOT NULL, order_id TEXT NOT NULL,
                    locale TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'watching',
                    shipment_status TEXT NOT NULL DEFAULT 'shipped', last_sequence INTEGER NOT NULL DEFAULT 0
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_shipping_followup
                    ON followup_tickets(customer_id, order_id) WHERE status = 'watching';
                CREATE TABLE IF NOT EXISTS shipment_events (
                    event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    shipment_status TEXT NOT NULL, mode TEXT NOT NULL, outcome TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS followup_notifications (
                    event_id TEXT PRIMARY KEY, ticket_id TEXT NOT NULL,
                    message TEXT NOT NULL, tools_used TEXT NOT NULL
                );
            """)

    def close(self) -> None:
        self._db.close()

    def _customer(self, customer_id: str) -> None:
        if customer_id not in self.store.customers:
            raise FollowupError(translate("unknown_customer"), 401)

    def _ticket(self, ticket_id: str, customer_id: str | None = None) -> dict:
        row = self._db.execute("SELECT * FROM followup_tickets WHERE id = ?", (ticket_id,)).fetchone()
        if row is None or (customer_id is not None and row["customer_id"] != customer_id):
            raise FollowupError(translate("not_found"), 404)
        return dict(row)

    @staticmethod
    def _describe(ticket: dict) -> dict:
        return {
            **ticket,
            "goal": translate("goal", ticket["locale"]),
            "allowed_actions": ["track_shipment", "notify_customer"],
        }

    def create(self, customer_id: str, order_id: str, locale: str = "zh") -> dict:
        self._customer(customer_id)
        if locale not in {"zh", "en"}:
            raise ValueError("locale must be zh or en")
        order = self.store.order_for(customer_id, order_id)
        if order is None:
            raise FollowupError(translate("not_found", locale), 404)
        if order.status != "shipped":
            raise FollowupError(translate("not_shipped", locale), 409)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            existing = self._db.execute(
                "SELECT id FROM followup_tickets WHERE customer_id = ? AND order_id = ? AND status = 'watching'",
                (customer_id, order.id),
            ).fetchone()
            if existing:
                return self._describe(self._ticket(existing["id"]))
            # A completed followup is durable evidence that this demo order was delivered.
            delivered = self._db.execute(
                "SELECT 1 FROM followup_tickets WHERE customer_id = ? AND order_id = ? AND status = 'completed'",
                (customer_id, order.id),
            ).fetchone()
            if delivered:
                raise FollowupError(translate("not_shipped", locale), 409)
            ticket_id = uuid.uuid4().hex
            self._db.execute(
                "INSERT INTO followup_tickets(id, customer_id, order_id, locale) VALUES (?, ?, ?, ?)",
                (ticket_id, customer_id, order.id, locale),
            )
            return self._describe(self._ticket(ticket_id))

    def list_tickets(self, customer_id: str) -> list[dict]:
        self._customer(customer_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM followup_tickets WHERE customer_id = ? ORDER BY rowid", (customer_id,)
            ).fetchall()
            return [self._describe(dict(row)) for row in rows]

    def get(self, customer_id: str, ticket_id: str) -> dict:
        self._customer(customer_id)
        with self._lock:
            ticket = self._describe(self._ticket(ticket_id, customer_id))
            ticket["events"] = [
                dict(row)
                for row in self._db.execute(
                    "SELECT * FROM shipment_events WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)
                )
            ]
            ticket["notifications"] = [
                {**dict(row), "tools_used": json.loads(row["tools_used"])}
                for row in self._db.execute(
                    "SELECT * FROM followup_notifications WHERE ticket_id = ? ORDER BY rowid", (ticket_id,)
                )
            ]
            return ticket

    def cancel(self, customer_id: str, ticket_id: str) -> dict:
        self._customer(customer_id)
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._ticket(ticket_id, customer_id)
            self._db.execute(
                "UPDATE followup_tickets SET status = 'cancelled' WHERE id = ? AND status = 'watching'", (ticket_id,)
            )
            return self._describe(self._ticket(ticket_id))

    def process(self, ticket_id: str, event_id: str, sequence: int, status: str) -> dict:
        if not event_id.strip() or sequence < 1 or status not in {"delayed", "in_transit", "delivered"}:
            raise ValueError("invalid shipment event")
        with self._lock, self._db:
            # Serializes cancellation and event handling across service instances as well as threads.
            self._db.execute("BEGIN IMMEDIATE")
            ticket = self._ticket(ticket_id)
            previous = self._db.execute("SELECT * FROM shipment_events WHERE event_id = ?", (event_id,)).fetchone()
            if previous:
                if (previous["ticket_id"], previous["sequence"], previous["shipment_status"]) != (
                    ticket_id,
                    sequence,
                    status,
                ):
                    raise FollowupError(translate("event_conflict", ticket["locale"]), 409)
                return {**dict(previous), "duplicate": True}
            if ticket["status"] != "watching":
                outcome = "ignored_terminal"
            elif sequence <= ticket["last_sequence"]:
                outcome = "ignored_stale"
            elif self.mode == "observe":
                outcome = "observed"
            else:
                outcome = "unchanged" if ticket["shipment_status"] == status else "applied"
                if outcome == "applied":
                    tools_used = self._execute(ticket, status)
                    self._db.execute(
                        "INSERT INTO followup_notifications VALUES (?, ?, ?, ?)",
                        (
                            event_id,
                            ticket_id,
                            translate(status, ticket["locale"], order_id=ticket["order_id"]),
                            json.dumps(tools_used),
                        ),
                    )
                self._db.execute(
                    "UPDATE followup_tickets SET shipment_status = ?, last_sequence = ?, status = ? WHERE id = ?",
                    (status, sequence, "completed" if status == "delivered" else "watching", ticket_id),
                )
            self._db.execute(
                "INSERT INTO shipment_events VALUES (?, ?, ?, ?, ?, ?)",
                (event_id, ticket_id, sequence, status, self.mode, outcome),
            )
            return {
                "event_id": event_id,
                "ticket_id": ticket_id,
                "sequence": sequence,
                "shipment_status": status,
                "mode": self.mode,
                "outcome": outcome,
                "duplicate": False,
            }

    def _execute(self, ticket: dict, status: str) -> list[str]:
        order = self.store.order_for(ticket["customer_id"], ticket["order_id"])
        if order is None:
            raise FollowupError(translate("not_found", ticket["locale"]), 404)
        # Project the simulated carrier event only into this read-only run, not the payment/order store.
        snapshot = replace(self.store, orders={order.id: replace(order, status=status)})
        tools = [
            tool
            for tool in make_tools(snapshot, PolicyBase(self.data_dir / "policies.md"), PolicyConfig())
            if tool.name == "track_shipment"
        ]
        graph = build_graph(self.model, tools, snapshot.customers, InMemorySaver())
        try:
            result = graph.invoke(
                {"messages": [HumanMessage(f"Track shipment for order {order.id}.")]},
                {
                    "configurable": {"customer_id": ticket["customer_id"], "thread_id": ticket["id"]},
                    "recursion_limit": 16,
                },
            )
        except Exception as exc:
            raise FollowupError(translate("execution_failed", ticket["locale"]), 502) from exc
        for message in result["messages"]:
            if isinstance(message, ToolMessage) and message.name == "track_shipment":
                try:
                    evidence = json.loads(message.content)
                except (json.JSONDecodeError, TypeError):
                    continue
                if (
                    isinstance(evidence, dict)
                    and evidence.get("order_id") == order.id
                    and evidence.get("status") == status
                ):
                    return ["track_shipment"]
        raise FollowupError(translate("execution_failed", ticket["locale"]), 502)
