import base64
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as admin_app

SQUID_CONF = os.path.join(os.path.dirname(__file__), '..', 'squid', 'squid.conf')


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

        def fail_htpasswd(command, check, capture_output, text, input=None):
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


    def test_reload_squid_atomic_passwd_not_replaced_on_failure(self):
        """If passwd generation fails mid-way, the real passwd must not be replaced."""
        admin_app.save_users([
            {"login": "i.petrov", "password": "pass1", "first_name": "Иван",
             "last_name": "Петров", "middle_name": "", "status": ""},
            {"login": "s.sidorov", "password": "pass2", "first_name": "Семён",
             "last_name": "Сидоров", "middle_name": "", "status": ""},
        ])

        called_commands = []

        def selective_fail(command, check, capture_output, text, input=None):
            called_commands.append(list(command))
            if "htpasswd" in command and "s.sidorov" in command:
                raise subprocess.CalledProcessError(1, command, stderr="disk full")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(admin_app.subprocess, "run", side_effect=selective_fail):
            res = self.client.post("/api/reload_squid", headers=auth_header())

        self.assertEqual(res.status_code, 500)
        mv_calls = [c for c in called_commands if "mv" in c]
        self.assertEqual(mv_calls, [], "passwd must not be replaced when generation fails")

    def test_generate_login_long_names_fit_login_re(self):
        long_last = "а" * 80
        login = admin_app.generate_login("Иван", long_last, [])
        admin_app.validate_login(login)
        self.assertLessEqual(len(login), 64)

    def test_generate_login_long_names_collision_fits_login_re(self):
        long_last = "а" * 80
        existing_base = admin_app.generate_login("Иван", long_last, [])
        users_stub = [{"login": existing_base}]
        login2 = admin_app.generate_login("Иван", long_last, users_stub)
        admin_app.validate_login(login2)
        self.assertLessEqual(len(login2), 64)
        self.assertNotEqual(login2, existing_base)


class SquidConfTestCase(unittest.TestCase):
    def _rule_positions(self):
        lines = Path(SQUID_CONF).read_text(encoding="utf-8").splitlines()
        pos = {}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "http_access deny !Safe_ports":
                pos.setdefault("deny_safe_ports", i)
            elif stripped == "http_access deny CONNECT !SSL_ports":
                pos.setdefault("deny_connect", i)
            elif stripped == "http_access allow localhost manager":
                pos.setdefault("allow_manager", i)
            elif stripped == "http_access deny manager":
                pos.setdefault("deny_manager", i)
            elif "http_access allow" in stripped and "authenticated_users" in stripped:
                pos.setdefault("allow_users", i)
            elif stripped == "http_access deny all":
                pos.setdefault("deny_all", i)
        return pos

    def test_deny_unsafe_ports_before_allow_users(self):
        pos = self._rule_positions()
        self.assertIn("deny_safe_ports", pos, "http_access deny !Safe_ports not found")
        self.assertIn("allow_users", pos, "http_access allow authenticated_users not found")
        self.assertLess(pos["deny_safe_ports"], pos["allow_users"])

    def test_deny_connect_before_allow_users(self):
        pos = self._rule_positions()
        self.assertIn("deny_connect", pos)
        self.assertLess(pos["deny_connect"], pos["allow_users"])

    def test_manager_rules_before_allow_users(self):
        pos = self._rule_positions()
        self.assertIn("allow_manager", pos)
        self.assertIn("deny_manager", pos)
        self.assertLess(pos["allow_manager"], pos["allow_users"])
        self.assertLess(pos["deny_manager"], pos["allow_users"])

    def test_deny_all_is_last_access_rule(self):
        pos = self._rule_positions()
        self.assertIn("deny_all", pos)
        for key, line_no in pos.items():
            if key != "deny_all":
                self.assertLess(line_no, pos["deny_all"], f"{key} must come before deny all")


if __name__ == "__main__":
    unittest.main()
