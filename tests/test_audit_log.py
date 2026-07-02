"""Tests for `AuditLogService` (per-tenant JSONL audit trail)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.artifacts import get_artifact_store
from fabric_services.audit_log_service import AuditLogService
from fabric_services.context import TenantContext


class AuditLogServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Local artifact store rooted in the temp dir.
        self.tenant = TenantContext(
            tenant_id="acme",
            user_id="user-1",
            correlation_id="cid-1",
        )
        # Force the local-disk store by setting the prefix to an absolute path
        # that the get_artifact_store factory will treat as local.
        import os

        os.environ["FABRIC_ARTIFACT_ROOT"] = self._tmp.name
        self.store = get_artifact_store(prefix=self.tenant.storage_prefix)
        self.svc = AuditLogService(self.store, self.tenant)

    def tearDown(self) -> None:
        import os

        os.environ.pop("FABRIC_ARTIFACT_ROOT", None)

    def test_log_appends_event(self) -> None:
        event = self.svc.log("test.action", target_kind="item", target_id="i-1")
        self.assertIsNotNone(event)
        self.assertEqual(event.action, "test.action")
        self.assertEqual(event.tenant_id, "acme")
        self.assertEqual(event.actor_id, "user-1")
        events = self.svc.list_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].action, "test.action")

    def test_multiple_appends_persist(self) -> None:
        for i in range(3):
            self.svc.log(f"act.{i}")
        events = self.svc.list_events()
        self.assertEqual(len(events), 3)
        actions = [e.action for e in events]
        self.assertEqual(actions, ["act.0", "act.1", "act.2"])

    def test_jsonl_format(self) -> None:
        self.svc.log("formatting.check", details={"foo": "bar"})
        # Find the JSONL file under the per-tenant prefix.
        root = Path(self._tmp.name)
        jsonl = next(root.rglob("events.jsonl"))
        lines = jsonl.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        doc = json.loads(lines[0])
        self.assertEqual(doc["action"], "formatting.check")
        self.assertEqual(doc["details"]["foo"], "bar")
        self.assertIn("timestamp", doc)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
