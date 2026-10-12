"""SQLite-хранилище: пользователи/чаты, источники (ссылки на файлы), расписания."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from dataclasses import dataclass


@dataclass
class Source:
    day_idx: int
    url: str
    hash: str
    checked_at: float


class Storage:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY,
                group_name TEXT,
                location TEXT,
                notify INTEGER NOT NULL DEFAULT 1,
                active INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS sources (
                day_idx INTEGER PRIMARY KEY,
                url TEXT NOT NULL,
                hash TEXT NOT NULL,
                checked_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS schedules (
                date TEXT NOT NULL,
                group_name TEXT NOT NULL,
                data TEXT NOT NULL,
                PRIMARY KEY (date, group_name)
            );
            """
        )
        # Миграция существующей базы: добавляем выбор корпуса без потери подписок.
        user_cols = {r[1] for r in self.db.execute("PRAGMA table_info(users)")}
        if "location" not in user_cols:
            self.db.execute("ALTER TABLE users ADD COLUMN location TEXT")
        self.db.commit()

    # --- пользователи -------------------------------------------------
    def ensure_user(self, chat_id: int) -> None:
        self.db.execute("INSERT OR IGNORE INTO users (chat_id) VALUES (?)", (chat_id,))
        self.db.execute("UPDATE users SET active = 1 WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def get_user(self, chat_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()

    def set_location(self, chat_id: int, location: str) -> None:
        self.ensure_user(chat_id)
        self.db.execute("UPDATE users SET location = ?, group_name = NULL WHERE chat_id = ?", (location, chat_id))
        self.db.commit()

    def set_group(self, chat_id: int, group: str) -> None:
        self.ensure_user(chat_id)
        self.db.execute("UPDATE users SET group_name = ? WHERE chat_id = ?", (group, chat_id))
        self.db.commit()

    def toggle_notify(self, chat_id: int) -> bool:
        self.ensure_user(chat_id)
        self.db.execute("UPDATE users SET notify = 1 - notify WHERE chat_id = ?", (chat_id,))
        self.db.commit()
        return bool(self.get_user(chat_id)["notify"])

    def deactivate(self, chat_id: int) -> None:
        self.db.execute("UPDATE users SET active = 0 WHERE chat_id = ?", (chat_id,))
        self.db.commit()

    def subscribers(self, group: str) -> list[int]:
        rows = self.db.execute(
            "SELECT chat_id FROM users WHERE group_name = ? AND notify = 1 AND active = 1", (group,)
        ).fetchall()
        return [r["chat_id"] for r in rows]

    def stats(self) -> tuple[int, int]:
        total = self.db.execute("SELECT COUNT(*) FROM users WHERE active = 1").fetchone()[0]
        subs = self.db.execute(
            "SELECT COUNT(*) FROM users WHERE active = 1 AND notify = 1 AND group_name IS NOT NULL"
        ).fetchone()[0]
        return total, subs

    # --- источники ----------------------------------------------------
    def get_source(self, day_idx: int) -> Source | None:
        r = self.db.execute("SELECT * FROM sources WHERE day_idx = ?", (day_idx,)).fetchone()
        return Source(r["day_idx"], r["url"], r["hash"], r["checked_at"]) if r else None

    def set_source(self, day_idx: int, url: str, hash_: str, checked_at: float) -> None:
        self.db.execute(
            "INSERT INTO sources (day_idx, url, hash, checked_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day_idx) DO UPDATE SET url=excluded.url, hash=excluded.hash, checked_at=excluded.checked_at",
            (day_idx, url, hash_, checked_at),
        )
        self.db.commit()

    # --- расписания ---------------------------------------------------
    def has_any_schedule(self) -> bool:
        return self.db.execute("SELECT 1 FROM schedules LIMIT 1").fetchone() is not None

    def get_group_day(self, date: dt.date, group: str) -> list[dict] | None:
        r = self.db.execute(
            "SELECT data FROM schedules WHERE date = ? AND group_name = ?", (date.isoformat(), group)
        ).fetchone()
        return json.loads(r["data"]) if r else None

    def get_day_raw(self, date: dt.date) -> dict[str, str]:
        rows = self.db.execute(
            "SELECT group_name, data FROM schedules WHERE date = ?", (date.isoformat(),)
        ).fetchall()
        return {r["group_name"]: r["data"] for r in rows}

    def save_day(self, date: dt.date, groups: dict[str, list[dict]]) -> None:
        iso = date.isoformat()
        self.db.execute("DELETE FROM schedules WHERE date = ?", (iso,))
        self.db.executemany(
            "INSERT INTO schedules (date, group_name, data) VALUES (?, ?, ?)",
            [(iso, g, json.dumps(ls, ensure_ascii=False, sort_keys=True)) for g, ls in groups.items()],
        )
        self.db.commit()

    def dates(self, since: dt.date) -> list[dt.date]:
        rows = self.db.execute(
            "SELECT DISTINCT date FROM schedules WHERE date >= ? ORDER BY date", (since.isoformat(),)
        ).fetchall()
        return [dt.date.fromisoformat(r["date"]) for r in rows]

    def groups(self, since: dt.date, location: str | None = None) -> list[str]:
        rows = self.db.execute(
            "SELECT DISTINCT group_name FROM schedules WHERE date >= ? ORDER BY group_name", (since.isoformat(),)
        ).fetchall()
        groups = [r["group_name"] for r in rows]
        if location:
            prefix = location + "::"
            return [g for g in groups if g.startswith(prefix)]
        return groups

    def purge_before(self, date: dt.date) -> None:
        self.db.execute("DELETE FROM schedules WHERE date < ?", (date.isoformat(),))
        self.db.commit()


def course_of(group: str, today: dt.date) -> int | None:
    """Курс по году набора в названии группы: ГЭС-26-1 в 2026/27 уч. году -> 1."""
    group = group.split("::", 1)[-1]
    m = re.search(r"-(\d{2})(?:-|$)", group)
    if not m:
        return None
    academic_year = today.year if today.month >= 9 else today.year - 1
    course = academic_year - 2000 - int(m.group(1)) + 1
    return course if 1 <= course <= 6 else None
