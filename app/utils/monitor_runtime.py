"""
fnOS 日志事件监控运行时：组织所有轮询器 + 事件处理 → 推送出口。

server.py 的 main() 里：
    monitor_runtime.start_all(cfg, cursor_dir, notify_send_all_callable)

server.py 的信号处理（SIGTERM）里：
    monitor_runtime.stop_all()

此模块只负责"轮询器管理 + 事件→推送"的桥梁，不做配置读写、不做 UI。
"""

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from utils import event_formatter
from utils.db_log_poller import DBLogPoller
from utils.backup_db_poller import BackupDBPoller
from utils.media_photo_music_poller import MultiSourcePoller
from utils.docker_events_poller import DockerEventsPoller
from utils.ssh_wan_pollers import SshJournalPoller, SshOpenWatch, WanIpMonitor
from utils.event_aggregator import EventAggregator


# monitor 配置字段 → 说明：
# {
#   "monitor_events": ["LoginSucc", "SSH_LOGIN_SUCCESS", ...],   # 用户勾选的事件白名单
#   "poll_interval": 5,                                            # 默认轮询间隔（秒）
#   "logger_db_path": "/path/to/logger_data.db3",
#   "backup_db_path": "/path/to/basic_backup.db3",
#   "trim_media_db_path": "/path/to/trimmedia.db",
#   "trim_activity_db_path": "/path/to/trimactivity.db",
#   "photo_db_path": "/path/to/photo.db",
#   "music_db_path": "/path/to/music.db",
#   "docker_socket_path": "/var/run/docker.sock",
#   "ssh_open_remind_minutes": 30,
#   "wan_ip_poll_interval": 600,
#   "ignore_ssh_loopback": true,
#   "ignore_ssh_lan": false,
#   "enable": true,
# }


