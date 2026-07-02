"""Tests for `GovernanceService` (RBAC, sensitivity labels, tags, workspace roles)."""

from __future__ import annotations

import unittest
from unittest import mock

from fabric_services.errors import UpstreamError, ValidationError
from fabric_services.governance_service import GovernanceService


class GovernanceServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.svc = GovernanceService(self.client)

    # -- Item permissions ------------------------------------------------

    def test_list_item_permissions(self) -> None:
        self.client.list_item_role_assignments.return_value = [
            {"principal": {"id": "u-1"}, "role": "ReadAll"},
        ]
        out = self.svc.list_item_permissions("ws-1", "item-1")
        self.assertEqual(out[0]["role"], "ReadAll")

    def test_set_item_permission_validates_role(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.set_item_permission(
                "ws-1", "item-1",
                principal_id="p-1",
                principal_type="User",
                role="BadRole",
            )

    def test_set_item_permission_validates_principal_type(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.set_item_permission(
                "ws-1", "item-1",
                principal_id="p-1",
                principal_type="BadType",
                role="Read",
            )

    def test_set_item_permission_happy_path(self) -> None:
        self.client.set_item_permissions.return_value = {"status": "ok"}
        out = self.svc.set_item_permission(
            "ws-1", "item-1",
            principal_id="p-1",
            principal_type="User",
            role="Read",
        )
        self.assertEqual(out["status"], "ok")

    # -- Sensitivity labels ----------------------------------------------

    def test_apply_label_invalid_method(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.apply_sensitivity_label(
                "ws-1", "item-1", label_id="lbl-1", assignment_method="Bogus"
            )

    def test_apply_label_default_method(self) -> None:
        self.client.apply_sensitivity_label.return_value = {"status": "applied"}
        out = self.svc.apply_sensitivity_label("ws-1", "item-1", label_id="lbl-1")
        self.assertEqual(out["status"], "applied")

    def test_remove_label(self) -> None:
        self.client.remove_sensitivity_label.return_value = None
        self.svc.remove_sensitivity_label("ws-1", "item-1")
        self.client.remove_sensitivity_label.assert_called_once_with("ws-1", "item-1")

    # -- Tags ------------------------------------------------------------

    def test_apply_tags_validates_nonempty(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.apply_tags("ws-1", "item-1", tag_ids=[])

    def test_apply_tags_happy_path(self) -> None:
        self.client.apply_tags.return_value = {"status": "ok"}
        out = self.svc.apply_tags("ws-1", "item-1", tag_ids=["t-1", "t-2"])
        self.assertEqual(out["status"], "ok")

    # -- Workspace roles -------------------------------------------------

    def test_list_workspace_roles(self) -> None:
        self.client.list_workspace_role_assignments.return_value = [
            {"principal": {"id": "u-1"}, "role": "Admin"},
        ]
        out = self.svc.list_workspace_roles("ws-1")
        self.assertEqual(out[0]["role"], "Admin")

    def test_add_workspace_role_validates(self) -> None:
        with self.assertRaises(ValidationError):
            self.svc.add_workspace_role(
                "ws-1",
                principal_id="p-1",
                principal_type="User",
                role="Read",  # not a workspace role
            )

    def test_add_workspace_role_happy_path(self) -> None:
        self.client.add_workspace_role_assignment.return_value = {"status": "ok"}
        out = self.svc.add_workspace_role(
            "ws-1",
            principal_id="p-1",
            principal_type="User",
            role="Admin",
        )
        self.assertEqual(out["status"], "ok")

    def test_upstream_wrapped(self) -> None:
        self.client.list_item_role_assignments.side_effect = RuntimeError("boom")
        with self.assertRaises(UpstreamError):
            self.svc.list_item_permissions("ws-1", "item-1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
