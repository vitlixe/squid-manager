#!/usr/bin/env bash
#
# End-to-end check of an installed Squid Manager: services, panel API, and the
# proxy itself. CI runs it as root on a fresh Ubuntu machine after installing
# the .deb or the .tar.gz release.

set -euo pipefail

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

# Prints the HTTP status of a request; 000 when the connection fails.
status() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 20 "$@" || true
}

json_field() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

systemctl is-active --quiet squid || fail "squid is not running"
systemctl is-active --quiet squid-manager || fail "squid-manager is not running"
squid -k parse > /dev/null 2>&1 || fail "squid -k parse rejected the configuration"
[[ -s /opt/squid-manager/VERSION ]] || fail "/opt/squid-manager/VERSION is missing"
main_pid="$(systemctl show -p MainPID --value squid-manager)"
tr '\0' ' ' < "/proc/${main_pid}/cmdline" | grep -q 'gunicorn .*app:app' \
  || fail "the panel does not run under gunicorn"
[[ "$(systemctl show -p ProtectSystem --value squid-manager)" == "yes" ]] \
  || fail "the panel service runs without systemd sandboxing"

# shellcheck disable=SC1091
source /etc/squid-manager/squid-manager.env
panel="http://127.0.0.1:${SQUID_MANAGER_PORT}"
admin="${SQUID_MANAGER_LOGIN}:${SQUID_MANAGER_PASSWORD}"
csrf="X-Requested-With: squid-manager"

for _ in $(seq 1 30); do
  [[ "$(status -u "${admin}" "${panel}/api/users")" == "200" ]] && break
  sleep 1
done
[[ "$(status -u "${admin}" "${panel}/api/users")" == "200" ]] || fail "panel API is not reachable"
[[ "$(status "${panel}/api/users")" == "401" ]] || fail "panel API answers without admin credentials"
# Read output into variables before grep: with pipefail, "cmd | grep -q" fails
# whenever grep exits early and cmd gets SIGPIPE.
headers="$(curl -fsS -D - -o /dev/null "${panel}/")"
grep -qi '^X-Frame-Options: DENY' <<< "${headers}" || fail "the panel page can be framed"
page="$(curl -fsS "${panel}/")"
if grep -q 'https\?://' <<< "${page}"; then
  fail "the panel page loads external resources"
fi
[[ "$(status "${panel}/static/vendor/bootstrap/bootstrap.min.css")" == "200" ]] \
  || fail "bundled Bootstrap is not served"
[[ "$(status -u "${admin}" -X POST "${panel}/api/reload_squid")" == "403" ]] \
  || fail "panel accepts a changing request without the CSRF header"

user="$(curl -fsS -u "${admin}" -H "${csrf}" -H 'Content-Type: application/json' \
  -d '{"first_name": "Smoke", "last_name": "Test"}' "${panel}/api/users")"
login="$(json_field login <<< "${user}")"
password="$(json_field password <<< "${user}")"

reload_squid() {
  local reply
  reply="$(curl -sS -u "${admin}" -H "${csrf}" -X POST "${panel}/api/reload_squid")"
  [[ "$(json_field status <<< "${reply}")" == "ok" ]] || fail "Reload Squid failed: ${reply}"
}
status_field() {
  curl -fsS -u "${admin}" "${panel}/api/status" | json_field "$1"
}
[[ "$(status_field pending_changes)" == "True" ]] || fail "a new user is not reported as pending"
reload_squid
[[ "$(status_field pending_changes)" == "False" ]] || fail "changes are still pending after Reload Squid"
[[ "$(status_field version)" == "$(cat /opt/squid-manager/VERSION)" ]] || fail "the panel reports a wrong version"
[[ "$(stat -c '%a %U:%G' /etc/squid/passwd)" == "640 root:proxy" ]] \
  || fail "/etc/squid/passwd is $(stat -c '%a %U:%G' /etc/squid/passwd), expected 640 root:proxy"

# Use the machine's own address, not 127.0.0.1: the distribution squid.conf
# allows localhost before conf.d is included, so only non-local clients go
# through the Squid Manager rules.
proxy="http://$(hostname -I | awk '{print $1}'):3128"

# Squid answers 407 to anonymous requests once it has re-read its configuration.
for _ in $(seq 1 30); do
  [[ "$(status -x "${proxy}" http://github.com/)" == "407" ]] && break
  sleep 1
done
[[ "$(status -x "${proxy}" http://github.com/)" == "407" ]] \
  || fail "the proxy accepts requests without credentials"
[[ "$(status -x "${proxy}" -U "${login}:wrong-password" http://github.com/)" == "407" ]] \
  || fail "the proxy accepts a wrong password"
[[ "$(status -x "${proxy}" -U "${login}:${password}" http://example.com/)" == "403" ]] \
  || fail "the proxy allows a domain outside the whitelist"
code="$(status -x "${proxy}" -U "${login}:${password}" http://github.com/)"
[[ "${code}" == "200" || "${code}" == "301" ]] || fail "a whitelisted domain returned ${code}"
curl -fsS -o /dev/null --max-time 20 -x "${proxy}" -U "${login}:${password}" https://github.com/ \
  || fail "HTTPS through the proxy failed"

# A deleted user loses access once Squid's credential cache (1 minute) expires.
curl -fsS -o /dev/null -u "${admin}" -H "${csrf}" -X DELETE "${panel}/api/users/${login}"
reload_squid
for _ in $(seq 1 90); do
  [[ "$(status -x "${proxy}" -U "${login}:${password}" http://github.com/)" == "407" ]] && break
  sleep 1
done
[[ "$(status -x "${proxy}" -U "${login}:${password}" http://github.com/)" == "407" ]] \
  || fail "a deleted user can still use the proxy"

# journald may need a moment to store the panel's latest lines.
for _ in $(seq 1 10); do
  journal="$(journalctl -u squid-manager --no-pager)"
  grep -q "deleted user ${login}" <<< "${journal}" && break
  sleep 1
done
grep -q "deleted user ${login}" <<< "${journal}" || fail "admin actions are not in the journal"
if grep -qF "${password}" <<< "${journal}"; then
  fail "a proxy password leaked into the journal"
fi

echo "Smoke test passed for Squid Manager $(cat /opt/squid-manager/VERSION)"
