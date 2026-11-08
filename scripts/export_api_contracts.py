"""Export or check public OpenAPI contracts without running a server.

python -m scripts.export_api_contracts [--check] [--frontend]
"""

import argparse
import json
from pathlib import Path

from selark.contracts.openapi import dashboard_document, gateway_document

ROOT = Path(__file__).resolve().parents[1]


def documents():
    return {
        "dashboard.openapi.json": dashboard_document(),
        "gateway.openapi.json": gateway_document(),
    }


def render(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def contract_shape(value):
    """The independently pinned UI may retain older explanatory descriptions.

    Every field, type, required constraint, route and example must still match.
    Keep an API property named 'description' (its value is a schema, not prose).
    """
    if isinstance(value, dict):
        return {
            key: item
            if key in {"example", "examples", "default", "const", "enum"}
            else contract_shape(item)
            for key, item in value.items()
            if key != "description" or not isinstance(item, str)
        }
    if isinstance(value, list):
        return [contract_shape(item) for item in value]
    return value


def export(*, check=False, frontend=False):
    paths = []
    for name, value in documents().items():
        paths.append((ROOT / "docs/api" / name, render(value)))
        if frontend and name == "dashboard.openapi.json":
            paths.append((ROOT / "frontend/contracts" / name, render(value)))
    for path, content in paths:
        if check:
            current = path.read_text() if path.is_file() else None
            matches = current == content
            if current is not None and path.is_relative_to(ROOT / "frontend"):
                matches = contract_shape(json.loads(current)) == contract_shape(json.loads(content))
            if not matches:
                raise ValueError(f"Outdated contract: {path.relative_to(ROOT)}; rerun exporter")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    return [str(path.relative_to(ROOT)) for path, _ in paths]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--frontend", action="store_true", help="Also sync/check the submodule copy"
    )
    args = parser.parse_args()
    if args.frontend and not (ROOT / "frontend/package.json").is_file():
        parser.error("Initialize the frontend: git submodule update --init --recursive")
    for path in export(check=args.check, frontend=args.frontend):
        print(f"{'Checked' if args.check else 'Exported'} {path}")


if __name__ == "__main__":
    main()
