#!/usr/bin/env python3
"""Generate a Fabric semantic-model definition from a spec JSON file.

This is the executable side of the ``semantic-model-builder`` skill. It is a
thin CLI over the **shared, reusable** engine in
``app/intelligence/{spec,definition}.py`` — no modeling logic is duplicated
here, which keeps the deterministic core in one place.

Usage
-----
    python build_semantic_model.py --spec spec.json --format TMDL --out out_dir
    python build_semantic_model.py --spec spec.json --print-parts

The ``--spec`` JSON must conform to ``references/spec-schema.md``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _ensure_package_on_path() -> None:
    """Add the workspace root to ``sys.path`` so ``app.intelligence`` imports.

    The script lives at
    ``fabric_api/agents/skills/semantic-model-builder/scripts/`` so the project
    root is five levels up.
    """
    here = Path(__file__).resolve()
    project_root = here.parents[5]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))


def main(argv: list[str] | None = None) -> int:
    _ensure_package_on_path()
    # Imported after the path fix so the shared engine is reused, not copied.
    from app.intelligence.definition import DefinitionFormat, build_definition
    from app.intelligence.spec import SemanticModelSpec

    parser = argparse.ArgumentParser(description="Build a Fabric semantic-model definition.")
    parser.add_argument("--spec", required=True, help="Path to the spec JSON file.")
    parser.add_argument(
        "--format",
        default="TMDL",
        choices=[f.value for f in DefinitionFormat],
        help="Definition format (default: TMDL).",
    )
    parser.add_argument("--out", help="Output directory for the definition files.")
    parser.add_argument(
        "--print-parts",
        action="store_true",
        help="Print the base64 Fabric REST parts JSON to stdout instead of writing files.",
    )
    args = parser.parse_args(argv)

    spec_path = Path(args.spec)
    if not spec_path.is_file():
        parser.error(f"Spec file not found: {spec_path}")

    spec = SemanticModelSpec.from_dict(json.loads(spec_path.read_text(encoding="utf-8")))
    definition = build_definition(spec, DefinitionFormat(args.format))

    if args.print_parts or not args.out:
        json.dump(definition.definition_payload(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    out_dir = Path(args.out)
    for rel_path, text in definition.files.items():
        target = out_dir / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    parts_file = out_dir / "definition.parts.json"
    parts_file.write_text(
        json.dumps(definition.definition_payload(), indent=2), encoding="utf-8"
    )

    summary = {
        "model": spec.name,
        "format": definition.format.value,
        "tables": len(spec.tables),
        "relationships": len(spec.relationships),
        "measures": sum(len(t.measures) for t in spec.tables),
        "files": sorted(definition.files.keys()),
        "output_dir": str(out_dir),
    }
    json.dump(summary, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
