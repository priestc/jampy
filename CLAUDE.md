# Deployment

There are two machines running `takeloom`, installed differently:

- The laptop ("Framework"), reachable via `ssh framework` (non-interactive/key-auth works). Installed via `pipx` directly from this GitHub repo (`takeloom @ git+https://github.com/priestc/takeloom.git`) — not an editable clone, so picking up new code requires a reinstall, not a `git pull`.
- The Mac Mini — the studio hub machine with the Scarlett 4i4 attached (see `studio_hardware_scarlett_4i4` memory), and normally the machine Claude Code itself is already running on (`hostname` reports `Mac.local`). Installed via `pipx install --editable ~/Documents/GitHub/takeloom` — an editable install pointing straight at this working copy (confirm with `cat ~/.local/pipx/venvs/takeloom/lib/python*/site-packages/takeloom*.dist-info/direct_url.json`, should show `"editable": true`). Any commit — or even an uncommitted local edit — takes effect the next time `takeloom` is launched there; no reinstall step needed. (If a dependency is ever added/changed in `pyproject.toml`, that *does* still need `pipx install --editable --force ~/Documents/GitHub/takeloom` to pick up the new package.)

Any change **except code that only affects server mode** (i.e. changes confined to the headless `takeloom server` path — see `takeloom/__main__.py`'s `server_command` and anything only reachable from it) should be automatically deployed once committed:

1. Push to `origin` (`main`).
2. `ssh framework "pipx install --force git+https://github.com/priestc/takeloom.git"`.
3. Nothing further needed for the Mac Mini — just make sure `takeloom` (or `takeloom server`) is restarted there to pick up the change.
