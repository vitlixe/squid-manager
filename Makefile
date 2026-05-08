.PHONY: test check run

PYTHON ?= $(shell [ -x .venv/bin/python3 ] && echo .venv/bin/python3 || echo python3)

test:
	$(PYTHON) -m unittest discover squid-admin

check: test
	git diff --check

run:
	SQUID_ADMIN_LOGIN=admin \
	SQUID_ADMIN_PASSWORD=dev \
	SQUID_ADMIN_HOST=127.0.0.1 \
	SQUID_ADMIN_PORT=5000 \
	SQUID_ADMIN_USERS_FILE=squid-admin/users.json \
	SQUID_PASSWD=/tmp/squid-passwd-dev \
	$(PYTHON) squid-admin/app.py
