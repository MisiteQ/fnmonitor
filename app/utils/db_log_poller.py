"""
fnOS 主日志库轮询器：从 logger_data.db3 的 log 表轮询新记录。

表结构（fnOS 默认）：
    id           INTEGER PRIMARY KEY
    serviceId    TEXT             (服务名)
    uid          INTEGER          (用户 uid)
    uname        TEXT             (用户名)
    logtime      INTEGER          (10 位 Unix 时间戳)
    loglevel     INTEGER          (日志级别)
    eventId      TEXT             (事件 ID，如 LoginSucc)
    parameter    TEXT             (JSON，事件详细字段)
    category     TEXT

增量策略：读最大 id（cursor），每次 SELECT * FROM log WHERE id > cursor ORDER BY id ASC LIMIT batch_size。
游标存 cursor_dir / db_log_poller_cursor.txt，重启不丢进度。
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

# zoneinfo 在 Python 3.9+ 标准库；fnOS 自带 3.10+，Windows 开发机也有
try:
    from zoneinfo import ZoneInfo
    _TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    _TZ = None


# SQLite 原 eventId → fnmonitor 内部 event_type（通知用）
# 大部分 eventId 一致；少数有差异（如 StorageDegraded1 → STORAGE_DEGRADED）
DB_EVENT_ID_TO_TYPE: Dict[str, str] = {
    "LoginSucc": "LoginSucc",
    "LoginSucc2FA1": "LoginSucc2FA1",
    "LoginFail": "LoginFail",
    "Logout": "Logout",
    "FoundDisk": "FoundDisk",
    "InsertDisk": "InsertDisk",
    "EjectDisk": "EjectDisk",
    "StorageBroken": "StorageBroken",
    "StorageDegraded1": "STORAGE_DEGRADED",
    "APP_CRASH": "APP_CRASH",
    "SshdLoginSucc": "SSH_LOGIN_SUCCESS",
    "SshdLoginAuthFail": "SSH_AUTH_FAILED",
    "SshdLogonout": "SSH_DISCONNECTED",
    "APP_INSTALL_FAILED_INIT_DOCKER_EXCEPTION": "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "UPS_ONLINE": "UPS_ONLINE",
    "UPS_ONBATT_LOWBATT": "UPS_ONBATT_LOWBATT",
    "UPS_DISCONNET": "UPS_ONBATT",
    "UPS_CONNET_OL": "UPS_ONLINE",
    "UPS_ENABLE": "UPS_ENABLE",
    "UPS_DISABLE": "UPS_DISABLE",
    "APP_UPDATE_FAILED": "APP_UPDATE_FAILED",
    "APP_STARTED": "APP_STARTED",
    "APP_STOPPED": "APP_STOPPED",
    "APP_UPDATED": "APP_UPDATED",
    "APP_INSTALLED": "APP_INSTALLED",
    "APP_AUTO_STARTED": "APP_AUTO_STARTED",
    "APP_UNINSTALLED": "APP_UNINSTALLED",
    "DISK_IO_ERR": "DISK_IO_ERR",
    "DiskError": "DISK_IO_ERR",
    "DiskWakeup": "DiskWakeup",
    "DiskSpindown": "DiskSpindown",
    "CPU_USAGE_ALARM": "CPU_USAGE_ALARM",
    "CPU_USAGE_RESTORED": "CPU_USAGE_RESTORED",
    "MEMORY_USAGE_ALARM": "MEMORY_USAGE_ALARM",
    "MEMORY_USAGE_RESTORED": "MEMORY_USAGE_RESTORED",
    "ARCHIVING_SUCCESS": "ARCHIVING_SUCCESS",
    "DeleteFile": "DeleteFile",
    "MovetoTrashbin": "MovetoTrashbin",
    "SHARE_EVENTID_DEL": "SHARE_EVENTID_DEL",
    "SHARE_EVENTID_PUT": "SHARE_EVENTID_PUT",
    "WEBDAV_ENABLED": "WEBDAV_ENABLED",
    "WEBDAV_DISABLED": "WEBDAV_DISABLED",
    "SAMBA_ENABLED": "SAMBA_ENABLED",
    "SAMBA_DISABLED": "SAMBA_DISABLED",
    "DLNA_ENABLED": "DLNA_ENABLED",
    "DLNA_DISABLED": "DLNA_DISABLED",
    "FTP_ENABLED": "FTP_ENABLED",
    "FTP_DISABLED": "FTP_DISABLED",
    "NFS_ENABLED": "NFS_ENABLED",
    "NFS_DISABLED": "NFS_DISABLED",
    "FW_ENABLE": "FW_ENABLE",
    "FW_DISABLE": "FW_DISABLE",
    "SECURITY_PORTCHANGED": "SECURITY_PORTCHANGED",
    "SHUTDOWN_VM": "SHUTDOWN_VM",
    "STATUS_RUNNING_VM": "STATUS_RUNNING_VM",
    "DESTROY_VM": "DESTROY_VM",
    "USER_CREATE": "MEDIA_USER_CREATED",
    "CreateUser": "MEDIA_USER_CREATED",
    "AddUserSucc": "MEDIA_USER_CREATED",
    "USER_ADD_SUCCESS": "MEDIA_USER_CREATED",
    "MEDIA_USER_CREATE": "MEDIA_USER_CREATED",
}


def _logtime_to_str(logtime: Any) -> str:
    """10 位 Unix 时间戳 → YYYY-MM-DD HH:MM:SS（Asia/Shanghai）。"""
    try:
        ts = int(logtime)
        if _TZ is not None:
            dt = datetime.fromtimestamp(ts, tz=_TZ)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _parse_parameter(parameter: Any, uname: Any = None, uid: Any = None) -> Dict[str, Any]:
    """解析 parameter JSON。

    fnOS 实际格式：{"from": "user", "eventId": "SshdLoginSucc",
                   "data": {"IP": "192.168.31.48", "user": "mozi"}, "datetime": 1793708510}
    真实业务字段在嵌套的 data 里，必须先展开，否则 IP/user 全是 "user" 这种脏值。
    """
    data: Dict[str, Any] = {}
    if parameter and str(parameter).strip():
        try:
            parsed = json.loads(str(parameter))
            if isinstance(parsed, dict):
                data = parsed
            else:
                data = {"raw": str(parameter)}
        except (json.JSONDecodeError, ValueError, TypeError):
            data = {"raw": str(parameter)}

    inner = data.get("data")
    if isinstance(inner, dict):
        merged = dict(inner)
        for k, v in data.items():
            if k != "data" and k not in merged:
                merged[k] = v
        data = merged

    if uname is not None and "user" not in data:
        data["user"] = uname
    if uid is not None and "uid" not in data:
        data["uid"] = uid
    if "IP" not in data and "ip" not in data:
        cand = str(data.get("from") or data.get("FROM") or "")
        # from 可能是 "user" 这类来源标记，不是 IP，别硬塞进 IP 字段
        if cand and (cand[0].isdigit() or ":" in cand):
            data["IP"] = cand
    return data


class DBLogPoller:
    """logger_data.db3 增量轮询器。"""

    BATCH_SIZE = 200
    CONNECT_TIMEOUT = 3.0

    def __init__(
        self,
        db_path: str,
        cursor_dir: str,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        # 外部注入：拿到事件后的回调 (event_type, event_data)
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.db_path = (db_path or "").strip()
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._cursor_file = self.cursor_dir / "db_log_poller_cursor.txt"
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._last_cursor = 0
        # 连接失败时的退避
        self._consecutive_errors = 0

    # ---------- 游标 ----------
    def _read_cursor(self) -> int:
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text().strip()
                if raw.isdigit():
                    v = int(raw)
                    self._last_cursor = v
                    return v
        except Exception:
            pass
        self._last_cursor = 0
        return 0

    def _write_cursor(self, cursor: int) -> None:
        try:
            self._cursor_file.write_text(str(int(cursor)))
            self._last_cursor = int(cursor)
        except Exception:
            pass

    # ---------- 数据库查询 ----------
    def _connect(self) -> sqlite3.Connection:
        """只读 + immutable 回退：如果 journal 锁定，immutable 仍能读到主库快照。"""
        uri = f"file:{self.db_path}?mode=ro&immutable=1"
        return sqlite3.connect(uri, uri=True, timeout=self.CONNECT_TIMEOUT)

    def _max_id(self) -> int:
        """当前日志库最大 id（首次基线用，避免回放历史日志）。"""
        try:
            conn = self._connect()
            try:
                row = conn.execute("SELECT MAX(id) FROM log").fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception:
            return 0

    def _fetch_new(self, since_id: int) -> List[Dict[str, Any]]:
        """拉取所有 id > since_id 的记录，按 id 升序。"""
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            # 先看表结构列名（不同 fnOS 版本可能略有差异）
            # 注意 PRAGMA table_info 的列是 (cid, name, type, ...)，列名在 [1] 而不是 [0]
            cols = [r[1] for r in conn.execute("PRAGMA table_info(log)").fetchall()]
            col_map: Dict[str, str] = {}
            lower_cols = {c.lower(): c for c in cols}
            for want in ("id", "serviceId", "uid", "uname", "logtime", "loglevel", "eventId", "parameter", "category"):
                actual = lower_cols.get(want.lower())
                if actual:
                    col_map[want] = actual
            if "id" not in col_map:
                # 没有 log 表或没有 id 列：跳过
                return []
            query_cols = ", ".join([col_map[k] for k in ("id", "serviceId", "uid", "uname", "logtime", "loglevel", "eventId", "parameter", "category") if k in col_map])
            sql = f"SELECT {query_cols} FROM log WHERE {col_map['id']} > ? ORDER BY {col_map['id']} ASC LIMIT ?"
            rows = conn.execute(sql, (since_id, self.BATCH_SIZE * 5)).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ---------- 处理 ----------
    def _process_rows(self, rows: List[Dict[str, Any]]) -> int:
        """处理一批记录。返回这批最大的 id（作为下一个 cursor）。"""
        new_cursor = self._last_cursor
        for row in rows:
            raw_id = row.get("id") or 0
            try:
                new_cursor = max(new_cursor, int(raw_id))
            except (TypeError, ValueError):
                pass
            raw_event_id = str(row.get("eventId") or "").strip()
            if not raw_event_id:
                continue
            event_type = DB_EVENT_ID_TO_TYPE.get(raw_event_id, raw_event_id)
            # 用户未勾选 → 跳过（除非 monitor_events 为空，表示"全部放行"）
            if self.monitor_events and event_type not in self.monitor_events:
                # 动态 VM 事件一律放行
                if "VM" not in event_type.upper():
                    continue
            event_data = _parse_parameter(
                row.get("parameter"),
                row.get("uname"),
                row.get("uid"),
            )
            event_data["_source"] = "logger_db"
            event_data["_logtime"] = _logtime_to_str(row.get("logtime"))
            event_data["_service"] = row.get("serviceId") or "unknown"
            if self.on_event:
                try:
                    self.on_event(event_type, event_data)
                except Exception:
                    pass
        return new_cursor

    # ---------- 主循环 ----------
    def run_once(self) -> None:
        """跑一轮（拉取新记录 + 分发 + 写游标）。外部线程循环调用。"""
        if not self.db_path or not os.path.exists(self.db_path):
            return
        cursor = self._read_cursor()
        try:
            if cursor <= 0:
                # 首次基线：从未成功轮询过（修复前 _fetch_new 一直报错），
                # 直接推到当前最大 id，避免把十几万条历史日志一次性全推出去
                mid = self._max_id()
                if mid > 0:
                    self._write_cursor(mid)
                    print("[DBLogPoller] 首次基线建立，从 id=%d 开始（不回放历史日志）" % mid)
                    return
            rows = self._fetch_new(cursor)
            if rows:
                new_cursor = self._process_rows(rows)
                self._write_cursor(new_cursor)
            self._consecutive_errors = 0
        except Exception as exc:
            self._consecutive_errors += 1
            if self._consecutive_errors <= 3:
                # 前几次静默
                pass
            elif self._consecutive_errors % 10 == 0:
                print(f"[DBLogPoller] 连续失败 {self._consecutive_errors} 次：{exc}")

    def _loop(self) -> None:
        while self.running:
            try:
                self.run_once()
            except Exception:
                pass
            # poll_interval 秒一轮；检查 self.running 允许干净退出
            for _ in range(max(1, int(self.poll_interval * 2))):
                if not self.running:
                    break
                time.sleep(0.5)

    def start(self) -> None:
        """启动后台线程。db_path 未配置时不启动。"""
        if not self.db_path:
            return
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="DBLogPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
        db_path: Optional[str] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
        if db_path is not None:
            self.db_path = (db_path or "").strip()
