"""Tests for `verify_update` on `ModelService` and `ReportService`."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.intelligence.report_spec import ReportPage, ReportSpec
from app.intelligence.spec import (
    SemanticColumn,
    SemanticModelSpec,
    SemanticTable,
)
from fabric_services.model_service import ModelService
from fabric_services.report_service import ReportService


def _model_spec(name: str = "Demo") -> SemanticModelSpec:
    return SemanticModelSpec(
        name=name,
        description="A tidy model.",
        tables=[
            SemanticTable(
                name="Sales",
                source_schema="dbo",
                source_table="Sales",
                description="Sales facts.",
                columns=[
                    SemanticColumn(
                        name="Amount",
                        source_column="Amount",
                        data_type="decimal",
                        summarize_by="sum",
                        description="Net amount.",
                    )
                ],
            ),
            SemanticTable(
                name="Calendar",
                source_schema="dbo",
                source_table="Calendar",
                description="Dates.",
                is_date_table=True,
                columns=[
                    SemanticColumn(
                        name="Date",
                        source_column="Date",
                        data_type="dateTime",
                        description="Day.",
                    )
                ],
            ),
        ],
    )


def _report_spec(name: str = "DemoReport") -> ReportSpec:
    return ReportSpec(name=name, pages=[ReportPage(name="Home", display_name="Home")])


class ModelVerifyTests(unittest.TestCase):
    def test_verify_update_returns_after_section(self) -> None:
        fabric = mock.Mock()
        fabric.get_semantic_model_definition.return_value = SimpleNamespace(
            files={"model.bim": "{}"}
        )
        store = mock.Mock()
        tenant = mock.Mock(tenant_id="t-1", user_id="u-1", correlation_id=None)
        svc = ModelService(fabric, store, tenant)

        with mock.patch(
            "fabric_services.model_service.parse_semantic_model",
            return_value=_model_spec(),
        ):
            out = svc.verify_update("ws-1", "m-1")
        self.assertIn("after", out)
        self.assertIn("after_finding_count", out)

    def test_verify_update_with_before_computes_delta(self) -> None:
        fabric = mock.Mock()
        fabric.get_semantic_model_definition.return_value = SimpleNamespace(
            files={"model.bim": "{}"}
        )
        store = mock.Mock()
        tenant = mock.Mock(tenant_id="t-1", user_id="u-1", correlation_id=None)
        svc = ModelService(fabric, store, tenant)

        # Before: same spec but with a missing description on Sales to seed
        # a usability finding that will be closed in the after-spec.
        before = _model_spec()
        before.tables[0].description = ""

        with mock.patch(
            "fabric_services.model_service.parse_semantic_model",
            return_value=_model_spec(),
        ):
            out = svc.verify_update("ws-1", "m-1", before=before)
        self.assertIn("before_finding_count", out)
        self.assertIn("closed_codes", out)
        self.assertIn("introduced_codes", out)
        self.assertIsInstance(out["closed_codes"], list)


class ReportVerifyTests(unittest.TestCase):
    def test_verify_update_after_only(self) -> None:
        fabric = mock.Mock()
        fabric.get_report_definition.return_value = SimpleNamespace(
            files={"report.json": "{}"}
        )
        store = mock.Mock()
        tenant = mock.Mock(tenant_id="t-1", user_id="u-1", correlation_id=None)
        svc = ReportService(fabric, store, tenant)

        with mock.patch(
            "fabric_services.report_service.parse_report",
            return_value=_report_spec(),
        ):
            out = svc.verify_update("ws-1", "r-1")
        self.assertIn("after", out)
        self.assertIn("after_finding_count", out)

    def test_verify_update_with_before_returns_delta(self) -> None:
        fabric = mock.Mock()
        fabric.get_report_definition.return_value = SimpleNamespace(
            files={"report.json": "{}"}
        )
        store = mock.Mock()
        tenant = mock.Mock(tenant_id="t-1", user_id="u-1", correlation_id=None)
        svc = ReportService(fabric, store, tenant)

        before = _report_spec()
        with mock.patch(
            "fabric_services.report_service.parse_report",
            return_value=_report_spec(),
        ):
            out = svc.verify_update("ws-1", "r-1", before=before)
        self.assertIn("closed_codes", out)
        self.assertIn("introduced_codes", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
