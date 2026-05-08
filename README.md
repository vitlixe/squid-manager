# Squid Manager

Squid Manager is a small Flask-based web interface for managing Squid proxy users.
It lets an administrator add, remove, and reset proxy users, then rebuild the Squid
password file and reload the service.

## Project Structure

```text
squid-manager/
├── deploy/
│   ├── squid-admin.env.example
│   └── squid-admin.service
├── squid/
│   ├── squid.conf          # Squid configuration
│   ├── whitelist           # Allowed domains
│   └── passwd              # htpasswd file, ignored by git
├── squid-admin/
│   ├── app.py              # Flask backend
│   ├── requirements.txt    # Python dependencies
│   ├── static/
│   │   ├── index.html      # Web UI
│   │   └── images/
│   │       └── squid-admin.png
│   └── users.json          # Local user store, ignored by git
├── .gitignore
└── README.md
```

## Requirements

- Linux host with Squid
- Python 3
- `apache2-utils` for `htpasswd`
- `systemd` for service management

## Installation

### 1. Install Squid and dependencies

```bash
sudo apt update
sudo apt install -y squid apache2-utils python3-flask
```

### 2. Install Squid configuration

```bash
sudo cp <repository>/squid/squid.conf /etc/squid/squid.conf
sudo cp <repository>/squid/whitelist /etc/squid/whitelist
```

Create an empty password file with correct ownership:

```bash
sudo install -o root -g proxy -m 640 /dev/null /etc/squid/passwd
```

### 3. Install the admin application

```bash
sudo mkdir -p /opt/squid-admin
sudo cp -r <repository>/squid-admin/* /opt/squid-admin/
sudo chown -R proxy:proxy /opt/squid-admin
sudo chmod -R 750 /opt/squid-admin
```

### 4. Configure sudo permissions

The `proxy` user needs permission to update the Squid password file and reload Squid
without a password prompt.

```bash
sudo visudo
```

Add:

```text
proxy ALL=(root) NOPASSWD: /usr/bin/htpasswd -c -i /etc/squid/passwd.new *
proxy ALL=(root) NOPASSWD: /usr/bin/htpasswd -i /etc/squid/passwd.new *
proxy ALL=(root) NOPASSWD: /bin/mv /etc/squid/passwd.new /etc/squid/passwd
proxy ALL=(root) NOPASSWD: /usr/bin/rm -f /etc/squid/passwd.new
proxy ALL=(root) NOPASSWD: /usr/bin/truncate -s 0 /etc/squid/passwd
proxy ALL=(root) NOPASSWD: /bin/systemctl reload squid
```

The reload endpoint writes credentials to `/etc/squid/passwd.new` via `htpasswd -i`
(password read from stdin, never passed as a command argument), then atomically replaces
`/etc/squid/passwd` with `mv`. If generation fails partway, `passwd.new` is removed and
the live `passwd` file is left untouched. The `truncate` rule is used only when the user
list is empty.

### 5. Configure environment variables

Squid Manager has no default admin password. Install the example environment
file, then edit it:

```bash
sudo cp <repository>/deploy/squid-admin.env.example /etc/squid-admin.env
sudo nano /etc/squid-admin.env
```

The file contains:

```ini
SQUID_ADMIN_LOGIN=admin
SQUID_ADMIN_PASSWORD=change-this-password
SQUID_ADMIN_HOST=127.0.0.1
SQUID_ADMIN_PORT=5000
SQUID_ADMIN_USERS_FILE=/opt/squid-admin/users.json
SQUID_PASSWD=/etc/squid/passwd
```

Restrict access to the file:

```bash
sudo chown root:proxy /etc/squid-admin.env
sudo chmod 640 /etc/squid-admin.env
```

By default, the admin interface binds to `127.0.0.1`. For remote access, prefer an
SSH tunnel, VPN, or reverse proxy with TLS and additional access control.

### 6. Configure firewall

```bash
sudo ufw allow 3128/tcp
```

Do not expose port `5000/tcp` to the public internet. If direct access is required,
limit it to a trusted admin IP:

```bash
sudo ufw allow from <admin_ip> to any port 5000 proto tcp
```

### 7. Run manually

```bash
cd /opt/squid-admin
set -a
. /etc/squid-admin.env
set +a
python3 app.py
```

The interface will be available at:

```text
http://localhost:5000
```

## systemd Service

Install the provided systemd unit:

```bash
sudo cp <repository>/deploy/squid-admin.service /etc/systemd/system/squid-admin.service
```

The unit runs the app as `proxy`, loads `/etc/squid-admin.env`, and starts
`/opt/squid-admin/app.py`:

```ini
[Unit]
Description=Squid Admin Panel
After=network.target

[Service]
Type=simple
User=proxy
Group=proxy
WorkingDirectory=/opt/squid-admin
EnvironmentFile=/etc/squid-admin.env
ExecStart=/usr/bin/python3 /opt/squid-admin/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=network.target
```

Enable and start Squid and Squid Manager:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now squid squid-admin
```

## Testing

Install Python dependencies, then run:

```bash
python3 -m unittest discover squid-admin
```

## Security Notes

- Set a strong value for `SQUID_ADMIN_PASSWORD` before deployment. The placeholder
  `change-this-password` must not be used in production.
- Keep `/etc/squid-admin.env` readable only by root and the proxy group (`chmod 640`).
- `users.json` stores proxy credentials in plaintext. Restrict access: `chmod 600`.
- Do not expose the Flask development server to the public internet. Use a reverse proxy
  with TLS for any external access.
- After making changes in the web interface, use the "Reload Squid" button to apply them
  to `/etc/squid/passwd`.

## Screenshot

![Squid Manager Screenshot](./squid-admin/static/images/squid-admin.png)
