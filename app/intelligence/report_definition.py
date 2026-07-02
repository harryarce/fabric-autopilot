"""Render a :class:`~app.intelligence.report_spec.ReportSpec` into a Fabric
*report* definition (PBIR — the enhanced Power BI report format) and parse one
back.

A Fabric *Report* item is created/updated through the REST API with a
``definition`` whose ``parts`` each carry a ``path`` and a base64 ``payload``
(same envelope as a semantic-model definition). Unlike a semantic model, a
report definition has **no** ``format`` field.

PBIR-Enhanced lays the report out as a folder tree::

    definition.pbir                                   # dataset binding
    definition/version.json                           # PBIR engine version
    definition/report.json                            # theme + settings
    definition/pages/pages.json                       # page order / active
    definition/pages/<page>/page.json                 # one per page
    definition/pages/<page>/visuals/<visual>/visual.json
    .platform                                         # item metadata
    StaticResources/SharedResources/BaseThemes/<base>.json   # base theme
    StaticResources/RegisteredResources/<theme>.json  # custom theme (optional)

The renderer is deterministic. The parser is *scoped and tolerant*: it recovers
the visual types, titles, field bindings and theme an audit needs, ignoring the
long tail of PBIR detail it does not.
"""

from __future__ import annotations

import base64
import json
import uuid
from dataclasses import dataclass
from importlib import resources
from typing import Any

from .report_spec import (
    ReportField,
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
)

# PBIR schema URLs (versioned by Microsoft). Pinned to the revisions that the
# Fabric report import validates against successfully (mirrors a report that
# round-trips through the service's getDefinition/createReport APIs).
_PBIR_PROPS_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definitionProperties/2.0.0/schema.json"
)
_REPORT_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/report/3.3.0/schema.json"
)
_PAGES_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/pagesMetadata/1.1.0/schema.json"
)
_PAGE_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/page/2.1.0/schema.json"
)
_VISUAL_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/visualContainer/2.9.0/schema.json"
)
_VERSION_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definition/versionMetadata/1.0.0/schema.json"
)

# The base theme is required by PBIR. Fabric expects a *SharedResources* base
# theme whose file ships inside the definition; the engine-version object below
# matches the pinned report/page/visual schema revisions above.
_BASE_THEME_NAME = "CY26SU05"
_BASE_THEME_PATH = (
    f"StaticResources/SharedResources/BaseThemes/{_BASE_THEME_NAME}.json"
)
_REPORT_VERSION_AT_IMPORT = {
    "visual": "2.9.0",
    "report": "3.3.0",
    "page": "2.3.1",
}


def _load_base_theme() -> str:
    """Return the bundled base-theme JSON shipped with every report."""
    return (
        resources.files("app.intelligence.resources")
        .joinpath(f"{_BASE_THEME_NAME}.json")
        .read_text(encoding="utf-8")
    )
_PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)


@dataclass(frozen=True)
class ReportDefinition:
    """A rendered report definition ready for Fabric or disk."""

    files: dict[str, str]

    @property
    def parts(self) -> list[dict[str, str]]:
        return [
            {
                "path": path,
                "payload": base64.b64encode(text.encode("utf-8")).decode("ascii"),
                "payloadType": "InlineBase64",
            }
            for path, text in self.files.items()
        ]

    def definition_payload(self) -> dict[str, Any]:
        """The ``definition`` object for a create/update report call."""
        return {"parts": self.parts}

    def to_zip_bytes(self) -> bytes:
        import io
        import zipfile

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for path, text in self.files.items():
                zf.writestr(path, text)
        return buffer.getvalue()


# ---------------------------------------------------------------------------
# Field / query encoding
# ---------------------------------------------------------------------------


def _field_json(fld: ReportField) -> dict[str, Any]:
    """Encode a field reference into the PBIR query expression form."""
    wrapper = "Measure" if fld.kind == "measure" else "Column"
    return {
        wrapper: {
            "Expression": {"SourceRef": {"Entity": fld.entity}},
            "Property": fld.property,
        }
    }


