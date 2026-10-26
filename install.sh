#!/usr/bin/env bash
#
# Install Squid Manager on a Debian/Ubuntu server from the release archive or a
# repository clone. The .deb package installs the same files; both then run
# packaging/setup.sh.
#
# Run as root from the project folder:
#
#   sudo ./install.sh
#
# Re-running the script updates the panel and keeps the environment file, the
# user list, the Squid password file, and the whitelist.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="/opt/squid-manager"
LIB_DIR="/usr/lib/squid-manager"
UNIT_FILE="/usr/lib/systemd/system/squid-manager.service"
WHITELIST="/etc/squid-manager/whitelist"

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

if [[ "${EUID}" -ne 0 ]]; then
  echo "This script must be run as root (use: sudo ./install.sh)" >&2
  exit 1
fi

log "Installing packages"
apt-get update
apt-get install -y squid apache2-utils python3 python3-flask gunicorn sudo procps

log "Installing Squid Manager files"
install -d -m 755 "${APP_DIR}" "${LIB_DIR}"
install -o root -g root -m 644 "${SRC_DIR}/panel/app.py" "${APP_DIR}/app.py"
install -o root -g root -m 644 "${SRC_DIR}/VERSION" "${APP_DIR}/VERSION"
# Replace the static files so files removed upstream disappear too.
rm -rf "${APP_DIR}/static"
cp -r "${SRC_DIR}/panel/static" "${APP_DIR}/static"
chown -R root:root "${APP_DIR}/static"
find "${APP_DIR}/static" -type d -exec chmod 755 {} +
find "${APP_DIR}/static" -type f -exec chmod 644 {} +

install -o root -g root -m 755 "${SRC_DIR}/packaging/setup.sh" "${LIB_DIR}/setup.sh"
install -o root -g root -m 644 "${SRC_DIR}/packaging/squid-manager.env.example" "${LIB_DIR}/squid-manager.env.example"
install -o root -g root -m 644 "${SRC_DIR}/config/squid-manager.conf" "${LIB_DIR}/squid-manager.conf"
install -o root -g root -m 644 "${SRC_DIR}/packaging/squid-manager.service" "${UNIT_FILE}"
install -d -o root -g root -m 755 "$(dirname "${WHITELIST}")"
if [[ ! -e "${WHITELIST}" ]]; then
  install -o root -g root -m 644 "${SRC_DIR}/config/whitelist" "${WHITELIST}"
fi

exec "${LIB_DIR}/setup.sh"
