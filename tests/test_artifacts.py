"""Tests for the artifact store (:mod:`app.artifacts`).

Verify the storage-agnostic verbs over the local backend: definition save/load,
manifest indexing, audit persistence, and key-segment sanitisation.

Run with::

    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.artifacts import (
    ArtifactRef,
    LocalArtifactStore,
    get_artifact_store,
    sanitize_segment,
)


class SanitizeTests(unittest.TestCase):
    def test_replaces_unsafe_characters(self) -> None:
        self.assertEqual(sanitize_segment("My Model/v2"), "My_Model_v2")

    def test_fallback_on_empty(self) -> None:
        self.assertEqual(sanitize_segment("", fallback="x"), "x")
        self.assertEqual(sanitize_segment("***"), "___")


class ArtifactRefTests(unittest.TestCase):
    def test_prefix_is_forward_slash_and_unique(self) -> None:
        ref = ArtifactRef(
            kind="semanticModels",
            workspace_id="ws-1",
            item_id="id-1",
            workspace_name="My WS",
            item_name="Sales Model",
        )
        self.assertEqual(
            ref.prefix, "My_WS__ws-1/semanticModels/Sales_Model__id-1"
        )
        self.assertNotIn("\\", ref.prefix)


class LocalArtifactStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.mkdtemp()
        self.store = LocalArtifactStore(self._dir)
        self.ref = ArtifactRef(
            kind="semanticModels",
            workspace_id="ws-1",
            item_id="id-1",
            workspace_name="WS",
            item_name="Sales",
        )

    def test_save_and_load_definition(self) -> None:
        files = {
            "definition/model.tmdl": "model Model",
            "definition/tables/Sales.tmdl": "table Sales",
        }
        self.store.save_definition(self.ref, files, fmt="TMDL", source="imported")
        loaded = self.store.load_definition(self.ref)
        assert loaded is not None
        self.assertEqual(loaded.files, files)
        self.assertEqual(loaded.format, "TMDL")
        self.assertEqual(loaded.metadata["source"], "imported")
        self.assertEqual(loaded.metadata["id"], "id-1")

    def test_load_missing_returns_none(self) -> None:
        other = ArtifactRef(
            kind="reports", workspace_id="ws-1", item_id="zzz", item_name="None"
        )
        self.assertIsNone(self.store.load_definition(other))

    def test_manifest_and_list(self) -> None:
        self.store.save_definition(self.ref, {"definition/m.tmdl": "x"}, fmt="TMDL")
        report_ref = ArtifactRef(
            kind="reports",
            workspace_id="ws-1",
            item_id="r-1",
            workspace_name="WS",
            item_name="Dash",
        )
        self.store.save_definition(report_ref, {"report.json": "{}"}, source="generated")

        manifest = self.store.read_manifest()
        self.assertEqual(len(manifest), 2)

        models = self.store.list_items(kind="semanticModels")
        self.assertEqual([r.item_id for r in models], ["id-1"])
        reports = self.store.list_items(kind="reports")
        self.assertEqual([r.item_id for r in reports], ["r-1"])
        self.assertEqual(len(self.store.list_items()), 2)

    def test_save_audit(self) -> None:
        key = self.store.save_audit(self.ref, "usability", '{"score": 90}', ext="json")
        self.assertTrue(key.endswith(".json"))
        self.assertTrue((Path(self._dir) / Path(*key.split("/"))).is_file())

    def test_overwrite_updates_metadata(self) -> None:
        self.store.save_definition(self.ref, {"definition/m.tmdl": "v1"}, source="imported")
        self.store.save_definition(self.ref, {"definition/m.tmdl": "v2"}, source="generated")
        loaded = self.store.load_definition(self.ref)
        assert loaded is not None
        self.assertEqual(loaded.files["definition/m.tmdl"], "v2")
        self.assertEqual(loaded.metadata["source"], "generated")
        self.assertEqual(len(self.store.read_manifest()), 1)


class FactoryTests(unittest.TestCase):
    def test_default_factory_returns_local(self) -> None:
        store = get_artifact_store()
        self.assertIsInstance(store, LocalArtifactStore)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
