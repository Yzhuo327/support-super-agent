"""Run the shipping-followup HTTP contract without credentials or external services."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from support_agent.api import create_app
from support_agent.i18n import translate
from support_agent.offline_model import OfflineSupportModel
from support_agent.service import SupportService


def run_demo(locale: str = "zh") -> dict:
    customer = {"X-Customer-Id": "C100"}
    staff = {"X-Staff-Key": "demo-only-key"}
    with tempfile.TemporaryDirectory() as directory:
        db = str(Path(directory) / "demo.db")
        first = SupportService(model=OfflineSupportModel(), db_path=db, followup_mode="auto")
        with TestClient(create_app(first, staff_key=staff["X-Staff-Key"])) as client:
            response = client.post("/tickets", json={"order_id": "1002", "locale": locale}, headers=customer)
            response.raise_for_status()
            ticket = response.json()
        first.close()

        # Reopen the database in a fresh service instance; no task state is copied in memory.
        restarted = SupportService(model=OfflineSupportModel(), db_path=db, followup_mode="auto")
        with TestClient(create_app(restarted, staff_key=staff["X-Staff-Key"])) as client:
            payload = {"ticket_id": ticket["id"], "event_id": "demo-delayed", "sequence": 1, "status": "delayed"}
            applied = client.post("/events/shipments", json=payload, headers=staff)
            applied.raise_for_status()
            duplicate = client.post("/events/shipments", json=payload, headers=staff)
            duplicate.raise_for_status()
            delivered = client.post(
                "/events/shipments",
                json={**payload, "event_id": "demo-delivered", "sequence": 2, "status": "delivered"},
                headers=staff,
            )
            delivered.raise_for_status()
            state = client.get(f"/tickets/{ticket['id']}", headers=customer)
            state.raise_for_status()
            result = state.json()
        restarted.close()
        assert applied.json()["outcome"] == "applied"
        assert duplicate.json()["duplicate"] is True
        assert result["status"] == "completed"
        assert len(result["notifications"]) == 2
        return {"checks": {"reopened_database": True, "duplicate_suppressed": True, "completed": True}, **result}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--locale", choices=["zh", "en"], default="zh")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.locale), ensure_ascii=False, indent=2))
    print(translate("demo_done", args.locale))


if __name__ == "__main__":
    main()
