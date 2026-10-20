"""Generate the HACS release's private copy of the independently packaged client."""

import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULES = (
    "__init__.py",
    "async_client.py",
    "cloud_protocol.py",
    "cloud_backend.py",
    "cloud_edits.py",
    "database.py",
    "models.py",
    "shopping.py",
    "sync_snapshot.py",
)
DESTINATION = ROOT / "custom_components/recipe_keeper/_vendor/recipe_keeper_client"


def bundle(check: bool = False) -> None:
    hashes = {}
    for name in MODULES:
        source = (ROOT / "src/recipe_assistant" / name).read_bytes()
        destination = DESTINATION / name
        hashes[name] = hashlib.sha256(source).hexdigest()
        if check:
            if not destination.exists() or destination.read_bytes() != source:
                raise SystemExit(
                    f"Bundled client differs: {name}; run scripts/bundle_client.py"
                )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source)
    manifest = (
        json.dumps({"version": "0.2.1", "sha256": hashes}, indent=2) + "\n"
    ).encode()
    target = DESTINATION.parent / "manifest.json"
    if check:
        if not target.exists() or target.read_bytes() != manifest:
            raise SystemExit("Bundled client manifest differs")
    else:
        target.write_bytes(manifest)
        (DESTINATION.parent / "__init__.py").write_text(
            '"""Generated client bundle; edit canonical source, then regenerate."""\n'
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    bundle(parser.parse_args().check)
