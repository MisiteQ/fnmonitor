"""
SSH 日志监听（systemd journal）+ SSH 开启超时提醒 + WAN IP 变更。

三个独立线程，合并在一个文件减少零散模块：
1. SshJournalPoller：journalctl -f ssh.service → SSH_LOGIN_SUCCESS / SSH_DISCONNECTED / SSH_INVALID_USER / SSH_AUTH_FAILED
2. SshOpenWatch：定期检查 ssh.service 启动时间，超过 remind_minutes 仍在运行 → SSH_OPEN_TIMEOUT
3. WanIpMonitor：定时查 ipify.org / ifconfig.me → WAN_IP_CHANGED
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set, Tuple


# ---------- SSH journal ----------

# journalctl -f 输出中的 SSH 相关日志行正则（覆盖常见 sshd auth.log 格式）
_SSH_LOGIN_RE = re.compile(r"(?P<user>\S+)@(?P<ip>\S+)")
_SSH_RE_KEYS = {
    "accepted": "SSH_LOGIN_SUCCESS",
    "opened": "SSH_LOGIN_SUCCESS",
    "session opened": "SSH_LOGIN_SUCCESS",
    "disconnected": "SSH_DISCONNECTED",
    "session closed": "SSH_DISCONNECTED",
    "closed": "SSH_DISCONNECTED",
    "authentication failure": "SSH_AUTH_FAILED",
    "failed password": "SSH_AUTH_FAILED",
    "invalid user": "SSH_INVALID_USER",
    "refused connect": "SSH_INVALID_USER",
}


class SshJournalPoller:
    """通过 journalctl -f ssh.service 监听 SSH 登录/断开事件。"""

    DEBOUNCE_WINDOW = 5  # 同 user@ip 去重窗口（秒）
    DEDUP_TTL = 300

    def __init__(
        self,
        poll_interval: int = 5,
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        ignore_loopback: bool = True,
        ignore_lan: bool = False,
    ):
        self.poll_interval = max(1, int(poll_interval or 5))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.ignore_loopback = bool(ignore_loopback)
        self.ignore_lan = bool(ignore_lan)
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._proc: Optional[subprocess.Popen] = None
        self._dedup: Dict[str, float] = {}  # key = "event_type|user|ip"

    def _is_local_ip(self, ip: str) -> bool:
        """判断 IP 是否本机回环或局域网。"""
        if not ip:
            return True
        if ip in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
            return True
        # 172.16.0.0/12 私有段（172.16-31），避免误伤 172.x 公网地址
        if ip.startswith("192.168.") or ip.startswith("10.") or re.match(r"^172\.(1[6-9]|2\d|3[01])\.", ip):
            if self.ignore_lan:
                return True
        return False

    def _dedup_hit(self, key: str) -> bool:
        now = time.time()
        last = self._dedup.get(key, 0)
        if now - last < self.DEBOUNCE_WINDOW:
            return True
        self._dedup[key] = now
        # 清理过期
        self._dedup = {k: t for k, t in self._dedup.items() if now - t < self.DEDUP_TTL}
        return False

    def _parse_line(self, line: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        if not line:
            return None
        low = line.lower()
        event_type: Optional[str] = None
        for kw, et in _SSH_RE_KEYS.items():
            if kw in low:
                event_type = et
                break
        if not event_type:
            return None
        # 从行里提 user 和 IP（格式多样，尽量宽松）
        user = ""
        ip = ""
        m = _SSH_LOGIN_RE.search(line)
        if m:
            user = m.group("user").strip()
            ip = m.group("ip").strip()
        # invalid user 场景："Invalid user xxx from 1.2.3.4"
        if "invalid user" in low:
            um = re.search(r"[Ii]nvalid user (\S+) from (\S+)", line)
            if um:
                user = um.group(1)
                ip = um.group(2)
        # failed password 场景
        if "failed password" in low or "authentication failure" in low:
            um = re.search(r"(?:for|user) (\S+) from (\S+)", line)
            if um:
                user = um.group(1)
                ip = um.group(2)
        if self.ignore_loopback and self._is_local_ip(ip):
            return None
        if self._dedup_hit(f"{event_type}|{user}|{ip}"):
            return None
        return event_type, {"user": user, "IP": ip, "_source": "ssh_journal"}

    def _loop(self) -> None:
        while self.running:
            try:
                proc = subprocess.Popen(
                    ["journalctl", "-f", "-o", "short-iso", "ssh.service"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    text=True,
                    bufsize=1,
                )
            except Exception:
                # journalctl 不可用（非 systemd），直接放弃循环
                return
            self._proc = proc
            try:
                for line in proc.stdout:  # type: ignore[union-attr]
                    if not self.running:
                        break
                    result = self._parse_line(line.strip())
                    if result is None:
                        continue
                    event_type, event_data = result
                    if self.monitor_events and event_type not in self.monitor_events:
                        continue
                    if self.on_event:
                        try:
                            self.on_event(event_type, event_data)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                self._proc = None
                try:
                    proc.terminate()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=3.0)
                except Exception:
                    proc.kill()
            # 子进程退出，短暂退避后重连
            for _ in range(10):
                if not self.running:
                    return
                time.sleep(0.5)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="SshJournalPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        proc = self._proc
        if proc is not None and proc.poll() is None:
            # 结束 journalctl 子进程，让阻塞中的 for line 立刻返回
            try:
                proc.terminate()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()


# ---------- SSH open watch ----------

class SshOpenWatch:
    """定期检查 ssh.service 启动时间，超过 remind_minutes 仍在运行 → SSH_OPEN_TIMEOUT。"""

    def __init__(
        self,
        remind_minutes: int = 30,
        poll_interval: int = 120,
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.remind_minutes = max(5, int(remind_minutes or 30))
        self.poll_interval = max(30, int(poll_interval or 120))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._notified_ssh_open: float = 0  # 上次通知时间，避免刷屏

    def _ssh_active(self) -> Optional[float]:
        """返回 ssh.service 已启动的秒数（None 表示 SSH 未运行或无法判断）。"""
        try:
            proc = subprocess.run(
                ["systemctl", "is-active", "ssh.service"],
                capture_output=True, text=True, timeout=3.0,
            )
            if proc.returncode != 0:
                return None
            # 取 ActiveEnterTimestamp
            proc2 = subprocess.run(
                ["systemctl", "show", "ssh.service", "--property=ActiveEnterTimestamp", "--value"],
                capture_output=True, text=True, timeout=3.0,
            )
            val = (proc2.stdout or "").strip()
            if not val:
                return None
            # 格式：Thu 2025-01-15 12:30:00 UTC
            try:
                dt = datetime.strptime(val, "%a %Y-%m-%d %H:%M:%S %Z")
                return (datetime.now() - dt).total_seconds()
            except Exception:
                return None
        except Exception:
            return None

    def _loop(self) -> None:
        while self.running:
            age = self._ssh_active()
            if age is not None and age > self.remind_minutes * 60:
                # 只在超过 remind_minutes 时提醒一次；下次再到 remind_minutes 的整数倍时再提醒
                now = time.time()
                notify_interval = self.remind_minutes * 60  # 每 remind_minutes 分钟提醒一次
                if now - self._notified_ssh_open >= notify_interval:
                    self._notified_ssh_open = now
                    event_data = {
                        "remind_minutes": self.remind_minutes,
                        "elapsed_minutes": int(age / 60),
                        "_source": "ssh_open_watch",
                    }
                    if self.on_event:
                        try:
                            self.on_event("SSH_OPEN_TIMEOUT", event_data)
                        except Exception:
                            pass
            for _ in range(self.poll_interval * 2):
                if not self.running:
                    return
                time.sleep(0.5)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="SshOpenWatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()


# ---------- WAN IP monitor ----------

_WAN_IP_API = "https://api.ipify.org?format=json"  # 备用：https://ifconfig.me/ip


class WanIpMonitor:
    """定时查询外网 IP，变更时推送 WAN_IP_CHANGED。"""

    def __init__(
        self,
        poll_interval: int = 600,
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.poll_interval = max(60, int(poll_interval or 600))
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._last_ip: str = ""

    def _fetch_wan_ip(self) -> Optional[str]:
        try:
            req = urllib.request.Request(_WAN_IP_API, timeout=10, headers={"User-Agent": "fnmonitor"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read().decode("utf-8", errors="replace").strip()
            if data.startswith("{"):
                obj = json.loads(data)
                return str(obj.get("ip") or "").strip()
            return data.strip()
        except Exception:
            return None

    def _loop(self) -> None:
        # 启动先测一次
        self._last_ip = self._fetch_wan_ip() or ""
        while self.running:
            new_ip = self._fetch_wan_ip()
            if new_ip and self._last_ip and new_ip != self._last_ip:
                event_data = {
                    "old_ip": self._last_ip,
                    "new_ip": new_ip,
                    "_source": "wan_ip",
                }
                if self.on_event and "WAN_IP_CHANGED" not in self.monitor_events:
                    pass  # 未勾选时静默
                elif self.on_event:
                    try:
                        self.on_event("WAN_IP_CHANGED", event_data)
                    except Exception:
                        pass
                self._last_ip = new_ip
            elif new_ip:
                self._last_ip = new_ip
            for _ in range(self.poll_interval * 2):
                if not self.running:
                    return
                time.sleep(0.5)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="WanIpMonitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()
