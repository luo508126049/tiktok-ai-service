"""SQLite-backed storage for collected product history."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    history_id INTEGER NOT NULL UNIQUE,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    FOREIGN KEY(history_id) REFERENCES history_records(id)
                );
                CREATE INDEX IF NOT EXISTS idx_product_claims_username
                    ON product_claims(username, claimed_at DESC);
                CREATE TABLE IF NOT EXISTS liaison_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    claim_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    order_requirement TEXT NOT NULL DEFAULT '',
                    task_amount REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(claim_id) REFERENCES product_claims(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_liaison_records_claim
                    ON liaison_records(claim_id, created_at DESC);
                """
            )
            liaison_columns = {row[1] for row in connection.execute("PRAGMA table_info(liaison_records)")}
            for column, definition in (
                ("unit_price", "REAL NOT NULL DEFAULT 0"),
                ("total_orders", "INTEGER NOT NULL DEFAULT 0"),
                ("downstream_unit_cost", "REAL NOT NULL DEFAULT 0"),
                ("net_profit", "REAL NOT NULL DEFAULT 0"),
            ):
                if column not in liaison_columns:
                    connection.execute(f"ALTER TABLE liaison_records ADD COLUMN {column} {definition}")
            connection.execute(
                "UPDATE liaison_records SET net_profit=ROUND(task_amount, 2) "
                "WHERE net_profit=0 AND task_amount<>0"
            )

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
        month_sale_gt: float | None = None, has_contact: bool | str | None = None,
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
        if has_contact == "valid_phone":
            wechat = "TRIM(COALESCE(merchant_product_id, ''))"
            mobile = "TRIM(COALESCE(mobile, ''))"
            clauses.append(
                f"({wechat} <> '' OR ({wechat} = '' AND length({mobile}) = 11 "
                f"AND substr({mobile}, 1, 1) = '1' AND {mobile} NOT GLOB '*[^0-9]*'))"
            )
        elif has_contact is True:
            clauses.append("(TRIM(COALESCE(merchant_product_id, '')) <> '' OR TRIM(COALESCE(mobile, '')) <> '')")
        elif has_contact is False:
            clauses.append("(TRIM(COALESCE(merchant_product_id, '')) = '' AND TRIM(COALESCE(mobile, '')) = '')")
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
        has_contact: bool | str | None = None,
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
                has_contact=has_contact,
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

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        item = json.loads(row["raw_json"])
        item.update({
            "history_id": row["history_id"], "claim_id": row["claim_id"],
            "username": row["username"], "display_name": row["display_name"],
            "claimed_at": row["claimed_at"],
        })
        if "mobile" in row.keys():
            item["mobile"] = item.get("mobile") or row["mobile"]
        item["wechat"] = item.get("wechat") or item.get("merchant_product_id")
        return item

    def claim_info(self, history_ids: Iterable[int | str]) -> dict[int, dict[str, Any]]:
        ids = [int(value) for value in history_ids if str(value).isdigit()]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT history_id, username, display_name, claimed_at, id AS claim_id "
                f"FROM product_claims WHERE history_id IN ({marks})", ids
            ).fetchall()
        return {int(row["history_id"]): dict(row) for row in rows}

    def claim_product(self, history_id: int | str, username: str, display_name: str) -> dict[str, Any]:
        try:
            history_id = int(history_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("商品记录无效") from exc
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM history_records WHERE id = ?", (history_id,)).fetchone() is None:
                raise ValueError("商品不存在")
            if connection.execute("SELECT 1 FROM product_claims WHERE history_id = ?", (history_id,)).fetchone():
                raise ValueError("该商品已被其他用户认领")
            cursor = connection.execute(
                "INSERT INTO product_claims(history_id, username, display_name, claimed_at) VALUES (?, ?, ?, ?)",
                (history_id, username, display_name, now),
            )
            return {"claim_id": cursor.lastrowid, "history_id": history_id, "claimed_at": now}

    def cancel_claim(self, claim_id: int | str, username: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM product_claims WHERE id = ? AND username = ?", (claim_id, username)
            )
            if cursor.rowcount != 1:
                raise ValueError("只能取消当前用户认领的商品")

    def list_claimed_products(self, username: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        if username:
            clauses.append("c.username = ?")
            params.append(username)
        if status:
            clauses.append("EXISTS (SELECT 1 FROM liaison_records lr WHERE lr.claim_id = c.id AND lr.status = ?)")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT h.raw_json, h.mobile, h.id AS history_id, c.id AS claim_id, c.username, c.display_name, c.claimed_at "
                f"FROM product_claims c JOIN history_records h ON h.id = c.history_id {where} "
                "ORDER BY c.claimed_at DESC, c.id DESC", params
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def get_claim(self, claim_id: int | str, username: str | None = None) -> dict[str, Any] | None:
        query = "SELECT h.raw_json, h.mobile, h.id AS history_id, c.id AS claim_id, c.username, c.display_name, c.claimed_at FROM product_claims c JOIN history_records h ON h.id=c.history_id WHERE c.id=?"
        params: list[Any] = [claim_id]
        if username:
            query += " AND c.username=?"
            params.append(username)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return self._task_row(row) if row else None

    def list_liaison_records(self, claim_id: int | str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, claim_id, status, order_requirement, unit_price, total_orders, "
                "downstream_unit_cost, COALESCE(net_profit, task_amount, 0) AS net_profit, "
                "COALESCE(net_profit, task_amount, 0) AS task_amount, "
                "ROUND(COALESCE(unit_price, 0) * COALESCE(total_orders, 0), 2) AS customer_payment, "
                "created_at, updated_at "
                "FROM liaison_records WHERE claim_id=? ORDER BY created_at DESC, id DESC", (claim_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _nonnegative_money(value: str | float | None, field_name: str) -> float:
        try:
            parsed = round(float(value or 0), 2)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name}必须是数字") from exc
        if not math.isfinite(parsed) or parsed < 0:
            raise ValueError(f"{field_name}不能小于 0")
        return parsed

    @staticmethod
    def _nonnegative_integer(value: str | int | None, field_name: str) -> int:
        try:
            parsed = float(value or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name}必须是整数") from exc
        if not math.isfinite(parsed) or not parsed.is_integer() or parsed < 0:
            raise ValueError(f"{field_name}必须是大于等于 0 的整数")
        return int(parsed)

    def add_liaison_record(
        self,
        claim_id: int | str,
        username: str,
        status: str,
        requirement: str,
        unit_price: str | float | None,
        total_orders: str | int | None,
        downstream_unit_cost: str | float | None,
    ) -> dict[str, Any]:
        allowed = {"商家拒绝", "商家正在考虑", "商家已下单"}
        if status not in allowed:
            raise ValueError("商家对接情况无效")
        price = self._nonnegative_money(unit_price, "本次下单单价")
        order_count = self._nonnegative_integer(total_orders, "总单数")
        downstream_cost = self._nonnegative_money(downstream_unit_cost, "每单下游服务商抽取价")
        net_profit = round(order_count * price - order_count * downstream_cost, 2)
        claim = self.get_claim(claim_id, username)
        if claim is None:
            raise ValueError("只能为当前用户认领的商品新增对接记录")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO liaison_records(claim_id,status,order_requirement,unit_price,total_orders,"
                "downstream_unit_cost,net_profit,task_amount,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (claim_id, status, str(requirement or "").strip(), price, order_count, downstream_cost, net_profit, net_profit, now, now),
            )
        return {
            "id": cursor.lastrowid,
            "claim_id": claim_id,
            "status": status,
            "order_requirement": str(requirement or "").strip(),
            "unit_price": price,
            "total_orders": order_count,
            "downstream_unit_cost": downstream_cost,
            "customer_payment": round(order_count * price, 2),
            "net_profit": net_profit,
            "task_amount": net_profit,
            "created_at": now,
            "updated_at": now,
        }

    def update_liaison_financials(
        self,
        record_id: int | str,
        username: str,
        unit_price: str | float | None,
        total_orders: str | int | None,
        downstream_unit_cost: str | float | None,
    ) -> None:
        price = self._nonnegative_money(unit_price, "本次下单单价")
        order_count = self._nonnegative_integer(total_orders, "总单数")
        downstream_cost = self._nonnegative_money(downstream_unit_cost, "每单下游服务商抽取价")
        net_profit = round(order_count * price - order_count * downstream_cost, 2)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE liaison_records SET unit_price=?, total_orders=?, downstream_unit_cost=?, "
                "net_profit=?, task_amount=?, updated_at=? WHERE id=? AND claim_id IN "
                "(SELECT id FROM product_claims WHERE username=?)",
                (price, order_count, downstream_cost, net_profit, net_profit, datetime.now(timezone.utc).isoformat(), record_id, username),
            )
            if cursor.rowcount != 1:
                raise ValueError("只能修改当前用户商品的财务字段")

    def delete_liaison_record(self, record_id: int | str, username: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM liaison_records WHERE id=? AND claim_id IN (SELECT id FROM product_claims WHERE username=?)",
                (record_id, username),
            )
            if cursor.rowcount != 1:
                raise ValueError("只能删除当前用户商品的对接记录")

    def dashboard_stats(self, start_date: str, end_date: str, username: str | None = None) -> dict[str, Any]:
        """Aggregate liaison activity by user and Beijing calendar day."""
        try:
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
        except ValueError as exc:
            raise ValueError("日期范围无效") from exc
        if start > end:
            raise ValueError("开始日期不能晚于结束日期")
        if (end - start).days > 366:
            raise ValueError("日期范围不能超过 366 天")
        dates = [(start + timedelta(days=index)).isoformat() for index in range((end - start).days + 1)]
        shop_key = "CASE WHEN h.shop_id IS NOT NULL AND h.shop_id <> '' THEN h.shop_id ELSE 'history:' || h.id END"
        day_key = "date(l.created_at, '+8 hours')"
        user_scope = " WHERE c.username = ?" if username else ""
        scope_params = (username,) if username else ()
        day_scope = f" WHERE {day_key} BETWEEN ? AND ?" + (" AND c.username = ?" if username else "")
        day_params = (start_date, end_date) + ((username,) if username else ())
        with self._connect() as connection:
            user_merchants = connection.execute(
                f"SELECT c.username, c.display_name, COUNT(DISTINCT {shop_key}) AS merchant_count "
                "FROM liaison_records l JOIN product_claims c ON c.id=l.claim_id "
                "JOIN history_records h ON h.id=c.history_id "
                f"{user_scope} GROUP BY c.username, c.display_name "
                "ORDER BY merchant_count DESC, c.username",
                scope_params,
            ).fetchall()
            merchant_details = connection.execute(
                f"SELECT c.username, c.display_name, {shop_key} AS shop_key, "
                "COALESCE(NULLIF(h.shop_name, ''), '未命名商家') AS shop_name, "
                "SUM(COALESCE(l.net_profit, l.task_amount, 0)) AS net_profit, COUNT(DISTINCT h.id) AS product_count "
                "FROM liaison_records l JOIN product_claims c ON c.id=l.claim_id "
                "JOIN history_records h ON h.id=c.history_id "
                f"{user_scope} GROUP BY c.username, c.display_name, shop_key, shop_name "
                "ORDER BY c.display_name, net_profit DESC, shop_name",
                scope_params,
            ).fetchall()
            user_amounts = connection.execute(
                "SELECT c.username, c.display_name, COALESCE(SUM(COALESCE(l.net_profit, l.task_amount, 0)), 0) AS net_profit "
                "FROM liaison_records l JOIN product_claims c ON c.id=l.claim_id "
                "JOIN history_records h ON h.id=c.history_id "
                f"{user_scope} GROUP BY c.username, c.display_name "
                "ORDER BY net_profit DESC, c.username",
                scope_params,
            ).fetchall()
            daily_contacts = connection.execute(
                f"SELECT {day_key} AS day, COUNT(DISTINCT {shop_key}) AS contacted, "
                f"COUNT(DISTINCT CASE WHEN l.status = '商家已下单' THEN {shop_key} END) AS agreed, "
                f"COUNT(DISTINCT CASE WHEN l.status = '商家拒绝' THEN {shop_key} END) AS refused "
                "FROM liaison_records l JOIN product_claims c ON c.id=l.claim_id "
                "JOIN history_records h ON h.id=c.history_id "
                f"{day_scope} GROUP BY day ORDER BY day",
                day_params,
            ).fetchall()
            daily_amounts = connection.execute(
                f"SELECT {day_key} AS day, c.username, c.display_name, "
                "COALESCE(SUM(COALESCE(l.net_profit, l.task_amount, 0)), 0) AS net_profit "
                "FROM liaison_records l JOIN product_claims c ON c.id=l.claim_id "
                "JOIN history_records h ON h.id=c.history_id "
                f"{day_scope} GROUP BY day, c.username, c.display_name "
                "ORDER BY day, c.display_name",
                day_params,
            ).fetchall()
        details: dict[str, list[dict[str, Any]]] = {}
        for row in merchant_details:
            details.setdefault(row["username"], []).append({
                "shop_id": row["shop_key"],
                "shop_name": row["shop_name"],
                "net_profit": round(float(row["net_profit"] or 0), 2),
                "product_count": int(row["product_count"] or 0),
            })
        contact_by_day = {date: {"contacted": 0, "agreed": 0, "refused": 0} for date in dates}
        for row in daily_contacts:
            if row["day"] in contact_by_day:
                contact_by_day[row["day"]] = {
                    "contacted": int(row["contacted"] or 0),
                    "agreed": int(row["agreed"] or 0),
                    "refused": int(row["refused"] or 0),
                }
        users: dict[str, dict[str, Any]] = {}
        for row in daily_amounts:
            users.setdefault(row["username"], {
                "username": row["username"], "display_name": row["display_name"],
                "values": [0.0 for _ in dates],
            })
            if row["day"] in dates:
                users[row["username"]]["values"][dates.index(row["day"])] = round(float(row["net_profit"] or 0), 2)
        return {
            "start_date": start_date,
            "end_date": end_date,
            "dates": dates,
            "user_merchants": [
                {"username": row["username"], "display_name": row["display_name"], "merchant_count": int(row["merchant_count"] or 0)}
                for row in user_merchants
            ],
            "merchant_details": details,
            "user_amounts": [
                {"username": row["username"], "display_name": row["display_name"], "net_profit": round(float(row["net_profit"] or 0), 2)}
                for row in user_amounts
            ],
            "daily_contacts": [contact_by_day[date] | {"date": date} for date in dates],
            "daily_amounts": list(users.values()),
        }
