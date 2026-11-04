"""
推送记录明细：每次发送后写入 SQLite，供 Web 查看历史与统计。
独立模块，仅依赖 Python 标准库，无第三方包。

与 fnOS 主进程共用 data_dir，数据库文件为 data_dir/push_history.db。
超过 MAX_RECORDS 条时自动删除最老的 DELETE_BATCH 条。
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any, Dict, List, Optional

# zoneinfo 在 Python 3.9+ 标准库；fnOS 自带 3.10+，Windows 开发机也有
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _TZ = None

_lock = threading.Lock()
_db_path: str = ""
MAX_RECORDS = 10000
DELETE_BATCH = 3000
MAX_DETAIL_CHARS = 2000


def _now_str() -> str:
    """当前时间字符串（Asia/Shanghai，回退本地时间）。"""
    if _TZ is not None:
        try:
            return datetime.now(_TZ).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _trunc(value: Any, limit: int = 500) -> str:
    s = str(value or "")
    if len(s) <= limit:
        return s
    return s[: max(0, limit - 3)] + "..."


def init(data_dir: str) -> None:
    """设置数据库路径并确保表存在。fnmonitor 启动时调用一次。"""
    global _db_path
    if not data_dir:
        return
    os.makedirs(data_dir, exist_ok=True)
    _db_path = os.path.join(os.path.abspath(data_dir), "push_history.db")
    _ensure_table()


def _ensure_table() -> None:
    if not _db_path:
        return
    with _lock:
        conn = sqlite3.connect(_db_path)
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS push_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    detail TEXT
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_push_history_created_at ON push_history(created_at)"
            )
            conn.commit()
        finally:
            conn.close()


def _conn() -> sqlite3.Connection:
    return sqlite3.connect(_db_path)


def record(
    success: bool,
    event_type: str,
    summary: str = "",
    detail: Optional[Dict[str, Any]] = None,
) -> None:
    """写入一条推送记录。detail 存 JSON 字符串，过长时自动截断。"""
    if not _db_path:
        return
    created_at = _now_str()
    if summary:
        summary = _trunc(summary, 500)
    detail_str: Optional[str] = None
    if detail is not None:
        try:
            raw = json.dumps(detail, ensure_ascii=False, default=str)
            if len(raw) > MAX_DETAIL_CHARS:
                detail_str = raw[:MAX_DETAIL_CHARS - 3] + "..."
            else:
                detail_str = raw
        except Exception:
            detail_str = None
    with _lock:
        conn = _conn()
        try:
            conn.execute(
                "INSERT INTO push_history (created_at, event_type, success, summary, detail) VALUES (?, ?, ?, ?, ?)",
                (created_at, str(event_type or "Unknown"), 1 if success else 0, summary or "", detail_str),
            )
            conn.commit()
            # 超量清理
            cur = conn.execute("SELECT COUNT(*) FROM push_history")
            count = cur.fetchone()[0]
            if count > MAX_RECORDS:
                cur = conn.execute(
                    "SELECT id FROM push_history ORDER BY id ASC LIMIT ?", (DELETE_BATCH,)
                )
                ids = [row[0] for row in cur.fetchall()]
                if ids:
                    placeholders = ",".join("?" * len(ids))
                    conn.execute(
                        f"DELETE FROM push_history WHERE id IN ({placeholders})", ids
                    )
                    conn.commit()
        finally:
            conn.close()


def list_records(
    limit: int = 50,
    offset: int = 0,
    success_filter: Optional[bool] = None,
) -> List[Dict[str, Any]]:
    """分页查询推送记录，按时间倒序。success_filter: True 仅成功，False 仅失败，None 全部。"""
    if not _db_path:
        return []
    limit = max(1, min(100, int(limit)))
    offset = max(0, int(offset))
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            sql = "SELECT id, created_at, event_type, success, summary, detail FROM push_history"
            params: List[Any] = []
            if success_filter is not None:
                sql += " WHERE success = ?"
                params.append(1 if success_filter else 0)
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cur = conn.execute(sql, params)
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "created_at": r["created_at"],
                    "event_type": r["event_type"],
                    "success": bool(r["success"]),
                    "summary": r["summary"] or "",
                    "detail": r["detail"],
                }
                for r in rows
            ]
        finally:
            conn.close()


def get_record(record_id: int) -> Optional[Dict[str, Any]]:
    """单条推送记录详情。"""
    if not _db_path:
        return None
    with _lock:
        conn = _conn()
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(
                "SELECT id, created_at, event_type, success, summary, detail FROM push_history WHERE id = ?",
                (int(record_id),),
            )
            r = cur.fetchone()
            if r is None:
                return None
            return {
                "id": r["id"],
                "created_at": r["created_at"],
                "event_type": r["event_type"],
                "success": bool(r["success"]),
                "summary": r["summary"] or "",
                "detail": r["detail"],
            }
        finally:
            conn.close()


def get_total_counts() -> Dict[str, int]:
    """总统计：total, success, fail。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
                "FROM push_history"
            )
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            return {"total": total, "success": ok, "fail": total - ok}
        finally:
            conn.close()


def get_today_counts() -> Dict[str, int]:
    """当日统计：total, success, fail（按 Asia/Shanghai 日期匹配）。"""
    if not _db_path:
        return {"total": 0, "success": 0, "fail": 0}
    today = _now_str()[:10]
    with _lock:
        conn = _conn()
        try:
            cur = conn.execute(
                "SELECT COUNT(*) AS total, SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
                "FROM push_history WHERE substr(created_at, 1, 10) = ?",
                (today,),
            )
            row = cur.fetchone()
            total = row[0] or 0
            ok = row[1] or 0
            return {"total": total, "success": ok, "fail": total - ok}
        finally:
            conn.close()


def clear_all() -> None:
    """清空所有推送记录。"""
    if not _db_path:
        return
    with _lock:
        conn = _conn()
        try:
            conn.execute("DELETE FROM push_history")
            conn.commit()
        finally:
            conn.close()


def get_stats() -> Dict[str, Any]:
    """一次返回总统计 + 当日统计，减少两次 DB 调用。"""
    if not _db_path:
        return {"total": {"total": 0, "success": 0, "fail": 0},
                "today": {"total": 0, "success": 0, "fail": 0}}
    return {"total": get_total_counts(), "today": get_today_counts()}


def db_path() -> str:
    return _db_path
