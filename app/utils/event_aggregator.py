"""
事件聚合器：把「同类事件集中爆发」压成一条推送。

三种聚合模式：
  - 影视 / 音乐静默汇总：最后一条记录后静默 N 秒（默认 60）再合成一条
      * 影视：入库 + 刮削合并为「影视处理本轮汇总」，同一资源去重，
        电视剧按作品 + 季归并单集
      * 音乐：入库 + 移出合并为「音乐库变更汇总」
  - 磁盘唤醒 / 休眠：同类型事件在窗口内合并（唤醒一组、休眠一组）
  - 全局同类合并：开启开关后，其它噪音类事件（文件、应用、Docker、
    相册、收藏、歌单等）按「事件类型」在窗口内合并

安全 / 告警 / 登录 / 存储故障 / UPS 等事件始终即时推送，不进聚合。
"""

import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from utils import event_formatter


# 永不聚合：安全、登录、存储故障、资源告警、备份结果、UPS 等必须即时到达
NEVER_AGGREGATE = frozenset({
    "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout",
    "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
    "SSH_ENABLED", "SSH_DISABLED", "SSH_OPEN_TIMEOUT",
    "StorageBroken", "STORAGE_DEGRADED", "DISK_IO_ERR",
    "FoundDisk", "InsertDisk", "EjectDisk",
    "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED",
    "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED", "CPU_TEMPERATURE_ALARM",
    "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE", "UPS_ENABLE", "UPS_DISABLE",
    "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    "APP_CRASH", "APP_UPDATE_FAILED", "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
    "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "WAN_IP_CHANGED", "SECURITY_PORTCHANGED", "FW_ENABLE", "FW_DISABLE",
})

MEDIA_TRIM_TYPES = frozenset({"TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"})
MEDIA_MUSIC_TYPES = frozenset({"MUSIC_TRACK_ADDED", "MUSIC_TRACK_REMOVED"})
DISK_POWER_TYPES = frozenset({"DiskWakeup", "DiskSpindown"})

# 汇总标题里的静态标签
SUMMARY_LABELS = {
    "DiskWakeup": "磁盘唤醒",
    "DiskSpindown": "磁盘休眠",
    "TRIM_RESOURCE_ADDED": "影视入库",
    "TRIM_SCRAPE_SUCCESS": "影视刮削完成",
    "MUSIC_TRACK_ADDED": "音乐入库",
    "MUSIC_TRACK_REMOVED": "音乐移出库",
    "MUSIC_FAVORITED": "音乐收藏",
    "MUSIC_PLAYLIST_TRACK_ADDED": "加入歌单",
    "PHOTO_SHARE_CREATED": "相册分享创建",
    "PHOTO_SHARE_EXPIRED": "相册分享过期",
    "PHOTO_DEVICE_REGISTERED": "相册同步设备",
    "FACE_RECOGNITION_UPDATED": "人脸识别",
    "DeleteFile": "文件删除",
    "MovetoTrashbin": "文件移到回收站",
    "ARCHIVING_SUCCESS": "归档成功",
    "SHARE_EVENTID_DEL": "共享删除",
    "SHARE_EVENTID_PUT": "共享更新",
    "APP_STARTED": "应用启动",
    "APP_STOPPED": "应用停止",
    "APP_UPDATED": "应用更新",
    "APP_INSTALLED": "应用安装",
    "APP_UNINSTALLED": "应用卸载",
    "APP_AUTO_STARTED": "应用自启动",
    "DOCKER_CONTAINER_CREATE": "容器创建",
    "DOCKER_CONTAINER_START": "容器启动",
    "DOCKER_CONTAINER_STOP": "容器停止",
    "DOCKER_CONTAINER_DIE": "容器退出",
    "DOCKER_CONTAINER_OOM": "容器 OOM",
    "DOCKER_CONTAINER_KILL": "容器 Kill",
    "DOCKER_CONTAINER_PAUSE": "容器暂停",
    "DOCKER_CONTAINER_UNPAUSE": "容器恢复",
    "DOCKER_CONTAINER_RESTART": "容器重启",
    "DOCKER_CONTAINER_DESTROY": "容器删除",
}

_MAX_ITEMS = 300      # 单桶最多保留的明细（超出只累加计数，避免内存失控）
_MAX_LINES = 30        # 汇总正文最多列出的条目数


def _clamp(v, low, high, default):
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        n = int(default)
    return max(low, min(high, n))


def _hhmmss(ts: float) -> str:
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except Exception:
        return ""


