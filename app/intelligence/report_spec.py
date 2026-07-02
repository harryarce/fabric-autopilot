"""Provider-agnostic *report* intermediate representation (IR).

Mirrors :mod:`app.intelligence.spec` (which models semantic models) but for
Power BI **reports**. A :class:`ReportSpec` is a small, serialisable description
of a report's pages, visuals, field bindings and theme — deliberately *scoped*
(decision 1A) to the parts we actually reason about: visual types, field
encodings and formatting/theme properties. It is **not** a full-fidelity PBIR
model.

The IR is the hand-off point between three concerns, exactly like the
semantic-model IR:

* deterministic construction — :mod:`report_definition` renders a spec to a
  Fabric report definition (PBIR), and parses one back for auditing;
* AI enrichment — the report-build flow grounds suggested visuals in a real
  :class:`~app.intelligence.spec.SemanticModelSpec`, emitting/patching this IR;
* auditing — the formatting/WCAG auditor inspects this IR, never raw JSON.

Everything here is deterministic and dependency-free.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

# Default report canvas (Power BI 16:9 page).
DEFAULT_PAGE_WIDTH = 1280
DEFAULT_PAGE_HEIGHT = 720

# Visual types we generate / understand. The string values are the Power BI
# ``visualType`` identifiers used verbatim in PBIR ``visual.json``.
VISUAL_TYPES = {
    "table",
    "tableEx",
    "matrix",
    "card",
    "multiRowCard",
    "columnChart",
    "clusteredColumnChart",
    "barChart",
    "clusteredBarChart",
    "lineChart",
    "pieChart",
    "donutChart",
    "slicer",
    "kpi",
    "gauge",
}

FieldKind = Literal["column", "measure"]


@dataclass
class ReportField:
    """A reference to one model column or measure, bound to a visual role."""

    kind: FieldKind
    entity: str  # the table (entity) name in the semantic model
    property: str  # the column or measure name

    @property
    def query_ref(self) -> str:
        return f"{self.entity}.{self.property}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "entity": self.entity, "property": self.property}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportField":
        return cls(
            kind=data.get("kind", "column"),  # type: ignore[arg-type]
            entity=data["entity"],
            property=data["property"],
        )


@dataclass
class ReportVisual:
    """A single visual on a page: its type, placement, fields and formatting."""

    name: str  # stable id (also the folder name in PBIR)
    visual_type: str
    title: str | None = None
    x: float = 0
    y: float = 0
    width: float = 320
    height: float = 240
    z: int = 0
    # role -> fields, e.g. {"Values": [...], "Category": [...]}. Role names are
    # the Power BI data-role names for the chosen visual type.
    projections: dict[str, list[ReportField]] = field(default_factory=dict)
    # Free-form formatting objects (kept scoped; passed through to PBIR
    # ``visual.objects``). Example: {"general": {...}}.
    formatting: dict[str, Any] = field(default_factory=dict)

    def all_fields(self) -> list[ReportField]:
        return [f for fields in self.projections.values() for f in fields]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "visual_type": self.visual_type,
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "z": self.z,
            "projections": {
                role: [f.to_dict() for f in fields]
                for role, fields in self.projections.items()
            },
        }
        if self.title:
            data["title"] = self.title
        if self.formatting:
            data["formatting"] = self.formatting
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportVisual":
        return cls(
            name=data["name"],
            visual_type=data.get("visual_type", "table"),
            title=data.get("title"),
            x=data.get("x", 0),
            y=data.get("y", 0),
            width=data.get("width", 320),
            height=data.get("height", 240),
            z=data.get("z", 0),
            projections={
                role: [ReportField.from_dict(f) for f in fields]
                for role, fields in (data.get("projections") or {}).items()
            },
            formatting=data.get("formatting", {}) or {},
        )


@dataclass
class ReportPage:
    """A report page (canvas) holding a set of visuals."""

    name: str  # stable id (folder name in PBIR)
    display_name: str
    width: int = DEFAULT_PAGE_WIDTH
    height: int = DEFAULT_PAGE_HEIGHT
    visuals: list[ReportVisual] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "width": self.width,
            "height": self.height,
            "visuals": [v.to_dict() for v in self.visuals],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportPage":
        return cls(
            name=data["name"],
            display_name=data.get("display_name", data["name"]),
            width=data.get("width", DEFAULT_PAGE_WIDTH),
            height=data.get("height", DEFAULT_PAGE_HEIGHT),
            visuals=[ReportVisual.from_dict(v) for v in data.get("visuals", [])],
        )


@dataclass
class ReportTheme:
    """A scoped report theme (the bits relevant to formatting/WCAG audits)."""

    name: str = "FabricDevAI"
    # Ordered data colours (hex). Drives series palette + contrast checks.
    data_colors: list[str] = field(default_factory=list)
    background: str | None = None
    foreground: str | None = None
    table_accent: str | None = None

    def to_theme_json(self) -> dict[str, Any]:
        """Render the Power BI theme JSON object (registered resource)."""
        theme: dict[str, Any] = {"name": self.name}
        if self.data_colors:
            theme["dataColors"] = list(self.data_colors)
        if self.background:
            theme["background"] = self.background
        if self.foreground:
            theme["foreground"] = self.foreground
        if self.table_accent:
            theme["tableAccent"] = self.table_accent
        return theme

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v not in (None, [], "")}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportTheme":
        return cls(
            name=data.get("name", "FabricDevAI"),
            data_colors=list(data.get("data_colors", []) or data.get("dataColors", [])),
            background=data.get("background"),
            foreground=data.get("foreground"),
            table_accent=data.get("table_accent") or data.get("tableAccent"),
        )


@dataclass
class ReportSpec:
    """The complete, format-agnostic description of a report."""

    name: str
    pages: list[ReportPage] = field(default_factory=list)
    theme: ReportTheme | None = None
    # Binding to the semantic model this report is built on. ``dataset_id`` is
    # the Fabric semantic-model item id (preferred for same-workspace binding);
    # ``dataset_name`` is used for a relative ``byPath`` reference in a PBIP.
    dataset_id: str | None = None
    dataset_name: str | None = None
    description: str | None = None
    base_theme: str = "CY24SU10"  # built-in Power BI base theme

    def page(self, name: str) -> ReportPage | None:
        return next((p for p in self.pages if p.name == name), None)

    def all_visuals(self) -> list[ReportVisual]:
        return [v for page in self.pages for v in page.visuals]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": self.name,
            "base_theme": self.base_theme,
            "pages": [p.to_dict() for p in self.pages],
        }
        if self.theme:
            data["theme"] = self.theme.to_dict()
        if self.dataset_id:
            data["dataset_id"] = self.dataset_id
        if self.dataset_name:
            data["dataset_name"] = self.dataset_name
        if self.description:
            data["description"] = self.description
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReportSpec":
        return cls(
            name=data["name"],
            pages=[ReportPage.from_dict(p) for p in data.get("pages", [])],
            theme=ReportTheme.from_dict(data["theme"]) if data.get("theme") else None,
            dataset_id=data.get("dataset_id"),
            dataset_name=data.get("dataset_name"),
            description=data.get("description"),
            base_theme=data.get("base_theme", "CY24SU10"),
        )
