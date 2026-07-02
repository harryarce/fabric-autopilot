"""Payload-parsing tests for the Fabric REST dataclasses.

These cover the lakehouse discovery surface that powers Direct Lake on
OneLake: the ``Lakehouse`` and ``LakehouseTable`` shapes returned by the
``List Lakehouses`` and (preview) ``List Tables`` APIs.
"""

from __future__ import annotations

import unittest
import unittest.mock

from app.fabric_client import Lakehouse, LakehouseTable, fabric_item_web_url


class FabricItemWebUrlTests(unittest.TestCase):
    def test_semantic_model_link(self):
        url = fabric_item_web_url("ws-1", "model-1", "SemanticModel")
        self.assertEqual(
            url, "https://app.fabric.microsoft.com/groups/ws-1/datasets/model-1"
        )

    def test_report_link(self):
        url = fabric_item_web_url("ws-1", "rep-1", "Report")
        self.assertEqual(
            url, "https://app.fabric.microsoft.com/groups/ws-1/reports/rep-1"
        )

    def test_unknown_type_falls_back_to_items_segment(self):
        url = fabric_item_web_url("ws-1", "x-1", "Mystery")
        self.assertEqual(
            url, "https://app.fabric.microsoft.com/groups/ws-1/items/x-1"
        )

    def test_missing_ids_return_none(self):
        self.assertIsNone(fabric_item_web_url(None, "x", "Report"))
        self.assertIsNone(fabric_item_web_url("ws", None, "Report"))



class LakehouseParsingTests(unittest.TestCase):
    def _payload(self) -> dict:
        return {
            "id": "lh-1",
            "displayName": "DemoLakehouse",
            "description": "demo",
            "type": "Lakehouse",
            "workspaceId": "ws-1",
            "properties": {
                "defaultSchema": "dbo",
                "oneLakeTablesPath": (
                    "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Tables"
                ),
                "oneLakeFilesPath": (
                    "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files"
                ),
                "sqlEndpointProperties": {
                    "connectionString": "abc.datawarehouse.fabric.microsoft.com",
                    "id": "ep-1",
                    "provisioningStatus": "Success",
                },
            },
        }

    def test_from_payload_maps_all_fields(self):
        lh = Lakehouse.from_payload(self._payload(), workspace_id="fallback-ws")
        self.assertEqual(lh.id, "lh-1")
        self.assertEqual(lh.name, "DemoLakehouse")
        self.assertEqual(lh.workspace_id, "ws-1")
        self.assertEqual(lh.default_schema, "dbo")
        self.assertEqual(
            lh.onelake_tables_path,
            "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Tables",
        )
        self.assertEqual(
            lh.onelake_files_path,
            "https://onelake.dfs.fabric.microsoft.com/ws-1/lh-1/Files",
        )
        self.assertEqual(
            lh.sql_endpoint_server,
            "abc.datawarehouse.fabric.microsoft.com",
        )
        self.assertEqual(lh.sql_endpoint_id, "ep-1")
        self.assertEqual(lh.sql_endpoint_status, "Success")

    def test_is_schema_enabled(self):
        lh = Lakehouse.from_payload(self._payload(), workspace_id="ws-1")
        self.assertTrue(lh.is_schema_enabled)

    def test_workspace_id_falls_back_when_missing(self):
        payload = self._payload()
        del payload["workspaceId"]
        lh = Lakehouse.from_payload(payload, workspace_id="fallback-ws")
        self.assertEqual(lh.workspace_id, "fallback-ws")

    def test_handles_missing_properties(self):
        lh = Lakehouse.from_payload(
            {"id": "lh-2", "displayName": "Bare"}, workspace_id="ws-2"
        )
        self.assertEqual(lh.workspace_id, "ws-2")
        self.assertIsNone(lh.default_schema)
        self.assertIsNone(lh.sql_endpoint_server)
        self.assertFalse(lh.is_schema_enabled)

    def test_sql_connection_string(self):
        lh = Lakehouse.from_payload(self._payload(), workspace_id="ws-1")
        conn = lh.sql_connection_string
        self.assertIsNotNone(conn)
        self.assertIn("abc.datawarehouse.fabric.microsoft.com", conn)
        self.assertIn("Database=DemoLakehouse", conn)

    def test_sql_connection_string_none_without_endpoint(self):
        lh = Lakehouse.from_payload(
            {"id": "lh-3", "displayName": "NoEndpoint"}, workspace_id="ws-3"
        )
        self.assertIsNone(lh.sql_connection_string)


