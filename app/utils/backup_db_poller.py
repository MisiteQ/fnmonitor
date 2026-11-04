"""
备份任务库轮询器：从 basic_backup.db3 的 operations 表轮询新记录。

表结构（根据 FnMessageBots 源码推断）：
    id           INTEGER PRIMARY KEY
    taskId       TEXT             (任务 UUID)
    taskName     TEXT             (任务名)
    startTime    INTEGER          (Unix 时间戳，毫秒或秒)
    endTime      INTEGER          (Unix 时间戳)
    status       TEXT             (success / failed / partial_success / ...)
    targetName   TEXT             (备份目标名)
    description  TEXT             (描述/备注)

事件类型：BACKUP_TASK_SUCCESS / BACKUP_TASK_FAILED / BACKUP_TASK_PARTIAL_SUCCESS
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


BACKUP_STATUS_TO_EVENT: Dict[str, str] = {
    "success": "BACKUP_TASK_SUCCESS",
    "SUCCEEDED": "BACKUP_TASK_SUCCESS",
    "failed": "BACKUP_TASK_FAILED",
    "FAILED": "BACKUP_TASK_FAILED",
    "partial_success": "BACKUP_TASK_PARTIAL_SUCCESS",
    "partial": "BACKUP_TASK_PARTIAL_SUCCESS",
}


def _ts_to_str(ts: Any) -> str:
    """Unix 时间戳（秒或毫秒）→ YYYY-MM-DD HH:MM:SS。"""
    if ts is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if v > 10_000_000_000:  # 毫秒 → 秒
        v = int(v / 1000)
    if v <= 0:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class BackupDBPoller:
    """basic_backup.db3 增量轮询器。"""

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 10,
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.db_path = (db_path or "").strip()
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 10))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "backup_db_poller_cursor.txt"
        self.cursor_dir.mkdir(parents=True, exist_ok=True)

    def _read_cursor(self) -> int:
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text().strip()
                if raw.isdigit():
                    return int(raw)
        except Exception:
            pass
        return 0

    def _write_cursor(self, cursor: int) -> None:
        try:
            self._cursor_file.write_text(str(int(cursor)))
        except Exception:
            pass

    def _max_id(self) -> int:
        """operations 表最大 id（首次基线用，避免回放历史任务）。"""
        try:
            conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True, timeout=3.0)
            try:
                row = conn.execute("SELECT MAX(id) FROM operations").fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    def run_once(self) -> None:
        if not self.db_path or not os.path.exists(self.db_path):
            return
        cursor = self._read_cursor()
        if cursor <= 0:
            mid = self._max_id()
            if mid > 0:
                self._write_cursor(mid)
                print("[BackupDBPoller] 首次基线建立，从 id=%d 开始（不回放历史任务）" % mid)
                return
        conn = sqlite3.connect(f"file:{self.db_path}?mode=ro&immutable=1", uri=True, timeout=3.0)
        conn.row_factory = sqlite3.Row
        try:
            # PRAGMA table_info 的列是 (cid, name, type, ...)，列名在 [1]
            cols = [r[1] for r in conn.execute("PRAGMA table_info(operations)").fetchall()]
            if "id" not in cols:
                return
            id_col = "id"
            status_col = "status" if "status" in cols else None
            if not status_col:
                return
            sql = f"SELECT * FROM operations WHERE {id_col} > ? ORDER BY {id_col} ASC LIMIT 200"
            rows = [dict(r) for r in conn.execute(sql, (cursor,)).fetchall()]
        except Exception as exc:
            # 静默返回
            return
        finally:
            conn.close()

        new_cursor = cursor
        for row in rows:
            try:
                new_cursor = max(new_cursor, int(row.get("id") or 0))
            except (TypeError, ValueError):
                pass
            raw_status = str(row.get("status") or "").strip().lower()
            event_type = BACKUP_STATUS_TO_EVENT.get(raw_status)
            if not event_type:
                continue
            if self.monitor_events and event_type not in self.monitor_events:
                continue
            event_data = {
                "task_name": row.get("taskName") or row.get("taskName") or row.get("name") or "",
                "target_name": row.get("targetName") or row.get("target") or "",
                "start_time": _ts_to_str(row.get("startTime")),
                "end_time": _ts_to_str(row.get("endTime")),
                "description": row.get("description") or "",
                "_source": "backup_db",
            }
            if self.on_event:
                try:
                    self.on_event(event_type, event_data)
                except Exception:
                    pass
        self._write_cursor(new_cursor)

    def _loop(self) -> None:
        while self.running:
            try:
                self.run_once()
            except Exception:
                pass
            for _ in range(max(1, int(self.poll_interval * 2))):
                if not self.running:
                    break
                time.sleep(0.5)

    def start(self) -> None:
        if not self.db_path:
            return
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="BackupDBPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()
