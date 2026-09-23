from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self) -> None:
        with self._connect() as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS processed_images (
                    image_hash TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS occupations (
                    occupation_key TEXT PRIMARY KEY,
                    occupied_at TEXT NOT NULL,
                    city_name TEXT NOT NULL,
                    guild_name TEXT NOT NULL,
                    output_text TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def is_image_processed(self, image_hash: str) -> bool:
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM processed_images WHERE image_hash=?", (image_hash,)
            ).fetchone()
        return row is not None

    def mark_image_processed(self, image_hash: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT OR IGNORE INTO processed_images(image_hash, processed_at) VALUES(?, ?)",
                (image_hash, datetime.now().isoformat(timespec="seconds")),
            )

    def is_occupation_duplicate(self, occupation_key: str, hours: int) -> bool:
        threshold = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
        with self._connect() as con:
            row = con.execute(
                "SELECT 1 FROM occupations WHERE occupation_key=? AND created_at>=?",
                (occupation_key, threshold),
            ).fetchone()
        return row is not None

    def save_occupation(
        self,
        occupation_key: str,
        occupied_at: str,
        city_name: str,
        guild_name: str,
        output_text: str,
    ) -> None:
        with self._connect() as con:
            con.execute(
                """
                INSERT OR REPLACE INTO occupations
                (occupation_key, occupied_at, city_name, guild_name, output_text, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    occupation_key,
                    occupied_at,
                    city_name,
                    guild_name,
                    output_text,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    def cleanup(self, occupation_hours: int, image_days: int) -> None:
        occ_threshold = (datetime.now() - timedelta(hours=occupation_hours)).isoformat(timespec="seconds")
        img_threshold = (datetime.now() - timedelta(days=image_days)).isoformat(timespec="seconds")
        with self._connect() as con:
            con.execute("DELETE FROM occupations WHERE created_at < ?", (occ_threshold,))
            con.execute("DELETE FROM processed_images WHERE processed_at < ?", (img_threshold,))
