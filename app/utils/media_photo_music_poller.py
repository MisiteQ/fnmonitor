"""
影视 / 相册 / 音乐 多库轮询器（合并文件，减少零散模块）。

各库表结构各不相同，增量策略统一：读最大 id 作为游标 → SELECT ... WHERE id > cursor。
具体事件类型映射在 _HANDLERS 字典里，按 (db_label, raw_row) → (event_type, event_data)。

支持的库：
    trimmedia.db      → TRIM_RESOURCE_ADDED / TRIM_SCRAPE_SUCCESS / MEDIA_USER_CREATED
    trimactivity.db   → MEDIA_LOGIN_SUCC / MEDIA_LOGOUT
    photo.db          → PHOTO_SHARE_CREATED / PHOTO_SHARE_EXPIRED / PHOTO_DEVICE_REGISTERED / FACE_RECOGNITION_UPDATED
    music.db          → MUSIC_TRACK_ADDED / MUSIC_TRACK_REMOVED / MUSIC_FAVORITED / MUSIC_PLAYLIST_TRACK_ADDED
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class _SubPoller:
    """单个库 + 单张表的增量轮询器。被 MultiSourcePoller 内部持有。"""

    def __init__(
        self,
        label: str,
        db_path: str,
        table_name: str,
        handler: Callable[[Dict[str, Any]], Optional[Tuple[str, Dict[str, Any]]]],
        cursor_dir: Path,
        poll_interval: int = 10,
        select_sql: Optional[str] = None,
        baseline_sql: Optional[str] = None,
        cursor_field: str = "id",
        cursor_mode: str = "field",
    ):
        self.label = label
        self.db_path = (db_path or "").strip()
        self.table_name = table_name
        self.handler = handler
        self.cursor_dir = cursor_dir
        self.poll_interval = max(1, int(poll_interval or 10))
        self.select_sql = select_sql
        # baseline_sql：这些表的主键不是自增 id（如影视库 item 用 guid），
        # 首次基线要单独指定（MAX(create_time) / MAX(update_time) 等）
        self.baseline_sql = baseline_sql
        # 游标取行里的哪个字段（默认 id；影视库/活动库用 create_time / update_time）
        self.cursor_field = cursor_field
        # cursor_mode="now"：游标存「上次检查时间」，用于分享过期、音乐删除这类无自增 id 的场景
        self.cursor_mode = cursor_mode
        self._missing_logged = False
        self._cursor_file = cursor_dir / f"multi_poller_{label}_cursor.txt"
        self._init_file = cursor_dir / f"multi_poller_{label}.init"
        self._last_cursor = 0

    def _initialized(self) -> bool:
        """是否已建过基线。表里一条数据都没有时游标为 0，
        单靠游标值无法区分「没初始化」和「已初始化但为空」。"""
        try:
            return self._init_file.exists()
        except Exception:
            return False

    def _mark_initialized(self) -> None:
        try:
            self._init_file.write_text("1")
        except Exception:
            pass

    def _read_cursor(self) -> int:
        try:
            if self._cursor_file.exists():
                raw = self._cursor_file.read_text().strip()
                if raw.isdigit():
                    self._last_cursor = int(raw)
                    return self._last_cursor
        except Exception:
            pass
        self._last_cursor = 0
        return 0

    def _write_cursor(self, v: int) -> None:
        try:
            self._cursor_file.write_text(str(int(v)))
            self._last_cursor = int(v)
        except Exception:
            pass

    def fetch_new(self, since_id: int) -> List[Dict[str, Any]]:
        if not self.db_path or not os.path.exists(self.db_path):
            return []
        try:
            uri = f"file:{self.db_path}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            conn.row_factory = sqlite3.Row
            try:
                # 需要联表取名称的子库（影视 / 音乐）走定制 SQL（游标仍是 id）
                if self.select_sql:
                    rows = conn.execute(self.select_sql, (since_id,)).fetchall()
                    return [dict(r) for r in rows]
                # PRAGMA table_info 的列是 (cid, name, type, ...)，列名在 [1]
                cols = [r[1] for r in conn.execute(f"PRAGMA table_info({self.table_name})").fetchall()]
                if not cols:
                    # 表不存在（对应应用没装或库结构变了）：只提示一次，避免每分钟刷日志
                    if not self._missing_logged:
                        self._missing_logged = True
                        print("[MultiSourcePoller] %s 跳过：%s 里没有表 %s"
                              % (self.label, self.db_path, self.table_name))
                    return []
                if "id" not in cols:
                    return []
                rows = conn.execute(
                    f"SELECT * FROM {self.table_name} WHERE id > ? ORDER BY id ASC LIMIT 200",
                    (since_id,),
                ).fetchall()
                return [dict(r) for r in rows]
            finally:
                conn.close()
        except Exception:
            return []

    def max_id(self) -> Optional[int]:
        """当前水位（首次基线用，避免回放历史记录）。
        时间游标模式下水位就是「现在」，否则查 baseline_sql 或 MAX(id)。
        库被应用占用 / 查询异常时返回 None（不建基线，下轮重试），
        避免把一次读取失败当成「水位 0」而永久漏掉之后的增量。"""
        if self.cursor_mode == "now":
            return int(time.time())
        if not self.db_path or not os.path.exists(self.db_path):
            return 0
        sql = self.baseline_sql or (f"SELECT MAX(id) FROM {self.table_name}")
        try:
            uri = f"file:{self.db_path}?mode=ro&immutable=1"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0)
            try:
                row = conn.execute(sql).fetchone()
                return int(row[0] or 0)
            finally:
                conn.close()
        except Exception:
            return None


def _ts_to_str(ts: Any) -> str:
    if ts is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        v = int(ts)
    except (TypeError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if v > 10_000_000_000:
        v = int(v / 1000)
    if v <= 0:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        return datetime.fromtimestamp(v).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, ValueError):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 各库的 row → (event_type, event_data) handler ----------

def _trimmedia_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """trimmedia.db 的 item 表：影视资源入库。
    真实字段：guid / type / title / path / parent_guid / create_time(ms) / fetch_status。
    之前按 resource 表 + name/scrape_time 字段写，fnOS 里根本没有这张表，等于永不触发。"""
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    return "TRIM_RESOURCE_ADDED", {
        "title": title,
        "type": str(row.get("type") or ""),
        "path": str(row.get("path") or ""),
        "_source": "trimmedia",
    }


def _trim_scrape_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """trimmedia.db 的 item 表：刮削完成（update_time 前进且 fetch_status > 0）。"""
    title = str(row.get("title") or "").strip()
    try:
        status = int(row.get("fetch_status") or 0)
    except (TypeError, ValueError):
        status = 0
    if not title or status <= 0:
        return None
    return "TRIM_SCRAPE_SUCCESS", {
        "title": title,
        "type": str(row.get("type") or ""),
        "_source": "trimmedia",
    }


def _trim_user_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """trimmedia.db 的 user 表：新增影视库用户（跳过 default-user-template 模板行）。"""
    name = str(row.get("username") or "").strip()
    if not name or "template" in name:
        return None
    return "MEDIA_USER_CREATED", {
        "user": name,
        "is_admin": row.get("is_admin") or 0,
        "_source": "trimmedia",
    }


def _trim_login_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """trimactivity.db 的 user_token 表：新增 token 且 status=1 → 影视登录。"""
    if str(row.get("status") or "").strip() != "1":
        return None
    return "MEDIA_LOGIN_SUCC", {
        "user": row.get("username") or row.get("user_guid") or "",
        "IP": row.get("ip") or "",
        "device": row.get("device") or row.get("app_name") or "",
        "_source": "trimactivity",
    }


def _trim_logout_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """trimactivity.db 的 user_token 表：status=0 且 update_time 前进 → 影视登出。"""
    if str(row.get("status") or "").strip() != "0":
        return None
    return "MEDIA_LOGOUT", {
        "user": row.get("username") or row.get("user_guid") or "",
        "IP": row.get("ip") or "",
        "_source": "trimactivity",
    }


def _photo_share_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """photo.db 的 share_link 表：新增分享。"""
    return "PHOTO_SHARE_CREATED", {
        "name": row.get("name") or row.get("share_id") or "",
        "share_id": row.get("share_id") or "",
        "valid_to": row.get("valid_to") or 0,
        "user": row.get("nas_uid") or "",
        "_source": "photo",
    }


def _photo_expire_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """photo.db 的 share_link 表：valid_to 已到期。"""
    return "PHOTO_SHARE_EXPIRED", {
        "name": row.get("name") or row.get("share_id") or "",
        "share_id": row.get("share_id") or "",
        "valid_to": row.get("valid_to") or 0,
        "_source": "photo",
    }


def _photo_device_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """photo.db 的 device 表：新设备注册（手机备份端）。"""
    name = str(row.get("device_name") or row.get("device_uniq_name") or row.get("device_id") or "").strip()
    return "PHOTO_DEVICE_REGISTERED", {
        "device_name": name,
        "device_id": row.get("device_id") or "",
        "user": row.get("nas_uid") or "",
        "_source": "photo",
    }


def _photo_face_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """photo.db 的 face_task_log 表：人脸识别任务新增（无时间列，按 id 增量）。"""
    return "FACE_RECOGNITION_UPDATED", {
        "user_photo_id": row.get("user_photo_id") or 0,
        "count": 1,
        "_source": "photo",
    }


def _music_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """music.db 的 track 表：新曲目入库。"""
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    return "MUSIC_TRACK_ADDED", {"title": title, "_source": "music"}


def _music_removed_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """music.db 的 track 表：is_audio_file_deleted / is_admin_deleted 置位 → 移出曲库。"""
    title = str(row.get("title") or "").strip()
    if not title:
        return None
    return "MUSIC_TRACK_REMOVED", {"title": title, "_source": "music"}


def _music_favorite_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """music.db 的 favorite_track 表：收藏曲目。"""
    return "MUSIC_FAVORITED", {
        "title": row.get("title") or "",
        "user": row.get("username") or "",
        "_source": "music",
    }


def _music_playlist_handler(row: Dict[str, Any]) -> Optional[Tuple[str, Dict[str, Any]]]:
    """music.db 的 playlist_track 表：加入歌单。"""
    return "MUSIC_PLAYLIST_TRACK_ADDED", {
        "title": row.get("title") or "",
        "playlist_name": row.get("playlist_name") or "",
        "_source": "music",
    }


# ---------- 入口：MultiSourcePoller ----------

class MultiSourcePoller:
    """多库增量轮询器，后台线程顺序轮询每个子库。"""

    def __init__(
        self,
        cursor_dir: str,
        poll_interval: int = 15,
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.cursor_dir = Path(cursor_dir)
        self.poll_interval = max(1, int(poll_interval or 15))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self.cursor_dir.mkdir(parents=True, exist_ok=True)
        self._pollers: List[_SubPoller] = []

    # ---------- 子库注册 ----------
    def register(
        self,
        label: str,
        db_path: str,
        table_name: str,
        handler: Callable[[Dict[str, Any]], Optional[Tuple[str, Dict[str, Any]]]],
        select_sql: Optional[str] = None,
        baseline_sql: Optional[str] = None,
        cursor_field: str = "id",
        cursor_mode: str = "field",
    ) -> None:
        self._pollers.append(_SubPoller(
            label, db_path, table_name, handler, self.cursor_dir, self.poll_interval,
            select_sql=select_sql, baseline_sql=baseline_sql,
            cursor_field=cursor_field, cursor_mode=cursor_mode,
        ))

    def register_trimmedia(self, db_path: str, table_name: str = "item") -> None:
        """影视库 trimmedia.db：item 表没有自增 id（主键是 guid），
        只能按 create_time / update_time（毫秒）做时间游标。"""
        self.register(
            "trim_add", db_path, table_name, _trimmedia_handler,
            select_sql=(
                "SELECT guid, type, title, path, parent_guid, season_number, "
                "episode_number, fetch_status, create_time, update_time FROM item "
                "WHERE create_time > ? AND type IN ('Movie', 'TV', 'Season', 'Episode', 'Video') "
                "ORDER BY create_time ASC LIMIT 200"
            ),
            baseline_sql="SELECT COALESCE(MAX(create_time), 0) FROM item",
            cursor_field="create_time",
        )
        self.register(
            "trim_scrape", db_path, table_name, _trim_scrape_handler,
            select_sql=(
                "SELECT guid, type, title, fetch_status, create_time, update_time FROM item "
                "WHERE update_time > ? AND fetch_status > 0 "
                "AND type IN ('Movie', 'TV', 'Episode', 'Video') "
                "ORDER BY update_time ASC LIMIT 200"
            ),
            baseline_sql="SELECT COALESCE(MAX(update_time), 0) FROM item",
            cursor_field="update_time",
        )
        # user 表在影视主库（trimmedia.db）里，不在活动库，必须跟着主库注册
        self.register(
            "trim_user", db_path, "user", _trim_user_handler,
            select_sql=(
                "SELECT guid, username, is_admin, status, create_time, update_time FROM user "
                "WHERE create_time > ? ORDER BY create_time ASC LIMIT 200"
            ),
            baseline_sql="SELECT COALESCE(MAX(create_time), 0) FROM user",
            cursor_field="create_time",
        )

    def register_trimactivity(self, db_path: str, table_name: str = "user_token") -> None:
        """影视登录库 trimactivity.db：user_token 表（无 id，用 rowid）。
        登录 = 新 token；登出 = status 被置 0 且 update_time 前进。"""
        self.register(
            "media_login", db_path, table_name, _trim_login_handler,
            select_sql=(
                "SELECT rowid AS _cursor, token, user_guid, ip, device, app_name, "
                "status, create_time, update_time FROM user_token "
                "WHERE rowid > ? AND status = 1 ORDER BY rowid ASC LIMIT 200"
            ),
            baseline_sql="SELECT COALESCE(MAX(rowid), 0) FROM user_token",
            cursor_field="_cursor",
        )
        self.register(
            "media_logout", db_path, table_name, _trim_logout_handler,
            select_sql=(
                "SELECT rowid AS _cursor, token, user_guid, ip, device, status, "
                "create_time, update_time FROM user_token "
                "WHERE status = 0 AND update_time > ? ORDER BY update_time ASC LIMIT 200"
            ),
            baseline_sql="SELECT COALESCE(MAX(update_time), 0) FROM user_token",
            cursor_field="update_time",
        )

    def register_photo(self, db_path: str, table_name: str = "share_link") -> None:
        """相册 photo.db：share_link（分享 / 过期）、device（设备注册）、face_task_log（人脸）。"""
        self.register(
            "photo_share", db_path, "share_link", _photo_share_handler,
            select_sql=(
                "SELECT sl.id, sl.name, sl.share_id, sl.owner, sl.album_id, sl.valid_to, "
                "sl.create_time, u.nas_uid FROM share_link sl "
                "LEFT JOIN user u ON u.id = sl.owner WHERE sl.id > ? ORDER BY sl.id ASC LIMIT 200"
            ),
        )
        self.register(
            "photo_expire", db_path, "share_link", _photo_expire_handler,
            select_sql=(
                "SELECT sl.id, sl.name, sl.share_id, sl.valid_to, sl.owner, u.nas_uid "
                "FROM share_link sl LEFT JOIN user u ON u.id = sl.owner "
                "WHERE sl.valid_to > ? AND sl.valid_to <= CAST(strftime('%s','now') AS INTEGER) "
                "ORDER BY sl.valid_to ASC LIMIT 200"
            ),
            cursor_mode="now",
        )
        self.register(
            "photo_device", db_path, "device", _photo_device_handler,
            select_sql=(
                "SELECT d.id, d.user_id, d.device_id, d.device_name, d.device_uniq_name, "
                "d.created_at, u.nas_uid FROM device d "
                "LEFT JOIN user u ON u.id = d.user_id WHERE d.id > ? ORDER BY d.id ASC LIMIT 200"
            ),
        )
        self.register(
            "photo_face", db_path, "face_task_log", _photo_face_handler,
            select_sql=(
                "SELECT ftl.id, ftl.user_photo_id, ftl.user_id FROM face_task_log ftl "
                "WHERE ftl.id > ? ORDER BY ftl.id ASC LIMIT 200"
            ),
        )

    def register_music(self, db_path: str, table_name: str = "track") -> None:
        """音乐库 music.db：track 入库 / 移出、favorite_track 收藏、playlist_track 加入歌单。"""
        self.register("music", db_path, table_name, _music_handler)
        self.register(
            "music_removed", db_path, "track", _music_removed_handler,
            select_sql=(
                "SELECT id, title, updated_at FROM track "
                "WHERE (is_audio_file_deleted = 1 OR is_admin_deleted = 1) "
                "AND updated_at >= datetime(?, 'unixepoch', 'localtime') "
                "ORDER BY updated_at ASC LIMIT 200"
            ),
            cursor_mode="now",
        )
        self.register(
            "music_favorite", db_path, "favorite_track", _music_favorite_handler,
            select_sql=(
                "SELECT f.id, f.user_id, t.title, u.name AS username FROM favorite_track f "
                "LEFT JOIN track t ON t.id = f.track_id "
                "LEFT JOIN user u ON u.id = f.user_id "
                "WHERE f.id > ? ORDER BY f.id ASC LIMIT 200"
            ),
        )
        self.register(
            "music_playlist", db_path, "playlist_track", _music_playlist_handler,
            select_sql=(
                "SELECT pt.id, pt.user_id, pt.playlist_id, pt.added_at, t.title, "
                "p.name AS playlist_name FROM playlist_track pt "
                "LEFT JOIN track t ON t.id = pt.track_id "
                "LEFT JOIN playlist p ON p.id = pt.playlist_id "
                "WHERE pt.id > ? ORDER BY pt.id ASC LIMIT 200"
            ),
        )

    def clear(self) -> None:
        self._pollers.clear()

    def update_config(
        self,
        monitor_events: Optional[List[str]] = None,
        poll_interval: Optional[int] = None,
    ) -> None:
        if monitor_events is not None:
            self.monitor_events = set(monitor_events)
        if poll_interval is not None:
            self.poll_interval = max(1, int(poll_interval))
            for sp in self._pollers:
                sp.poll_interval = self.poll_interval

    # ---------- 主循环 ----------
    def run_once(self) -> None:
        if not self._pollers:
            return
        for sp in self._pollers:
            if not sp.db_path or not os.path.exists(sp.db_path):
                continue
            since = sp._read_cursor()
            if not sp._initialized():
                # 首次基线：从未轮询过的子库不回放历史记录，直接推到最新
                mid = sp.max_id()
                if mid is None:
                    # 库被占用 / 读失败：本轮不建基线，下一轮重试
                    continue
                sp._write_cursor(mid)
                sp._mark_initialized()
                if mid > 0:
                    print("[MultiSourcePoller] %s 首次基线建立，从 %s=%d 开始（不回放历史）"
                          % (sp.label, "now" if sp.cursor_mode == "now" else sp.cursor_field, mid))
                continue
            try:
                rows = sp.fetch_new(since)
            except Exception:
                continue
            new_cursor = since
            for row in rows:
                # cursor_mode="now" 的轮询器（分享过期 / 音乐删除）游标不是行内字段
                if sp.cursor_mode != "now":
                    try:
                        new_cursor = max(new_cursor, int(row.get(sp.cursor_field) or 0))
                    except (TypeError, ValueError):
                        pass
                try:
                    result = sp.handler(row)
                except Exception:
                    result = None
                if result is None:
                    continue
                event_type, event_data = result
                if self.monitor_events and event_type not in self.monitor_events:
                    continue
                event_data["_source"] = sp.label
                if self.on_event:
                    try:
                        self.on_event(event_type, event_data)
                    except Exception:
                        pass
            sp._write_cursor(int(time.time()) if sp.cursor_mode == "now" else new_cursor)

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
        if not self._pollers:
            return
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="MultiSourcePoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()