def _name_of(event_type: str, data: Dict[str, Any]) -> str:
    """事件的可读主体名（用于汇总列表）。"""
    d = data or {}
    et = event_type
    if et in DISK_POWER_TYPES:
        return str(d.get("name") or d.get("dev") or "").strip()
    if et in ("DeleteFile", "MovetoTrashbin"):
        raw = str(d.get("FILE") or d.get("file") or d.get("FILE_NAME") or "").strip()
        return raw.rstrip("/").split("/")[-1] if raw else ""
    if et.startswith("DOCKER_CONTAINER_"):
        return str(d.get("container_name") or "").strip()
    if et.startswith("APP_"):
        return str(d.get("app_name") or d.get("name") or d.get("APP_NAME")
                   or (d.get("data") or {}).get("DISPLAY_NAME")
                   or (d.get("data") or {}).get("APP_NAME") or "").strip()
    if et == "PHOTO_DEVICE_REGISTERED":
        return str(d.get("device_name") or d.get("device_id") or "").strip()
    if et == "PHOTO_SHARE_CREATED" or et == "PHOTO_SHARE_EXPIRED":
        return str(d.get("name") or d.get("share_id") or "").strip()
    if et == "MUSIC_PLAYLIST_TRACK_ADDED":
        title = str(d.get("title") or "").strip()
        pl = str(d.get("playlist_name") or "").strip()
        if title and pl:
            return "%s → %s" % (title, pl)
        return title or pl
    if et in ("MUSIC_FAVORITED", "MUSIC_TRACK_ADDED", "MUSIC_TRACK_REMOVED",
              "TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS"):
        return str(d.get("title") or "").strip()
    if "VM" in str(et).upper():
        return str(d.get("vm_name") or d.get("VM_NAME") or d.get("name") or "").strip()
    return ""


def _label_for(event_type: str) -> str:
    label = SUMMARY_LABELS.get(event_type)
    if label:
        return label
    tpl = event_formatter.EVENT_TITLES.get(event_type) or event_type
    # 去掉模板里的占位符及其后的尾巴：'应用已启动 {app_name}' → '应用已启动'
    head = tpl.split("{", 1)[0].strip(" ：:·-")
    return head or event_type


def _strip_label(title: str, label: str) -> str:
    t = (title or "").strip()
    if label and t.startswith(label):
        t = t[len(label):].strip(" ：:·-")
    return t or (title or "").strip()


def _dedup_lines(pairs: List[Tuple[str, float]]) -> List[str]:
    """[(条目文本, 时间)] → 去重后的行（重复项标注次数），保持首次出现顺序。"""
    seen: Dict[str, int] = {}
    order: List[str] = []
    for text, _ts in pairs:
        t = (text or "").strip()
        if not t:
            continue
        if t not in seen:
            seen[t] = 0
            order.append(t)
        seen[t] += 1
    out = []
    for t in order:
        n = seen[t]
        out.append("%s（%d 次）" % (t, n) if n > 1 else t)
    return out


class _Bucket:
    __slots__ = ("key", "primary", "mode", "window", "items", "first", "last", "total")

    def __init__(self, key: str, primary: str, mode: str, window: int, now: float):
        self.key = key
        self.primary = primary
        self.mode = mode          # "silence"（静默）/ "window"（固定窗口）
        self.window = window
        self.items: List[Tuple[str, Dict[str, Any], float]] = []
        self.first = now
        self.last = now
        self.total = 0


