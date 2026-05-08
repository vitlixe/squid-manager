from functools import wraps
from flask import Flask, request, jsonify, send_from_directory, Response
import hmac
import json
import os
import re
import secrets
import string
import subprocess

app = Flask(__name__, static_folder="static")
USERS_FILE = os.environ.get("SQUID_ADMIN_USERS_FILE", "users.json")
SQUID_PASSWD = os.environ.get("SQUID_PASSWD", "/etc/squid/passwd")
ADMIN_LOGIN = os.environ.get("SQUID_ADMIN_LOGIN", "")
ADMIN_PASSWORD = os.environ.get("SQUID_ADMIN_PASSWORD", "")
APP_HOST = os.environ.get("SQUID_ADMIN_HOST", "127.0.0.1")
APP_PORT = int(os.environ.get("SQUID_ADMIN_PORT", "5000"))

LOGIN_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
NAME_RE = re.compile(r"^[A-Za-zА-Яа-яЁё0-9 .'\-]{1,80}$")

def check_auth(username, password):
    if not ADMIN_LOGIN or not ADMIN_PASSWORD:
        return False
    return hmac.compare_digest(username, ADMIN_LOGIN) and hmac.compare_digest(password, ADMIN_PASSWORD)

def authenticate():
    """Отправка 401 Unauthorized при неверных данных"""
    return Response(
        'Необходима авторизация', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    """Декоратор для защиты API"""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def transliterate(text):
    mapping = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'y',
               'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f','х':'h',
               'ц':'ts','ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
               'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'E','Ж':'Zh','З':'Z','И':'I','Й':'Y',
               'К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F',
               'Х':'H','Ц':'Ts','Ч':'Ch','Ш':'Sh','Щ':'Sch','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'Yu','Я':'Ya'}
    return ''.join(mapping.get(c,c) for c in text)

def load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

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

def validate_name(value, field):
    if not isinstance(value, str):
        raise ValueError(f"{field} is required")
    value = value.strip()
    if not value:
        raise ValueError(f"{field} is required")
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"{field} contains unsupported characters")
    return value

def validate_optional_text(value, field, max_len=80):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    value = value.strip()
    if len(value) > max_len:
        raise ValueError(f"{field} is too long")
    return value

def validate_user_payload(data):
    if not isinstance(data, dict):
        raise ValueError("JSON body is required")
    return {
        "last_name": validate_name(data.get("last_name"), "last_name"),
        "first_name": validate_name(data.get("first_name"), "first_name"),
        "middle_name": validate_optional_text(data.get("middle_name", ""), "middle_name"),
        "status": validate_optional_text(data.get("status", "-"), "status") or "-",
    }

def generate_login(first_name, last_name, users):
    first_en = normalize_login_part(first_name)
    last_en = normalize_login_part(last_name)
    if not first_en or not last_en:
        raise ValueError("first_name and last_name must contain login-safe characters")
    base = f"{first_en[0]}.{last_en}"
    login = base
    counter = 1
    existing = {u["login"] for u in users}
    while login in existing:
        login = f"{base}{counter}"
        counter += 1
    return login

def generate_password(length=10):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(chars) for _ in range(length))

def validate_login(login):
    if not LOGIN_RE.fullmatch(login):
        raise ValueError("invalid login")
    return login

def run_checked(command, safe_command=None):
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()
        stdout = (exc.stdout or "").strip()
        detail = stderr or stdout or f"exit code {exc.returncode}"
        label = safe_command or " ".join(command)
        raise RuntimeError(f"{label} failed: {detail}") from exc

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")

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
        "status": data.get("status","-"),
        "login": login,
        "password": password
    }
    users.append(new_user)
    save_users(users)
    return jsonify(new_user)

@app.route("/api/users/<login>", methods=["DELETE"])
@requires_auth
def delete_user(login):
    try:
        login = validate_login(login)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    users = load_users()
    before = len(users)
    users = [u for u in users if u["login"] != login]
    if len(users) == before:
        return jsonify({"error":"User not found"}), 404
    save_users(users)
    return jsonify({"status":"ok"})

@app.route("/api/users/<login>/password", methods=["PUT"])
@requires_auth
def reset_password(login):
    try:
        login = validate_login(login)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    users = load_users()
    for u in users:
        if u["login"] == login:
            new_pass = generate_password()
            u["password"] = new_pass
            save_users(users)
            return jsonify({"login":login,"password":new_pass})
    return jsonify({"error":"User not found"}), 404

@app.route("/api/reload_squid", methods=["POST"])
@requires_auth
def reload_squid():
    try:
        users = load_users()
        run_checked(["sudo", "truncate", "-s", "0", SQUID_PASSWD])
        for u in users:
            login = validate_login(u["login"])
            run_checked(
                ["sudo", "htpasswd", "-b", SQUID_PASSWD, login, u["password"]],
                safe_command=f"sudo htpasswd -b {SQUID_PASSWD} {login} <password>",
            )
        run_checked(["sudo", "systemctl", "reload", "squid"])
        return jsonify({"status":"ok","message":"Squid обновлён"})
    except (RuntimeError, ValueError) as exc:
        return jsonify({"status":"error","error":str(exc)}), 500

if __name__ == "__main__":
    app.run(host=APP_HOST, port=APP_PORT)
