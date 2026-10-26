#!/usr/bin/env bash
#
# Configure Squid Manager after its files are in place. Runs as root from
# install.sh and from the .deb postinst, and is safe to run repeatedly: it keeps
# the environment file, the user list, the Squid password file, and the whitelist.

set -euo pipefail

LIB_DIR="/usr/lib/squid-manager"
APP_DIR="/opt/squid-manager"
CONF_DIR="/etc/squid-manager"
ENV_FILE="${CONF_DIR}/squid-manager.env"
WHITELIST="${CONF_DIR}/whitelist"
SUDOERS_FILE="/etc/sudoers.d/squid-manager"
SQUID_PASSWD="/etc/squid/passwd"
SQUID_DROPIN="/etc/squid/conf.d/squid-manager.conf"
SERVICE_USER="proxy"
SERVICE_GROUP="proxy"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m warning:\033[0m %s\n' "$*" >&2; }

VERSION="$(cat "${APP_DIR}/VERSION")"

# Settings the administrator edits: the panel environment and the whitelist.
install -d -o root -g root -m 755 "${CONF_DIR}"

# The panel writes users.json and its lock file next to the code.
chown "${SERVICE_USER}:${SERVICE_GROUP}" "${APP_DIR}"
chmod 750 "${APP_DIR}"

if [[ ! -e "${SQUID_PASSWD}" ]]; then
  log "Creating empty Squid password file"
  install -o root -g "${SERVICE_GROUP}" -m 640 /dev/null "${SQUID_PASSWD}"
fi
# Only root writes the password file; Squid (group proxy) reads it.
chown "root:${SERVICE_GROUP}" "${SQUID_PASSWD}"
chmod 640 "${SQUID_PASSWD}"

GENERATED_PASSWORD=""
if [[ ! -e "${ENV_FILE}" ]]; then
  log "Creating ${ENV_FILE} with a generated admin password"
  GENERATED_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(18))')"
  env_tmp="$(mktemp)"
  sed "s|^SQUID_MANAGER_PASSWORD=.*|SQUID_MANAGER_PASSWORD=${GENERATED_PASSWORD}|" \
    "${LIB_DIR}/squid-manager.env.example" > "${env_tmp}"
  install -o root -g "${SERVICE_GROUP}" -m 640 "${env_tmp}" "${ENV_FILE}"
  rm -f "${env_tmp}"
elif grep -q '^SQUID_MANAGER_PASSWORD=change-this-password$' "${ENV_FILE}"; then
  warn "${ENV_FILE} still uses the template password. Set a strong SQUID_MANAGER_PASSWORD."
fi

log "Installing sudo rules"
sudoers_tmp="$(mktemp)"
cat > "${sudoers_tmp}" <<EOF
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/htpasswd -c -i ${SQUID_PASSWD}.new *
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/htpasswd -i ${SQUID_PASSWD}.new *
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/truncate -s 0 ${SQUID_PASSWD}.new
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/chown root\:${SERVICE_GROUP} ${SQUID_PASSWD}.new
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/chmod 640 ${SQUID_PASSWD}.new
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/mv ${SQUID_PASSWD}.new ${SQUID_PASSWD}
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/rm -f ${SQUID_PASSWD}.new
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/systemctl reload squid
EOF
if ! visudo -cqf "${sudoers_tmp}"; then
  rm -f "${sudoers_tmp}"
  echo "Generated sudoers rules failed validation." >&2
  exit 1
fi
install -o root -g root -m 440 "${sudoers_tmp}" "${SUDOERS_FILE}"
rm -f "${sudoers_tmp}"

log "Installing Squid configuration"
install -d -o root -g root -m 755 "$(dirname "${SQUID_DROPIN}")"
install -o root -g root -m 644 "${LIB_DIR}/squid-manager.conf" "${SQUID_DROPIN}"
if ! squid -k parse > /dev/null 2>&1; then
  echo "Squid rejected its configuration. Run 'squid -k parse' for details." >&2
  exit 1
fi

log "Enabling and starting services"
systemctl daemon-reload
systemctl enable squid
systemctl reload-or-restart squid
systemctl enable squid-manager
# restart so an update picks up the new application code.
systemctl restart squid-manager

log "Squid Manager ${VERSION} is running on 127.0.0.1 (see ${ENV_FILE} for the port)."
if [[ -n "${GENERATED_PASSWORD}" ]]; then
  echo "Admin login: $(sed -n 's/^SQUID_MANAGER_LOGIN=//p' "${ENV_FILE}")"
  echo "Admin password: ${GENERATED_PASSWORD}"
  echo "The password is stored in ${ENV_FILE}."
fi
echo "For remote access use an SSH tunnel, VPN, or a TLS reverse proxy with access control."
