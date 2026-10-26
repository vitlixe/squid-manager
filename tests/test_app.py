import base64
import json
import os
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import app as admin_app

SQUID_CONF = os.path.join(os.path.dirname(__file__), "..", "config", "squid-manager.conf")


def auth_header(username="admin", password="secret", csrf=True):
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    if csrf:
        headers[admin_app.CSRF_HEADER] = admin_app.CSRF_VALUE
    return headers


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

    def test_changing_requests_require_csrf_header(self):
        headers = auth_header(csrf=False)
        requests = [
            ("post", "/api/users", {"json": {"first_name": "Иван", "last_name": "Петров"}}),
            ("post", "/api/reload_squid", {"data": "x=1", "content_type": "application/x-www-form-urlencoded"}),
            ("put", "/api/users/i.petrov/password", {}),
            ("delete", "/api/users/i.petrov", {}),
        ]
        with patch.object(admin_app.subprocess, "run") as run:
            for method, url, kwargs in requests:
                with self.subTest(method=method, url=url):
                    res = getattr(self.client, method)(url, headers=headers, **kwargs)
                    self.assertEqual(res.status_code, 403)
            run.assert_not_called()
        self.assertFalse(self.users_file.exists())

    def test_read_requests_do_not_need_csrf_header(self):
        res = self.client.get("/api/users", headers=auth_header(csrf=False))
        self.assertEqual(res.status_code, 200)

    def test_add_user_validates_required_names(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "", "last_name": "Иванов"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("First name is required", res.get_json()["error"])

    def test_add_user_validates_unsupported_chars(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "Ivan<script>", "last_name": "Иванов"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("unsupported characters", res.get_json()["error"])

    def test_add_user_validates_last_name_required(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "Иван", "last_name": ""},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Last name is required", res.get_json()["error"])

    def test_add_user_generates_transliterated_login(self):
        res = self.client.post(
            "/api/users",
            headers=auth_header(),
            json={"first_name": "Иван", "last_name": "Петров", "middle_name": "", "status": "Student"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(data["login"], "i.petrov")
        self.assertEqual(data["status"], "Student")
        self.assertEqual(len(data["password"]), 16)

        saved = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertEqual(saved[0]["login"], "i.petrov")

    def test_generate_login_avoids_collisions(self):
        users = [{"login": "i.petrov"}, {"login": "i.petrov1"}]
        login = admin_app.generate_login("Иван", "Петров", users)
        self.assertEqual(login, "i.petrov2")

    def _status(self):
        res = self.client.get("/api/status", headers=auth_header())
        self.assertEqual(res.status_code, 200)
        return res.get_json()

    def test_status_reports_version_from_version_file(self):
        version_file = Path(__file__).resolve().parent.parent / "VERSION"
        self.assertEqual(self._status()["version"], version_file.read_text(encoding="utf-8").strip())

    def test_status_tracks_changes_not_yet_applied_to_squid(self):
        self.assertFalse(self._status()["pending_changes"])

        self.client.post("/api/users", headers=auth_header(), json={"first_name": "Иван", "last_name": "Петров"})
        self.assertTrue(self._status()["pending_changes"])

        self._reload_commands()
        self.assertFalse(self._status()["pending_changes"])

        self.client.put("/api/users/i.petrov/password", headers=auth_header())
        self.assertTrue(self._status()["pending_changes"])

        self._reload_commands()
        self.client.delete("/api/users/i.petrov", headers=auth_header())
        self.assertTrue(self._status()["pending_changes"])

    def test_failed_reload_keeps_changes_pending(self):
        admin_app.save_users([{"login": "i.petrov", "password": "pass1"}])

        def fail(command, check, capture_output, text, input=None):
            raise subprocess.CalledProcessError(1, command, stderr="boom")

        with patch.object(admin_app.subprocess, "run", side_effect=fail):
            self.client.post("/api/reload_squid", headers=auth_header())
        self.assertTrue(self._status()["pending_changes"])

    def test_admin_actions_are_logged_without_passwords(self):
        with self.assertLogs("squid_manager", level="INFO") as logs:
            payload = {"first_name": "Иван", "last_name": "Петров"}
            res = self.client.post("/api/users", headers=auth_header(), json=payload)
            password = res.get_json()["password"]
            res = self.client.put("/api/users/i.petrov/password", headers=auth_header())
            new_password = res.get_json()["password"]
            self.client.delete("/api/users/i.petrov", headers=auth_header())
            self.client.get("/api/users", headers=auth_header(password="wrong"))
        output = "\n".join(logs.output)
        self.assertIn("admin from 127.0.0.1: added user i.petrov", output)
        self.assertIn("reset password for i.petrov", output)
        self.assertIn("deleted user i.petrov", output)
        self.assertIn("Failed admin login as 'admin'", output)
        for secret in (password, new_password, "wrong", "secret"):
            self.assertNotIn(secret, output)

    def test_generated_passwords_are_safe_in_proxy_urls(self):
        for _ in range(50):
            password = admin_app.generate_password()
            self.assertEqual(len(password), 16)
            self.assertTrue(password.isascii() and password.isalnum(), password)

    def test_non_ascii_admin_password_is_rejected_not_crashing(self):
        res = self.client.get("/api/users", headers=auth_header(password="пароль"))
        self.assertEqual(res.status_code, 401)

    def test_non_ascii_admin_password_can_log_in(self):
        admin_app.ADMIN_PASSWORD = "пароль-секрет"
        res = self.client.get("/api/users", headers=auth_header(password="пароль-секрет"))
        self.assertEqual(res.status_code, 200)

    def test_responses_forbid_framing_and_sniffing(self):
        for url in ("/", "/api/users"):
            with self.subTest(url=url):
                res = self.client.get(url, headers=auth_header())
                self.assertEqual(res.headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", res.headers["Content-Security-Policy"])
                self.assertEqual(res.headers["X-Content-Type-Options"], "nosniff")

    def test_page_loads_no_external_resources(self):
        page = self.client.get("/").get_data(as_text=True)
        self.assertNotIn("https://", page)
        self.assertNotIn("http://", page)
        stylesheets = [
            "/static/vendor/bootstrap/bootstrap.min.css",
            "/static/vendor/bootstrap-icons/bootstrap-icons.min.css",
        ]
        for url in stylesheets:
            self.assertIn(f'href="{url}"', page)
        for url in [*stylesheets, "/static/vendor/bootstrap-icons/fonts/bootstrap-icons.woff2"]:
            with self.subTest(url=url):
                res = self.client.get(url)
                self.assertEqual(res.status_code, 200)
                res.close()

    def test_api_responses_are_not_cached(self):
        res = self.client.get("/api/users", headers=auth_header())
        self.assertEqual(res.headers["Cache-Control"], "no-store")

    def test_generate_password_uses_requested_length(self):
        password = admin_app.generate_password(24)
        self.assertEqual(len(password), 24)

    def test_reload_squid_reports_command_errors_without_password_leak(self):
        admin_app.save_users(
            [
                {
                    "login": "i.petrov",
                    "password": "secret-password",
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "middle_name": "",
                    "status": "Student",
                }
            ]
        )

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

    def _reload_commands(self):
        commands = []

        def record(command, check, capture_output, text, input=None):
            commands.append(list(command))
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch.object(admin_app.subprocess, "run", side_effect=record):
            res = self.client.post("/api/reload_squid", headers=auth_header())
        self.assertEqual(res.status_code, 200)
        return commands

    def test_reload_squid_restricts_passwd_file_before_replacing_it(self):
        admin_app.save_users([{"login": "i.petrov", "password": "pass1"}])
        tmp = admin_app.SQUID_PASSWD + ".new"
        commands = self._reload_commands()
        chown = commands.index(["sudo", "chown", "root:proxy", tmp])
        chmod = commands.index(["sudo", "chmod", "640", tmp])
        mv = commands.index(["sudo", "mv", tmp, admin_app.SQUID_PASSWD])
        self.assertLess(chown, mv)
        self.assertLess(chmod, mv)

    def test_reload_squid_with_no_users_replaces_passwd_with_empty_file(self):
        tmp = admin_app.SQUID_PASSWD + ".new"
        commands = self._reload_commands()
        self.assertEqual(commands[0], ["sudo", "truncate", "-s", "0", tmp])
        self.assertIn(["sudo", "chmod", "640", tmp], commands)
        self.assertIn(["sudo", "mv", tmp, admin_app.SQUID_PASSWD], commands)
        self.assertNotIn(["sudo", "truncate", "-s", "0", admin_app.SQUID_PASSWD], commands)

    def test_reload_squid_atomic_passwd_not_replaced_on_failure(self):
        """If passwd generation fails mid-way, the real passwd must not be replaced."""
        admin_app.save_users(
            [
                {
                    "login": "i.petrov",
                    "password": "pass1",
                    "first_name": "Иван",
                    "last_name": "Петров",
                    "middle_name": "",
                    "status": "",
                },
                {
                    "login": "s.sidorov",
                    "password": "pass2",
                    "first_name": "Семён",
                    "last_name": "Сидоров",
                    "middle_name": "",
                    "status": "",
                },
            ]
        )

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

    def test_corrupted_users_file_returns_readable_error(self):
        self.users_file.write_text("{not json", encoding="utf-8")
        res = self.client.get("/api/users", headers=auth_header())
        self.assertEqual(res.status_code, 500)
        self.assertIn("Cannot read users file", res.get_json()["error"])

    def test_users_file_must_contain_list(self):
        self.users_file.write_text('{"login": "i.petrov"}', encoding="utf-8")
        res = self.client.get("/api/users", headers=auth_header())
        self.assertEqual(res.status_code, 500)
        self.assertIn("must contain a list", res.get_json()["error"])

    def test_concurrent_adds_keep_every_user(self):
        count = 20

        def add(i):
            client = admin_app.app.test_client()
            return client.post(
                "/api/users",
                headers=auth_header(),
                json={"first_name": "Ivan", "last_name": f"Petrov{i}"},
            ).status_code

        with ThreadPoolExecutor(max_workers=count) as pool:
            codes = list(pool.map(add, range(count)))

        self.assertEqual(codes, [200] * count)
        saved = json.loads(self.users_file.read_text(encoding="utf-8"))
        self.assertEqual(len(saved), count)


class SquidConfTestCase(unittest.TestCase):
    def _directives(self):
        lines = Path(SQUID_CONF).read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]

    def _index(self, directives, prefix):
        for i, line in enumerate(directives):
            if line.startswith(prefix):
                return i
        self.fail(f"{prefix!r} not found in {SQUID_CONF}")

    def test_auth_program_defined_before_proxy_auth_acl(self):
        directives = self._directives()
        self.assertLess(
            self._index(directives, "auth_param basic program"),
            self._index(directives, "acl squid_manager_users proxy_auth"),
        )

    def test_deny_other_ports_before_allow_users(self):
        directives = self._directives()
        self.assertLess(
            self._index(directives, "http_access deny !squid_manager_ports"),
            self._index(directives, "http_access allow squid_manager_users"),
        )

    def test_allow_requires_user_and_whitelist(self):
        directives = self._directives()
        allow = directives[self._index(directives, "http_access allow")]
        self.assertEqual(allow, "http_access allow squid_manager_users squid_manager_whitelist")

    def test_deny_all_is_last_access_rule(self):
        access_rules = [line for line in self._directives() if line.startswith("http_access")]
        self.assertEqual(access_rules[-1], "http_access deny all")

    def test_checked_passwords_expire_within_a_minute(self):
        directives = self._directives()
        ttl = directives[self._index(directives, "auth_param basic credentialsttl")]
        self.assertEqual(ttl, "auth_param basic credentialsttl 1 minute")


if __name__ == "__main__":
    unittest.main()