class MonitorRuntime:
    """持有所有轮询器的统一运行时。"""

    # 需要跨数据源去重的事件（登录/登出/SSH 类，字段里带 user + IP）
    _DEDUP_EVENTS = frozenset({
        "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout",
        "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
    })

    def __init__(
        self,
        cursor_dir: str,
        notify_fn: Callable[[str, str], Any],
        cfg: Optional[Dict[str, Any]] = None,
    ):
        """
        notify_fn: server.py 里的 notify_send_all(cfg, subject, body) 调用
        cfg: 完整的 config dict（包含 monitor_* 字段）
        """
        self.cursor_dir = cursor_dir
        self.notify_fn = notify_fn
        self.cfg = cfg or {}
        self._started = False
        self._lock = threading.Lock()
        # 去重：同一次 SSH 登录可能同时被主日志库（SshdLoginSucc）和 SSH journal 抓到
        self._dedup_lock = threading.Lock()
        self._dedup: Dict[str, float] = {}
        self._dedup_window = int(self.cfg.get("dedup_window") or 300)
        # 日志节流：轮询器每轮都会命中「未勾选的事件」，逐条打印会把日志刷满
        self._log_lock = threading.Lock()
        self._log_ts: Dict[str, float] = {}
        # 推送队列：采集线程只负责入队，HTTP 推送交给独立线程。
        # 之前是在轮询线程里同步发 HTTP，一个渠道慢 / 失败就会把后续事件全堵住，
        # 表现为「事情已经发生，推送却晚到十几秒」。
        self._push_q: "queue.Queue" = queue.Queue(maxsize=300)
        self._push_thread: Optional[threading.Thread] = None
        # 同类事件聚合：影视/音乐静默汇总、磁盘唤醒/休眠合并、全局同类合并
        self._agg = EventAggregator(self._on_aggregated, cfg=self.cfg)
        # 轮询器实例
        self.db_log: Optional[DBLogPoller] = None
        self.backup: Optional[BackupDBPoller] = None
        self.multi: Optional[MultiSourcePoller] = None
        self.docker: Optional[DockerEventsPoller] = None
        self.ssh_journal: Optional[SshJournalPoller] = None
        self.ssh_open: Optional[SshOpenWatch] = None
        self.wan_ip: Optional[WanIpMonitor] = None
        self._ensure_push_thread()

    # ---------- 事件统一处理：event_type + event_data → 推送 ----------
    _LOG_INTERVAL = 600  # 同类提示最多 10 分钟一条，避免轮询器把日志刷满

    def _log_throttled(self, key: str, msg: str) -> None:
        """按 key 节流打印：轮询每轮都会重复命中的提示只保留首条（及每 10 分钟一条）。"""
        now = time.time()
        with self._log_lock:
            if now - self._log_ts.get(key, 0.0) < self._LOG_INTERVAL:
                return
            self._log_ts[key] = now
            if len(self._log_ts) > 200:
                cutoff = now - self._LOG_INTERVAL
                self._log_ts = {k: v for k, v in self._log_ts.items() if v >= cutoff}
        print(msg)

    def _on_event(self, event_type: str, event_data: Dict[str, Any]) -> None:
        """所有轮询器拿到事件后都走这里：白名单 → 去重 → 聚合 / 即时推送。"""
        # 白名单检查：空列表 = 一条都不推送（与「清空」按钮语义一致）
        monitor_events = self.cfg.get("monitor_events")
        if monitor_events is None:
            monitor_events = []
        allowed = {str(x) for x in monitor_events}
        if event_type not in allowed and "VM" not in str(event_type).upper():
            # 动态 VM 事件（CREATE_VM / START_VM 等）放行，其余按白名单丢弃
            self._log_throttled("drop:%s" % event_type,
                                "[fnmonitor][monitor] 事件被白名单丢弃：%s（已选 %d 项，同类提示 10 分钟内只打一条）"
                                % (event_type, len(allowed)))
            return
        # 同一次登录/断开可能被主日志库与 SSH journal 各抓一遍，
        # 按「类型+用户+IP」去重，_dedup_window 时间窗内的重复事件只推一次
        if event_type in self._DEDUP_EVENTS:
            user = str(event_data.get("user") or event_data.get("username") or "")
            ip = str(event_data.get("IP") or event_data.get("ip") or "")
            now = time.time()
            fp = "%s|%s|%s" % (event_type, user, ip)
            with self._dedup_lock:
                if now - self._dedup.get(fp, 0) < self._dedup_window:
                    self._log_throttled("dedup:%s" % fp,
                                        "[fnmonitor][monitor] 重复事件已跳过：%s %s@%s" % (event_type, user, ip))
                    return
                self._dedup[fp] = now
                if len(self._dedup) > 200:
                    cutoff = now - self._dedup_window
                    self._dedup = {k: v for k, v in self._dedup.items() if v >= cutoff}
        # 聚合开关打开时先把同类事件收进缓冲，静默/窗口到期后再合成一条推送
        if self._agg.add(event_type, event_data):
            print("[fnmonitor][monitor] 事件进入聚合缓冲：%s（当前待汇总 %d）"
                  % (event_type, self._agg.pending()))
            return
        self._emit(event_type, event_data)

    def _emit(self, event_type: str, event_data: Dict[str, Any]) -> None:
        """格式化后入推送队列（单条事件 / 聚合汇总共用）。"""
        msg = event_formatter.build_message(event_type, event_data)
        if msg is None:
            print("[fnmonitor][monitor] 事件无格式化模板，跳过：%s" % event_type)
            return
        title, body = msg
        if not title and not body:
            return
        print("[fnmonitor][monitor] 触发推送：%s" % event_type)
        # 入队即返回，绝不在采集线程里等网络
        try:
            self._push_q.put_nowait((title, body, event_type))
        except queue.Full:
            print("[fnmonitor][monitor] 推送队列已满，丢弃事件：%s" % event_type)

    def _on_aggregated(self, title: str, body: str, event_type: str) -> None:
        """聚合器到期回调：汇总已是成品 (title, body)，直接入推送队列。"""
        print("[fnmonitor][monitor] 聚合汇总推送：%s（%s）" % (title, event_type))
        try:
            self._push_q.put_nowait((title, body, event_type))
        except queue.Full:
            print("[fnmonitor][monitor] 推送队列已满，丢弃汇总：%s" % event_type)

    def _push_loop(self) -> None:
        """独立发送线程：串行消费队列，网络慢也只影响发送节奏，不影响采集。"""
        while True:
            item = self._push_q.get()
            if item is None:
                return
            title, body, event_type = item
            try:
                self.notify_fn(title, body)
            except Exception as e:
                print("[fnmonitor][monitor] 推送调用异常 %s：%s" % (event_type, e))
            try:
                self._push_q.task_done()
            except Exception:
                pass

    def _ensure_push_thread(self) -> None:
        if self._push_thread is not None and self._push_thread.is_alive():
            return
        self._push_thread = threading.Thread(target=self._push_loop, name="MonitorPush", daemon=True)
        self._push_thread.start()

    # ---------- 启动 ----------
    def start(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        """按 cfg 里 enable 的数据源启动所有轮询器。已启动的先 stop 再 start。
        单个轮询器启动失败只记录日志、不影响其它轮询器（避免一个数据源挂掉导致全盘不工作）。"""
        if cfg is not None:
            self.cfg = cfg
        with self._lock:
            self._stop_unlocked()   # ← 内部调用走不加锁版本，避免自死锁
        self._ensure_push_thread()
        # 聚合配置随 monitor cfg 一起更新（保存设置后 runtime 会重建）
        self._agg.configure(self.cfg)
        self._agg.start()

        poll_interval = max(3, int(self.cfg.get("poll_interval") or 5))
        monitor_events = self.cfg.get("monitor_events") or []
        # 数据源开关（source_enabled）：关掉的库即使路径有值也不启动轮询器。
        # 缺省 / 旧配置没有这个字段时一律视为启用。
        _src = self.cfg.get("source_enabled") or {}
        if not isinstance(_src, dict):
            _src = {}

        def _src_on(key):
            return bool(_src.get(key, True))

        def _safe(label, factory):
            """factory() 返回 poller 实例并 start；失败时打印并返回 None。"""
            try:
                p = factory()
                p.start()
                print("[fnmonitor][monitor] 轮询器已启动：%s" % label)
                return p
            except Exception as e:
                print("[fnmonitor][monitor] 轮询器启动失败 %s：%s" % (label, e))
                return None

        # db_log
        if _src_on("logger_db_path") and self.cfg.get("logger_db_path"):
            self.db_log = _safe("主日志库", lambda: DBLogPoller(
                db_path=self.cfg["logger_db_path"],
                cursor_dir=self.cursor_dir,
                poll_interval=poll_interval,
                monitor_events=monitor_events,
                on_event=self._on_event,
            ))

        # backup
        if _src_on("backup_db_path") and self.cfg.get("backup_db_path"):
            self.backup = _safe("备份任务库", lambda: BackupDBPoller(
                db_path=self.cfg["backup_db_path"],
                cursor_dir=self.cursor_dir,
                poll_interval=max(5, int(self.cfg.get("backup_poll_interval") or poll_interval)),
                monitor_events=monitor_events,
                on_event=self._on_event,
            ))

        # media_photo_music
        self.multi = MultiSourcePoller(
            cursor_dir=self.cursor_dir,
            poll_interval=max(10, int(self.cfg.get("media_poll_interval") or poll_interval * 2)),
            monitor_events=monitor_events,
            on_event=self._on_event,
        )
        if _src_on("trim_media_db_path") and self.cfg.get("trim_media_db_path"):
            self.multi.register_trimmedia(self.cfg["trim_media_db_path"])
        if _src_on("trim_activity_db_path") and self.cfg.get("trim_activity_db_path"):
            self.multi.register_trimactivity(self.cfg["trim_activity_db_path"])
        if _src_on("photo_db_path") and self.cfg.get("photo_db_path"):
            self.multi.register_photo(self.cfg["photo_db_path"])
        if _src_on("music_db_path") and self.cfg.get("music_db_path"):
            self.multi.register_music(self.cfg["music_db_path"])
        if self.multi._pollers:
            try:
                self.multi.start()
                print("[fnmonitor][monitor] 轮询器已启动：影视/相册/音乐")
            except Exception as e:
                print("[fnmonitor][monitor] 轮询器启动失败 影视/相册/音乐：%s" % e)

        # docker
        if self.cfg.get("enable_docker", True):
            self.docker = _safe("Docker 容器", lambda: DockerEventsPoller(
                socket_path=self.cfg.get("docker_socket_path") or "/var/run/docker.sock",
                monitor_events=monitor_events,
                on_event=self._on_event,
            ))

        # ssh journal
        if self.cfg.get("enable_ssh_journal", True):
            self.ssh_journal = _safe("SSH journal", lambda: SshJournalPoller(
                poll_interval=poll_interval,
                monitor_events=monitor_events,
                on_event=self._on_event,
                ignore_loopback=bool(self.cfg.get("ignore_ssh_loopback", False)),
                ignore_lan=bool(self.cfg.get("ignore_ssh_lan", False)),
            ))

        # ssh open watch
        if self.cfg.get("enable_ssh_open_watch", True):
            self.ssh_open = _safe("SSH 开启超时", lambda: SshOpenWatch(
                remind_minutes=max(5, int(self.cfg.get("ssh_open_remind_minutes") or 30)),
                poll_interval=max(30, int(self.cfg.get("ssh_open_poll_interval") or 120)),
                monitor_events=monitor_events,
                on_event=self._on_event,
            ))

        # wan ip
        if self.cfg.get("enable_wan_ip", True):
            self.wan_ip = _safe("WAN IP 变更", lambda: WanIpMonitor(
                poll_interval=max(60, int(self.cfg.get("wan_ip_poll_interval") or 600)),
                monitor_events=monitor_events,
                on_event=self._on_event,
            ))

        self._started = True
        print("[fnmonitor][monitor] runtime 启动完成（started=True，白名单事件 %d 项）" % len(monitor_events))

    # ---------- 停止 ----------
    def stop(self) -> None:
        with self._lock:
            self._stop_unlocked()

    def _stop_unlocked(self) -> None:
        """不加锁版本（由 start/restart 内部持锁时调用，避免自死锁）。
        各轮询器并行停止：串行 join 每个最多等 poll_interval+2 秒，
        保存设置时 7 个轮询器叠加就是几秒钟的卡顿。"""
        self._started = False
        stopping = []
        for attr in ("db_log", "backup", "multi", "docker", "ssh_journal", "ssh_open", "wan_ip"):
            poller = getattr(self, attr, None)
            if poller is None:
                continue
            setattr(self, attr, None)
            t = threading.Thread(target=self._safe_stop, args=(poller,), daemon=True)
            t.start()
            stopping.append(t)
        for t in stopping:
            t.join(timeout=6)
        # 停止聚合器：把还没到期的缓冲立刻合成推送，避免升级重启丢事件
        try:
            self._agg.stop(flush=True)
        except Exception:
            pass
        if self._push_thread is not None:
            try:
                self._push_q.put(None, timeout=0.2)
            except queue.Full:
                pass
            self._push_thread.join(timeout=3)
            if not self._push_thread.is_alive():
                self._push_thread = None

    @staticmethod
    def _safe_stop(poller) -> None:
        try:
            poller.stop()
        except Exception:
            pass

    def update_config(self, cfg: Dict[str, Any]) -> None:
        """配置变更 → 优雅重启所有轮询器。"""
        self.cfg = cfg
        self.start()

    # ---------- 状态查询 ----------
    def status(self) -> Dict[str, Any]:
        """返回各轮询器运行状态（供 Web 接口查询）。"""
        out: Dict[str, Any] = {"started": self._started, "pollers": {}}
        try:
            out["aggregate"] = {
                "enabled": bool(self._agg.aggregate_enabled),
                "window": self._agg.aggregate_window,
                "disk_enabled": bool(self._agg.disk_enabled),
                "disk_window": self._agg.disk_window,
                "media_enabled": bool(self._agg.media_enabled),
                "media_silence": self._agg.media_silence,
                "pending": self._agg.pending(),
            }
        except Exception:
            out["aggregate"] = {}
        for attr, label in [
            ("db_log", "主日志库"),
            ("backup", "备份任务库"),
            ("multi", "影视/相册/音乐"),
            ("docker", "Docker 容器"),
            ("ssh_journal", "SSH journal"),
            ("ssh_open", "SSH 开启超时"),
            ("wan_ip", "WAN IP 变更"),
        ]:
            p = getattr(self, attr, None)
            # 单个轮询器状态查询失败不能拖垮整个接口
            try:
                out["pollers"][label] = bool(p and p.is_running())
            except Exception:
                out["pollers"][label] = False
        return out
