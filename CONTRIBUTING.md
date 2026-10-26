# Contributing

## Requirements

- Python 3.9+ and pip 25.1+ (for `pip install --group`)
- A Debian/Ubuntu test server or VM to try the installer and the `.deb`

## Local setup

```bash
git clone https://github.com/vitlixe/squid-manager.git
cd squid-manager
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install --group dev
```

Start the panel locally with `admin / admin`:

```bash
SQUID_MANAGER_LOGIN=admin SQUID_MANAGER_PASSWORD=admin SQUID_MANAGER_PORT=5001 \
SQUID_MANAGER_USERS_FILE=panel/users.json SQUID_MANAGER_PASSWD_FILE=/tmp/squid-passwd-dev \
.venv/bin/python panel/app.py
```

**Reload Squid** fails locally because it needs the server's sudo rules.

## Development workflow

Run the same checks as CI:

```bash
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/mypy && .venv/bin/pytest
```

Use `.venv/bin/ruff format .` to format the code.

CI also builds `squid-manager.deb` and `squid-manager.tar.gz`, installs each on
fresh Ubuntu 22.04 and 24.04 machines, and runs `tests/smoke.sh` against the
installed panel and proxy. Debian 12 is not covered by CI; check it on a test VM
before a release. To try the installer yourself, run `sudo ./install.sh` from a clone on a
test server.

## Making changes

1. Create a branch from `main`.
2. Add or update tests where applicable.
3. Run the checks above; they must pass.
4. Keep install logic in `packaging/setup.sh`: both `install.sh` and the `.deb`
   postinst run it.
5. Open a PR with a clear description of what and why.

## Releases

To publish a release, update `VERSION`, commit, and push a matching tag:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The tag must match the version, otherwise the release fails. After the checks
and install tests pass, CI publishes a GitHub release with `squid-manager.deb`,
`squid-manager.tar.gz`, and `checksums.sha256`.
