# Deploy to the MacBook

Run the service as the macOS user who uses Recipe Keeper. Install Recipe Keeper,
sign in, and let the app sync before starting the bridge. The bridge does not
perform Recipe Keeper's cloud sync or keep the app syncing in the background.

1. Copy this source folder or clone the eventual repository onto the MacBook.
   Do not copy `.venv`, `.env`, logs, or your other Mac's database.
2. Install uv and run `uv sync --locked` in the checkout.
3. Run `uv run recipe-assistant init` to generate a token unique to that Mac.
4. Run `uv run recipe-assistant inspect`. Resolve macOS permissions before
   continuing. The default database path uses that Mac's current home folder.
5. Run `uv run recipe-assistant serve` and test `/docs` and `/v1/status`.

## Reach it from trappserv.er

The default listener accepts connections on the Mac only. For an initial test,
run an SSH tunnel from trappserv.er, replacing the MacBook name and username:

```sh
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 macuser@macbook-hostname
```

Clients on trappserv.er can then use `http://127.0.0.1:8765` with the bearer token.
If Home Assistant runs in a container or VM, its `127.0.0.1` is usually a
different network namespace; arrange a reachable tunnel endpoint or reverse
proxy for that runtime.

For a direct homelab connection, set `RECIPE_ASSISTANT_HOST` in the Mac's `.env`
to its LAN or private VPN IP and restart the bridge. Use the Mac's address from
Home Assistant. The token authenticates requests but plain HTTP does not encrypt
it: use an encrypted private network, SSH tunnel, or HTTPS reverse proxy for
ongoing use. Keep this local service off public port forwards.

## Start at login with launchd

After interactive verification, render a plist using this checkout's paths:

```sh
uv run python scripts/render_launchagent.py
mkdir -p ~/Library/LaunchAgents
cp local-data/com.recipeassistant.api.plist ~/Library/LaunchAgents/
launchctl bootstrap "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.recipeassistant.api.plist
```

The renderer only writes a local file. Installation and startup happen in the
last two commands. This is a user LaunchAgent: it starts at login and runs while
that user session exists. It is not a pre-login system daemon. Source paths and
the virtual environment must stay in place. Do not use `sudo` to run the bridge.

Logs are in `logs/service.log` and `logs/service-error.log`. If launchd cannot
read the protected database, review Full Disk Access for its Python process.
An open lid is not required if your MacBook's supported power configuration
keeps it awake; an asleep Mac will not serve requests.

To stop or unload:

```sh
launchctl bootout "gui/$(id -u)" \
  ~/Library/LaunchAgents/com.recipeassistant.api.plist
```

After changing `.env` or code, restart:

```sh
launchctl kickstart -k "gui/$(id -u)/com.recipeassistant.api"
```

## Updates

Stop the agent, update source, run `uv sync --locked`, run tests and `inspect`,
then bootstrap it again. Regenerate the plist if the checkout moves. Rerun the
renderer on the destination Mac; a plist generated on another Mac contains
that machine's absolute paths.
