"""
Docker 容器事件监听：通过 Docker Unix socket 订阅 Engine /events 流。

两种实现：
1. 如果 docker SDK 已安装 → 用 docker.Events()（官方 SDK，重连/筛选更好）
2. 否则直接从 /var/run/docker.sock 读 HTTP /events 流（JSON 每行）

优雅降级：socket 不可访问时，start() 不报错，但 run_once 返回 None。
"""

import json
import os
import socket
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional


DOCKER_ACTION_TO_EVENT: Dict[str, str] = {
    "start": "DOCKER_CONTAINER_START",
    "die": "DOCKER_CONTAINER_DIE",
    "stop": "DOCKER_CONTAINER_STOP",
    "oom": "DOCKER_CONTAINER_OOM",
    "kill": "DOCKER_CONTAINER_KILL",
    "pause": "DOCKER_CONTAINER_PAUSE",
    "unpause": "DOCKER_CONTAINER_UNPAUSE",
    "restart": "DOCKER_CONTAINER_RESTART",
    "destroy": "DOCKER_CONTAINER_DESTROY",
    "create": "DOCKER_CONTAINER_CREATE",
}

# 默认过滤的 exec_* 动作（健康检查等高频探活）——不映射到任何通知事件，
# 避免 pg_isready / mihomo 探活等产生 24+ 事件/分钟的噪声
_EXEC_ACTIONS = frozenset({"exec_create", "exec_start", "exec_die", "exec_detach"})


