"""Tests for `LifecycleService` (Git + Deployment Pipelines)."""

from __future__ import annotations

import unittest
from unittest import mock

from fabric_services.errors import UpstreamError
from fabric_services.lifecycle_service import LifecycleService


class LifecycleServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = mock.Mock()
        self.svc = LifecycleService(self.client)

    def test_connect_git_passes_kwargs(self) -> None:
        self.client.git_connect.return_value = {"status": "Connected"}
        out = self.svc.connect_git(
            "ws-1",
            provider="AzureDevOps",
            organization="org",
            project="proj",
            repository="repo",
            branch="main",
            directory="/",
        )
        self.assertEqual(out, {"status": "Connected"})
        self.client.git_connect.assert_called_once()

    def test_git_status_proxies(self) -> None:
        self.client.git_status.return_value = {"workspaceHead": "abc"}
        self.assertEqual(self.svc.git_status("ws-1"), {"workspaceHead": "abc"})
        self.client.git_status.assert_called_once_with("ws-1")

    def test_commit_to_git_no_items(self) -> None:
        self.client.git_commit_to_repo.return_value = ("Succeeded", {"id": "op-1"})
        out = self.svc.commit_to_git("ws-1", comment="initial")
        self.assertEqual(out["status"], "Succeeded")
        self.assertEqual(out["scope"], "all")

    def test_update_from_git(self) -> None:
        self.client.git_update_from_repo.return_value = ("Succeeded", None)
        out = self.svc.update_from_git("ws-1")
        self.assertEqual(out["status"], "Succeeded")
        self.client.git_update_from_repo.assert_called_once_with("ws-1")

    def test_list_pipelines(self) -> None:
        self.client.list_deployment_pipelines.return_value = [{"id": "p-1"}]
        self.assertEqual(self.svc.list_pipelines(), [{"id": "p-1"}])

    def test_deploy(self) -> None:
        self.client.deploy_to_stage.return_value = ("Succeeded", {"items": []})
        out = self.svc.deploy(
            "p-1",
            source_stage_id="s-1",
            target_stage_id="s-2",
        )
        self.assertEqual(out["status"], "Succeeded")

    def test_upstream_error_wraps(self) -> None:
        self.client.git_status.side_effect = RuntimeError("boom")
        with self.assertRaises(UpstreamError):
            self.svc.git_status("ws-1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
