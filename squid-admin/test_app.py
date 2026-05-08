import base64
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as admin_app


def auth_header(username="admin", password="secret"):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


class SquidAdminTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.users_file = Path(self.tmpdir.name) / "users.json"
        self.old_users_file = admin_app.USERS_FILE
        self.old_squid_passwd = admin_app.SQUID_PASSWD
        self.old_login = admin_app.ADMIN_LOGIN
        self.old_password = admin_app.ADMIN_PASSWORD

        admin_app.USERS_FILE = str(self.users_file)
        admin_app.SQUID_PASSWD = "/tmp/squid-passwd-test"
        admin_app.ADMIN_LOGIN = "admin"
        admin_app.ADMIN_PASSWORD = "secret"
        admin_app.app.config.update(TESTING=True)
        self.client = admin_app.app.test_client()

    def tearDown(self):
        admin_app.USERS_FILE = self.old_users_file
        admin_app.SQUID_PASSWD = self.old_squid_passwd
        admin_app.ADMIN_LOGIN = self.old_login
        admin_app.ADMIN_PASSWORD = self.old_password
        self.tmpdir.cleanup()

    def test_api_requires_auth(self):
        res = self.client.get("/api/users")
        self.assertEqual(res.status_code, 401)

    def test_add_user_validates_required_names(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "", "last_name": "Иванов"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("first_name", res.get_json()["error"])

    def test_add_user_generates_transliterated_login(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "Иван", "last_name": "Петров", "middle_name": "", "status": "Студент"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["login"], "i.petrov")
        self.assertEqual(data["status"], "Студент")
        self.assertEqual(len(data["password"]), 10)

        saved = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["login"], "i.petrov")

    def test_generate_login_avoids_collisions(self):
        users = [{"login": "i.petrov"}, {"login": "i.petrov1"}]
        login = admin_app.generate_login("Иван", "Петров", users)
        self.assertEqual(login, "i.petrov2")

    def test_generate_password_uses_requested_length(self):
        password = admin_app.generate_password(24)
        self.assertEqual(len(password), 24)

    def test_reload_squid_reports_command_errors_without_password_leak(self):
        admin_app.save_users([{
            "login": "i.petrov",
            "password": "secret-password",
            "first_name": "Иван",
            "last_name": "Петров",
            "middle_name": "",
            "status": "Студент",
        }])

        def fail_htpasswd(command, check, capture_output, text):
            if "htpasswd" in command:
                raise subprocess.CalledProcessError(1, command, stderr="boom")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(admin_app.subprocess, "run", side_effect=fail_htpasswd):
            res = self.client.post("/api/reload_squid", headers=auth_header())

        self.assertEqual(res.status_code, 500)
        payload = res.get_json()
        self.assertEqual(payload["status"], "error")
        self.assertIn("htpasswd", payload["error"])
        self.assertNotIn("secret-password", payload["error"])


if __name__ == "__main__":
    unittest.main()
