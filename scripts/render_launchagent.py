"""Render a launchd agent for this checkout without installing or starting it."""

import plistlib
from pathlib import Path

root = Path(__file__).resolve().parents[1]
output = root / "local-data/com.recipeassistant.api.plist"
output.parent.mkdir(exist_ok=True, mode=0o700)
logs = root / "logs"
logs.mkdir(exist_ok=True, mode=0o700)
agent = {
    "Label": "com.recipeassistant.api",
    "ProgramArguments": [
        str(root / ".venv/bin/python"),
        "-m",
        "recipe_assistant",
        "serve",
        "--env-file",
        str(root / ".env"),
    ],
    "WorkingDirectory": str(root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ThrottleInterval": 30,
    "StandardOutPath": str(logs / "service.log"),
    "StandardErrorPath": str(logs / "service-error.log"),
    "Umask": 0o077,
}
with output.open("wb") as file:
    plistlib.dump(agent, file)
output.chmod(0o600)
print(f"Rendered {output}; not installed or started")