class EventAggregator:
    """线程安全的聚合缓冲：add() 入桶，后台线程到期合成并回调 on_flush。"""

    def __init__(self, on_flush: Callable[[str, str, str], None],
                 cfg: Optional[Dict[str, Any]] = None):
        self._on_flush = on_flush
        self._lock = threading.Lock()
        self._buckets: Dict[str, _Bucket] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.configure(cfg)

    # ---------- 配置 ----------
    def configure(self, cfg: Optional[Dict[str, Any]]) -> None:
        d = cfg or {}
        self.aggregate_enabled = bool(d.get("aggregate_enabled"))
        self.aggregate_window = _clamp(d.get("aggregate_window"), 5, 3600, 30)
        self.disk_enabled = bool(d.get("disk_aggregate_enabled", 1))
        self.disk_window = _clamp(d.get("disk_aggregate_window"), 5, 3600, 60)
        self.media_enabled = bool(d.get("media_aggregate_enabled", 1))
        self.media_silence = _clamp(d.get("media_silence_window"), 10, 600, 60)

    def _policy(self, event_type: str) -> Optional[Tuple[str, str, int]]:
        """返回 (桶键, 模式, 窗口秒)；None 表示立即推送。"""
        et = str(event_type or "")
        if et in MEDIA_TRIM_TYPES:
            return ("media_trim", "silence", self.media_silence) if self.media_enabled else None
        if et in MEDIA_MUSIC_TYPES:
            return ("media_music", "silence", self.media_silence) if self.media_enabled else None
        if et in DISK_POWER_TYPES:
            return ("disk:" + et, "window", self.disk_window) if self.disk_enabled else None
        if not self.aggregate_enabled:
            return None
        if et in NEVER_AGGREGATE:
            return None
        # 动态 VM 事件（CREATE_VM / START_VM…）保持即时
        if "VM" in et.upper():
            return None
        return ("type:" + et, "window", self.aggregate_window)

    # ---------- 入桶 ----------
    def add(self, event_type: str, event_data: Dict[str, Any]) -> bool:
        """尝试聚合；True 表示已入桶（调用方不应再即时推送），False 表示应即时推送。"""
        spec = self._policy(event_type)
        if spec is None:
            return False
        key, mode, window = spec
        now = time.time()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(key, event_type, mode, window, now)
                self._buckets[key] = b
            b.last = now
            b.total += 1
            if len(b.items) < _MAX_ITEMS:
                b.items.append((event_type, event_data or {}, now))
        return True

    # ---------- 到期合并 ----------
    def tick(self) -> int:
        now = time.time()
        due: List[_Bucket] = []
        with self._lock:
            for key, b in list(self._buckets.items()):
                ready = (now - b.last >= b.window) if b.mode == "silence" \
                    else (now - b.first >= b.window)
                if ready:
                    due.append(b)
                    self._buckets.pop(key, None)
        for b in due:
            self._flush(b)
        return len(due)

    def flush_all(self) -> int:
        with self._lock:
            due = list(self._buckets.values())
            self._buckets.clear()
        for b in due:
            self._flush(b)
        return len(due)

    def pending(self) -> int:
        with self._lock:
            return sum(b.total for b in self._buckets.values())

    def _flush(self, b: _Bucket) -> None:
        try:
            if b.key == "media_trim":
                title, body = _render_trim(b)
            elif b.key == "media_music":
                title, body = _render_music(b)
            else:
                title, body = _render_generic(b)
            if title and body is not None and self._on_flush:
                self._on_flush(title, body, b.primary)
        except Exception as e:
            print("[fnmonitor][monitor] 聚合汇总渲染失败 %s：%s" % (b.key, e))

    # ---------- 后台线程 ----------
    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="EventAggregator", daemon=True)
        self._thread.start()

    def stop(self, flush: bool = True) -> None:
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=2.0)
        self._thread = None
        if flush:
            try:
                self.flush_all()
            except Exception:
                pass

    def _loop(self) -> None:
        while self._running:
            try:
                self.tick()
            except Exception:
                pass
            for _ in range(10):   # 每轮约 0.5 秒，够按秒判断到期
                if not self._running:
                    break
                time.sleep(0.05)


# ============================================================
# 汇总渲染
# ============================================================

def _render_generic(b: _Bucket) -> Tuple[str, str]:
    label = _label_for(b.primary)
    unit = "块" if b.primary in DISK_POWER_TYPES else "条"
    title = "%s（%d %s）" % (label, b.total, unit)
    pairs = []
    for et, data, ts in b.items:
        name = _name_of(et, data)
        if not name:
            msg = event_formatter.build_message(et, data)
            name = _strip_label(msg[0], _label_for(et)) if msg else ""
        pairs.append((name or _label_for(et), ts))
    lines = _dedup_lines(pairs)
    body_lines = ["时间：%s ~ %s" % (_hhmmss(b.first), _hhmmss(b.last)), "明细："]
    for line in lines[:_MAX_LINES]:
        body_lines.append("· " + line)
    if len(lines) > _MAX_LINES:
        body_lines.append("…等 %d 条" % b.total)
    return title, "\n".join(body_lines)


def _trim_episode_text(data: Dict[str, Any]) -> Tuple[str, int, int]:
    """影视行 → (作品名, 季号, 集号)。非剧集返回季/集为 0。"""
    d = data or {}
    title = str(d.get("title") or "").strip()
    typ = str(d.get("type") or "").strip()
    work = str(d.get("work") or "").strip()
    try:
        season = int(d.get("season") or 0)
    except (TypeError, ValueError):
        season = 0
    try:
        episode = int(d.get("episode") or 0)
    except (TypeError, ValueError):
        episode = 0
    if typ in ("Episode", "Season"):
        if not work:
            work = str(d.get("series_title") or d.get("parent_title") or "").strip()
        if not work:
            work = title
    else:
        work = work or title
    return work, season, episode


def _resource_key(data: Dict[str, Any]) -> Tuple[str, int, int]:
    """影视资源的去重键：作品 + 季 + 集（电影只有作品名）。"""
    work, season, episode = _trim_episode_text(data)
    return (work, season, episode if episode else -1)


