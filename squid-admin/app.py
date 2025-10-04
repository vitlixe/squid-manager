from flask import Flask, request, jsonify, send_from_directory, Response
import subprocess, json, os, random, string

app = Flask(__name__, static_folder="static")
USERS_FILE = "users.json"
SQUID_PASSWD = "/etc/squid/passwd"

# Данные для авторизации (можно хранить в отдельном файле или переменных окружения)
ADMIN_LOGIN = "admin"
ADMIN_PASSWORD = "admin"

def check_auth(username, password):
    return username == ADMIN_LOGIN and password == ADMIN_PASSWORD

def authenticate():
    """Отправка 401 Unauthorized при неверных данных"""
    return Response(
        'Необходима авторизация', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    """Декоратор для защиты API"""
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
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
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2, ensure_ascii=False)

def generate_login(first_name, last_name, users):
    first_en = transliterate(first_name)
    last_en = transliterate(last_name)
    base = f"{first_en[0].lower()}.{last_en.lower()}"
    login = base
    counter = 1
    existing = {u["login"] for u in users}
    while login in existing:
        login = f"{base}{counter}"
        counter += 1
    return login

def generate_password(length=10):
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(random.choice(chars) for _ in range(length))

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
    data = request.json
    users = load_users()
    login = generate_login(data["first_name"], data["last_name"], users)
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
    users = load_users()
    users = [u for u in users if u["login"] != login]
    save_users(users)
    return jsonify({"status":"ok"})

@app.route("/api/users/<login>/password", methods=["PUT"])
@requires_auth
def reset_password(login):
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
    result = subprocess.run(["sudo", "truncate", "-s", "0", SQUID_PASSWD])
    users = load_users()
    for u in users:
        subprocess.run(["sudo", "htpasswd", "-b", SQUID_PASSWD, u["login"], u["password"]])
    subprocess.run(["sudo", "systemctl", "reload", "squid"])
    return jsonify({"status":"ok","message":"Squid обновлён"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)