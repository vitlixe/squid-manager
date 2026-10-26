import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import string
import subprocess
from contextlib import contextmanager
from functools import wraps

from flask import Flask, Response, jsonify, request

app = Flask(__name__, static_folder="static")
# Audit log of admin actions; systemd sends stderr to the journal
# (journalctl -u squid-manager). Passwords are never logged.
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("squid_manager")
USERS_FILE = os.environ.get("SQUID_MANAGER_USERS_FILE", "users.json")
SQUID_PASSWD = os.environ.get("SQUID_MANAGER_PASSWD_FILE", "/etc/squid/passwd")
SQUID_PASSWD_OWNER = "root:proxy"
ADMIN_LOGIN = os.environ.get("SQUID_MANAGER_LOGIN", "")
ADMIN_PASSWORD = os.environ.get("SQUID_MANAGER_PASSWORD", "")
APP_HOST = os.environ.get("SQUID_MANAGER_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("SQUID_MANAGER_PORT", "5000"))
APP_DIR = os.path.dirname(os.path.abspath(__file__))

SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "squid-manager"

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 .'\-]{1,80}$")


def check_auth(username, password):
    if not ADMIN_LOGIN or not ADMIN_PASSWORD:
        return False
    # compare_digest rejects non-ASCII str, so compare UTF-8 bytes instead.
    login_ok = hmac.compare_digest(username.encode(), ADMIN_LOGIN.encode())
    password_ok = hmac.compare_digest(password.encode(), ADMIN_PASSWORD.encode())
    return login_ok and password_ok


def authenticate():
    """Send 401 Unauthorized for missing or invalid credentials."""
    return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})


def requires_auth(f):
    """Protect an API endpoint with Basic authentication.

    Browsers resend cached Basic credentials on cross-site requests, so changing
    requests must also carry a custom header, which other sites cannot set
    without a CORS preflight the panel never allows.
    """

    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            if auth:
                log.warning("Failed admin login as %r from %s", auth.username, request.remote_addr)
            return authenticate()
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            return jsonify({"error": f"Missing {CSRF_HEADER} header"}), 403
        return f(*args, **kwargs)

    return decorated


@app.after_request
def add_security_headers(response):
    # Forbid framing (clickjacking) and MIME sniffing; keep passwords out of caches.
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


def actor():
    auth = request.authorization
    return f"{auth.username if auth else '-'} from {request.remote_addr}"


def audit(message, *args):
    log.info("%s: " + message, actor(), *args)


def transliterate(text):
    # fmt: off
    mapping = {"а":"a","б":"b","в":"v","г":"g","д":"d","е":"e","ё":"e","ж":"zh","з":"z","и":"i","й":"y",
               "к":"k","л":"l","м":"m","н":"n","о":"o","п":"p","р":"r","с":"s","т":"t","у":"u","ф":"f","х":"h",
               "ц":"ts","ч":"ch","ш":"sh","щ":"sch","ъ":"","ы":"y","ь":"","э":"e","ю":"yu","я":"ya",
               "А":"A","Б":"B","В":"V","Г":"G","Д":"D","Е":"E","Ё":"E","Ж":"Zh","З":"Z","И":"I","Й":"Y",
               "К":"K","Л":"L","М":"M","Н":"N","О":"O","П":"P","Р":"R","С":"S","Т":"T","У":"U","Ф":"F",
               "Х":"H","Ц":"Ts","Ч":"Ch","Ш":"Sh","Щ":"Sch","Ъ":"","Ы":"Y","Ь":"","Э":"E","Ю":"Yu","Я":"Ya"}
    # fmt: on
    return "".join(mapping.get(c, c) for c in text)


class UsersFileError(Exception):
    """The users file exists but cannot be read as a list of users."""


@app.errorhandler(UsersFileError)
def users_file_error(exc):
    return jsonify({"error": str(exc)}), 500