def _render_trim(b: _Bucket) -> Tuple[str, str]:
    added_all = [(d, ts) for et, d, ts in b.items if et == "TRIM_RESOURCE_ADDED"]
    scraped_all = [(d, ts) for et, d, ts in b.items if et == "TRIM_SCRAPE_SUCCESS"]
    # 同一资源在「入库」里已经出现过，就不再重复列进「刮削完成」
    added_keys = {_resource_key(d) for d, _ts in added_all}
    scraped = [(d, ts) for d, ts in scraped_all if _resource_key(d) not in added_keys]
    added = added_all
    added_lines, add_total = _trim_lines(added)
    scraped_lines, scrape_total = _trim_lines(scraped)
    # total 包含超出 _MAX_ITEMS 的部分，按比例兜底保证标题计数不丢
    if b.total > len(b.items):
        extra = b.total - len(b.items)
        if b.primary == "TRIM_RESOURCE_ADDED":
            add_total += extra
        else:
            scrape_total += extra
    title = "影视处理本轮汇总（入库 %d · 刮削 %d）" % (add_total, scrape_total)
    parts: List[str] = []
    if added:
        parts.append("【本轮入库 · %d 项】" % add_total)
        parts.extend(added_lines)
    if scraped:
        if parts:
            parts.append("")
        parts.append("【刮削完成 · %d 项】" % scrape_total)
        parts.extend(scraped_lines)
    parts.append("")
    parts.append("统计时间：%s ~ %s" % (_hhmmss(b.first), _hhmmss(b.last)))
    return title, "\n".join(parts)


def _trim_lines(rows: List[Tuple[Dict[str, Any], float]]) -> Tuple[List[str], int]:
    """影视条目 → (去重后的展示行, 去重后的资源数)；电视剧按「作品 + 季」聚合单集。"""
    movies: List[str] = []
    seen_movie: set = set()
    series: Dict[Tuple[str, int], List[int]] = {}
    series_order: List[Tuple[str, int]] = []
    for data, _ts in rows:
        work, season, episode = _trim_episode_text(data)
        title = str((data or {}).get("title") or "").strip()
        typ = str((data or {}).get("type") or "").strip()
        if typ in ("Episode", "Season") or episode or season:
            k = (work or title, season)
            if k not in series:
                series[k] = []
                series_order.append(k)
            if episode and episode not in series[k]:
                series[k].append(episode)
        else:
            name = title or work
            if not name:
                continue
            if name not in seen_movie:
                seen_movie.add(name)
                movies.append(name)
    lines: List[str] = []
    unique = len(movies) + sum(1 for _k in series_order)
    idx = 0
    for name in movies:
        idx += 1
        if idx > _MAX_LINES:
            break
        lines.append("· 《%s》" % name)
    for work, season in series_order:
        idx += 1
        if idx > _MAX_LINES:
            break
        eps = sorted(series.get((work, season)) or [])
        show = "《%s》" % (work or "未知作品")
        if season > 0:
            show += "第 %d 季" % season
        if eps:
            if len(eps) > 10:
                ep_txt = "第 %s、%s… 集（共 %d 集）" % (
                    eps[0], eps[1], len(eps))
            else:
                ep_txt = "第 %s 集" % "、".join(str(x) for x in eps)
            lines.append("· %s：%s" % (show, ep_txt))
        else:
            lines.append("· %s（整季入库）" % show)
    if len(lines) > _MAX_LINES:
        lines = lines[:_MAX_LINES]
        lines.append("…等 %d 项" % unique)
    if not lines:
        return ["· （无可读条目）"], 0
    return lines, unique


def _render_music(b: _Bucket) -> Tuple[str, str]:
    added = [(d, ts) for et, d, ts in b.items if et == "MUSIC_TRACK_ADDED"]
    removed = [(d, ts) for et, d, ts in b.items if et == "MUSIC_TRACK_REMOVED"]
    add_total = len(added)
    rm_total = len(removed)
    if b.total > len(b.items):
        if b.primary == "MUSIC_TRACK_ADDED":
            add_total += b.total - len(b.items)
        else:
            rm_total += b.total - len(b.items)
    title = "音乐库变更汇总（入库 %d 首 · 移出 %d 首）" % (add_total, rm_total)
    parts: List[str] = []
    if added:
        parts.append("【入库 · %d 首】" % add_total)
        parts.extend(_music_lines(added))
    if removed:
        if parts:
            parts.append("")
        parts.append("【移出库 · %d 首】" % rm_total)
        parts.extend(_music_lines(removed))
    parts.append("")
    parts.append("统计时间：%s ~ %s" % (_hhmmss(b.first), _hhmmss(b.last)))
    return title, "\n".join(parts)


def _music_lines(rows: List[Tuple[Dict[str, Any], float]]) -> List[str]:
    pairs = [(str((d or {}).get("title") or "").strip(), ts) for d, ts in rows]
    lines = _dedup_lines(pairs)
    out = ["· " + x for x in lines[:_MAX_LINES]]
    if len(lines) > _MAX_LINES:
        out.append("…等 %d 首" % len(lines))
    return out or ["· （无可读条目）"]
