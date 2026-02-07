#!/usr/bin/env python3
"""
SQLite storage backend for McApp.

Provides persistent message storage as an alternative to in-memory deque.
Uses Python's built-in sqlite3 with asyncio.to_thread() for async operations.
"""
import asyncio
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean
from typing import Any

from .logging_setup import get_logger

VERSION = "v0.50.0"

logger = get_logger(__name__)

# Schema version for migrations
SCHEMA_VERSION = 1

# Constants matching message_storage.py
BUCKET_SECONDS = 5 * 60
VALID_RSSI_RANGE = (-140, -30)
VALID_SNR_RANGE = (-30, 12)
SEVEN_DAYS_MS = 7 * 24 * 60 * 60 * 1000
GAP_THRESHOLD_MULTIPLIER = 6
MIN_DATAPOINTS_FOR_STATS = 100

CREATE_SCHEMA_SQL = """
-- Main messages table
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT UNIQUE,
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    msg TEXT,
    type TEXT DEFAULT 'msg',
    timestamp INTEGER NOT NULL,
    rssi INTEGER,
    snr REAL,
    src_type TEXT,
    raw_json TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Indexes for common queries
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp);
CREATE INDEX IF NOT EXISTS idx_messages_src ON messages(src);
CREATE INDEX IF NOT EXISTS idx_messages_dst ON messages(dst);
CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(type);

-- Schema version tracking
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

-- Precomputed mheard statistics (optional caching)
CREATE TABLE IF NOT EXISTS mheard_cache (
    callsign TEXT PRIMARY KEY,
    last_seen INTEGER,
    message_count INTEGER,
    avg_rssi REAL,
    avg_snr REAL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


class SQLiteStorage:
    """
    SQLite-based message storage backend.

    Provides the same interface as MessageStorageHandler but with
    persistent SQLite storage.
    """

    def __init__(self, db_path: str | Path, max_size_mb: int = 50):
        self.db_path = Path(db_path)
        self.max_size_mb = max_size_mb
        self._initialized = False

        # Ensure parent directory exists
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        logger.info("SQLite storage initialized at %s", self.db_path)

    async def initialize(self) -> None:
        """Initialize database schema."""
        if self._initialized:
            return

        def _init_db() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.executescript(CREATE_SCHEMA_SQL)

                # Check/set schema version
                cursor = conn.execute("SELECT version FROM schema_version LIMIT 1")
                row = cursor.fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_version (version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )

                conn.commit()

        await asyncio.to_thread(_init_db)
        self._initialized = True
        logger.info("SQLite database initialized")

    async def _execute(
        self,
        query: str,
        params: tuple = (),
        fetch: bool = True,
    ) -> list[dict[str, Any]]:
        """Execute a query in thread pool."""

        def _run() -> list[dict[str, Any]]:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.execute(query, params)
                if fetch:
                    return [dict(row) for row in cursor.fetchall()]
                conn.commit()
                return []

        return await asyncio.to_thread(_run)

    async def _execute_many(self, query: str, params_list: list[tuple]) -> None:
        """Execute many queries in thread pool."""

        def _run() -> None:
            with sqlite3.connect(self.db_path) as conn:
                conn.executemany(query, params_list)
                conn.commit()

        await asyncio.to_thread(_run)

    async def store_message(self, message: dict[str, Any], raw: str) -> None:
        """Store a message with automatic filtering."""
        if not isinstance(message, dict):
            logger.warning("store_message: invalid input, message is not a dict")
            return

        # Filter conditions (matching MessageStorageHandler)
        if self._should_filter_message(message):
            return

        msg_id = message.get("msg_id")
        src = message.get("src", "")
        dst = message.get("dst", "")
        msg = message.get("msg", "")
        msg_type = message.get("type", "msg")
        timestamp = message.get("timestamp", int(time.time() * 1000))
        rssi = message.get("rssi")
        snr = message.get("snr")
        src_type = message.get("src_type", "")

        query = """
            INSERT OR IGNORE INTO messages
            (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        params = (msg_id, src, dst, msg, msg_type, timestamp, rssi, snr, src_type, raw)

        await self._execute(query, params, fetch=False)

    def _should_filter_message(self, message: dict[str, Any]) -> bool:
        """Check if message should be filtered out."""
        msg_content = message.get("msg", "")
        src_type = message.get("src_type", "")
        src = message.get("src", "")

        if msg_content.startswith("{CET}"):
            return True
        if src_type == "BLE":
            return True
        if message.get("transformer") in ("mh", "generic_ble"):
            return True
        if src == "response":
            return True
        if src_type == "TEST":
            return True
        if msg_content == "-- invalid character --":
            return True
        if "No core dump" in msg_content:
            return True

        return False

    async def get_message_count(self) -> int:
        """Get current message count."""
        result = await self._execute("SELECT COUNT(*) as count FROM messages")
        return result[0]["count"] if result else 0

    async def get_storage_size_mb(self) -> float:
        """Get current database file size in MB."""

        def _get_size() -> float:
            if self.db_path.exists():
                return self.db_path.stat().st_size / (1024 * 1024)
            return 0.0

        return await asyncio.to_thread(_get_size)

    async def prune_messages(self, prune_hours: int, block_list: list[str]) -> int:
        """Prune old messages and blocked sources."""
        cutoff_ms = int((datetime.utcnow() - timedelta(hours=prune_hours)).timestamp() * 1000)

        # Delete by time
        await self._execute(
            "DELETE FROM messages WHERE timestamp < ?",
            (cutoff_ms,),
            fetch=False,
        )

        # Delete blocked sources
        if block_list:
            placeholders = ",".join("?" * len(block_list))
            await self._execute(
                f"DELETE FROM messages WHERE src IN ({placeholders})",
                tuple(block_list),
                fetch=False,
            )

        # Delete invalid messages
        await self._execute(
            "DELETE FROM messages WHERE msg = '-- invalid character --'"
            " OR msg LIKE '%No core dump%'",
            fetch=False,
        )

        # Vacuum to reclaim space
        await self._execute("VACUUM", fetch=False)

        count = await self.get_message_count()
        logger.info("After pruning: %d messages remaining", count)
        return count

    async def get_initial_payload(self) -> list[str]:
        """Get initial payload for websocket clients."""
        # Get recent messages grouped by destination
        msgs_query = """
            SELECT raw_json FROM messages
            WHERE type = 'msg' AND msg NOT LIKE '%:ack%'
            ORDER BY timestamp DESC
            LIMIT 1000
        """
        msg_rows = await self._execute(msgs_query)

        # Get recent positions grouped by source
        pos_query = """
            SELECT raw_json FROM messages
            WHERE type = 'pos'
            ORDER BY timestamp DESC
            LIMIT 500
        """
        pos_rows = await self._execute(pos_query)

        # Process like MessageStorageHandler
        msgs_per_dst: dict[str, list[str]] = defaultdict(list)
        pos_per_src: dict[str, list[str]] = defaultdict(list)

        for row in msg_rows:
            raw = row["raw_json"]
            try:
                data = json.loads(raw)
                dst = data.get("dst")
                if dst and len(msgs_per_dst[dst]) < 50:
                    msgs_per_dst[dst].append(raw)
            except (json.JSONDecodeError, TypeError):
                continue

        for row in pos_rows:
            raw = row["raw_json"]
            try:
                data = json.loads(raw)
                src = data.get("src")
                if src and len(pos_per_src[src]) < 50:
                    pos_per_src[src].append(raw)
            except (json.JSONDecodeError, TypeError):
                continue

        # Flatten
        msg_msgs = []
        for msg_list in msgs_per_dst.values():
            msg_msgs.extend(reversed(msg_list))

        pos_msgs = []
        for pos_list in pos_per_src.values():
            pos_msgs.extend(pos_list)

        return msg_msgs + pos_msgs

    async def get_smart_initial(self, limit_per_dst: int = 15) -> dict:
        """Get smart initial payload: last N messages per dst + latest pos per src + ACKs."""
        # Messages: recent non-ack messages, excluding BLE register data
        msg_rows = await self._execute(
            "SELECT raw_json FROM messages"
            " WHERE type = 'msg' AND msg NOT LIKE '%:ack%'"
            " ORDER BY timestamp DESC LIMIT 1000",
        )

        # Group by dst, limit per dst
        msgs_per_dst: dict[str, list[str]] = defaultdict(list)
        for row in msg_rows:
            raw = row["raw_json"]
            try:
                data = json.loads(raw)
                dst = data.get("dst")
                if dst and len(msgs_per_dst[dst]) < limit_per_dst:
                    msgs_per_dst[dst].append(raw)
            except (json.JSONDecodeError, TypeError):
                continue

        messages = []
        for msg_list in msgs_per_dst.values():
            messages.extend(reversed(msg_list))

        # Positions: latest per source with field merging
        pos_rows = await self._execute(
            "SELECT raw_json FROM messages"
            " WHERE type = 'pos'"
            " ORDER BY timestamp DESC LIMIT 500",
        )

        pos_per_src: dict[str, dict] = {}
        for row in pos_rows:
            raw = row["raw_json"]
            try:
                data = json.loads(raw)
                src = data.get("src")
                if not src:
                    continue
                if src not in pos_per_src:
                    pos_per_src[src] = data
                else:
                    existing = pos_per_src[src]
                    for key in (
                        "via", "lat", "long", "alt", "battery_level",
                        "firmware", "fw_sub", "aprs_symbol",
                        "aprs_symbol_group", "rssi", "snr", "hw_id",
                        "lora_mod", "mesh",
                    ):
                        if key not in existing and key in data:
                            existing[key] = data[key]
            except (json.JSONDecodeError, TypeError):
                continue

        positions = [json.dumps(d, ensure_ascii=False) for d in pos_per_src.values()]

        # ACKs: both type="ack" and inline ack messages
        ack_rows = await self._execute(
            "SELECT raw_json FROM messages"
            " WHERE type = 'ack' OR (type = 'msg' AND msg LIKE '%:ack%')"
            " ORDER BY timestamp DESC LIMIT 200",
        )
        acks = [row["raw_json"] for row in ack_rows]

        logger.info(
            "smart_initial: %d msgs, %d pos, %d acks",
            len(messages), len(positions), len(acks),
        )
        return {"messages": messages, "positions": positions, "acks": acks}

    async def get_summary(self) -> dict:
        """Get message count per destination."""
        rows = await self._execute(
            "SELECT dst, COUNT(*) as cnt FROM messages"
            " WHERE type = 'msg' AND msg NOT LIKE '%:ack%' GROUP BY dst",
        )
        return {row["dst"]: row["cnt"] for row in rows if row["dst"]}

    async def get_full_dump(self) -> list[str]:
        """Get full message dump."""
        query = (
            "SELECT raw_json FROM messages WHERE type = 'msg'"
            " ORDER BY timestamp"
        )
        rows = await self._execute(query)
        return [row["raw_json"] for row in rows]

    async def process_mheard_store_parallel(self, progress_callback=None) -> list[dict[str, Any]]:
        """Process messages for MHeard statistics."""
        now_ms = int(time.time() * 1000)
        cutoff_ms = now_ms - SEVEN_DAYS_MS

        if progress_callback:
            await progress_callback("start", "Querying database...")

        # Query aggregated data directly from SQLite
        query = """
            SELECT
                src,
                timestamp,
                rssi,
                snr
            FROM messages
            WHERE timestamp >= ?
                AND rssi IS NOT NULL
                AND snr IS NOT NULL
                AND rssi BETWEEN ? AND ?
                AND snr BETWEEN ? AND ?
        """
        params = (
            cutoff_ms,
            VALID_RSSI_RANGE[0], VALID_RSSI_RANGE[1],
            VALID_SNR_RANGE[0], VALID_SNR_RANGE[1],
        )

        rows = await self._execute(query, params)
        logger.info("Processing %d rows for mheard statistics", len(rows))

        if progress_callback:
            await progress_callback("bucketing", f"Processing {len(rows)} rows...")

        # Group by bucket and callsign
        buckets: dict[tuple[int, str], dict[str, list]] = defaultdict(
            lambda: {"rssi": [], "snr": []}
        )

        for row in rows:
            src = row["src"]
            if not src:
                continue

            timestamp_ms = row["timestamp"]
            bucket_time = int(timestamp_ms // 1000 // BUCKET_SECONDS * BUCKET_SECONDS)

            # Handle comma-separated callsigns
            callsigns = [s.strip() for s in src.split(",")]
            for call in callsigns:
                key = (bucket_time, call)
                buckets[key]["rssi"].append(row["rssi"])
                buckets[key]["snr"].append(row["snr"])

        # Build result with gap markers
        if progress_callback:
            result = await self._build_stats_with_gaps_async(
                buckets, progress_callback
            )
        else:
            result = self._build_stats_with_gaps(buckets)

        if progress_callback:
            stats_entries = [r for r in result if not r.get("is_gap_marker")]
            callsign_count = len(set(e["callsign"] for e in stats_entries)) if stats_entries else 0
            await progress_callback(
                "done",
                f"{len(stats_entries)} data points for {callsign_count} stations",
            )

        return result

    def _build_stats_with_gaps(
        self,
        buckets: dict[tuple[int, str], dict[str, list]],
    ) -> list[dict[str, Any]]:
        """Build statistics with gap markers for Chart.js."""
        gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS

        # Group by callsign
        callsign_data: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for (bucket_time, callsign), values in buckets.items():
            callsign_data[callsign].append((bucket_time, values))

        final_result = []

        for callsign, entries in callsign_data.items():
            if len(entries) < MIN_DATAPOINTS_FOR_STATS:
                continue

            # Sort by time
            entries.sort(key=lambda x: x[0])

            segment_id = 0
            prev_time = None

            for bucket_time, values in entries:
                # Check for gap
                if prev_time and (bucket_time - prev_time) > gap_threshold:
                    # Insert gap marker
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - BUCKET_SECONDS,
                        "callsign": callsign,
                        "rssi": None,
                        "snr": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                rssi_values = values["rssi"]
                snr_values = values["snr"]
                count = min(len(rssi_values), len(snr_values))

                if count > 0:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": round(mean(rssi_values), 2),
                        "snr": round(mean(snr_values), 2),
                        "count": count,
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    })

                prev_time = bucket_time

        logger.info("Generated %d statistics entries", len(final_result))
        return sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

    async def _build_stats_with_gaps_async(
        self,
        buckets: dict[tuple[int, str], dict[str, list]],
        progress_callback,
    ) -> list[dict[str, Any]]:
        """Async version with per-callsign progress."""
        gap_threshold = GAP_THRESHOLD_MULTIPLIER * BUCKET_SECONDS

        callsign_data: dict[str, list[tuple[int, dict]]] = defaultdict(list)
        for (bucket_time, callsign), values in buckets.items():
            callsign_data[callsign].append((bucket_time, values))

        final_result = []

        for callsign, entries in callsign_data.items():
            if len(entries) < MIN_DATAPOINTS_FOR_STATS:
                continue

            await progress_callback("gaps", f"Analyzing {callsign}...", callsign)

            entries.sort(key=lambda x: x[0])
            segment_id = 0
            prev_time = None

            for bucket_time, values in entries:
                if prev_time and (bucket_time - prev_time) > gap_threshold:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time - BUCKET_SECONDS,
                        "callsign": callsign,
                        "rssi": None,
                        "snr": None,
                        "count": None,
                        "segment_id": f"{callsign}_gap_{segment_id}_to_{segment_id + 1}",
                        "segment_size": 1,
                        "is_gap_marker": True,
                    })
                    segment_id += 1

                rssi_values = values["rssi"]
                snr_values = values["snr"]
                count = min(len(rssi_values), len(snr_values))

                if count > 0:
                    final_result.append({
                        "src_type": "STATS",
                        "timestamp": bucket_time,
                        "callsign": callsign,
                        "rssi": round(mean(rssi_values), 2),
                        "snr": round(mean(snr_values), 2),
                        "count": count,
                        "segment_id": f"{callsign}_seg_{segment_id}",
                        "segment_size": 1,
                    })

                prev_time = bucket_time

        logger.info("Generated %d statistics entries", len(final_result))
        return sorted(final_result, key=lambda x: (x["callsign"], x["timestamp"]))

    async def load_dump(self, filename: str) -> int:
        """Load messages from JSON dump file."""
        path = Path(filename)
        if not path.exists():
            logger.info("Dump file not found: %s", filename)
            return 0

        def _load() -> list[dict]:
            with open(path, encoding="utf-8") as f:
                return json.load(f)

        data = await asyncio.to_thread(_load)

        # Bulk insert
        insert_query = """
            INSERT OR IGNORE INTO messages
            (msg_id, src, dst, msg, type, timestamp, rssi, snr, src_type, raw_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """

        params_list = []
        for item in data:
            raw = item.get("raw", "")
            timestamp_str = item.get("timestamp", "")

            try:
                parsed = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            # Skip filtered messages (matching store_message logic)
            if self._should_filter_message(parsed):
                continue

            params_list.append((
                parsed.get("msg_id"),
                parsed.get("src", ""),
                parsed.get("dst", ""),
                parsed.get("msg", ""),
                parsed.get("type", "msg"),
                parsed.get("timestamp", 0),
                parsed.get("rssi"),
                parsed.get("snr"),
                parsed.get("src_type", ""),
                raw,
                timestamp_str,
            ))

        if params_list:
            await self._execute_many(insert_query, params_list)

        count = await self.get_message_count()
        logger.info("Loaded %d messages from %s (total: %d)", len(params_list), filename, count)
        return len(params_list)

    async def save_dump(self, filename: str) -> int:
        """Save messages to JSON dump file (for compatibility)."""
        query = "SELECT raw_json, created_at FROM messages ORDER BY timestamp"
        rows = await self._execute(query)

        data = [{"raw": row["raw_json"], "timestamp": row["created_at"]} for row in rows]

        def _save() -> None:
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        await asyncio.to_thread(_save)
        logger.info("Saved %d messages to %s", len(data), filename)
        return len(data)

    async def close(self) -> None:
        """Close the database connection (no-op for connection-per-query model)."""
        pass


async def create_sqlite_storage(
    db_path: str | Path,
    max_size_mb: int = 50,
) -> SQLiteStorage:
    """Create and initialize a SQLite storage instance."""
    storage = SQLiteStorage(db_path, max_size_mb)
    await storage.initialize()
    return storage