def _projection_json(fld: ReportField) -> dict[str, Any]:
    return {
        "field": _field_json(fld),
        "queryRef": fld.query_ref,
        "nativeQueryRef": fld.property,
    }


def _visual_query_json(visual: ReportVisual) -> dict[str, Any]:
    query_state: dict[str, Any] = {}
    for role, fields in visual.projections.items():
        if not fields:
            continue
        projections = [_projection_json(f) for f in fields]
        # Exported reports mark the first projection in each role as active.
        projections[0]["active"] = True
        query_state[role] = {"projections": projections}
    return {"queryState": query_state} if query_state else {}


def _literal_text(value: str) -> dict[str, Any]:
    """A PBIR literal-text expression (used for static visual titles)."""
    escaped = value.replace("'", "''")
    return {"expr": {"Literal": {"Value": f"'{escaped}'"}}}


# ---------------------------------------------------------------------------
# Part rendering
# ---------------------------------------------------------------------------


def _render_visual(visual: ReportVisual) -> str:
    payload: dict[str, Any] = {
        "$schema": _VISUAL_SCHEMA,
        "name": visual.name,
        "position": {
            "x": visual.x,
            "y": visual.y,
            "z": visual.z,
            "width": visual.width,
            "height": visual.height,
            "tabOrder": visual.z,
        },
        "visual": {"visualType": visual.visual_type},
    }
    query = _visual_query_json(visual)
    if query:
        payload["visual"]["query"] = query
    if visual.formatting:
        payload["visual"]["objects"] = visual.formatting
    if visual.title:
        payload["visual"]["visualContainerObjects"] = {
            "title": [
                {
                    "properties": {
                        "show": {"expr": {"Literal": {"Value": "true"}}},
                        "text": _literal_text(visual.title),
                    }
                }
            ]
        }
    # Cross-filtering other visuals is the default behavior in exported reports.
    payload["visual"]["drillFilterOtherVisuals"] = True
    return json.dumps(payload, indent=2)


def _render_page(page: ReportPage) -> str:
    payload = {
        "$schema": _PAGE_SCHEMA,
        "name": page.name,
        "displayName": page.display_name,
        "displayOption": "FitToPage",
        "height": page.height,
        "width": page.width,
    }
    return json.dumps(payload, indent=2)


def _render_pages_metadata(spec: ReportSpec) -> str:
    order = [p.name for p in spec.pages]
    payload = {
        "$schema": _PAGES_SCHEMA,
        "pageOrder": order,
        "activePageName": order[0] if order else "",
    }
    return json.dumps(payload, indent=2)


def _render_report_json(spec: ReportSpec) -> str:
    theme_collection: dict[str, Any] = {
        "baseTheme": {
            "name": _BASE_THEME_NAME,
            "reportVersionAtImport": _REPORT_VERSION_AT_IMPORT,
            "type": "SharedResources",
        }
    }
    resource_packages: list[dict[str, Any]] = [
        {
            "name": "SharedResources",
            "type": "SharedResources",
            "items": [
                {
                    "name": _BASE_THEME_NAME,
                    "path": f"BaseThemes/{_BASE_THEME_NAME}.json",
                    "type": "BaseTheme",
                }
            ],
        }
    ]
    if spec.theme:
        theme_file = f"{spec.theme.name}.json"
        theme_collection["customTheme"] = {
            "name": theme_file,
            "reportVersionAtImport": _REPORT_VERSION_AT_IMPORT,
            "type": "RegisteredResources",
        }
        resource_packages.append(
            {
                "name": "RegisteredResources",
                "type": "RegisteredResources",
                "items": [
                    {
                        "name": theme_file,
                        "path": theme_file,
                        "type": "CustomTheme",
                    }
                ],
            }
        )
    payload: dict[str, Any] = {
        "$schema": _REPORT_SCHEMA,
        "themeCollection": theme_collection,
        "objects": {
            "section": [
                {
                    "properties": {
                        "verticalAlignment": {
                            "expr": {"Literal": {"Value": "'Top'"}}
                        }
                    }
                }
            ]
        },
        "resourcePackages": resource_packages,
        "settings": {
            "useStylableVisualContainerHeader": True,
            "defaultDrillFilterOtherVisuals": True,
            "allowChangeFilterTypes": True,
            "useEnhancedTooltips": True,
            "useDefaultAggregateDisplayName": True,
        },
    }
    return json.dumps(payload, indent=2)


