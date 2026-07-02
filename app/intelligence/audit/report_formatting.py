"""Report **formatting & accessibility** audit.

Implements the "Power BI Report Formatting / WCAG" requirement: deterministic
checks over a :class:`~app.intelligence.report_spec.ReportSpec` covering layout
hygiene (titles, overlap, off-canvas, clutter), binding completeness, and WCAG
colour-contrast of the report theme.

Contrast is computed with the WCAG 2.1 relative-luminance formula; no rendering
engine is required. Pure function of a ``ReportSpec``.
"""

from __future__ import annotations

from ..report_spec import ReportSpec, ReportTheme, ReportVisual
from .base import INFO, WARNING, AuditReport

FEATURE = "report-formatting"

# WCAG 2.1 thresholds.
_TEXT_CONTRAST_MIN = 4.5  # normal text (AA)
_GRAPHIC_CONTRAST_MIN = 3.0  # large text / non-text graphical objects (AA)

# Visual types that need at least one measure/value to be meaningful.
_VALUE_VISUALS = {"card", "kpi", "gauge", "multiRowCard"}
# Chart visuals that need both a category and a value.
_CATEGORY_VALUE_VISUALS = {
    "columnChart",
    "clusteredColumnChart",
    "barChart",
    "clusteredBarChart",
    "lineChart",
    "pieChart",
    "donutChart",
}
_VALUE_ROLES = ("Values", "Y", "Y2", "Value")
_CATEGORY_ROLES = ("Category", "Axis", "X", "Group", "Legend")
_MAX_VISUALS_PER_PAGE = 20


def _parse_hex(color: str) -> tuple[int, int, int] | None:
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(ch * 2 for ch in value)
    if len(value) != 6:
        return None
    try:
        return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    except ValueError:
        return None


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(color_a: str, color_b: str) -> float | None:
    """WCAG contrast ratio between two hex colours, or ``None`` if unparseable."""
    rgb_a, rgb_b = _parse_hex(color_a), _parse_hex(color_b)
    if rgb_a is None or rgb_b is None:
        return None
    lum_a, lum_b = _relative_luminance(rgb_a), _relative_luminance(rgb_b)
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _overlaps(a: ReportVisual, b: ReportVisual) -> bool:
    return not (
        a.x + a.width <= b.x
        or b.x + b.width <= a.x
        or a.y + a.height <= b.y
        or b.y + b.height <= a.y
    )


def audit(spec: ReportSpec) -> AuditReport:
    """Run the formatting/accessibility rule set over ``spec``."""
    report = AuditReport(feature=FEATURE, target=spec.name)

    _audit_theme(report, spec.theme)

    if not spec.pages:
        report.add(
            WARNING,
            "RPT_FMT_NO_PAGES",
            "The report has no pages.",
            recommendation="Add at least one page with visuals.",
        )

    for page in spec.pages:
        if not page.visuals:
            report.add(
                INFO,
                "RPT_FMT_EMPTY_PAGE",
                f"Page '{page.display_name}' has no visuals.",
                object_ref=page.name,
            )
            continue

        if len(page.visuals) > _MAX_VISUALS_PER_PAGE:
            report.add(
                INFO,
                "RPT_FMT_PAGE_CLUTTER",
                f"Page '{page.display_name}' has {len(page.visuals)} visuals.",
                object_ref=page.name,
                recommendation="Split dense pages; many visuals hurt readability "
                "and performance.",
            )

        for visual in page.visuals:
            _audit_visual(report, page.name, page.width, page.height, visual)

        # Overlap detection (pairwise within a page).
        visuals = page.visuals
        for i in range(len(visuals)):
            for j in range(i + 1, len(visuals)):
                if _overlaps(visuals[i], visuals[j]):
                    report.add(
                        WARNING,
                        "RPT_FMT_OVERLAP",
                        f"Visuals '{visuals[i].name}' and '{visuals[j].name}' "
                        f"overlap on page '{page.display_name}'.",
                        object_ref=page.name,
                        recommendation="Separate overlapping visuals so content "
                        "is not hidden.",
                    )

    return report


def _audit_visual(
    report: AuditReport,
    page_name: str,
    page_width: int,
    page_height: int,
    visual: ReportVisual,
) -> None:
    ref = f"{page_name}/{visual.name}"

    if not visual.title:
        report.add(
            WARNING,
            "RPT_FMT_VISUAL_NO_TITLE",
            f"Visual '{visual.name}' has no title.",
            object_ref=ref,
            recommendation="Add a descriptive title; screen readers and authors "
            "rely on it (WCAG 1.3.1).",
        )

    if visual.x < 0 or visual.y < 0 or (
        visual.x + visual.width > page_width or visual.y + visual.height > page_height
    ):
        report.add(
            WARNING,
            "RPT_FMT_OFF_CANVAS",
            f"Visual '{visual.name}' extends outside the page canvas.",
            object_ref=ref,
            recommendation="Reposition/resize the visual to fit within the page.",
        )

    has_value = any(visual.projections.get(role) for role in _VALUE_ROLES)
    has_category = any(visual.projections.get(role) for role in _CATEGORY_ROLES)

    if visual.visual_type in _VALUE_VISUALS and not has_value:
        report.add(
            WARNING,
            "RPT_FMT_VISUAL_NO_VALUE",
            f"{visual.visual_type} '{visual.name}' has no value field.",
            object_ref=ref,
            recommendation="Bind a measure to the visual's value role.",
        )
    if visual.visual_type in _CATEGORY_VALUE_VISUALS and not (
        has_value and has_category
    ):
        report.add(
            WARNING,
            "RPT_FMT_CHART_INCOMPLETE",
            f"Chart '{visual.name}' is missing a category or value field.",
            object_ref=ref,
            recommendation="A chart needs both an axis/category and a value.",
        )


def _audit_theme(report: AuditReport, theme: ReportTheme | None) -> None:
    if theme is None:
        report.add(
            INFO,
            "RPT_FMT_NO_CUSTOM_THEME",
            "The report uses only the base theme.",
            recommendation="Apply an accessible, brand-aligned theme with "
            "sufficient colour contrast.",
        )
        return

    background = theme.background
    if background and theme.foreground:
        ratio = contrast_ratio(theme.foreground, background)
        if ratio is not None and ratio < _TEXT_CONTRAST_MIN:
            report.add(
                WARNING,
                "RPT_FMT_TEXT_CONTRAST",
                f"Foreground/background contrast is {ratio:.1f}:1 "
                f"(WCAG AA requires {_TEXT_CONTRAST_MIN}:1).",
                object_ref=theme.name,
                recommendation="Increase text/background contrast.",
            )

    if background:
        for idx, color in enumerate(theme.data_colors):
            ratio = contrast_ratio(color, background)
            if ratio is not None and ratio < _GRAPHIC_CONTRAST_MIN:
                report.add(
                    WARNING,
                    "RPT_FMT_DATACOLOR_CONTRAST",
                    f"Data colour #{idx + 1} ({color}) has "
                    f"{ratio:.1f}:1 contrast against the background.",
                    object_ref=theme.name,
                    recommendation="Use data colours with at least "
                    f"{_GRAPHIC_CONTRAST_MIN}:1 contrast (WCAG 1.4.11).",
                )