@contextmanager
def users_lock():
    """Serialize read-modify-write cycles on the users file across threads and processes."""
    users_dir = os.path.dirname(USERS_FILE)
    if users_dir:
        os.makedirs(users_dir, exist_ok=True)
    with open(f"{USERS_FILE}.lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            users = json.load(f)
    except (OSError, ValueError) as exc:
        raise UsersFileError(f"Cannot read users file {USERS_FILE}: {exc}") from exc
    if not isinstance(users, list):
        raise UsersFileError(f"Users file {USERS_FILE} must contain a list")
    return users


def read_version():
    # Installed next to app.py; in a repository clone it sits one level up.
    for path in (os.path.join(APP_DIR, "VERSION"), os.path.join(APP_DIR, "..", "VERSION")):
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return "dev"


def credentials_digest(users):
    """Fingerprint of the logins and passwords Squid needs, without storing them."""
    pairs = sorted((u["login"], u["password"]) for u in users)
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()


def applied_file():
    # Digest of the credentials written to Squid by the last successful reload.
    return f"{USERS_FILE}.applied"


def read_applied_digest():
    try:
        with open(applied_file(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def has_pending_changes(users):
    applied = read_applied_digest()
    if applied is None:
        return bool(users)
    return applied != credentials_digest(users)


def save_users(users):
    users_dir = os.path.dirname(USERS_FILE)
    if users_dir:
        os.makedirs(users_dir, exist_ok=True)
    tmp_file = f"{USERS_FILE}.tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp_file, USERS_FILE)
    try:
        os.chmod(USERS_FILE, 0o600)
    except OSError:
        pass


def normalize_login_part(value):
    value = transliterate(value).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def validate_name(value, missing_msg):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(missing_msg)
    value = value.strip()
    if not NAME_RE.fullmatch(value):
        raise ValueError("Field contains unsupported characters")
    return value


def validate_optional_text(value, field, max_len=80):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("Invalid value")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError("Field is too long")
    return value


def validate_user_payload(data):
    if not isinstance(data, dict):
        raise ValueError("Invalid request")
    return {
        "last_name": validate_name(data.get("last_name"), "Last name is required"),
        "first_name": validate_name(data.get("first_name"), "First name is required"),
        "middle_name": validate_optional_text(data.get("middle_name", ""), "middle_name"),
        "status": validate_optional_text(data.get("status", "-"), "status") or "-",
    }


_MAX_LOGIN = 64
_MAX_BASE = _MAX_LOGIN - 4  # reserve 4 chars for collision suffix up to 9999


def generate_login(first_name, last_name, users):
    first_en = normalize_login_part(first_name)
    last_en = normalize_login_part(last_name)
    if not first_en or not last_en:
        raise ValueError("first_name and last_name must contain login-safe characters")
    base = f"{first_en[0]}.{last_en}"[:_MAX_BASE]
    login = base
    counter = 1
    existing = {u["login"] for u in users}
    while login in existing:
        suffix = str(counter)
        login = f"{base[:_MAX_LOGIN - len(suffix)]}{suffix}"
        counter += 1
    return login


def generate_password(length=16):
    # Letters and digits only: clients often put credentials into proxy URLs
    # (http://login:password@host:3128), where @, #, % and similar break parsing.
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def validate_login(login):
    if not LOGIN_RE.fullmatch(login):
        raise ValueError("invalid login")
    return login


def run_checked(command, safe_command=None, stdin_data=None):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, input=stdin_data)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {exc.returncode}"
        label = safe_command or " ".join(command)
        raise RuntimeError(f"{label} failed: {detail}") from exc


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/status", methods=["GET"])
@requires_auth
def get_status():
    return jsonify({"version": read_version(), "pending_changes": has_pending_changes(load_users())})


@app.route("/api/users", methods=["GET"])
@requires_auth
def get_users():
    return jsonify(load_users())


@app.route("/api/users", methods=["POST"])
@requires_auth
def add_user():
    try:
        data = validate_user_payload(request.get_json(silent=True))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with users_lock():
        users = load_users()
        try:
            login = generate_login(data["first_name"], data["last_name"], users)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        password = generate_password()
        new_user = {
            "last_name": data["last_name"],
            "first_name": data["first_name"],
            "middle_name": data.get("middle_name", ""),
            "status": data.get("status", "-"),
            "login": login,
            "password": password,
        }
        users.append(new_user)
        save_users(users)
    audit("added user %s", login)
    return jsonify(new_user)


@app.route("/api/users/<login>", methods=["DELETE"])
@requires_auth
def delete_user(login):
    try:
        login = validate_login(login)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with users_lock():
        users = load_users()
        before = len(users)
        users = [u for u in users if u["login"] != login]
        if len(users) == before:
            return jsonify({"error": "User not found"}), 404
        save_users(users)
    audit("deleted user %s", login)
    return jsonify({"status": "ok"})


@app.route("/api/users/<login>/password", methods=["PUT"])
@requires_auth
def reset_password(login):
    try:
        login = validate_login(login)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    with users_lock():
        users = load_users()
        for u in users:
            if u["login"] == login:
                new_pass = generate_password()
                u["password"] = new_pass
                save_users(users)
                audit("reset password for %s", login)
                return jsonify({"login": login, "password": new_pass})
    return jsonify({"error": "User not found"}), 404


@app.route("/api/reload_squid", methods=["POST"])
@requires_auth
def reload_squid():
    tmp_passwd = SQUID_PASSWD + ".new"
    with users_lock():
        try:
            users = load_users()
            if users:
                first = True
                for u in users:
                    login = validate_login(u["login"])
                    if first:
                        run_checked(
                            ["sudo", "htpasswd", "-c", "-i", tmp_passwd, login],
                            safe_command=f"sudo htpasswd -c -i {tmp_passwd} {login}",
                            stdin_data=u["password"],
                        )
                        first = False
                    else:
                        run_checked(
                            ["sudo", "htpasswd", "-i", tmp_passwd, login],
                            safe_command=f"sudo htpasswd -i {tmp_passwd} {login}",
                            stdin_data=u["password"],
                        )
            else:
                run_checked(["sudo", "truncate", "-s", "0", tmp_passwd])
            # htpasswd creates the file world-readable; only Squid needs to read it.
            run_checked(["sudo", "chown", SQUID_PASSWD_OWNER, tmp_passwd])
            run_checked(["sudo", "chmod", "640", tmp_passwd])
            run_checked(["sudo", "mv", tmp_passwd, SQUID_PASSWD])
            run_checked(["sudo", "systemctl", "reload", "squid"])
            with open(applied_file(), "w", encoding="utf-8") as f:
                f.write(credentials_digest(users) + "\n")
            audit("reloaded Squid with %d users", len(users))
            return jsonify({"status": "ok", "message": "Squid reloaded"})
        except (RuntimeError, ValueError) as exc:
            try:
                run_checked(["sudo", "rm", "-f", tmp_passwd])
            except Exception:
                pass
            log.error("%s: Squid reload failed: %s", actor(), exc)
            return jsonify({"status": "error", "error": str(exc)}), 500


if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT)