def _render_pbir(spec: ReportSpec) -> str:
    if spec.dataset_id:
        dataset_reference: dict[str, Any] = {
            "byConnection": {
                "connectionString": f"semanticmodelid={spec.dataset_id}",
            }
        }
    else:
        name = spec.dataset_name or spec.name
        dataset_reference = {"byPath": {"path": f"../{name}.SemanticModel"}}
    payload = {
        "$schema": _PBIR_PROPS_SCHEMA,
        "version": "4.0",
        "datasetReference": dataset_reference,
    }
    return json.dumps(payload, indent=2)


def _render_platform(spec: ReportSpec) -> str:
    payload = {
        "$schema": _PLATFORM_SCHEMA,
        "metadata": {"type": "Report", "displayName": spec.name},
        "config": {"version": "2.0", "logicalId": str(uuid.uuid4())},
    }
    return json.dumps(payload, indent=2)


def _render_version() -> str:
    payload = {
        "$schema": _VERSION_SCHEMA,
        "version": "2.0.0",
    }
    return json.dumps(payload, indent=2)


def build_report_definition(spec: ReportSpec) -> ReportDefinition:
    """Render ``spec`` into a Fabric PBIR report definition."""
    files: dict[str, str] = {
        "definition.pbir": _render_pbir(spec),
        "definition/version.json": _render_version(),
        "definition/report.json": _render_report_json(spec),
        "definition/pages/pages.json": _render_pages_metadata(spec),
        ".platform": _render_platform(spec),
        _BASE_THEME_PATH: _load_base_theme(),
    }
    for page in spec.pages:
        files[f"definition/pages/{page.name}/page.json"] = _render_page(page)
        for visual in page.visuals:
            path = (
                f"definition/pages/{page.name}/visuals/"
                f"{visual.name}/visual.json"
            )
            files[path] = _render_visual(visual)
    if spec.theme:
        files[
            f"StaticResources/RegisteredResources/{spec.theme.name}.json"
        ] = json.dumps(spec.theme.to_theme_json(), indent=2)
    return ReportDefinition(files=files)


# ---------------------------------------------------------------------------
# Parsing (scoped + tolerant)
# ---------------------------------------------------------------------------


def _norm(path: str) -> str:
    return path.replace("\\", "/")


def _parse_field(field_json: dict[str, Any]) -> ReportField | None:
    for kind, wrapper in (("measure", "Measure"), ("column", "Column")):
        node = field_json.get(wrapper)
        if isinstance(node, dict):
            entity = (
                node.get("Expression", {}).get("SourceRef", {}).get("Entity", "")
            )
            prop = node.get("Property", "")
            if entity or prop:
                return ReportField(kind=kind, entity=entity, property=prop)  # type: ignore[arg-type]
    return None


def _parse_visual_title(visual_node: dict[str, Any]) -> str | None:
    objects = visual_node.get("visualContainerObjects", {}) or {}
    title_entries = objects.get("title")
    if isinstance(title_entries, list) and title_entries:
        props = title_entries[0].get("properties", {}) or {}
        literal = (
            props.get("text", {}).get("expr", {}).get("Literal", {}).get("Value")
        )
        if isinstance(literal, str):
            return literal.strip().strip("'").replace("''", "'")
    return None


