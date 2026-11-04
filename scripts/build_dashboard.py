"""Bundle the pinned Selark-Web frontend for Node-free Python installations."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"
DESTINATION = ROOT / "src" / "selark" / "dashboard_assets"


def bundle(*, check=False, skip_build=False):
    if not (FRONTEND / "package.json").is_file():
        raise SystemExit("Run git submodule update --init --recursive first.")
    if not skip_build:
        subprocess.run(["npm", "run", "build"], cwd=FRONTEND, check=True)
    dist = FRONTEND / "dist"
    vite = json.loads((dist / ".vite" / "manifest.json").read_text())
    entry = vite["index.html"]
    if not entry.get("isEntry") or not entry["file"].startswith("assets/"):
        raise SystemExit("Unexpected Vite entry point; review the frontend build.")
    files = {}
    for path in sorted(dist.rglob("*")):
        if path.is_dir() or ".vite" in path.relative_to(dist).parts:
            continue
        name = path.relative_to(dist).as_posix()
        if path.is_symlink() or not (
            name in {"index.html", "favicon.svg"}
            or (
                len(Path(name).parts) == 2
                and name.startswith("assets/")
                and path.suffix in {".js", ".css", ".svg", ".png", ".webp", ".jpg", ".ico"}
            )
        ):
            raise SystemExit(f"Unexpected public asset: {name}; review before packaging.")
        files[name] = path.read_bytes()
    required = {"index.html", "favicon.svg", entry["file"], *entry.get("css", [])}
    if not required <= files.keys():
        raise SystemExit("The Vite build is incomplete.")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=FRONTEND, text=True
    ).strip()
    manifest = {
        "repository": "https://github.com/nbcctwya/Selark-Web",
        "revision": revision,
        "files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()},
    }
    files["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    installed = {
        path.relative_to(DESTINATION).as_posix(): path.read_bytes()
        for path in DESTINATION.rglob("*")
        if path.is_file()
    }
    if check:
        if files != installed:
            raise SystemExit("Packaged Dashboard is stale. Run python scripts/build_dashboard.py.")
        print(f"Packaged Dashboard matches Selark-Web {revision[:12]} ({len(files) - 1} assets).")
        return
    for name in installed.keys() - files.keys():
        (DESTINATION / name).unlink()
    for name, content in files.items():
        target = DESTINATION / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    print(f"Packaged Selark-Web {revision[:12]} ({len(files) - 1} assets).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="Verify without changing packaged files"
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="Use an already verified dist build"
    )
    args = parser.parse_args()
    bundle(check=args.check, skip_build=args.skip_build)
