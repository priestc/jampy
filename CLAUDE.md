# Deployment

The laptop ("Framework") is reachable via `ssh framework` (non-interactive/key-auth works). `takeloom` there is installed via `pipx` directly from this GitHub repo (`takeloom @ git+https://github.com/priestc/takeloom.git`) — not an editable clone, so picking up new code requires a reinstall, not a `git pull`.

Any change **except code that only affects server mode** (i.e. changes confined to the headless `takeloom server` path — see `takeloom/__main__.py`'s `server_command` and anything only reachable from it) should be automatically deployed to the laptop once committed:

1. Push to `origin` (`main`).
2. `ssh framework "pipx install --force git+https://github.com/priestc/takeloom.git"`.
