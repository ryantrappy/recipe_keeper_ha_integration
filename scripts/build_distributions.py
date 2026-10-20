"""Build an offline-installable HA ZIP and the separately usable Python client."""

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from bundle_client import MODULES, ROOT, bundle


def build() -> None:
    bundle(check=True)
    component = ROOT / "custom_components/recipe_keeper"
    version = json.loads((component / "manifest.json").read_text())["version"]
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    archive = destination / f"recipe_keeper-{version}.zip"
    with ZipFile(archive, "w", ZIP_DEFLATED) as output:
        for source in sorted(component.rglob("*")):
            if (
                source.is_file()
                and "__pycache__" not in source.parts
                and source.suffix in {".py", ".json", ".yaml", ".js", ".png"}
            ):
                output.write(source, source.relative_to(ROOT))
    with tempfile.TemporaryDirectory(prefix="recipe-keeper-client-") as temp:
        project = Path(temp)
        shutil.copyfile(ROOT / "client/pyproject.toml", project / "pyproject.toml")
        package = project / "src/recipe_keeper_client"
        package.mkdir(parents=True)
        for name in MODULES:
            shutil.copyfile(ROOT / "src/recipe_assistant" / name, package / name)
        subprocess.run(
            ["uv", "build", str(project), "--out-dir", str(destination)], check=True
        )
    print(f"HA installation archive: {archive}")
    print(f"Independent client distributions: {destination}")


if __name__ == "__main__":
    build()