class LakehouseTableParsingTests(unittest.TestCase):
    def test_managed_table(self):
        table = LakehouseTable.from_payload(
            {
                "type": "Managed",
                "name": "fact_sales",
                "location": "abfss://ws@onelake.dfs.fabric.microsoft.com/lh/Tables/fact_sales",
                "format": "Delta",
            }
        )
        self.assertEqual(table.name, "fact_sales")
        self.assertEqual(table.table_type, "Managed")
        self.assertEqual(table.table_format, "Delta")
        self.assertIn("fact_sales", table.location)

    def test_external_table(self):
        table = LakehouseTable.from_payload(
            {"type": "External", "name": "ext", "format": "Delta"}
        )
        self.assertEqual(table.table_type, "External")

    def test_defaults_when_type_missing(self):
        table = LakehouseTable.from_payload({"name": "t"})
        self.assertEqual(table.table_type, "Managed")
        self.assertIsNone(table.table_format)


class _FakeTokenProvider:
    def fabric_token(self, *, use_fallback: bool = False) -> str:
        return "fake-token"


class _FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by the LRO tests."""

    def __init__(
        self,
        status_code: int,
        *,
        headers: dict | None = None,
        body: dict | None = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}
        self.text = ""
        self.content = b"x" if body or status_code in (200, 201) else b""

    def json(self):
        return self._body


class UpdateDefinitionLroTests(unittest.TestCase):
    """Exercise ``updateDefinition`` against a mocked LRO session.

    These cover both terminal HTTP shapes:
      * Synchronous 200/201 → return ``"Updated"`` immediately.
      * Long-running 202 → poll Get-Operation-State, then read result.
    """

    def setUp(self) -> None:
        from app.fabric_client import FabricClient

        self.client = FabricClient(_FakeTokenProvider())  # type: ignore[arg-type]

        # Patch out time.sleep so the LRO doesn't add ~5s/test.
        import time as _time
        self._sleep_patch = unittest.mock.patch.object(_time, "sleep", lambda *_a, **_kw: None)
        self._sleep_patch.start()

    def tearDown(self) -> None:
        self._sleep_patch.stop()

    def test_update_semantic_model_sync_200(self) -> None:
        posts: list = []

        def fake_post(url, headers=None, json=None, timeout=None):
            posts.append({"url": url, "body": json})
            return _FakeResponse(200, body={"id": "m-1"})

        with unittest.mock.patch.object(self.client._session, "post", fake_post):
            status = self.client.update_semantic_model_definition(
                "ws-1", "m-1", {"parts": []}
            )
        self.assertEqual(status, "Updated")
        self.assertEqual(len(posts), 1)
        self.assertIn("/workspaces/ws-1/semanticModels/m-1/updateDefinition", posts[0]["url"])
        self.assertEqual(posts[0]["body"], {"definition": {"parts": []}})

    def test_update_report_lro_succeeds(self) -> None:
        """202 → poll → Succeeded → fetch result."""
        gets: list[str] = []

        op_url = "https://api.fabric.microsoft.com/v1/operations/op-1"

        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeResponse(
                202,
                headers={
                    "Location": op_url,
                    "x-ms-operation-id": "op-1",
                    "Retry-After": "1",
                },
            )

        def fake_get(url, headers=None, params=None, timeout=None):
            gets.append(url)
            if url == op_url:
                return _FakeResponse(200, body={"status": "Succeeded"})
            if url.endswith("/operations/op-1/result"):
                return _FakeResponse(200, body={"id": "rep-1"})
            raise AssertionError(f"unexpected GET: {url}")

        with (
            unittest.mock.patch.object(self.client._session, "post", fake_post),
            unittest.mock.patch.object(self.client._session, "get", fake_get),
        ):
            status = self.client.update_report_definition("ws-1", "rep-1", {"parts": []})

        self.assertEqual(status, "Succeeded")
        # First GET polls the operation, second GET fetches the result.
        self.assertEqual(gets[0], op_url)
        self.assertTrue(gets[1].endswith("/operations/op-1/result"))

    def test_update_definition_lro_failure_raises(self) -> None:
        from app.fabric_client import FabricApiError

        op_url = "https://api.fabric.microsoft.com/v1/operations/op-2"

        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeResponse(
                202,
                headers={"Location": op_url, "x-ms-operation-id": "op-2", "Retry-After": "1"},
            )

        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeResponse(
                200, body={"status": "Failed", "error": {"message": "boom"}}
            )

        with (
            unittest.mock.patch.object(self.client._session, "post", fake_post),
            unittest.mock.patch.object(self.client._session, "get", fake_get),
        ):
            with self.assertRaises(FabricApiError) as ctx:
                self.client.update_semantic_model_definition("ws-1", "m-1", {"parts": []})
        self.assertIn("boom", str(ctx.exception))


class WritePacingTests(unittest.TestCase):
    """Proactive client-side pacing spaces out mutating Fabric calls.

    A burst of create/update POSTs is what trips Fabric's per-principal write
    limit (HTTP 429), so ``_pace_write`` sleeps to keep at least
    ``MIN_WRITE_INTERVAL_SECONDS`` between successive writes.
    """

    def setUp(self) -> None:
        from app.fabric_client import FabricClient

        self.client = FabricClient(_FakeTokenProvider())  # type: ignore[arg-type]

    def test_first_write_does_not_sleep(self) -> None:
        sleeps: list[float] = []
        with unittest.mock.patch(
            "app.fabric_client.time.sleep", lambda s: sleeps.append(s)
        ):
            self.client._pace_write()
        self.assertEqual(sleeps, [])

    def test_back_to_back_writes_are_spaced(self) -> None:
        import app.fabric_client as fc

        sleeps: list[float] = []
        # A controllable monotonic clock: first write records t=100, the second
        # write observes t=100.2 (0.2s later), so the pacer must sleep the
        # remaining ~0.8s to reach the 1.0s minimum interval.
        clock = iter([100.0, 100.2, 100.2])
        with (
            unittest.mock.patch.object(fc, "MIN_WRITE_INTERVAL_SECONDS", 1.0),
            unittest.mock.patch.object(
                fc.time, "monotonic", lambda: next(clock)
            ),
            unittest.mock.patch.object(
                fc.time, "sleep", lambda s: sleeps.append(s)
            ),
        ):
            self.client._pace_write()  # records t=100
            self.client._pace_write()  # 0.2s elapsed → sleep ~0.8s
        self.assertEqual(len(sleeps), 1)
        self.assertAlmostEqual(sleeps[0], 0.8, places=6)

    def test_zero_interval_disables_pacing(self) -> None:
        import app.fabric_client as fc

        sleeps: list[float] = []
        with (
            unittest.mock.patch.object(fc, "MIN_WRITE_INTERVAL_SECONDS", 0.0),
            unittest.mock.patch.object(
                fc.time, "sleep", lambda s: sleeps.append(s)
            ),
        ):
            self.client._pace_write()
            self.client._pace_write()
        self.assertEqual(sleeps, [])

    def test_post_paces_before_sending(self) -> None:
        """``_post`` invokes the pacer so creates inherit write spacing."""
        called = {"paced": False}

        def fake_post(url, headers=None, json=None, timeout=None):
            return _FakeResponse(201, body={"id": "m-1"})

        with (
            unittest.mock.patch.object(
                self.client, "_pace_write", lambda: called.__setitem__("paced", True)
            ),
            unittest.mock.patch.object(self.client._session, "post", fake_post),
        ):
            self.client._post("https://example/create", {"a": 1})
        self.assertTrue(called["paced"])


class SchemaDiscoveryCachingTests(unittest.TestCase):
    """Schema-explorer list calls should collapse onto a per-workspace cache.

    Streamlit reruns the script on every selectbox change, which previously
    re-issued ``list_sql_endpoints`` (3 underlying paged calls) plus a
    duplicate ``list_lakehouses`` on every render — a frequent driver of
    Fabric 429s. The cache + shared lakehouse payload should make repeat
    explorer renders within ``LIST_CACHE_TTL_SECONDS`` issue zero new GETs.
    """

    def setUp(self) -> None:
        from app.fabric_client import FabricClient

        self.client = FabricClient(_FakeTokenProvider())  # type: ignore[arg-type]
        self.gets: list[str] = []

    def _install_routes(self, routes: dict[str, dict]) -> None:
        """Patch ``session.get`` to serve canned JSON for each Fabric URL."""

        def fake_get(url, headers=None, params=None, timeout=None):
            self.gets.append(url)
            if url not in routes:
                return _FakeResponse(404, body={"error": "not found"})
            return _FakeResponse(200, body=routes[url])

        patcher = unittest.mock.patch.object(self.client._session, "get", fake_get)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_list_lakehouses_cached_and_shared_with_sql_endpoints(self) -> None:
        """One GET feeds ``list_lakehouses`` AND the SQL-endpoint aggregator."""
        base = "https://api.fabric.microsoft.com/v1/workspaces/ws-1"
        self._install_routes(
            {
                f"{base}/warehouses": {"value": []},
                f"{base}/lakehouses": {
                    "value": [
                        {
                            "id": "lh-1",
                            "displayName": "Demo",
                            "workspaceId": "ws-1",
                            "properties": {
                                "sqlEndpointProperties": {
                                    "connectionString": "abc.datawarehouse.fabric.microsoft.com",
                                    "id": "ep-1",
                                }
                            },
                        }
                    ]
                },
                f"{base}/sqlEndpoints": {"value": []},
            }
        )

        first = self.client.list_lakehouses("ws-1")
        endpoints = self.client.list_sql_endpoints("ws-1")
        # A second call to list_lakehouses (same render or next rerun) must
        # be served from cache and issue zero new requests.
        second = self.client.list_lakehouses("ws-1")
        # Re-asking for SQL endpoints inside the TTL also hits the cache.
        endpoints_again = self.client.list_sql_endpoints("ws-1")

        self.assertEqual(len(first), 1)
        self.assertEqual(first, second)
        self.assertEqual(len(endpoints), 1)
        self.assertEqual(endpoints, endpoints_again)
        # Exactly one GET per underlying Fabric resource — no duplicates for
        # the second list_lakehouses, no re-aggregation for the second
        # list_sql_endpoints.
        self.assertEqual(
            sorted(self.gets),
            sorted(
                [
                    f"{base}/warehouses",
                    f"{base}/lakehouses",
                    f"{base}/sqlEndpoints",
                ]
            ),
        )

    def test_list_sql_endpoints_caches_aggregate(self) -> None:
        """Repeated ``list_sql_endpoints`` calls issue zero new GETs."""
        base = "https://api.fabric.microsoft.com/v1/workspaces/ws-2"
        self._install_routes(
            {
                f"{base}/warehouses": {
                    "value": [
                        {
                            "id": "w-1",
                            "displayName": "Wh",
                            "properties": {"connectionString": "x.sql.example"},
                        }
                    ]
                },
                f"{base}/lakehouses": {"value": []},
                f"{base}/sqlEndpoints": {"value": []},
            }
        )

        first = self.client.list_sql_endpoints("ws-2")
        baseline = len(self.gets)
        second = self.client.list_sql_endpoints("ws-2")

        self.assertEqual(first, second)
        # The second call must not have issued any additional GETs.
        self.assertEqual(len(self.gets), baseline)

    def test_list_lakehouse_tables_cached(self) -> None:
        base = (
            "https://api.fabric.microsoft.com/v1/workspaces/ws-3/"
            "lakehouses/lh-9/tables"
        )
        self._install_routes(
            {base: {"data": [{"name": "t", "type": "Managed", "format": "Delta"}]}}
        )

        first = self.client.list_lakehouse_tables("ws-3", "lh-9")
        baseline = len(self.gets)
        second = self.client.list_lakehouse_tables("ws-3", "lh-9")

        self.assertEqual(first, second)
        self.assertEqual(len(self.gets), baseline)

    def test_sql_endpoints_throttling_propagates_not_swallowed(self) -> None:
        """A 429 on the preview ``/sqlEndpoints`` API must not be hidden.

        Masking throttling as an empty list silently hides the cool-off
        window from upstream callers (and from the operator). The aggregator
        now lets ``FabricThrottledError`` bubble while still tolerating other
        preview-API failures (404/501).
        """
        from app.fabric_client import FabricThrottledError

        base = "https://api.fabric.microsoft.com/v1/workspaces/ws-4"

        def fake_get(url, headers=None, params=None, timeout=None):
            if url.endswith("/sqlEndpoints"):
                # Simulate Fabric throttling on the preview API.
                return _FakeResponse(
                    429,
                    headers={"Retry-After": "30"},
                    body={"error": "RequestBlocked"},
                )
            return _FakeResponse(200, body={"value": []})

        # _FakeResponse defaults text to "" — the throttle parser needs it
        # readable, which the default already provides.
        with unittest.mock.patch.object(self.client._session, "get", fake_get):
            with self.assertRaises(FabricThrottledError):
                self.client.list_sql_endpoints("ws-4")

    def test_lakehouse_tables_throttling_propagates(self) -> None:
        """Same contract for the preview ``/lakehouses/{id}/tables`` API."""
        from app.fabric_client import FabricThrottledError

        def fake_get(url, headers=None, params=None, timeout=None):
            return _FakeResponse(
                429,
                headers={"Retry-After": "30"},
                body={"error": "RequestBlocked"},
            )

        with unittest.mock.patch.object(self.client._session, "get", fake_get):
            with self.assertRaises(FabricThrottledError):
                self.client.list_lakehouse_tables("ws-5", "lh-5")


if __name__ == "__main__":
    unittest.main()
