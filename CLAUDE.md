# Deployment

There are two machines running `takeloom`, both installed via `pipx` directly from this GitHub repo (`takeloom @ git+https://github.com/priestc/takeloom.git`) — not an editable clone, so picking up new code requires a reinstall, not a `git pull`, on each one independently:

- The laptop ("Framework"), reachable via `ssh framework` (non-interactive/key-auth works).
- The Mac Mini — the studio hub machine with the Scarlett 4i4 attached (see `studio_hardware_scarlett_4i4` memory). This is normally the machine Claude Code itself is already running on (`hostname` reports `Mac.local`), so its reinstall runs locally, no ssh needed.

Any change **except code that only affects server mode** (i.e. changes confined to the headless `takeloom server` path — see `takeloom/__main__.py`'s `server_command` and anything only reachable from it) should be automatically deployed to both machines once committed:

1. Push to `origin` (`main`).
2. `ssh framework "pipx install --force git+https://github.com/priestc/takeloom.git"`.
3. `pipx install --force git+https://github.com/priestc/takeloom.git` (local — the Mac Mini).
