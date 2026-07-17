"""SQLite-backed storage for collected product history."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
from uuid import uuid4


class HistoryStore:
    """Store product snapshots without loading the complete history into memory."""

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
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS history_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    collection_id TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    commodity_id TEXT,
                    product_id TEXT,
                    shop_id TEXT,
                    merchant_product_id TEXT,
                    mobile TEXT,
                    name TEXT,
                    image_url TEXT,
                    shop_name TEXT,
                    shop_score TEXT,
                    month_sale TEXT,
                    detail_url TEXT,
                    raw_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_history_collected_at
                    ON history_records(collected_at DESC);
                CREATE INDEX IF NOT EXISTS idx_history_collection_id
                    ON history_records(collection_id);
                CREATE INDEX IF NOT EXISTS idx_history_product_id
                    ON history_records(product_id);
                CREATE INDEX IF NOT EXISTS idx_history_shop_id
                    ON history_records(shop_id);
                CREATE INDEX IF NOT EXISTS idx_history_merchant_product_id
                    ON history_records(merchant_product_id);
                """
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(history_records)")}
            if "shop_score" not in columns:
                connection.execute("ALTER TABLE history_records ADD COLUMN shop_score TEXT")
            if "mobile" not in columns:
                connection.execute("ALTER TABLE history_records ADD COLUMN mobile TEXT")

    @staticmethod
    def _text(value: Any) -> str | None:
        return None if value is None else str(value)

    @staticmethod
    def _raw_shop_fields(record: dict[str, Any]) -> tuple[str | None, str | None, Any]:
        raw = record.get("raw") or {}
        base_model = raw.get("base_model") if isinstance(raw, dict) else {}
        shop_info = base_model.get("shop_info") if isinstance(base_model, dict) else {}
        shop_info = shop_info if isinstance(shop_info, dict) else {}
        score_info = shop_info.get("shop_score_info") or {}
        score = score_info.get("shop_score") if isinstance(score_info, dict) else {}
        score = score.get("score") if isinstance(score, dict) else score
        product_info = base_model.get("product_info") if isinstance(base_model, dict) else {}
        product_info = product_info if isinstance(product_info, dict) else {}
        month_sale = product_info.get("month_sale")
        if isinstance(month_sale, dict):
            month_sale = month_sale.get("origin")
        text = lambda value: None if value is None else str(value)
        return text(shop_info.get("shop_name")), text(score), month_sale

    def start_collection(self) -> dict[str, str]:
        return {
            "collection_id": uuid4().hex,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }

    def save_records(
        self,
        records: Iterable[dict[str, Any]],
        *,
        collection_id: str | None = None,
        collected_at: str | None = None,
    ) -> dict[str, Any]:
        records = list(records)
        collection_id = collection_id or uuid4().hex
        collected_at = collected_at or datetime.now(timezone.utc).isoformat()
        values = [
            (
                collection_id,
                collected_at,
                self._text(record.get("commodity_id")),
                self._text(record.get("product_id")),
                self._text(record.get("shop_id")),
                self._text(record.get("merchant_product_id")),
                self._text(record.get("mobile")),
                self._text(record.get("name")),
                self._text(record.get("image_url")),
                self._text(record.get("shop_name")) or self._raw_shop_fields(record)[0],
                self._text(record.get("shop_score")) or self._raw_shop_fields(record)[1],
                self._text(record.get("month_sale")) or self._text(self._raw_shop_fields(record)[2]),
                self._text(record.get("detail_url")),
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
            )
            for record in records
        ]
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO history_records (
                    collection_id, collected_at, commodity_id, product_id, shop_id,
                    merchant_product_id, mobile, name, image_url, shop_name, shop_score,
                    month_sale, detail_url, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )
        return {"collection_id": collection_id, "collected_at": collected_at, "count": len(records)}

    def migrate_json_once(self, json_path: Path) -> dict[str, Any] | None:
        """Import the old snapshot only when the database is still empty."""
        with self._connect() as connection:
            has_records = connection.execute("SELECT 1 FROM history_records LIMIT 1").fetchone()
        if has_records or not json_path.exists():
            return None
        records = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(records, list) or not records:
            return None
        result = self.save_records(records)
        result["source"] = str(json_path)
        return result

    def list_records(
        self, *, limit: int = 100, offset: int = 0, query: str | None = None,
        collection_id: str | None = None, shop_score_lt: float | None = None,
        month_sale_gt: float | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        clauses = []
        parameters: list[str | int] = []
        if query:
            clauses.append("(name LIKE ? OR shop_name LIKE ? OR product_id = ? OR commodity_id = ? OR merchant_product_id = ?)")
            wildcard = f"%{query}%"
            parameters.extend([wildcard, wildcard, query, query, query])
        if collection_id:
            clauses.append("collection_id = ?")
            parameters.append(collection_id)
        if shop_score_lt is not None:
            clauses.append("CAST(shop_score AS REAL) < ?")
            parameters.append(shop_score_lt)
        if month_sale_gt is not None:
            clauses.append("CAST(month_sale AS REAL) > ?")
            parameters.append(month_sale_gt)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM history_records {where} ORDER BY collected_at DESC, id DESC LIMIT ? OFFSET ?",
                [*parameters, limit, offset],
            ).fetchall()
        result = []
        for row in rows:
            item = json.loads(row["raw_json"])
            shop_name, shop_score, month_sale = self._raw_shop_fields(item)
            item["shop_name"] = item.get("shop_name") or row["shop_name"] or shop_name
            item["shop_score"] = item.get("shop_score") or row["shop_score"] or shop_score
            item["month_sale"] = item.get("month_sale") or row["month_sale"] or self._text(month_sale)
            item["mobile"] = item.get("mobile") or row["mobile"]
            item["history_id"] = row["id"]
            item["collection_id"] = row["collection_id"]
            item["collected_at"] = row["collected_at"]
            result.append(item)
        return result

    def list_all_records(
        self, *, query: str | None = None, collection_id: str | None = None,
        shop_score_lt: float | None = None, month_sale_gt: float | None = None,
    ) -> list[dict[str, Any]]:
        """Return every record matching the filters for full-page exports."""
        records: list[dict[str, Any]] = []
        offset = 0
        batch_size = 1000
        while True:
            batch = self.list_records(
                limit=batch_size,
                offset=offset,
                query=query,
                collection_id=collection_id,
                shop_score_lt=shop_score_lt,
                month_sale_gt=month_sale_gt,
            )
            records.extend(batch)
            if len(batch) < batch_size:
                return records
            offset += len(batch)

    def count(self, *, collection_id: str | None = None) -> int:
        clause = "WHERE collection_id = ?" if collection_id else ""
        parameters = [collection_id] if collection_id else []
        with self._connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM history_records {clause}", parameters).fetchone()[0])

    def delete_records(self, history_ids: Iterable[int | str]) -> int:
        ids = []
        for history_id in history_ids:
            try:
                value = int(history_id)
            except (TypeError, ValueError):
                continue
            if value > 0 and value not in ids:
                ids.append(value)
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM history_records WHERE id IN ({placeholders})",
                ids,
            )
            return int(cursor.rowcount)

    def list_shop_ids(self) -> set[str]:
        """Return shop IDs already present in the history database."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT shop_id FROM history_records "
                "WHERE shop_id IS NOT NULL AND shop_id <> ''"
            ).fetchall()
        return {str(row[0]) for row in rows}