def _parse_visual(payload: dict[str, Any]) -> ReportVisual:
    position = payload.get("position", {}) or {}
    visual_node = payload.get("visual", {}) or {}
    projections: dict[str, list[ReportField]] = {}
    query_state = (visual_node.get("query", {}) or {}).get("queryState", {}) or {}
    for role, role_node in query_state.items():
        fields: list[ReportField] = []
        for proj in (role_node or {}).get("projections", []) or []:
            fld = _parse_field(proj.get("field", {}) or {})
            if fld is not None:
                fields.append(fld)
        if fields:
            projections[role] = fields
    return ReportVisual(
        name=payload.get("name", "visual"),
        visual_type=visual_node.get("visualType", "table"),
        title=_parse_visual_title(visual_node),
        x=position.get("x", 0),
        y=position.get("y", 0),
        width=position.get("width", 320),
        height=position.get("height", 240),
        z=position.get("z", 0),
        projections=projections,
        formatting=visual_node.get("objects", {}) or {},
    )


def _parse_dataset_reference(pbir: dict[str, Any]) -> tuple[str | None, str | None]:
    ref = pbir.get("datasetReference", {}) or {}
    by_connection = ref.get("byConnection") or {}
    dataset_id = by_connection.get("pbiModelDatabaseName")
    if not dataset_id:
        conn = by_connection.get("connectionString")
        if isinstance(conn, str):
            for part in conn.split(";"):
                key, _, value = part.partition("=")
                if key.strip().lower() == "semanticmodelid" and value.strip():
                    dataset_id = value.strip()
                    break
    dataset_name = None
    by_path = ref.get("byPath") or {}
    path = by_path.get("path")
    if path:
        leaf = _norm(path).rstrip("/").split("/")[-1]
        dataset_name = leaf.replace(".SemanticModel", "")
    return dataset_id, dataset_name


def parse_report(files: dict[str, str], *, name: str | None = None) -> ReportSpec:
    """Parse a report definition (PBIR-Enhanced) into a scoped :class:`ReportSpec`.

    Tolerant of missing parts: a report with no parseable visuals still yields a
    valid (empty) spec rather than raising, so audits can report "no visuals".
    """
    norm_files = {_norm(p): t for p, t in files.items()}

    def _load(path: str) -> dict[str, Any]:
        text = norm_files.get(path)
        if not text:
            return {}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}

    # Dataset binding.
    dataset_id, dataset_name = _parse_dataset_reference(_load("definition.pbir"))

    # Theme.
    report_json = _load("definition/report.json")
    base_theme = (
        report_json.get("themeCollection", {}).get("baseTheme", {}).get("name")
        or "CY24SU10"
    )
    theme: ReportTheme | None = None
    for path, text in norm_files.items():
        if path.startswith("StaticResources/RegisteredResources/") and path.endswith(
            ".json"
        ):
            try:
                theme = ReportTheme.from_dict(json.loads(text))
            except json.JSONDecodeError:
                theme = None
            break

    # Pages + visuals.
    pages_meta = _load("definition/pages/pages.json")
    page_order: list[str] = list(pages_meta.get("pageOrder", []) or [])
    discovered_pages: dict[str, ReportPage] = {}
    page_visuals: dict[str, list[ReportVisual]] = {}

    for path, text in norm_files.items():
        if not path.startswith("definition/pages/"):
            continue
        rest = path[len("definition/pages/"):]
        parts = rest.split("/")
        if len(parts) == 2 and parts[1] == "page.json":
            page_payload = _load(path)
            page_name = parts[0]
            discovered_pages[page_name] = ReportPage(
                name=page_name,
                display_name=page_payload.get("displayName", page_name),
                width=page_payload.get("width", 1280),
                height=page_payload.get("height", 720),
            )
        elif len(parts) == 4 and parts[1] == "visuals" and parts[3] == "visual.json":
            page_name = parts[0]
            page_visuals.setdefault(page_name, []).append(_parse_visual(_load(path)))

    ordered_names = page_order or list(discovered_pages.keys())
    pages: list[ReportPage] = []
    for page_name in ordered_names:
        page = discovered_pages.get(page_name) or ReportPage(
            name=page_name, display_name=page_name
        )
        page.visuals = page_visuals.get(page_name, [])
        pages.append(page)

    return ReportSpec(
        name=name or "Report",
        pages=pages,
        theme=theme,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        base_theme=base_theme,
    )
