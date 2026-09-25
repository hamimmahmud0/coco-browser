import hashlib
import secrets
import sqlite3
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


class AuthStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def initialize(self):
        with self.connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK (role IN ('manager', 'annotator')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    csrf_token TEXT NOT NULL,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions(user_id);
                """
            )

    @staticmethod
    def normalize_username(username):
        return str(username or "").strip().lower()

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def has_users(self):
        with self.connect() as connection:
            return connection.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    def create_user(self, username, password, role="annotator", active=True):
        username = self.normalize_username(username)
        if not username or not password:
            raise ValueError("Username and password are required")
        if role not in {"manager", "annotator"}:
            raise ValueError("Invalid user role")
        now = time.time()
        user_id = secrets.token_hex(16)
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO users (id, username, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, username, generate_password_hash(password), role, int(active), now, now),
            )
        return self.get_user(user_id)

    def get_user(self, user_id):
        with self.connect() as connection:
            row = connection.execute("SELECT id, username, role, active, created_at, updated_at FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    def get_user_by_username(self, username):
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE username = ?", (self.normalize_username(username),)).fetchone()
        return dict(row) if row else None

    def list_users(self):
        with self.connect() as connection:
            rows = connection.execute("SELECT id, username, role, active, created_at, updated_at FROM users ORDER BY username").fetchall()
        return [dict(row) for row in rows]

    def authenticate(self, username, password):
        user = self.get_user_by_username(username)
        if not user or not user["active"] or not check_password_hash(user["password_hash"], password):
            return None
        user.pop("password_hash", None)
        return user

    def set_active(self, user_id, active):
        with self.connect() as connection:
            connection.execute("UPDATE users SET active = ?, updated_at = ? WHERE id = ?", (int(active), time.time(), user_id))
        return self.get_user(user_id)

    def set_password(self, user_id, password):
        if not password:
            raise ValueError("Password is required")
        with self.connect() as connection:
            connection.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (generate_password_hash(password), time.time(), user_id))
        return self.get_user(user_id)

    def create_session(self, user_id, ttl=86400 * 7):
        token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        now = time.time()
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,))
            connection.execute(
                "INSERT INTO sessions (token_hash, csrf_token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
                (self.token_hash(token), csrf_token, user_id, now, now + ttl),
            )
        return token, csrf_token

    def get_session(self, token):
        if not token:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT sessions.token_hash, sessions.csrf_token, users.id, users.username, users.role, users.active, sessions.expires_at FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ? AND sessions.expires_at > ?",
                (self.token_hash(token), time.time()),
            ).fetchone()
        if not row or not row["active"]:
            return None
        return {"token_hash": row["token_hash"], "csrf_token": row["csrf_token"], "user_id": row["id"], "username": row["username"], "role": row["role"]}

    def delete_session(self, token):
        if not token:
            return
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (self.token_hash(token),))

    def delete_user_sessions(self, user_id):
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
