import json
import os
import re
import secrets
import socket
import sqlite3
import threading
import webbrowser
from hashlib import pbkdf2_hmac
from http.cookies import SimpleCookie
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("FURKA_PORT", "8000"))
DB_PATH = os.path.join(BASE_DIR, "users.db")
SESSION_NAME = "furka_session"
PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[\w]{3,20}$", re.UNICODE)
SESSION_MAX_AGE = 60 * 60 * 24 * 30


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at TEXT NOT NULL)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL)"""
    )
    conn.commit()
    conn.close()


def hash_password(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return digest.hex(), salt.hex()


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Handler(SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"[{self.address_string()}] {fmt % args}")

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, code, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return None

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_NAME)
        return morsel.value if morsel else None

    def session_user(self):
        token = self.session_token()
        if not token:
            return None
        conn = db()
        row = conn.execute(
            "SELECT username FROM sessions WHERE token = ?", (token,)
        ).fetchone()
        conn.close()
        return row["username"] if row else None

    def create_session(self, username):
        token = secrets.token_urlsafe(32)
        conn = db()
        conn.execute(
            "INSERT INTO sessions (token, username, created_at) VALUES (?, ?, datetime('now'))",
            (token, username),
        )
        conn.commit()
        conn.close()
        return token

    def delete_session(self, token):
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()

    def session_cookie_header(self, token, max_age=SESSION_MAX_AGE):
        return {
            "Set-Cookie": f"{SESSION_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}"
        }

    def prepare_path(self):
        path = urlparse(self.path).path
        if path == "/FurkaLauncher.exe":
            self.path = "/dist/FurkaLauncher.exe"

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/me":
            user = self.session_user()
            data = {"logged_in": bool(user)}
            if user:
                conn = db()
                row = conn.execute(
                    "SELECT created_at FROM users WHERE username = ?", (user,)
                ).fetchone()
                conn.close()
                data["username"] = user
                data["created_at"] = row["created_at"] if row else None
            self.send_json(200, data)
            return
        if path == "/FurkaLauncher.exe":
            if not self.session_user():
                self.send_json(401, {"error": "Войдите, чтобы скачать лаунчер"})
                return
            self.path = "/dist/FurkaLauncher.exe"
        self.prepare_path()
        super().do_GET()

    def do_HEAD(self):
        path = urlparse(self.path).path
        if path == "/FurkaLauncher.exe" and not self.session_user():
            self.send_json(401, {"error": "Войдите, чтобы скачать лаунчер"})
            return
        self.prepare_path()
        super().do_HEAD()

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/register":
            self.api_register()
            return
        if path == "/api/login":
            self.api_login()
            return
        if path == "/api/logout":
            self.api_logout()
            return
        self.send_json(404, {"error": "Не найдено"})

    def api_register(self):
        data = self.read_json() or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        if not USERNAME_RE.match(username):
            self.send_json(400, {"error": "Логин: 3–20 символов (буквы, цифры, _)"})
            return
        if len(password) < 6:
            self.send_json(400, {"error": "Пароль: минимум 6 символов"})
            return
        pass_hash, salt = hash_password(password)
        conn = db()
        try:
            conn.execute(
                "INSERT INTO users (username, pass_hash, salt, created_at) VALUES (?, ?, ?, datetime('now'))",
                (username, pass_hash, salt),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            self.send_json(409, {"error": "Этот логин уже занят"})
            return
        conn.close()
        token = self.create_session(username)
        self.send_json(
            200, {"ok": True, "username": username}, self.session_cookie_header(token)
        )

    def api_login(self):
        data = self.read_json() or {}
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        conn = db()
        row = conn.execute(
            "SELECT pass_hash, salt FROM users WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        if not row:
            self.send_json(401, {"error": "Неверный логин или пароль"})
            return
        digest, _ = hash_password(password, row["salt"])
        if digest != row["pass_hash"]:
            self.send_json(401, {"error": "Неверный логин или пароль"})
            return
        token = self.create_session(username)
        self.send_json(
            200, {"ok": True, "username": username}, self.session_cookie_header(token)
        )

    def api_logout(self):
        token = self.session_token()
        if token:
            self.delete_session(token)
        self.send_json(200, {"ok": True}, self.session_cookie_header("", max_age=0))


def main():
    init_db()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    ip = local_ip()
    print("=" * 56, flush=True)
    print(" Сайт лаунчера запущен!", flush=True)
    print(f" Этот ПК:      http://localhost:{PORT}", flush=True)
    print(f" По сети:      http://{ip}:{PORT}", flush=True)
    print(" Другие устройства: открыть ссылку выше в браузере", flush=True)
    print(" Остановка: Ctrl+C", flush=True)
    print("=" * 56, flush=True)
    if not os.environ.get("FURKA_NO_BROWSER"):
        threading.Timer(0.5, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
        server.server_close()


if __name__ == "__main__":
    main()
