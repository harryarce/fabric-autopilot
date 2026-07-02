"""Intelligence layer for turning Fabric SQL schemas into semantic models.

This package contains the reusable building blocks that sit between the raw
schema extracted from a Fabric SQL endpoint and a deployable Power BI / Fabric
*semantic model* definition:

* :mod:`app.intelligence.spec` — a provider-agnostic intermediate
  representation (IR) of a semantic model plus a deterministic mapping from the
  SQL schema (:class:`app.sql_client.TableSchema`) into that IR.
* :mod:`app.intelligence.definition` — renders the IR into Fabric item
  definition *parts* (TMDL or TMSL ``model.bim`` + ``definition.pbism``),
  base64-encoded exactly the way the Fabric REST API expects.
* :mod:`app.intelligence.agent` — a thin, reusable wrapper around the Microsoft
  Agent Framework that builds a Foundry-backed agent, equips it with the
  ``semantic-model-builder`` skill, and can publish it as a *remote* (hosted)
  Foundry prompt agent.

The deterministic core (``spec`` + ``definition``) has no AI or network
dependencies, so it can be unit-tested and reused on its own. The agent layer is
optional and imported lazily.
"""

from __future__ import annotations

from .dax_generator import GeneratedMeasure, generate_dax_measure
from .definition import (
    DefinitionFormat,
    SemanticModelDefinition,
    build_definition,
)
from .nl_to_dax import DaxTranslation, NlToDaxError, nl_to_dax
from .report_builder import (
    VisualSuggestion,
    compose_report,
    deterministic_visual_suggestions,
    ground_visual_suggestions,
    suggest_report,
)
from .report_definition import (
    ReportDefinition,
    build_report_definition,
    parse_report,
)
from .report_design_agent import (
    ReportBuildOutcome,
    ReportDesignConfig,
    report_design_available,
    suggest_report_with_agent,
)
from .report_spec import (
    ReportField,
    ReportPage,
    ReportSpec,
    ReportTheme,
    ReportVisual,
)
from .spec import (
    SemanticColumn,
    SemanticMeasure,
    SemanticModelSpec,
    SemanticModelSuggestions,
    SemanticModelValidationIssue,
    SemanticModelValidationResult,
    SemanticRelationship,
    SemanticTable,
    SuggestedMeasure,
    SuggestedRelationship,
    apply_suggestions,
    dax_column_ref,
    dax_measure_ref,
    dax_qualified_column,
    dax_table_ref,
    dedupe_measure_names,
    direct_lake_incompatible_columns,
    drop_direct_lake_incompatible_columns,
    drop_unresolved_measures,
    resolvable_relationships,
    spec_from_schemas,
    suggest_from_schemas,
    suggest_measures,
    suggest_relationships,
    validate_semantic_model_spec,
)
from .suggestions import (
    SuggestionKind,
    SuggestionSpec,
    SuggestionStatus,
    apply_model_suggestion,
    apply_model_suggestions,
    apply_report_suggestion,
    apply_report_suggestions,
    suggestions_from_dict,
    suggestions_to_dict,
)
from .tmdl_parser import TmdlParseError, parse_semantic_model

__all__ = [
    "DefinitionFormat",
    "ReportBuildOutcome",
    "ReportDefinition",
    "ReportDesignConfig",
    "ReportField",
    "ReportPage",
    "ReportSpec",
    "ReportTheme",
    "ReportVisual",
    "VisualSuggestion",
    "SemanticColumn",
    "SemanticMeasure",
    "SemanticModelDefinition",
    "SemanticModelSpec",
    "SemanticModelSuggestions",
    "SemanticModelValidationIssue",
    "SemanticModelValidationResult",
    "SemanticRelationship",
    "SemanticTable",
    "SuggestedMeasure",
    "SuggestedRelationship",
    "SuggestionKind",
    "SuggestionSpec",
    "SuggestionStatus",
    "TmdlParseError",
    "GeneratedMeasure",
    "DaxTranslation",
    "NlToDaxError",
    "generate_dax_measure",
    "nl_to_dax",
    "apply_model_suggestion",
    "apply_model_suggestions",
    "apply_report_suggestion",
    "apply_report_suggestions",
    "apply_suggestions",
    "build_definition",
    "build_report_definition",
    "compose_report",
    "deterministic_visual_suggestions",
    "ground_visual_suggestions",
    "report_design_available",
    "suggest_report_with_agent",
    "suggestions_from_dict",
    "suggestions_to_dict",
    "dax_column_ref",
    "dax_measure_ref",
    "dax_qualified_column",
    "dax_table_ref",
    "dedupe_measure_names",
    "direct_lake_incompatible_columns",
    "drop_direct_lake_incompatible_columns",
    "drop_unresolved_measures",
    "parse_report",
    "parse_semantic_model",
    "resolvable_relationships",
    "spec_from_schemas",
    "suggest_from_schemas",
    "suggest_measures",
    "suggest_relationships",
    "suggest_report",
    "validate_semantic_model_spec",
]
