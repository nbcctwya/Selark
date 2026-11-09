"""Export the public Dashboard contract from an isolated, synthetic local ledger.

Run with: python -m scripts.export_dashboard_fixtures
Never reads a personal profile or makes provider requests.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.verify_dashboard import seed
from selark import dashboard_data as data
from selark.dashboard_api import service as management
from selark.plans import parse_quota
from selark.runtime import Runtime
from selark.storage import utc_now


def export_fixture():
    with tempfile.TemporaryDirectory(prefix="selark-frontend-fixture-") as temporary:
        directory = Path(temporary) / "profile"
        with patch.dict(os.environ, {"SELARK_DATA_DIR": str(directory / "data")}):
            seed(directory, count=65)
            with Runtime(directory) as runtime:
                runtime.add_connection(
                    "openai-demo", "openai", "fixture-ui-only-key", name="OpenAI Demo"
                )
                for config in runtime.connection_settings():
                    models = list(config.allowed_models)
                    if config.connection_id == "openai-demo":
                        models = ["demo-text", "demo-text-small"]
                    elif config.connection_id == "mimo-default":
                        models.append("demo-unapproved")
                    runtime.save_discovery(config, models)
                    if config.resource_type == "token_plan":
                        runtime.save_plan_status(
                            config,
                            parse_quota({"usage": {"limit": 1000, "used": 900, "remaining": 100}}),
                            utc_now(),
                        )
                runtime.change_models("openai-demo", "enable", ["demo-text"])
                runtime.change_models("openai-demo", "default", default="demo-text")
                # Exercise a finite quota and unknown-consumption state in the UI.
                caller = next(t for t in runtime.store.tokens() if t["name"] == "Cursor Workspace")
                runtime.change_policy(caller["id"], {"daily_token_limit": 500000})
                accounting = {
                    row["request_id"]: {
                        "quota_tokens": row["resources"]["quota_tokens"],
                        "token_basis": row["resources"]["token_basis"],
                    }
                    for row in runtime.store.records({}, limit=1000)
                }
            filters = data.query_filters({})
            snapshot = data.snapshot(directory, filters, offline=True)
            snapshot["capabilities"] = {"model_approval": True}
            with Runtime(directory, readonly=True) as runtime:
                managed_connections = management.connections(runtime)["items"]
                managed_applications = management.applications(runtime)["items"]
                management_data = {
                    "capabilities": {
                        "connection_management": True,
                        "model_approval": True,
                        "model_discovery": True,
                        "application_management": True,
                    },
                    "providers": management.providers()["items"],
                    "connections": managed_connections,
                    "models": {
                        item["id"]: management.models(runtime, item["id"])
                        for item in managed_connections
                    },
                    "applications": managed_applications,
                    "quotas": {
                        item["id"]: management.application_quota(runtime, item["id"])
                        for item in managed_applications
                    },
                }
            records = []
            for offset in range(0, 65, 25):
                records.extend(data.records(directory, filters, offset=offset)["items"])
            result = {
                "fixture_only": True,
                "contract": "selark-dashboard-v2",
                "reference_time": snapshot["checked_at"],
                "snapshot": snapshot,
                "records": records,
                "accounting": accounting,
                "logs": data.log_events(directory),
                "backups": data.backup_inventory(directory),
                "management": management_data,
            }
            # Stable synthetic caller IDs make component fixtures easier to discuss.
            encoded = json.dumps(result, ensure_ascii=False, indent=2)
            for index, application in enumerate(snapshot["applications"]):
                if application["id"] != "legacy":
                    encoded = encoded.replace(application["id"], f"demo-app-{index}")
            for forbidden in (
                "fixture-only-key",
                "fixture-ui-only-key",
                "private-error",
                temporary,
            ):
                if forbidden in encoded:
                    raise ValueError("Private fixture data appeared in the public projection.")
            return json.loads(encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("frontend/src/mocks/fixture.json"),
    )
    args = parser.parse_args()
    if args.output == Path("frontend/src/mocks/fixture.json") and not args.output.parent.is_dir():
        parser.error("Initialize the frontend first: git submodule update --init --recursive")
    fixture = export_fixture()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n")
    print(f"Exported {len(fixture['records'])} synthetic records to {args.output}")


if __name__ == "__main__":
    main()