def _parse_event_timestamp(ev: Dict[str, Any]) -> str:
    ts = ev.get("time") or ev.get("Time")
    if isinstance(ts, (int, float)) and ts > 0:
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class DockerEventsPoller:
    """后台线程阻塞读取 Docker Engine events，分发事件。"""

    RECONNECT_BACKOFF = 5  # 秒，重连退避

    def __init__(
        self,
        socket_path: str = "/var/run/docker.sock",
        monitor_events: Optional[List[str]] = None,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    ):
        self.socket_path = (socket_path or "/var/run/docker.sock").strip()
        self.monitor_events = set(monitor_events or [])
        self.on_event = on_event
        self.running = False
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None

    def _available(self) -> bool:
        """socket 存在且可读写。"""
        try:
            st = os.stat(self.socket_path)
            import stat as _stat
            return _stat.S_ISSOCK(st.st_mode) and os.access(self.socket_path, os.R_OK | os.W_OK)
        except Exception:
            return False

    def _decode(self, line: bytes) -> Optional[Dict[str, Any]]:
        try:
            raw = line.decode("utf-8", errors="replace").strip()
            if not raw:
                return None
            obj = json.loads(raw)
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None

    @staticmethod
    def _is_chunk_size(line: bytes) -> bool:
        """HTTP chunked 编码的长度行（纯十六进制，可带分号扩展）。"""
        s = line.split(b";", 1)[0].strip()
        try:
            int(s, 16)
            return True
        except ValueError:
            return False

    def _dispatch(self, ev: Dict[str, Any]) -> None:
        action = str(ev.get("Action") or "").strip().lower()
        # exec_* 是健康检查高频探活（pg_isready 每 10s 一次等），跳过以降噪 + 减负
        if action in _EXEC_ACTIONS:
            return
        event_type = DOCKER_ACTION_TO_EVENT.get(action)
        if not event_type:
            return
        if self.monitor_events and event_type not in self.monitor_events:
            return
        actor = ev.get("Actor") or {}
        if not isinstance(actor, dict):
            actor = {}
        attrs = actor.get("Attributes") or {}
        if not isinstance(attrs, dict):
            attrs = {}
        container_id = actor.get("ID") or ""
        container_name = attrs.get("name") or ""
        image = attrs.get("image") or ""
        exit_code = attrs.get("exitCode") or ev.get("exitCode") or ""
        event_data = {
            "container_name": container_name,
            "container_id": container_id[:12],
            "image": image,
            "exit_code": exit_code,
            "_source": "docker",
        }
        if self.on_event:
            try:
                self.on_event(event_type, event_data)
            except Exception:
                pass

    def _loop(self) -> None:
        """简单 HTTP 实现：connect → GET /events → 按行解析 JSON。"""
        # 连接后保持长连接；Docker Engine 按行发 JSON event
        # 无 since/until 游标：重连后只看到新事件（Engine 保留历史上限内）
        # 如果要"从上次断开位置续上"，需要传 since=Unix 秒
        since = int(time.time()) - 60  # 重连最多补拉 1 分钟
        while self.running:
            if not self._available():
                # socket 不可用，退避等
                for _ in range(self.RECONNECT_BACKOFF * 2):
                    if not self.running:
                        return
                    time.sleep(0.5)
                continue
            try:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(30.0)
                sock.connect(self.socket_path)
                try:
                    sock.sendall(
                        f"GET /events?since={since}&filters={{\"type\":[\"container\"]}} HTTP/1.1\r\n"
                        f"Host: localhost\r\nConnection: keep-alive\r\n\r\n".encode()
                    )
                    # 读 HTTP 响应头直到 \r\n\r\n
                    buf = b""
                    while b"\r\n\r\n" not in buf:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        buf += chunk
                        if len(buf) > 8192:
                            break
                    # 读 body 行（Docker events 是 HTTP chunked 或按行）。
                    # 保持 30s 超时：空闲超时后回查 running，保证 stop() 能打断阻塞 recv
                    sock.settimeout(30.0)
                    self._sock = sock
                    line_buf = b""
                    carry = b""  # JSON 被拆到多行时暂存，拼到下一行重试
                    MAX_CARRY = 65536  # carry 硬上限 64KB，超限直接丢弃重连
                    while self.running:
                        try:
                            chunk = sock.recv(4096)
                        except (socket.timeout, TimeoutError):
                            continue
                        if not chunk:
                            break
                        line_buf += chunk
                        while b"\n" in line_buf:
                            line, line_buf = line_buf.split(b"\n", 1)
                            # 先单独检查 line：空行 / chunk 长度行直接跳过，
                            # 避免 carry 非空时 _is_chunk_size(data) 永远失效的问题
                            ls = line.strip()
                            if not ls or self._is_chunk_size(ls):
                                continue

                            data = carry + line
                            had_carry = bool(carry)
                            carry = b""
                            ev = self._decode(data)

                            # 解析失败时，逐层尝试修复：
                            if ev is None:
                                # 1) carry 可能是上轮残留脏数据，试一下只解析 line
                                if had_carry:
                                    ev = self._decode(line)
                                # 2) line 里可能粘了 chunk 长度行前缀（如 "627{"），
                                #    剥掉第一个 { 之前的内容再试
                                if ev is None:
                                    brace_idx = line.find(b"{")
                                    if brace_idx > 0:
                                        ev = self._decode(line[brace_idx:])

                            if ev is None:
                                # 最终失败：只保留 line 作为下轮 carry，
                                # 加上限防止无限增长（Issue #42 核心修复）
                                carry = line[-MAX_CARRY:]
                                continue

                            self._dispatch(ev)
                            t = ev.get("time") or ev.get("Time")
                            if isinstance(t, (int, float)) and t > since:
                                since = int(t)
                finally:
                    self._sock = None
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    try:
                        sock.close()
                    except Exception:
                        pass
            except Exception:
                pass
            # 重连退避
            for _ in range(self.RECONNECT_BACKOFF * 2):
                if not self.running:
                    return
                time.sleep(0.5)
            since = int(time.time()) - 10  # 重连时 since 往回拉 10 秒

    def start(self) -> None:
        if not self._available():
            # 启动时不报错——run_once 会退避等待
            pass
        if self._thread and self._thread.is_alive():
            return
        self.running = True
        self._thread = threading.Thread(target=self._loop, name="DockerEventsPoller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.running = False
        sock = self._sock
        if sock is not None:
            # 关闭 socket 让阻塞中的 recv 立刻抛错返回
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=3.0)
        self._thread = None

    def is_running(self) -> bool:
        return self.running and bool(self._thread) and self._thread.is_alive()
