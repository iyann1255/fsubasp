from __future__ import annotations
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Optional, Protocol

from pymongo import MongoClient

@dataclass
class FileRecord:
    file_id: str
    db_chat_id: int
    db_message_id: int
    kind: str
    caption: str | None = None

@dataclass
class UserFsubState:
    user_id: int
    offset: int
    last_rotated_ts: int  # unix seconds

class Storage(Protocol):
    # files
    def upsert(self, rec: FileRecord) -> None: ...
    def get(self, file_id: str) -> Optional[FileRecord]: ...

    # short links
    def save_link(self, code: str, file_id: str) -> None: ...
    def get_file_id_by_code(self, code: str) -> Optional[str]: ...

    # fsub state
    def get_user_state(self, user_id: int) -> UserFsubState: ...
    def bump_rotate(self, user_id: int) -> UserFsubState: ...
    def set_rotated_ts(self, user_id: int, ts: int) -> None: ...

    # stats
    def inc_skip(self, check_chat: str, n: int = 1) -> None: ...
    def top_skipped(self, limit: int = 10) -> list[tuple[str, int]]: ...

class SQLiteStorage:
    def __init__(self, path: str = "data.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file_id TEXT PRIMARY KEY,
            db_chat_id INTEGER NOT NULL,
            db_message_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            caption TEXT
        )
        """)
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS links (
            code TEXT PRIMARY KEY,
            file_id TEXT NOT NULL
        )
        """)
        # per-user rotate state
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS user_fsub_state (
            user_id INTEGER PRIMARY KEY,
            offset INTEGER NOT NULL DEFAULT 0,
            last_rotated_ts INTEGER NOT NULL DEFAULT 0
        )
        """)
        # skip stats per channel/check_chat
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS fsub_skip_stats (
            check_chat TEXT PRIMARY KEY,
            skips INTEGER NOT NULL DEFAULT 0
        )
        """)
        self.conn.commit()

    def upsert(self, rec: FileRecord) -> None:
        self.conn.execute("""
        INSERT INTO files(file_id, db_chat_id, db_message_id, kind, caption)
        VALUES(?,?,?,?,?)
        ON CONFLICT(file_id) DO UPDATE SET
          db_chat_id=excluded.db_chat_id,
          db_message_id=excluded.db_message_id,
          kind=excluded.kind,
          caption=excluded.caption
        """, (rec.file_id, rec.db_chat_id, rec.db_message_id, rec.kind, rec.caption))
        self.conn.commit()

    def get(self, file_id: str) -> Optional[FileRecord]:
        cur = self.conn.execute(
            "SELECT file_id, db_chat_id, db_message_id, kind, caption FROM files WHERE file_id=?",
            (file_id,),
        )
        row = cur.fetchone()
        return FileRecord(*row) if row else None

    def save_link(self, code: str, file_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO links(code, file_id) VALUES(?, ?)",
            (code, file_id),
        )
        self.conn.commit()

    def get_file_id_by_code(self, code: str) -> Optional[str]:
        cur = self.conn.execute("SELECT file_id FROM links WHERE code=?", (code,))
        row = cur.fetchone()
        return row[0] if row else None

    # ===== state =====
    def get_user_state(self, user_id: int) -> UserFsubState:
        cur = self.conn.execute(
            "SELECT user_id, offset, last_rotated_ts FROM user_fsub_state WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if row:
            return UserFsubState(*row)
        # create default
        self.conn.execute(
            "INSERT OR IGNORE INTO user_fsub_state(user_id, offset, last_rotated_ts) VALUES(?, 0, 0)",
            (user_id,),
        )
        self.conn.commit()
        return UserFsubState(user_id=user_id, offset=0, last_rotated_ts=0)

    def bump_rotate(self, user_id: int) -> UserFsubState:
        st = self.get_user_state(user_id)
        new_offset = st.offset + 1
        now = int(time.time())
        self.conn.execute(
            "UPDATE user_fsub_state SET offset=?, last_rotated_ts=? WHERE user_id=?",
            (new_offset, now, user_id),
        )
        self.conn.commit()
        return UserFsubState(user_id=user_id, offset=new_offset, last_rotated_ts=now)

    def set_rotated_ts(self, user_id: int, ts: int) -> None:
        self.conn.execute(
            "UPDATE user_fsub_state SET last_rotated_ts=? WHERE user_id=?",
            (ts, user_id),
        )
        self.conn.commit()

    # ===== stats =====
    def inc_skip(self, check_chat: str, n: int = 1) -> None:
        self.conn.execute(
            "INSERT INTO fsub_skip_stats(check_chat, skips) VALUES(?, ?) "
            "ON CONFLICT(check_chat) DO UPDATE SET skips = skips + ?",
            (check_chat, n, n),
        )
        self.conn.commit()

    def top_skipped(self, limit: int = 10) -> list[tuple[str, int]]:
        cur = self.conn.execute(
            "SELECT check_chat, skips FROM fsub_skip_stats ORDER BY skips DESC LIMIT ?",
            (limit,),
        )
        return [(r[0], int(r[1])) for r in cur.fetchall()]

class MongoStorage:
    def __init__(self, uri: str, db_name: str) -> None:
        if not uri:
            raise ValueError("MONGO_URI kosong")
        self.client = MongoClient(uri)
        db = self.client[db_name]

        self.files = db["files"]
        self.files.create_index("file_id", unique=True)

        self.links = db["links"]
        self.links.create_index("code", unique=True)

        self.user_state = db["user_fsub_state"]
        self.user_state.create_index("user_id", unique=True)

        self.skip_stats = db["fsub_skip_stats"]
        self.skip_stats.create_index("check_chat", unique=True)

    def upsert(self, rec: FileRecord) -> None:
        self.files.update_one({"file_id": rec.file_id}, {"$set": rec.__dict__}, upsert=True)

    def get(self, file_id: str) -> Optional[FileRecord]:
        doc = self.files.find_one({"file_id": file_id}, {"_id": 0})
        return FileRecord(**doc) if doc else None

    def save_link(self, code: str, file_id: str) -> None:
        self.links.update_one({"code": code}, {"$set": {"code": code, "file_id": file_id}}, upsert=True)

    def get_file_id_by_code(self, code: str) -> Optional[str]:
        doc = self.links.find_one({"code": code}, {"_id": 0, "file_id": 1})
        return doc["file_id"] if doc else None

    def get_user_state(self, user_id: int) -> UserFsubState:
        doc = self.user_state.find_one({"user_id": user_id}, {"_id": 0})
        if doc:
            return UserFsubState(user_id=int(doc["user_id"]), offset=int(doc.get("offset", 0)), last_rotated_ts=int(doc.get("last_rotated_ts", 0)))
        self.user_state.update_one(
            {"user_id": user_id},
            {"$setOnInsert": {"user_id": user_id, "offset": 0, "last_rotated_ts": 0}},
            upsert=True,
        )
        return UserFsubState(user_id=user_id, offset=0, last_rotated_ts=0)

    def bump_rotate(self, user_id: int) -> UserFsubState:
        now = int(time.time())
        doc = self.user_state.find_one_and_update(
            {"user_id": user_id},
            {"$inc": {"offset": 1}, "$set": {"last_rotated_ts": now}, "$setOnInsert": {"user_id": user_id}},
            upsert=True,
            return_document=True,
        )
        # Some pymongo versions return dict-like; normalize
        st = self.user_state.find_one({"user_id": user_id}, {"_id": 0})
        return UserFsubState(user_id=int(st["user_id"]), offset=int(st.get("offset", 0)), last_rotated_ts=int(st.get("last_rotated_ts", 0)))

    def set_rotated_ts(self, user_id: int, ts: int) -> None:
        self.user_state.update_one({"user_id": user_id}, {"$set": {"last_rotated_ts": int(ts)}}, upsert=True)

    def inc_skip(self, check_chat: str, n: int = 1) -> None:
        self.skip_stats.update_one(
            {"check_chat": check_chat},
            {"$inc": {"skips": int(n)}, "$setOnInsert": {"check_chat": check_chat}},
            upsert=True,
        )

    def top_skipped(self, limit: int = 10) -> list[tuple[str, int]]:
        cur = self.skip_stats.find({}, {"_id": 0}).sort("skips", -1).limit(int(limit))
        return [(d["check_chat"], int(d.get("skips", 0))) for d in cur]

def build_storage(backend: str, mongo_uri: str, mongo_db: str) -> Storage:
    backend = (backend or "sqlite").lower()
    if backend == "mongo":
        return MongoStorage(mongo_uri, mongo_db)
    return SQLiteStorage(os.getenv("SQLITE_PATH", "data.db"))
