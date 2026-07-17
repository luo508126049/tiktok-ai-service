"""SQLite-backed application users and the single administrator lease."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash


class AuthStore:
    """Persist application accounts and the active administrator session."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS admin_session (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    session_id TEXT NOT NULL,
                    task_active INTEGER NOT NULL DEFAULT 0,
                    task_session_id TEXT,
                    updated_at TEXT NOT NULL
                );
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "display_name" not in columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "UPDATE users SET display_name = username WHERE display_name IS NULL OR display_name = ''"
            )
            # A process restart means no in-memory collection is still running.
            connection.execute(
                "UPDATE admin_session SET task_active = 0, task_session_id = NULL WHERE id = 1"
            )
            admin = connection.execute(
                "SELECT username FROM users WHERE username = 'admin'"
            ).fetchone()
            if admin is None:
                now = self._now()
                connection.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, role, active, created_at, updated_at)
                    VALUES (?, ?, ?, 'admin', 1, ?, ?)
                    """,
                    ("admin", "管理员", generate_password_hash("123456"), now, now),
                )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_user(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "username": row["username"],
            "display_name": row["display_name"] or row["username"],
            "role": row["role"],
            "active": bool(row["active"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def authenticate(self, username: str, password: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1",
                (username,),
            ).fetchone()
        if row is None or not check_password_hash(row["password_hash"], password):
            return None
        return self._row_to_user(row)

    def get_user(self, username: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username = ? AND active = 1",
                (username,),
            ).fetchone()
        return self._row_to_user(row)

    def create_user(self, username: str, display_name: str, password: str) -> dict[str, Any]:
        now = self._now()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO users(username, display_name, password_hash, role, active, created_at, updated_at)
                    VALUES (?, ?, ?, 'user', 1, ?, ?)
                    """,
                    (username, display_name, generate_password_hash(password), now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("账号已存在") from exc
        return {
            "username": username,
            "display_name": display_name,
            "role": "user",
            "active": True,
            "created_at": now,
            "updated_at": now,
        }

    def update_password(self, username: str, password: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE username = ? AND active = 1",
                (generate_password_hash(password), self._now(), username),
            )
            if cursor.rowcount != 1:
                raise ValueError("账号不存在")

    def delete_user(self, username: str) -> None:
        if username.lower() == "admin":
            raise ValueError("不能删除管理员账号")
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM users WHERE username = ? AND role = 'user'",
                (username,),
            )
            if cursor.rowcount != 1:
                raise ValueError("普通账号不存在")

    def list_users(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT username, display_name, role, active, created_at, updated_at FROM users ORDER BY username"
            ).fetchall()
        return [self._row_to_user(row) for row in rows if row is not None]

    def get_admin_session(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT session_id, task_active, task_session_id, updated_at FROM admin_session WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "task_active": bool(row["task_active"]),
            "task_session_id": row["task_session_id"],
            "updated_at": row["updated_at"],
        }

    def claim_admin_session(self, session_id: str) -> None:
        now = self._now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO admin_session(id, session_id, task_active, task_session_id, updated_at)
                VALUES (1, ?, 0, NULL, ?)
                ON CONFLICT(id) DO UPDATE SET
                    session_id = excluded.session_id,
                    task_active = 0,
                    task_session_id = NULL,
                    updated_at = excluded.updated_at
                """,
                (session_id, now),
            )

    def clear_admin_session(self, session_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM admin_session WHERE id = 1 AND session_id = ? AND task_active = 0",
                (session_id,),
            )

    def set_admin_task(self, session_id: str, active: bool) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE admin_session
                SET task_active = ?, task_session_id = ?, updated_at = ?
                WHERE id = 1 AND session_id = ?
                """,
                (int(active), session_id if active else None, self._now(), session_id),
            )
