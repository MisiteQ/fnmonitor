#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fnMonitor - 飞牛 fnOS 系统监控后端
====================================
功能：
  1. 系统资源采集：CPU / 内存 / 磁盘 / 网络 / 温度 / 负载 / 运行时间
  2. Docker 容器监控：容器列表、状态、CPU / 内存占用、端口
  3. 功能模块检测：文件共享、影视、相册、下载、SSH 等运行状态
  4. 历史趋势：SQLite 持久化，保留 N 天，提供趋势查询 API
  5. HTTP API + 静态面板（零第三方依赖，仅 Python 标准库）

用法：
  python3 server.py --port 8777 --data-dir /vol1/@appdata/fnmonitor
"""
import argparse
import base64
import csv
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import math
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.parse
import urllib.request

def _read_manifest_version():
    """从 manifest 读取权威版本号（打包版本由 manifest 决定，避免与应用内常量双处维护不一致）。
    依次尝试：TRIM_APPDEST（fnOS 应用目录）、仓库布局 app/ 上一级、fnOS 应用基础目录。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (os.environ.get("TRIM_APPDEST", ""), os.path.dirname(here),
                 "/var/apps/fnmonitor"):
        if not base:
            continue
        try:
            with open(os.path.join(base, "manifest"), "r", encoding="utf-8",
                      errors="ignore") as f:
                for line in f:
                    m = re.match(r'\s*version\s*=\s*"?([^"\r\n]+?)"?\s*$', line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    return ""

VERSION = _read_manifest_version() or "2.18.4"   # manifest 不可读时回退（须与 manifest 同步）
UPDATE_REPO = "MisiteQ/fnmonitor"          # GitHub 仓库：在线检查更新 / 下载安装包
UPDATE_CHECK_INTERVAL = 6 * 3600           # 自动更新检查周期（6 小时）
# 下载加速：直连 GitHub 下载域在国内常不可达，失败后自动依次尝试公共加速镜像
GH_MIRRORS = ["", "https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/", "https://gh.llkk.cc/"]
DEFAULT_CONFIG = {"interval": 10, "retention_days": 7, "port": 0, "history_interval": 60, "weather_city": "", "data_dir": "", "update_autocheck": 1, "update_autodownload": 0, "update_autoupdate": 0, "traffic_exclude_bridge": 0, "power_tdp_w": 65, "power_rate_yuan": 0.6, "power_disk_typical_w": 8, "power_nic_fixed_w": 5, "power_base_w": 8, "disk_standby_protect": 1, "disk_scan_enabled": 1, "disk_scan_interval_h": 6, "open_mode": "url", "monthly_summary_enabled": 0, "auth_enabled": 0, "auth_password_hash": "", "auth_session_days": 7, "ec_fan_probe": 0}
# 提醒阈值（v2.18.0 新增）：前端「设置 → 提醒阈值」写入 ui.json 的 thresholds 字段，
# 界面着色 / 趋势阈值线 / 首页结论与健康报告共用同一口径。键 → (默认值, 最小, 最大)
THRESHOLD_SPEC = {
    "cpuWarn": (85, 30, 100), "cpuCrit": (95, 30, 100),
    "memWarn": (85, 30, 100), "memCrit": (95, 30, 100),
    "capWarn": (85, 30, 100), "capCrit": (95, 30, 100),
    "tempWarn": (80, 40, 110), "tempCrit": (90, 40, 115),
    "diskTempWarn": (50, 30, 80), "diskTempCrit": (58, 30, 85),
}


def sanitize_thresholds(raw):
    """校验并补全阈值：非数字或越界的项回落默认值，并保证 严重 ≥ 警告。"""
    out = {}
    raw = raw if isinstance(raw, dict) else {}
    for k, (dv, lo, hi) in THRESHOLD_SPEC.items():
        try:
            v = float(raw.get(k, dv))
            if not math.isfinite(v):
                raise ValueError
            v = max(lo, min(hi, v))
        except Exception:
            v = float(dv)
        out[k] = int(v) if float(v).is_integer() else round(v, 1)
    for w, c in (("cpuWarn", "cpuCrit"), ("memWarn", "memCrit"), ("capWarn", "capCrit"),
                 ("tempWarn", "tempCrit"), ("diskTempWarn", "diskTempCrit")):
        if out[c] < out[w]:
            out[c] = out[w]
    return out


# ---------------------------------------------------------------------------
# 安全基础设施（v2.18.0 加固）
# ---------------------------------------------------------------------------
# 登录口令：config.json 只保存 PBKDF2-HMAC-SHA256 哈希（auth_password_hash，格式
#   pbkdf2_sha256$<迭代次数>$<salt_b64>$<hash_b64>），绝不保存 / 回传明文。旧版明文
#   auth_password 在 load_config 时自动迁移为哈希并从文件中删除。
# 忘记口令的恢复方法（README 用）：SSH 登录 NAS，编辑
#   /var/apps/fnmonitor/var/config.json（即 TRIM_PKGVAR/config.json），删除
#   "auth_password_hash" 一行（或把 "auth_enabled" 改为 0），然后在应用中心重启
#   「飞牛监控」。之后在局域网内用 IP 地址打开面板即可重新设置口令。
SECRET_CONFIG_KEYS = ("auth_password", "auth_password_hash")
PBKDF2_ITERATIONS = 240000
PASSWORD_MIN_LEN = 6
MAX_BODY_BYTES = 1024 * 1024          # POST 请求体上限（ui.json 正常只有几 KB）
_CFG_WRITE_LOCK = threading.RLock()   # config.json 读改写串行化


def public_config(cfg):
    """去掉口令哈希等敏感字段后的配置副本（所有对外接口 / 导出统一使用）。"""
    out = {k: v for k, v in (cfg or {}).items() if k not in SECRET_CONFIG_KEYS}
    out["auth_password_set"] = bool(str((cfg or {}).get("auth_password_hash") or "").strip())
    return out


def hash_password(pw, iterations=PBKDF2_ITERATIONS, salt=None):
    salt = salt or os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), salt, int(iterations))
    return "pbkdf2_sha256$%d$%s$%s" % (int(iterations), base64.b64encode(salt).decode("ascii"),
                                       base64.b64encode(dk).decode("ascii"))


def verify_password(pw, stored):
    """恒定时间比较口令与已存哈希；格式异常一律返回 False。"""
    try:
        algo, iters, salt_b64, hash_b64 = str(stored or "").split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        iters = int(iters)
        if iters < 1000 or iters > 10000000:
            return False
        salt = base64.b64decode(salt_b64)
        expect = base64.b64decode(hash_b64)
        dk = hashlib.pbkdf2_hmac("sha256", str(pw).encode("utf-8"), salt, iters)
        return hmac.compare_digest(dk, expect)
    except Exception:
        return False


def write_json_secure(path, obj):
    """原子写 JSON 文件且权限 0600：临时文件（O_CREAT 0600）→ fsync → chmod → os.replace。"""
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    tmp = "%s.tmp.%d.%d" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            try:
                os.fsync(f.fileno())
            except Exception:
                pass
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise


_CITY_BAD_CHARS_RE = re.compile(r"[\x00-\x1f\x7f<>\"'&`\\]")


def clean_text(s, max_len=64):
    """外部 / 用户字符串净化：去控制字符与 HTML 敏感字符，压缩空白，截断长度。"""
    s = _CITY_BAD_CHARS_RE.sub("", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_len]


def parse_coords(s):
    """'lat,lon' → (lat, lon)；格式或范围不合法返回 None。"""
    parts = [p.strip() for p in str(s or "").split(",")]
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except Exception:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon)) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    return lat, lon


def norm_ip(ip):
    """'::ffff:1.2.3.4' → '1.2.3.4'；去掉 IPv6 zone。"""
    ip = str(ip or "").split("%", 1)[0]
    try:
        a = ipaddress.ip_address(ip)
        if a.version == 6 and a.ipv4_mapped:
            return str(a.ipv4_mapped)
        return str(a)
    except Exception:
        return ip


def split_host_port(host):
    """解析 Host 头 → (hostname 小写, port 或 None)。兼容 [v6]:port / 裸 v6。"""
    h = str(host or "").strip().lower()
    if not h:
        return "", None
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return h, None
        name, rest = h[1:end], h[end + 1:]
        port = rest[1:] if rest.startswith(":") else ""
    elif h.count(":") == 1:
        name, port = h.split(":", 1)
    else:
        name, port = h, ""          # 主机名或未加括号的 IPv6
    try:
        port = int(port) if port else None
    except Exception:
        port = -1
    return name.rstrip("."), port


# 鉴权关闭时允许的 Host（防 DNS rebinding）：IP 字面量、localhost、单标签主机名、
# 常见局域网后缀。通过公网 / 自有域名访问前须先开启登录口令（cookie 保护后任意 Host 均可）。
LAN_HOST_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost")


def host_is_lan(host):
    name, port = split_host_port(host)
    if not name:
        return True                 # HTTP/1.0 无 Host：非浏览器客户端，不存在 rebinding
    if port == -1:
        return False
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
        return True
    except Exception:
        pass
    if name == "localhost" or "." not in name:
        return True
    return name.endswith(LAN_HOST_SUFFIXES)


HOST_BLOCKED_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>飞牛监控 · 访问受限</title>
<style>body{font-family:system-ui,'PingFang SC','Microsoft YaHei',sans-serif;background:#f1f5f9;color:#0f172a;
margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}
.c{background:#fff;border-radius:16px;padding:32px 36px;max-width:520px;box-shadow:0 2px 12px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 12px}p{line-height:1.7;color:#334155;margin:8px 0}code{background:#f1f5f9;padding:1px 6px;border-radius:6px}</style>
</head><body><div class="c"><h1>通过域名访问前请先开启登录口令</h1>
<p>为防止 DNS 重绑定等攻击，未开启登录口令时，飞牛监控只允许通过局域网 IP 地址（如 <code>http://192.168.1.10:8777</code>）、
<code>localhost</code> 或局域网主机名访问。</p>
<p>请先在局域网内用 IP 地址打开飞牛监控，进入「设置 → 登录口令」设置口令并开启登录保护，之后即可通过域名访问。</p>
</div></body></html>""".encode("utf-8")


class LoginLimiter:
    """登录失败限速：按来源 IP 计数，10 分钟内失败 5 次锁定 60 秒，再次触发翻倍，
    最长 15 分钟；另设全局上限（60 秒内全部来源失败 30 次 → 全局锁 30 秒），
    防止分布式 / 多地址爆破。所有口令校验（登录、修改口令的旧口令）共用。"""
    WINDOW = 600
    MAX_FAILS = 5
    BASE_LOCK = 60
    MAX_LOCK = 900
    GLOBAL_WINDOW = 60
    GLOBAL_MAX = 30
    GLOBAL_LOCK = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._ips = {}
        self._global = []
        self._global_until = 0.0
        # PBKDF2 很耗 CPU：限制同时进行的校验数，避免并发请求把赛扬 CPU 打满
        self.verify_sem = threading.BoundedSemaphore(2)

    def retry_after(self, ip):
        now = time.time()
        with self._lock:
            wait = 0.0
            if self._global_until > now:
                wait = self._global_until - now
            st = self._ips.get(ip)
            if st and st.get("until", 0) > now:
                wait = max(wait, st["until"] - now)
        return int(math.ceil(wait)) if wait > 0 else 0

    def fail(self, ip):
        now = time.time()
        with self._lock:
            self._global = [t for t in self._global if now - t < self.GLOBAL_WINDOW]
            self._global.append(now)
            if len(self._global) >= self.GLOBAL_MAX:
                self._global_until = now + self.GLOBAL_LOCK
                self._global = []
            st = self._ips.setdefault(ip, {"fails": [], "until": 0.0, "level": 0, "last": now})
            if now - st.get("last", now) > 3600:
                st["level"] = 0        # 1 小时无失败后锁定等级复位
            st["last"] = now
            st["fails"] = [t for t in st["fails"] if now - t < self.WINDOW] + [now]
            if len(st["fails"]) >= self.MAX_FAILS:
                st["level"] += 1
                st["until"] = now + min(self.MAX_LOCK, self.BASE_LOCK * (2 ** (st["level"] - 1)))
                st["fails"] = []
            if len(self._ips) > 4096:      # 内存上界：丢弃最久未活动的一半
                for k in sorted(self._ips, key=lambda k: self._ips[k].get("last", 0))[:2048]:
                    self._ips.pop(k, None)

    def success(self, ip):
        with self._lock:
            self._ips.pop(ip, None)


def sanitize_ignored(raw):
    """校验「已忽略的健康提醒」（v2.18.3）：[{key, fp, label, level, at}]，最多 100 条。
    key 标识问题、fp 为忽略时的状态指纹（前端比较指纹变化即重新提醒）；只保留字符串 / 数字，限制长度。"""
    out, seen = [], set()
    if not isinstance(raw, list):
        return out
    for x in raw[-100:]:
        if not isinstance(x, dict):
            continue
        key, fp = x.get("key"), x.get("fp")
        if not isinstance(key, str) or not isinstance(fp, str) or not key or len(key) > 120 or len(fp) > 400 or key in seen:
            continue
        seen.add(key)
        level = x.get("level") if x.get("level") in ("warn", "crit", "info") else "warn"
        try:
            at = int(float(x.get("at") or 0))
        except Exception:
            at = 0
        label = "".join(ch for ch in str(x.get("label") or key) if ch >= " ")[:160]
        out.append({"key": key, "fp": fp, "label": label, "level": level, "at": max(0, at)})
    return out


# 虚拟网卡前缀（Docker 网桥 / 容器 / VPN 等）：流量与功耗估算统一口径
VIRT_IFACE_PREFIXES = ("docker", "veth", "br-", "virbr", "tun", "tap", "vnet", "lxc", "kube", "wg")

# 运行期开关（采集线程按 config 刷新）：standby_protect=休眠硬盘保护，
# 开启后容量 statvfs / SMART / 温度 / 历史库写入 / 应用目录扫描等磁盘访问
# 对已 STANDBY 的硬盘一律跳过或走缓存，避免周期性轮询把机械硬盘唤醒
RUNTIME = {"standby_protect": True, "ec_fan_probe": False, "interval": 10}
# 巡检结果的内存副本：dev → 与 smart_detail 同形状的 dict。
# 60 秒采集循环与详情面板都从这里取，避免反复跑 smartctl（真正的读盘只发生在后台巡检线程）。
_DISK_HEALTH_MEM = {}

# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def read_text(path):
    try:
        with open(path, "r", errors="ignore") as f:
            return f.read()
    except Exception:
        return ""


def read_int_file(path):
    t = read_text(path).strip()
    try:
        return int(float(t))
    except Exception:
        return 0


def run_cmd(cmd, timeout=10):
    """执行外部命令，返回 (stdout, returncode)。"""
    try:
        r = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=timeout)
        return r.stdout, r.returncode
    except Exception:
        return "", -1


def is_linux():
    return sys.platform.startswith("linux")


def fmt_bytes(n):
    """字节数转可读字符串。"""
    try:
        n = float(n)
    except Exception:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return "%.1f %s" % (n, unit)
        n /= 1024.0
    return "%.1f PB" % n


def fmt_rate(n):
    """速率转可读字符串。"""
    return fmt_bytes(n) + "/s"


# ---------------------------------------------------------------------------
# 系统资源采集（Linux /proc /sys）
# ---------------------------------------------------------------------------
def read_cpu_times():
    """读取 /proc/stat 的 CPU 时间。返回 (总时间列表, 每核时间列表)。

    取前 8 个字段：user/nice/system/idle/iowait/irq/softirq/steal。
    steal（虚拟机被宿主机占用的时间）计入总量，虚拟机内使用率才准确；
    guest/guest_nice 已包含在 user 中，不再重复计入。
    cpuN 严格按数字后缀匹配，避免误收其他以 cpu 开头的行。"""
    total = None
    cores = []
    for line in read_text("/proc/stat").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            total = [int(x) for x in parts[1:9]]
        elif re.match(r"^cpu\d+$", parts[0]):
            try:
                cores.append([int(x) for x in parts[1:9]])
            except Exception:
                pass
    return total, cores


def calc_cpu_percent(prev, curr):
    """根据两次采样计算 CPU 使用率（%）和空闲率。"""
    if prev is None or curr is None:
        return 0.0
    if len(prev) < 4 or len(curr) < 4:
        return 0.0
    prev_idle = prev[3] + (prev[4] if len(prev) > 4 else 0)
    curr_idle = curr[3] + (curr[4] if len(curr) > 4 else 0)
    prev_total = sum(prev)
    curr_total = sum(curr)
    total_delta = curr_total - prev_total
    idle_delta = curr_idle - prev_idle
    if total_delta <= 0:
        return 0.0
    return round(max(0.0, 100.0 * (1.0 - idle_delta / total_delta)), 1)


def read_meminfo():
    """读取 /proc/meminfo，返回内存与 Swap 信息（字节）。"""
    data = {}
    for line in read_text("/proc/meminfo").splitlines():
        if ":" not in line:
            continue
        key, _, rest = line.partition(":")
        parts = rest.strip().split()
        try:
            data[key.strip()] = int(parts[0]) * 1024  # kB -> B
        except Exception:
            pass
    total = data.get("MemTotal", 0)
    available = data.get("MemAvailable", data.get("MemFree", 0))
    used = total - available
    if total <= 0:
        percent = 0.0
    else:
        percent = round(used / total * 100, 1)
    swap_total = data.get("SwapTotal", 0)
    swap_free = data.get("SwapFree", 0)
    swap_used = swap_total - swap_free
    swap_percent = round(swap_used / swap_total * 100, 1) if swap_total else 0.0
    return {
        "total": total, "used": used, "free": data.get("MemFree", 0),
        "available": available, "percent": percent,
        "cached": data.get("Cached", 0), "buffers": data.get("Buffers", 0),
        "swap_total": swap_total, "swap_used": swap_used, "swap_free": swap_free,
        "swap_percent": swap_percent,
    }


def read_loadavg():
    t = read_text("/proc/loadavg").split()
    try:
        return [float(t[0]), float(t[1]), float(t[2])]
    except Exception:
        return [0.0, 0.0, 0.0]


def read_uptime():
    t = read_text("/proc/uptime").split()
    try:
        return float(t[0])
    except Exception:
        return 0.0


def read_cpu_model():
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith("model name"):
            return line.split(":", 1)[1].strip()
    return ""


def read_cpu_topology():
    """读取 CPU 拓扑，返回 (物理核心数, 逻辑线程数, 插槽数)。

    优先 lscpu（x86/ARM 通用、字段稳定）：物理核 = Socket(s) × Core(s) per socket，
    逻辑线程 = CPU(s)；缺失时回退 /proc/cpuinfo：processor 条目数 = 逻辑线程数，
    物理核按 (physical id, core id) 去重，或累加各插槽的 "cpu cores" 字段。

    注意：/proc/cpuinfo 的 processor 条目数是逻辑线程数（超线程下 ≠ 物理核数），
    siblings 是「单插槽逻辑线程数」而非整机线程数，旧代码把这两项误当物理核/总线程，
    导致超线程 CPU 显示 8核/8线程、多路 CPU 数值完全错误。"""
    cores = threads = sockets = 0

    def _first_int(s):
        m = re.search(r"\d+", s or "")
        return int(m.group(0)) if m else None

    out, rc = run_cmd(["lscpu"], timeout=10)
    if rc == 0 and out:
        kv = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
        cpus = _first_int(kv.get("CPU(s)"))
        tpc = _first_int(kv.get("Thread(s) per core"))
        cps = _first_int(kv.get("Core(s) per socket"))
        skt = _first_int(kv.get("Socket(s)"))
        if cpus:
            threads = cpus
        if skt:
            sockets = skt
        if cps and skt:
            cores = cps * skt
        elif cpus and tpc:
            cores = cpus // tpc

    if not threads or not cores:
        proc_threads = 0
        core_pairs = set()       # (physical id, core id) 去重 = 物理核数
        socket_cores = set()     # (physical id, cpu cores) 各插槽物理核数
        for block in re.split(r"\n\s*\n", read_text("/proc/cpuinfo")):
            rec = {}
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    rec[k.strip()] = v.strip()
            if "processor" not in rec:
                continue
            proc_threads += 1
            pid = rec.get("physical id")
            cid = rec.get("core id")
            if pid is not None and cid is not None:
                core_pairs.add((pid, cid))
            cc = rec.get("cpu cores")
            if pid is not None and cc is not None:
                try:
                    socket_cores.add((pid, int(cc)))
                except Exception:
                    pass
        if not threads:
            threads = proc_threads
        if not cores:
            if core_pairs:
                cores = len(core_pairs)
            elif socket_cores:
                cores = sum(c for _, c in socket_cores)
            else:
                cores = proc_threads  # ARM / 无拓扑字段：每线程即一核
    if not sockets:
        sockets = 1 if cores else 0
    return cores, threads, sockets


def _unescape_mount_path(s):
    """还原 /proc/mounts 挂载点里的八进制转义（如 \\040 -> 空格），中文等多字节字符不受影响。"""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def read_mounts():
    """解析 /proc/mounts，返回挂载点原始列表（含文件系统类型与挂载选项）。"""
    mounts = []
    for line in read_text("/proc/mounts").splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        device, mountpoint, fstype, opts = parts[0], parts[1], parts[2], parts[3]
        mounts.append({
            "device": device,
            "mount": _unescape_mount_path(mountpoint),
            "fs": fstype,
            "opts": opts.split(","),
        })
    return mounts


def disk_usage(mountpoint):
    try:
        st = os.statvfs(mountpoint)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        percent = round(used / total * 100, 1) if total else 0.0
        return {
            "total": total, "used": used, "free": free, "percent": percent,
            # 文件系统 ID：同一文件系统的所有挂载点（子卷/绑定/设备别名）fsid 相同，用于精确去重
            "fsid": getattr(st, "f_fsid", 0) or 0,
        }
    except Exception:
        return {"total": 0, "used": 0, "free": 0, "percent": 0.0, "fsid": 0}


# 伪文件系统（proc/sys/tmpfs/overlay 等，不是磁盘空间）
_PSEUDO_FS = {
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "cgroup", "cgroup2",
    "mqueue", "pstore", "securityfs", "debugfs", "tracefs", "fusectl",
    "configfs", "bpf", "squashfs", "ramfs", "autofs", "binfmt_misc",
    "hugetlbfs", "rpc_pipefs", "nsfs", "overlay", "efivarfs",
    "fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse",
    # 网络文件系统：占用的是远端空间，不能计入本机存储
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "9p", "ceph", "glusterfs",
    "fuse.sshfs", "fuse.rclone", "fuse.curlftpfs",
}
# 路径级排除：引导 / 恢复分区、Docker / 容器运行时目录
_SKIP_PATH_EXACT = {"/boot", "/efi", "/boot/efi", "/rescue", "/recovery"}
_SKIP_PATH_PREFIX = ("/boot/", "/efi/", "/sys/", "/proc/", "/dev/", "/run/",
                     "/var/lib/docker", "/var/lib/containerd", "/snap/")
# 小于该容量的文件系统视为引导/恢复小分区，不计入存储统计（256 MB）
_MIN_FS_BYTES = 256 * 1024 * 1024


def collect_disks():
    """返回本机真实磁盘文件系统列表（去重、过滤后）。

    口径说明：
    - 仅统计本地可写块设备文件系统（btrfs / ext4 / xfs / zfs / fuseblk 等）；
      伪文件系统、网络挂载(nfs/cifs/…)、只读介质、引导/恢复分区、<256MB 小分区排除；
    - fnOS 的应用/系统子卷（挂载路径含 /@，如 /vol1/@appdata）与存储池是同一个
      btrfs 文件系统；按 statvfs 的文件系统 ID(fsid) 去重——同一文件系统无论被
      挂载多少次、设备名写法如何（/dev/mapper/xxx 与 /dev/dm-x、UUID 别名、绑定
      挂载），只保留路径最浅的一个挂载点，彻底避免同一存储池被重复统计；
    - used 口径与系统 df 一致（含预留块），返回值单位为字节。
    """
    disks = []
    seen = {}   # key -> 条目
    order = []  # 保持首次出现顺序
    states = disk_power_states()
    for m in read_mounts():
        mp, dev, fs = m["mount"], m["device"], m["fs"]
        if fs in _PSEUDO_FS:
            continue
        if mp in _SKIP_PATH_EXACT or mp.startswith(_SKIP_PATH_PREFIX):
            continue
        # fnOS btrfs 子卷（/vol1/@appdata、/vol1/@appcenter、/@docker…）与存储池同文件系统
        if "/@" in mp:
            continue
        if "ro" in (m.get("opts") or []):
            continue
        # 休眠保护：statvfs 会向已 STANDBY 的硬盘下发文件系统查询而将其唤醒。
        # 盘休眠时沿用上次活跃时缓存的容量数据，并在条目标记 standby；
        # 服务刚启动且盘就处于休眠（尚无缓存）时跳过该挂载点，绝不主动唤醒。
        sleeping = mount_on_standby(mp, states)
        if sleeping:
            u = _DISK_USAGE_CACHE.get(mp)
            if not u or not u.get("total"):
                continue
        else:
            u = disk_usage(mp)
            if not u["total"] or u["total"] < _MIN_FS_BYTES:
                continue
            _DISK_USAGE_CACHE[mp] = dict(u)
        # fsid 为 0（个别文件系统不提供）时回退为设备真实路径，兼容 /dev/mapper 别名
        key = ("fsid", u["fsid"]) if u["fsid"] else ("dev", os.path.realpath(dev) or dev)
        entry = {
            "device": dev, "mount": mp, "fs": fs,
            "total": u["total"], "used": u["used"], "free": u["free"],
            "percent": u["percent"], "standby": bool(sleeping),
        }
        if key in seen:
            # 同一文件系统多挂载点：保留路径最浅（顶层）的一个
            cur = seen[key]
            if mp.count("/") < cur["mount"].count("/"):
                seen[key] = entry
            continue
        seen[key] = entry
        order.append(key)
    disks = [seen[k] for k in order]
    disks.sort(key=lambda d: -d["total"])
    return disks


SMART_COUNTER_KEYS = ("reallocated_sectors", "reallocated_events", "pending_sectors",
                      "uncorrectable", "udma_crc")


def collect_disks_detail(prev=None):
    """按物理硬盘返回详情：名称/品牌/型号/容量/已用/使用率/温度/挂载点/休眠状态。

    prev：上一轮详情缓存。已 STANDBY 的硬盘不再下发 smartctl（lsblk 本身只读
    sysfs 不会唤醒硬盘），健康/转速/通电时长/温度沿用上一次活跃时的值，避免
    详情采集每 60 秒把休眠硬盘唤醒一次。"""
    result = []
    prev_by = {}
    for old in (prev or []):
        if old.get("name"):
            prev_by[old["name"]] = old

    def _walk_mounts(blk, acc):
        """递归收集该设备树上的所有挂载点（兼容分区/LVM/device-mapper 多层结构）。"""
        if blk.get("mountpoint"):
            acc.append(blk["mountpoint"])
        for ch in blk.get("children", []) or []:
            _walk_mounts(ch, acc)

    out, rc = run_cmd(
        ["lsblk", "-b", "-J", "-o", "NAME,MODEL,VENDOR,SIZE,FSTYPE,MOUNTPOINT,RO,TYPE"],
        timeout=10,
    )
    if rc != 0 or not out.strip():
        return result
    try:
        data = json.loads(out)
    except Exception:
        return result
    # 挂载点占用统计（现有口径）
    mounts = {}
    for d in collect_disks():
        mounts[d["mount"]] = d
    # 温度（read_temps 内部同样跳过休眠盘并返回缓存温度）
    temp_by = {}
    for t in read_temps().get("disks", []):
        temp_by[t["name"]] = t["temp"]
    states = disk_power_states()
    for blk in data.get("blockdevices", []) or []:
        name = blk.get("name") or ""
        btype = (blk.get("type") or "").lower()
        if not name or btype == "loop" or blk.get("ro"):
            continue
        if not (name.startswith("sd") or name.startswith("nvme") or name.startswith("vd")):
            continue
        model = (blk.get("model") or "").strip()
        vendor = (blk.get("vendor") or "").strip()
        size = blk.get("size") or 0
        mount_paths = []
        _walk_mounts(blk, mount_paths)
        used = 0
        total = 0
        for mp in mount_paths:
            m = mounts.get(mp)
            if m:
                used += m["used"]
                total += m["total"]
        percent = round(used / total * 100, 1) if total else 0.0
        state = states.get(name, "unknown")
        old = prev_by.get(name) or {}
        entry = {
            "name": name,
            "model": model or old.get("model") or "未知型号",
            "vendor": vendor or old.get("vendor", ""),
            "size": size or old.get("size", 0),
            "used": used,
            "total": total,
            "percent": percent,
            "temp": temp_by.get(name) if temp_by.get(name) is not None else old.get("temp"),
            "mounts": mount_paths,
            "state": state,
            "standby": state == "standby",
            "health": old.get("health", ""),
            "rpm": old.get("rpm", ""),
            "power_on_hours": old.get("power_on_hours", ""),
            "serial": old.get("serial", ""),
        }
        # SMART 告警计数（ATA 属性 5/196/197/198/199）：休眠盘沿用上一次活跃时的值，
        # 未知（NVMe / 读取失败）为 None
        for _k in SMART_COUNTER_KEYS:
            entry[_k] = old.get(_k)
        # SMART 健康 / 转速 / 通电时长 / 各类计数：优先取巡检缓存（后台线程每 6 小时
        # 全盘扫一次），这样 60 秒采集循环对任何盘数都不再跑 smartctl——原来「只读前
        # 8 块盘」的上限就是为了压制这个开销，缓存到位后上限没有存在意义，直接解掉。
        # 巡检还没覆盖到的盘（首装的前 60 秒）沿用旧行为：最多补读 8 块，避免首屏空白。
        if state != "standby":
            sd = _DISK_HEALTH_MEM.get(name)
            if sd is None:
                sd = smart_detail(name) if len(result) < 8 else {}
            if sd.get("standby"):
                entry["state"] = "standby"
                entry["standby"] = True
            else:
                entry["health"] = sd.get("health", entry["health"])
                entry["rpm"] = sd.get("rpm", entry["rpm"])
                entry["power_on_hours"] = sd.get("power_on_hours", entry["power_on_hours"])
                entry["serial"] = sd.get("serial", entry["serial"])
                for _k in SMART_COUNTER_KEYS:
                    if sd.get(_k) is not None:
                        entry[_k] = sd[_k]
                if sd.get("temp_c") is not None and entry["temp"] is None:
                    entry["temp"] = sd["temp_c"]
        if state == "standby" and not entry["health"]:
            entry["health"] = "休眠"
        result.append(entry)
    return result


def _read_proc_tcp_listeners():
    """兜底：从 /proc/net/tcp + tcp6 解析监听端口（无进程名）。"""
    result = []
    for path, ipver in (("/proc/net/tcp", "0.0.0.0"), ("/proc/net/tcp6", "::")):
        for line in read_text(path).splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4:
                continue
            if parts[3] != "0A":  # LISTEN
                continue
            local = parts[1]
            if ":" not in local:
                continue
            hex_ip, hex_port = local.rsplit(":", 1)
            try:
                port = int(hex_port, 16)
            except Exception:
                continue
            result.append({"addr": ipver, "port": port, "proto": "tcp", "process": "", "pid": ""})
    return result


_SS_LISTEN_CACHE = {"ts": 0.0, "rows": None}
_SS_LISTEN_TTL = 15.0


def _ss_listeners():
    """ss -tlnp 解析全部 TCP 监听端口（15 秒缓存，多模块共享，避免重复 spawn）。

    返回 [{addr, port, process, pid}]；ss 不可用时回退 /proc/net/tcp（无进程信息）。"""
    now = time.time()
    cached = _SS_LISTEN_CACHE["rows"]
    if cached is not None and now - _SS_LISTEN_CACHE["ts"] <= _SS_LISTEN_TTL:
        return [dict(x) for x in cached]
    rows = []
    out, rc = run_cmd(["ss", "-tlnpH"], timeout=10)
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split()
            if not parts or parts[0] != "LISTEN" or len(parts) < 5:
                continue
            addr = parts[3]
            proc = parts[5] if len(parts) >= 6 else ""
            if ":" not in addr:
                continue
            host, port_s = addr.rsplit(":", 1)
            try:
                port = int(port_s)
            except Exception:
                continue
            pname = ""
            pid = ""
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', proc)
            if m:
                pname, pid = m.group(1), m.group(2)
            rows.append({"addr": host, "port": port, "process": pname, "pid": pid})
        # 去重排序
        seen = set()
        dedup = []
        for l in sorted(rows, key=lambda x: (x["port"], x["addr"])):
            k = (l["addr"], l["port"], l["process"], l["pid"])
            if k in seen:
                continue
            seen.add(k)
            dedup.append(l)
        rows = dedup
    else:
        rows = _read_proc_tcp_listeners()
    _SS_LISTEN_CACHE["rows"] = rows
    _SS_LISTEN_CACHE["ts"] = now
    return [dict(x) for x in rows]


def collect_ports(docker_result=None):
    """端口占用：飞牛应用中心应用端口 + Docker 容器端口映射 + 系统监听端口。"""
    result = {"apps": [], "docker": [], "listeners": [], "ts": time.time()}
    # 1) 飞牛应用中心已安装应用：manifest 配置端口为默认值，实际运行端口以
    #    ss 进程监听交叉校验为准（部分应用安装后被用户改过端口，如默认 8000 实际 8899）
    for appid, info in _app_center_ports().items():
        port = str(info.get("port") or "").strip()
        if port:
            item = {"name": info.get("name") or appid,
                    "appid": appid, "port": port,
                    "running": bool(info.get("runtime_ports"))}
            if info.get("default_port") and info.get("default_port") != port:
                item["default_port"] = info["default_port"]
                item["port_source"] = "runtime"
            result["apps"].append(item)
    # 2) Docker 容器端口映射（host / bridge 自动探测结果已含）
    if docker_result and docker_result.get("available"):
        for c in docker_result.get("containers", []):
            ports = c.get("ports") or []
            if isinstance(ports, str):  # 逗号分隔字符串 → 数组
                ports = [p.strip() for p in ports.split(",") if p.strip()]
            if ports:
                result["docker"].append({
                    "name": c.get("name"), "image": c.get("image"),
                    "state": c.get("state"), "ports": ports,
                })
    # 3) 系统监听端口
    result["listeners"] = _ss_listeners()
    return result


def collect_hardware():
    """硬件信息：CPU / 主板 / BIOS / 显卡 / 网卡（参照飞牛官方资源管理器信息项）。"""
    info = {"cpu": {}, "board": {}, "gpu": [], "net": []}
    # CPU（物理核 / 逻辑线程 / 插槽数走 lscpu 拓扑，/proc/cpuinfo 回退）
    info["cpu"]["model"] = read_cpu_model()
    try:
        cores, threads, sockets = read_cpu_topology()
    except Exception:
        cores, threads, sockets = 0, 0, 0
    info["cpu"]["cores"] = cores
    info["cpu"]["threads"] = threads if threads else cores
    info["cpu"]["sockets"] = sockets
    out, _ = run_cmd(["lscpu"], timeout=10)
    arch = ""
    for line in out.splitlines():
        if line.startswith("Architecture"):
            arch = line.split(":", 1)[1].strip()
    info["cpu"]["arch"] = arch
    # 主板 / BIOS
    for key in ("sys_vendor", "board_vendor", "board_name", "board_version",
                "bios_vendor", "bios_version", "bios_date", "product_name"):
        info["board"][key] = read_text("/sys/class/dmi/id/" + key).strip()
    # 显卡
    out, rc = run_cmd(["lspci"], timeout=10)
    if rc == 0:
        for line in out.splitlines():
            low = line.lower()
            if "vga" in low or "3d controller" in low or "display controller" in low:
                name = line.split(" ", 1)[1] if " " in line else line
                if name not in info["gpu"]:
                    info["gpu"].append(name)
    # 网卡 IP / 速率（同时采集 IPv4 与 IPv6，过滤 Docker 网桥 / 链路本地）
    if is_linux():
        out, _ = run_cmd(["ip", "-o", "addr", "show"], timeout=8)
        ip_by_if = {}
        ip6_by_if = {}
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 4 and parts[0].endswith(":"):
                iface = parts[1].rstrip(":")
                fam = parts[2]
                addr = parts[3].split("/")[0]
                if not iface or iface == "lo":
                    continue
                if iface.startswith(("docker", "veth", "virbr", "br-", "vnet", "tun", "tap", "vxlan", "wg")):
                    continue
                if fam == "inet":
                    if _ip_show(addr):
                        ip_by_if.setdefault(iface, []).append(addr)
                elif fam == "inet6":
                    a6 = addr.split("%")[0].lower()
                    if a6.startswith("fe80") or a6 in ("::1", "::"):
                        continue
                    ip6_by_if.setdefault(iface, []).append(a6)
        speed_by_if = {}
        netdir = "/sys/class/net"
        if os.path.isdir(netdir):
            for iface in os.listdir(netdir):
                sp = read_int_file(os.path.join(netdir, iface, "speed"))
                speed_by_if[iface] = sp
        for iface in sorted(set(list(ip_by_if.keys()) + list(ip6_by_if.keys()))):
            info["net"].append({
                "name": iface,
                "ip": (ip_by_if.get(iface) or ["--"])[0],
                "ipv4": ip_by_if.get(iface, []),
                "ipv6": ip6_by_if.get(iface, []),
                "speed_mbps": speed_by_if.get(iface, 0),
            })
    return info


# ===================== 阵列卡（MegaRAID / storcli 结构化采集） =====================
# LSI/Broadcom MegaRAID（及 Dell PERC 等贴牌）RAID 模式下的物理盘不作为
# /dev/sdX 暴露给系统，hwmon 与 smartctl 均不可见，只能经厂商 CLI 读取。
# storcli 与 perccli（Dell 贴牌）命令语法与 JSON 输出完全一致，一并支持。
# 温度/SMART 由阵列卡读取盘上传感器数据，不需要盘片寻道，因此不会唤醒
# STANDBY 中的硬盘；每 300 秒采集一次，开销可忽略。
_STORCLI_CANDIDATES = (
    "storcli64", "storcli", "perccli64", "perccli",
    "/opt/MegaRAID/storcli/storcli64", "/opt/MegaRAID/storcli/storcli",
    "/opt/MegaRAID/perccli/perccli64", "/opt/lsi/storcli/storcli64",
    "/usr/local/bin/storcli64", "/usr/local/bin/storcli",
    "/usr/bin/storcli64", "/usr/bin/perccli64",
)


def _find_storcli():
    """按候选顺序查找 storcli/perccli 可执行文件，找不到返回空串。"""
    for name in _STORCLI_CANDIDATES:
        if "/" in name:
            if os.path.isfile(name) and os.access(name, os.X_OK):
                return name
        else:
            try:
                p = shutil.which(name)
            except Exception:
                p = None
            if p:
                return p
    return ""


def _storcli_ci(d, *names):
    """storcli JSON 键容错查找：大小写/空格/点号不敏感（各版本键名写法有差异）。"""
    if not isinstance(d, dict):
        return None
    norm = {}
    for k, v in d.items():
        norm[str(k).lower().replace(".", "").replace(" ", "")] = v
    for n in names:
        v = norm.get(n.lower().replace(".", "").replace(" ", ""))
        if v is not None:
            return v
    return None


def _storcli_num(v):
    """解析 storcli 数值字段：27 / "27" / "27C (80.60 F)" → int，失败返回 None。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        m = re.search(r"-?\d+", v)
        if m:
            return int(m.group())
    return None


def _storcli_txt(v):
    """值转去除首尾空白的字符串（None → 空串）。"""
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return str(v)
    return str(v).strip()


_STORCLI_PD_STATE = {
    "onln": ("ok", "在线"),
    "jbod": ("ok", "JBOD 直通"),
    "ugood": ("ok", "未配置(好)"),
    "rbld": ("warn", "重建中"),
    "copybk": ("warn", "拷贝中"),
    "cvd": ("warn", "创建中"),
    "cs": ("warn", "一致性检查"),
    "offln": ("fail", "离线"),
    "msng": ("fail", "丢失"),
    "ubad": ("fail", "未配置(坏)"),
    "bad": ("fail", "故障"),
    "fld": ("fail", "故障"),
}

_STORCLI_VD_STATE = {
    "optl": ("on", "正常"),
    "optimal": ("on", "正常"),
    "onln": ("on", "正常"),
    "dgrd": ("off", "降级"),
    "degraded": ("off", "降级"),
    "ofln": ("off", "离线"),
    "offln": ("off", "离线"),
    "rbld": ("", "重建中"),
}


def _storcli_pd_health(pd):
    """物理盘状态 → (health, label)。

    优先级：SMART 告警 / 离线丢失类 > 介质或预测失败计数 > 过渡态 > 正常。"""
    state = _storcli_txt(_storcli_ci(pd, "state")).lower()
    smart_alert = _storcli_txt(_storcli_ci(
        pd, "smart alert flagged by drive",
        "drive has flagged a smart alert", "smart alert")).lower()
    media_err = _storcli_num(_storcli_ci(pd, "media error count")) or 0
    other_err = _storcli_num(_storcli_ci(pd, "other error count")) or 0
    pf = _storcli_num(_storcli_ci(pd, "predictive failure count")) or 0
    if smart_alert in ("yes", "true"):
        return "fail", "SMART 告警"
    if state in ("offln", "msng", "ubad", "bad", "fld", "failed"):
        return _STORCLI_PD_STATE[state]
    if media_err > 0 or other_err > 0 or pf > 0:
        return "warn", "介质错误"
    if state in _STORCLI_PD_STATE:
        return _STORCLI_PD_STATE[state]
    if state:
        return "warn", state
    return "ok", "正常"


def _storcli_controllers(out):
    """解析 storcli /call show all J → {控制器ID: 信息字典}。"""
    res = {}
    try:
        data = json.loads(out)
    except Exception:
        return res
    for c in (data.get("Controllers") or []):
        if not isinstance(c, dict):
            continue
        cs = c.get("Command Status") or {}
        st = _storcli_txt(_storcli_ci(cs, "status")).lower()
        if st and st != "success":
            continue
        cid = _storcli_num(_storcli_ci(cs, "controller"))
        rows = c.get("Response Data") or {}
        if not isinstance(rows, dict):
            continue
        res[cid if cid is not None else 0] = {
            "model": _storcli_txt(_storcli_ci(rows, "model", "product name")),
            "serial": _storcli_txt(_storcli_ci(rows, "serial number")),
            "roc_temp": _storcli_num(_storcli_ci(
                rows, "roc temperature", "controller temperature")),
            "memory": _storcli_txt(_storcli_ci(
                rows, "memory size", "cache memory size", "memory")),
            "status": _storcli_txt(_storcli_ci(rows, "controller status", "status")),
        }
    return res


def _storcli_vds(out):
    """解析 storcli /call/vall show J → {控制器ID: [虚拟阵列, ...]}。"""
    res = {}
    try:
        data = json.loads(out)
    except Exception:
        return res
    for c in (data.get("Controllers") or []):
        if not isinstance(c, dict):
            continue
        cs = c.get("Command Status") or {}
        st = _storcli_txt(_storcli_ci(cs, "status")).lower()
        if st and st != "success":
            continue
        cid = _storcli_num(_storcli_ci(cs, "controller"))
        rows = c.get("Response Data") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        vds = []
        for vd in rows:
            if not isinstance(vd, dict):
                continue
            num = _storcli_ci(vd, "vd")
            dgv = _storcli_txt(_storcli_ci(vd, "dg/vd"))
            if num is None and dgv:
                num = dgv.split("/")[-1]   # "0/2" → VD 2
            if num is None:
                continue
            raw_state = _storcli_txt(_storcli_ci(vd, "state"))
            badge = _STORCLI_VD_STATE.get(raw_state.lower(), ("", raw_state or "--"))
            vds.append({
                "vd": _storcli_txt(num),
                "name": _storcli_txt(_storcli_ci(vd, "name")),
                "type": _storcli_txt(_storcli_ci(vd, "type")),
                "state": raw_state,
                "state_label": badge[1],
                "badge": badge[0],
                "size": _storcli_txt(_storcli_ci(vd, "size")),
            })
        res[cid if cid is not None else 0] = vds
    return res


def _storcli_pds(out):
    """解析 storcli /call/eall/sall show all J → {控制器ID: [物理盘, ...]}。"""
    res = {}
    try:
        data = json.loads(out)
    except Exception:
        return res
    for c in (data.get("Controllers") or []):
        if not isinstance(c, dict):
            continue
        cs = c.get("Command Status") or {}
        st = _storcli_txt(_storcli_ci(cs, "status")).lower()
        if st and st != "success":
            continue
        cid = _storcli_num(_storcli_ci(cs, "controller"))
        rows = c.get("Response Data") or []
        if isinstance(rows, dict):
            rows = list(rows.values())
        pds = []
        for pd in rows:
            if not isinstance(pd, dict):
                continue
            if _storcli_ci(pd, "slt") is None and _storcli_ci(pd, "eid") is None:
                continue
            health, label = _storcli_pd_health(pd)
            eid = _storcli_txt(_storcli_ci(pd, "eid")) or "?"
            slt = _storcli_txt(_storcli_ci(pd, "slt")) or "?"
            pds.append({
                "loc": eid + ":" + slt,
                "did": _storcli_num(_storcli_ci(pd, "did")),
                "model": _storcli_txt(_storcli_ci(pd, "model", "model number")),
                "serial": _storcli_txt(_storcli_ci(pd, "serial number", "serial")),
                "intf": _storcli_txt(_storcli_ci(pd, "intf", "protocol")),
                "media": _storcli_txt(_storcli_ci(pd, "med", "media type", "drive type")),
                "size": _storcli_txt(_storcli_ci(pd, "size", "capacity")),
                "state": _storcli_txt(_storcli_ci(pd, "state")),
                "temp": _storcli_num(_storcli_ci(pd, "drive temperature", "temperature")),
                "health": health,
                "health_label": label,
                "smart_alert": _storcli_txt(_storcli_ci(
                    pd, "smart alert flagged by drive",
                    "drive has flagged a smart alert", "smart alert")).lower() in ("yes", "true"),
                "media_errors": _storcli_num(_storcli_ci(pd, "media error count")) or 0,
                "other_errors": _storcli_num(_storcli_ci(pd, "other error count")) or 0,
                "pf_count": _storcli_num(_storcli_ci(pd, "predictive failure count")) or 0,
            })
        res[cid if cid is not None else 0] = pds
    return res


def collect_raid_card():
    """阵列卡检测与硬 RAID 物理盘温度/健康采集（LSI/Broadcom storcli 通用）。

    lspci 按优先级识别控制器（RAID > HBA/SAS > 存储控制器 > SATA 控制器，
    纯 SATA 主板不再误报为阵列卡）；MegaRAID（含 Dell PERC 贴牌）经 storcli
    的 JSON 输出解析：控制器型号/ROC 芯片温度、虚拟阵列状态、每块物理盘的
    盘位/型号/温度/健康/介质错误。温度与 SMART 由阵列卡读传感器，不寻道、
    不唤醒 STANDBY 硬盘。HBA 直通卡（IT 模式）物理盘由内核直接接管，
    走标准 hwmon/smartctl 路径，无需 storcli。"""
    info = {"type": "none", "label": "", "detail": "",
            "tool": "", "controllers": [], "error": ""}
    if not is_linux():
        return info
    out, rc = run_cmd(["lspci"], timeout=10)
    if rc != 0:
        return info
    pri_line = ""
    pri = 99
    for line in out.splitlines():
        low = line.lower()
        lvl = None
        if "megaraid" in low or ("raid" in low and "sata" not in low):
            lvl = 0
        elif "hba" in low or "sas" in low:
            lvl = 1
        elif "storage controller" in low:
            lvl = 2
        elif "sata controller" in low:
            lvl = 3
        if lvl is not None and lvl < pri:
            pri, pri_line = lvl, line
        if pri == 0:
            break
    if not pri_line:
        return info
    label = pri_line.split(" ", 1)[1] if " " in pri_line else pri_line
    info["label"] = label
    if pri == 0:
        info["type"] = "megaraid"
        tool = _find_storcli()
        info["tool"] = tool
        if not tool:
            info["detail"] = ("MegaRAID 阵列卡：未找到 storcli/perccli 工具，无法读取物理盘温度与健康。"
                              "从 Broadcom 官网下载 storcli 解压后，把 storcli64 放入 /usr/local/bin/"
                              " 或 /opt/MegaRAID/storcli/，稍候面板自动识别")
            info["error"] = "storcli not found"
            return info
        cout, crc = run_cmd([tool, "/call", "show", "all", "J"], timeout=30)
        vout, _ = run_cmd([tool, "/call", "vall", "show", "J"], timeout=30)
        pout, prc = run_cmd([tool, "/call", "eall", "sall", "show", "all", "J"], timeout=30)
        ctrls = _storcli_controllers(cout)
        vds = _storcli_vds(vout)
        pds = _storcli_pds(pout)
        if not ctrls and not pds and not vds:
            info["detail"] = ("MegaRAID 阵列卡：storcli 输出解析失败"
                              "（需 root 权限，或 storcli 版本过旧不支持 J JSON 输出）")
            info["error"] = "storcli parse failed"
            return info
        ids = sorted(set(ctrls) | set(vds) | set(pds))
        for cid in ids:
            ci = ctrls.get(cid) or {}
            info["controllers"].append({
                "id": cid,
                "model": ci.get("model") or label,
                "serial": ci.get("serial", ""),
                "roc_temp": ci.get("roc_temp"),
                "memory": ci.get("memory", ""),
                "status": ci.get("status", ""),
                "vd": vds.get(cid) or [],
                "pds": pds.get(cid) or [],
            })
        n = sum(len(c["pds"]) for c in info["controllers"])
        info["detail"] = "MegaRAID 阵列卡（RAID 模式），经 %s 读取 %d 块物理盘温度与健康" % (
            os.path.basename(tool), n)
        return info
    elif pri == 1:
        info["type"] = "hba"
        info["detail"] = "HBA 直通卡（IT 模式），物理盘由系统直接识别为 /dev/sdX，SMART 见硬盘面板"
    else:
        # 纯 SATA 主板（AHCI）：不算阵列卡，恢复为不显示
        info["type"] = "none"
        info["label"] = ""
        info["detail"] = ""
    return info


# ===================== 温度墙（网络主流硬件监控面板功能） =====================
# 传感器英文测点 → 中文翻译表（Nuvoton NCT67xx / PCH / 内存 / 通用）
_TEMP_NAME_ZH = {
    "SYSTIN": "主板温度",
    "CPUTIN": "主板·CPU 区域",
    "AUXTIN0": "扩展温度探头 0",
    "AUXTIN1": "扩展温度探头 1",
    "AUXTIN2": "扩展温度探头 2",
    "AUXTIN3": "扩展温度探头 3",
    "AUXTIN4": "扩展温度探头 4",
    "AUXTIN5": "扩展温度探头 5",
    "PECI Agent 0": "CPU PECI 代理 0",
    "PECI Agent 1": "CPU PECI 代理 1",
    "PCH_CHIP_TEMP": "PCH 芯片组温度",
    "PCH_CHIP_CPU_MAX_TEMP": "PCH 芯片组最高温度",
    "PCH_CPU_TEMP": "PCH CPU 温度",
    "PCH_MCH_TEMP": "PCH 内存控制器温度",
    "Agent0 Dimm0": "内存 DIMM0 温度",
    "Agent0 Dimm1": "内存 DIMM1 温度",
    "Agent1 Dimm0": "内存 DIMM0 温度（通道 1）",
    "Agent1 Dimm1": "内存 DIMM1 温度（通道 1）",
    "Composite": "复合温度",
    "THRM": "热敏电阻",
    "NB": "北桥温度",
    "Sensor 0": "传感器 0",
    "Sensor 1": "传感器 1",
    "Sensor 2": "传感器 2",
    "SMBUSMASTER 0": "SMBus 主控 0",
    "SMBUSMASTER 1": "SMBus 主控 1",
    "TSI0_TEMP": "TSI 温度 0",
    "TSI1_TEMP": "TSI 温度 1",
    "Tctl": "CPU 温度控制",
    "Tdie": "CPU 晶粒温度",
}


def _temp_name_zh(raw_name, chip_prefix=None):
    """把传感器原始英文名翻译成中文；Core N / Package id N / TccdN 单独处理。"""
    if raw_name in _TEMP_NAME_ZH:
        return _TEMP_NAME_ZH[raw_name]
    m = re.match(r"Core\s+(\d+)", raw_name)
    if m:
        return "CPU 核心 %s" % m.group(1)
    m = re.match(r"Tccd(\d+)", raw_name)
    if m:
        return "CPU CCD%s 温度" % m.group(1)
    m = re.match(r"Package id\s+(\d+)", raw_name)
    if m:
        return "CPU 封装温度" if m.group(1) == "0" else "CPU 封装温度 %s" % m.group(1)
    return raw_name


# ===================== RAPL 实时功耗（网络主流硬件监控面板功能） =====================
# Intel RAPL 通过 MSR 提供 CPU 封装/核心/非核心/内存真实功耗，Linux 以 powercap
# 子系统暴露在 /sys/class/powercap/intel-rapl:*/ 下。energy_uj 单调递增，
# 两次采样差值 / 时间间隔 = 平均功耗（瓦）。支持 Intel 全系 + AMD zen2+。
_RAPL_BASE = "/sys/class/powercap"


def _rapl_read_energy(domain_path):
    """读某个域的 energy_uj（微焦耳）。文件缺失返回 None。"""
    try:
        with open(domain_path + "/energy_uj", "r") as f:
            return int(f.read().strip())
    except Exception:
        return None


def _rapl_energy_max(domain_path):
    """该域能量计数器的回绕上限（微焦耳）。读不到给个安全默认。"""
    try:
        with open(domain_path + "/max_energy_range_uj", "r") as f:
            return int(f.read().strip())
    except Exception:
        return 262143300000  # 默认 2^18 uj * 1e6，Intel 常见值


def get_rapl_power():
    """读取 CPU 封装/核心/非核心/内存实时功耗（瓦）。

    返回 {"package": w, "core": w, "uncore": w, "dram": w, "ok": bool, "total": w}；
    RAPL 不可用（非 Intel / 内核未挂载）时返回 {"ok": False}。
    函数内两次读数差分（间隔 0.25s），不依赖跨请求状态，可安全放进缓存。
    """
    base = _RAPL_BASE
    if not os.path.isdir(os.path.join(base, "intel-rapl:0")):
        return {"ok": False}
    paths = {
        "package": os.path.join(base, "intel-rapl:0"),
        "core": os.path.join(base, "intel-rapl:0:0"),
        "uncore": os.path.join(base, "intel-rapl:0:1"),
        "dram": os.path.join(base, "intel-rapl:0:2"),
    }

    def sample():
        out = {}
        for k, p in paths.items():
            e = _rapl_read_energy(p)
            if e is not None:
                out[k] = e
        return out

    e1 = sample()
    if not e1:
        return {"ok": False}
    time.sleep(0.25)
    e2 = sample()
    watts = {}
    for k in ("package", "core", "uncore", "dram"):
        if k in e1 and k in e2:
            diff = e2[k] - e1[k]
            if diff < 0:  # 计数器回绕
                diff += _rapl_energy_max(paths[k])
            watts[k] = round(diff / 0.25 / 1e6, 2)
    if not watts or "package" not in watts:
        return {"ok": False}
    # 注意：package（封装）域本身已包含 core（核心/PP0）与 uncore（非核心/PP1），
    # 四者直接相加会把核心/非核心重复计入，总功耗虚高一倍以上。
    # 整机实测功耗 = CPU 封装 + 内存（DRAM 是封装外的独立域）；core/uncore 仅作分项展示。
    total = watts["package"] + watts.get("dram", 0.0)
    watts["ok"] = True
    watts["total"] = round(total, 2)
    return watts


# ===================== 功耗估算模型（RAPL 不可用时回退） =====================
def estimate_power(cpu_percent, disks_active, nics, tdp_w, disk_w, nic_w):
    """无硬件功耗传感器时，基于 CPU 负载 + 硬盘数 + 网卡数估算整机功耗（瓦）。
    公式：base = TDP * (cpu%/100 * 0.7 + 0.3) + disks_active * disk_w + nics * nic_w"""
    try:
        cpu_p = float(cpu_percent) if cpu_percent is not None else 0
        base = tdp_w * (cpu_p / 100.0 * 0.7 + 0.3)
        return round(base + disks_active * disk_w + nics * nic_w, 2)
    except Exception:
        return round(tdp_w * 0.3, 2)


# ===================== 硬盘休眠状态检测（不唤醒 STANDBY 硬盘） =====================
# 电源状态全局短缓存：同一次采集周期内多处复用，避免对每块盘反复 spawn smartctl
_DISK_STATE_LOCK = threading.Lock()
_DISK_STATE_CACHE = {"ts": 0.0, "states": {}}
_DISK_STATE_TTL = 8.0
_DISK_PROBE_UNAVAILABLE_TS = 0.0   # smartctl / hdparm 均不可用时长时间退避，不空跑进程
# 挂载点 -> 物理盘名集合（lsblk 解析，含 LVM/dm 多层），60 秒缓存
_MOUNT_DISK_LOCK = threading.Lock()
_MOUNT_DISK_CACHE = {"ts": 0.0, "mounts": {}}
# 挂载点 -> 上次活跃时的 statvfs 结果（盘休眠期间继续展示最后一次容量，不唤醒硬盘）
_DISK_USAGE_CACHE = {}
# 盘名 -> 上次活跃时读到的温度（休眠期间展示缓存温度）
_DISK_TEMP_CACHE = {}

# 整盘设备名（排除分区 sda1 / nvme0n1p1 与 loop/dm/md 等虚拟设备）
_WHOLE_DISK_RE = re.compile(r"^(sd[a-z]+|nvme\d+n\d+|vd[a-z]+|mmcblk\d+|xvd[a-z]+)$")


def _whole_disks():
    names = []
    try:
        for name in os.listdir("/sys/block"):
            if _WHOLE_DISK_RE.match(name):
                names.append(name)
    except Exception:
        pass
    return names


def _probe_disk_power_state(name):
    """非唤醒式探测单盘电源状态。

    smartctl -n standby 优先：-n standby 使 smartctl 在盘处于 STANDBY 时直接
    退出（rc=2）而不下发任何会唤醒硬盘的 SMART 命令；hdparm -C（CHECK POWER
    MODE，只查询状态不改变电源状态）作为兜底。
    返回 "standby" | "active/idle" | "unknown"。
    """
    global _DISK_PROBE_UNAVAILABLE_TS
    dev = "/dev/" + name
    if shutil.which("smartctl"):
        out, rc = run_cmd(["smartctl", "-n", "standby", "-i", dev], timeout=4)
        up = (out or "").upper()
        if "STANDBY" in up or "SLEEP" in up:
            return "standby"
        if rc == 0 and ("ACTIVE" in up or "IDLE" in up or "MODEL" in up or "SERIAL" in up):
            return "active/idle"
    if shutil.which("hdparm"):
        out, rc = run_cmd(["hdparm", "-C", dev], timeout=3)
        low = (out or "").lower()
        if rc == 0 and ("standby" in low or "sleeping" in low):
            return "standby"
        if rc == 0 and ("active" in low or "idle" in low):
            return "active/idle"
    return "unknown"


_DISK_PROBE_LOCK = threading.Lock()   # 串行化探测，避免多个请求线程同时对每块盘跑 smartctl（惊群）


def _disk_state_ttl():
    """缓存有效期与采集间隔对齐：采集线程每个 tick 强制刷新一次（force=True），
    请求线程在两个 tick 之间只读缓存，每块盘每个采集周期只探测一次。"""
    try:
        iv = float(RUNTIME.get("interval") or 10)
    except Exception:
        iv = 10.0
    return max(_DISK_STATE_TTL, iv * 2 + 2)


def disk_power_states(force=False):
    """返回 {盘名: standby / active/idle / unknown}，缓存见 _disk_state_ttl()。

    检测只使用 CHECK POWER MODE 类非唤醒命令；smartctl 与 hdparm 都不存在时
    5 分钟退避（此时返回 unknown，调用方按“非休眠”处理，保持旧行为）。"""
    global _DISK_PROBE_UNAVAILABLE_TS
    now = time.time()
    ttl = _disk_state_ttl()
    with _DISK_STATE_LOCK:
        if not force and _DISK_STATE_CACHE["ts"] and now - _DISK_STATE_CACHE["ts"] <= ttl:
            return dict(_DISK_STATE_CACHE["states"])
    with _DISK_PROBE_LOCK:
        # 等锁期间可能已有其他线程刚完成探测：直接复用（force 时只复用 1 秒内的结果）
        with _DISK_STATE_LOCK:
            age = time.time() - _DISK_STATE_CACHE["ts"] if _DISK_STATE_CACHE["ts"] else 1e9
            if age <= (1.0 if force else ttl):
                return dict(_DISK_STATE_CACHE["states"])
        return _probe_all_disk_states(now)


def _probe_all_disk_states(now):
    global _DISK_PROBE_UNAVAILABLE_TS
    states = {}
    if is_linux() and now - _DISK_PROBE_UNAVAILABLE_TS > 300:
        for name in _whole_disks():
            states[name] = _probe_disk_power_state(name)
        if states and all(v == "unknown" for v in states.values()) \
                and not shutil.which("smartctl") and not shutil.which("hdparm"):
            _DISK_PROBE_UNAVAILABLE_TS = now
    with _DISK_STATE_LOCK:
        _DISK_STATE_CACHE["states"] = states
        _DISK_STATE_CACHE["ts"] = now
    return dict(states)


def _mount_disk_map():
    """返回 {挂载点: set(物理盘名)}。

    直挂分区（/dev/sdb1、/dev/nvme0n1p1）由设备名直接归并；LVM / device-mapper
    等多层结构由 lsblk 设备树递归把挂载点归到顶层物理盘（lsblk 只读 sysfs，
    不会唤醒休眠硬盘）。60 秒缓存。"""
    now = time.time()
    with _MOUNT_DISK_LOCK:
        if _MOUNT_DISK_CACHE["ts"] and now - _MOUNT_DISK_CACHE["ts"] <= 60:
            return {k: set(v) for k, v in _MOUNT_DISK_CACHE["mounts"].items()}
    result = {}

    def _add(mp, disk):
        if mp and disk:
            result.setdefault(mp, set()).add(disk)

    # 1) /proc/mounts 直挂分区
    for m in read_mounts():
        dev = os.path.basename(m["device"] or "")
        mm = re.match(r"^(sd[a-z]+)\d*$", dev) \
            or re.match(r"^(nvme\d+n\d+)(p\d+)?$", dev) \
            or re.match(r"^(vd[a-z]+|xvd[a-z]+)\d*$", dev)
        if mm:
            _add(m["mount"], mm.group(1))
    # 2) lsblk 设备树（覆盖 LVM / dm-* / 多层分区挂载）
    try:
        out, rc = run_cmd(
            ["lsblk", "-b", "-J", "-o", "NAME,MOUNTPOINT,TYPE"], timeout=10)
        if rc == 0 and out.strip():
            data = json.loads(out)

            def _collect(blk, root, acc):
                if blk.get("mountpoint"):
                    acc.append(blk["mountpoint"])
                for ch in blk.get("children", []) or []:
                    _collect(ch, root, acc)

            for blk in data.get("blockdevices", []) or []:
                root = blk.get("name") or ""
                if not _WHOLE_DISK_RE.match(root):
                    continue
                mps = []
                _collect(blk, root, mps)
                for mp in mps:
                    _add(mp, root)
    except Exception:
        pass
    with _MOUNT_DISK_LOCK:
        _MOUNT_DISK_CACHE["mounts"] = {k: list(v) for k, v in result.items()}
        _MOUNT_DISK_CACHE["ts"] = now
    return result


def mount_on_standby(mountpoint, states=None):
    """该挂载点是否位于已休眠的物理硬盘上。保护开关关闭 / 状态未知时返回 False。"""
    if not RUNTIME.get("standby_protect", True) or not is_linux():
        return False
    states = states if states is not None else disk_power_states()
    disks = _mount_disk_map().get(mountpoint)
    return bool(disks) and any(states.get(d) == "standby" for d in disks)


def path_on_standby(path, states=None):
    """某文件路径（如数据目录）是否落在休眠硬盘上：取最长挂载点前缀匹配。"""
    if not RUNTIME.get("standby_protect", True) or not is_linux() or not path:
        return False
    states = states if states is not None else disk_power_states()
    mp_map = _mount_disk_map()
    best = ""
    for mp in mp_map:
        if (path == mp or path.startswith(mp.rstrip("/") + "/")) and len(mp) > len(best):
            best = mp
    return bool(best) and any(states.get(d) == "standby" for d in mp_map[best])


def read_disk_standby_states():
    """读取各物理硬盘的休眠状态。返回 [{name, state}]。
    state: active/idle | standby | unknown（全程使用非唤醒命令）"""
    if not is_linux():
        return []
    states = disk_power_states()
    return [{"name": n, "state": states.get(n, "unknown")} for n in _whole_disks()]


# ===================== Docker NetIO 解析 =====================
_NETIO_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?\d+)?)\s*([kKmMgGtTpP]?)(i?)[bB]\s*$")


def _docker_size_bytes(v):
    """docker stats 的大小文本 → 字节。docker 用 go-units HumanSize（十进制 kB/MB/GB，
    基数 1000）；podman 等可能输出二进制 KiB/MiB（基数 1024）。无法解析返回 None。"""
    m = _NETIO_RE.match(v or "")
    if not m:
        return None
    base = 1024 if m.group(3) else 1000
    exp = " KMGTP".index(m.group(2).upper()) if m.group(2) else 0
    try:
        return int(float(m.group(1)) * base ** exp)
    except Exception:
        return None


def parse_docker_netio(s):
    """解析 docker stats 的 NetIO 字段（如 '145kB / 1.21MB'）→ (in_bytes, out_bytes) int。

    2.18.0 前按 1024 进制且只认大写 KB，docker 实际输出的小写 kB 会被解析成 0。"""
    if not s or "/" not in s:
        return (0, 0)
    parts = s.split("/")
    if len(parts) != 2:
        return (0, 0)
    a, b = _docker_size_bytes(parts[0]), _docker_size_bytes(parts[1])
    return (a or 0, b or 0)


def _proc_netns_bytes(pid, cid, proc_root="/proc"):
    """容器网络命名空间的精确累计字节（rx, tx），读 /proc/<pid>/net/dev（排除 lo）。

    docker stats 的 NetIO 只有 3 位有效数字（如 1.21GB，分辨率 10MB），相邻两次差分
    得到的速率会严重量化；这里直接读内核计数器。安全校验：
      * /proc/<pid>/cgroup 必须包含完整容器 ID（PID 命名空间不同或 PID 被复用时拒绝）；
      * 与本进程同一网络命名空间（host 网络模式）时返回 None，避免把宿主机总流量算给容器。
    任何一步失败返回 None，由调用方回退 docker stats。只读 procfs，不触碰块设备。"""
    try:
        pid = int(pid)
        if pid <= 0 or not cid or len(cid) < 12:
            return None
        base = os.path.join(proc_root, str(pid))
        with open(os.path.join(base, "cgroup"), "r", errors="ignore") as f:
            if cid not in f.read():
                return None
        try:
            if os.readlink(os.path.join(base, "ns", "net")) == os.readlink(os.path.join(proc_root, "self", "ns", "net")):
                return None
        except OSError:
            return None
        rx = tx = 0
        with open(os.path.join(base, "net", "dev"), "r", errors="ignore") as f:
            for line in f.readlines()[2:]:
                if ":" not in line:
                    continue
                name, data = line.split(":", 1)
                if name.strip() == "lo":
                    continue
                cols = data.split()
                if len(cols) >= 9:
                    rx += int(cols[0])
                    tx += int(cols[8])
        return (rx, tx)
    except Exception:
        return None


def container_net_rates(prev, containers, now):
    """容器实时网速：相邻两次采样的累计字节差分 / 时间差 → rx_rate / tx_rate（B/s）。

    prev：{容器ID: (ts, in, out, src)}，返回新的 prev。首次采样、计数器回退（容器重启）、
    计数来源切换（精确 ↔ docker stats）或非运行状态时速率为 None，绝不编造数值。"""
    nxt = {}
    for c in containers:
        c.setdefault("rx_rate", None)
        c.setdefault("tx_rate", None)
        cid = c.get("id")
        if c.get("state") != "running" or c.get("net_src") not in ("proc", "stats"):
            continue
        cur = (now, c.get("net_in_bytes") or 0, c.get("net_out_bytes") or 0, c.get("net_src"))
        nxt[cid] = cur
        p = (prev or {}).get(cid)
        if not p or p[3] != cur[3]:
            continue
        dt = cur[0] - p[0]
        di, do = cur[1] - p[1], cur[2] - p[2]
        if dt <= 0.5 or di < 0 or do < 0:
            continue
        c["rx_rate"] = round(di / dt, 1)
        c["tx_rate"] = round(do / dt, 1)
        c["rate_window"] = round(dt, 1)
    return nxt


# ===================== GPU 实时监控（网络主流硬件监控面板功能） =====================
_GPU_IDENT_CACHE = {"ts": 0.0, "list": None}


def _gpu_ident_list():
    """lspci 识别显卡设备（硬件列表运行期不变：缓存 1 小时，避免每 10 秒跑一次 lspci）。"""
    now = time.time()
    if _GPU_IDENT_CACHE["list"] is not None and now - _GPU_IDENT_CACHE["ts"] < 3600:
        return [dict(x) for x in _GPU_IDENT_CACHE["list"]]
    idents = _gpu_ident_list_uncached()
    _GPU_IDENT_CACHE.update(ts=now, list=[dict(x) for x in idents])
    return idents


def _gpu_ident_list_uncached():
    idents = []
    out, rc = run_cmd(["lspci", "-nn"], timeout=10)
    if rc != 0:
        return idents
    for line in out.splitlines():
        low = line.lower()
        if "vga" not in low and "3d controller" not in low and "display controller" not in low:
            continue
        vendor = ""
        m = re.search(r"\[([0-9a-f]{4}):([0-9a-f]{4})\]", line)
        if m:
            vendor = m.group(1).lower()
        rest = line.split(":", 1)[1] if ":" in line else line
        name = re.sub(r"\s*\[[0-9a-f]{4}:[0-9a-f]{4}\]", "", rest).strip()
        pci = line.split()[0] if line.split() else ""
        gtype = "igpu" if vendor == "8086" else "other"
        if vendor == "10de":
            gtype = "nvidia"
        elif vendor == "1002":
            gtype = "amd"
        idents.append({"vendor": vendor, "type": gtype, "name": name, "pci": pci,
                       "name_full": name, "name_arch": ""})
    return idents


def _gpu_temp_from_sysfs_live(pci):
    """AMD / 部分独显：从 /sys/class/drm 读 GPU 温度。"""
    try:
        base = (pci or "").strip()
        for name in os.listdir("/sys/class/drm"):
            if not re.match(r"^card\d+$", name):
                continue
            devdir = os.path.join("/sys/class/drm", name, "device")
            uevent = os.path.join(devdir, "uevent")
            if not os.path.exists(uevent):
                continue
            data = open(uevent).read()
            mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
            if not mm:
                continue
            dev = mm.group(1)
            if base and not (base == dev or dev.endswith(base) or base.endswith(dev)):
                continue
            hw = os.path.join(devdir, "hwmon")
            if os.path.isdir(hw):
                for h in os.listdir(hw):
                    hpath = os.path.join(hw, h)
                    for i in range(1, 5):
                        p = os.path.join(hpath, "temp%d_input" % i)
                        if os.path.exists(p):
                            t = int(open(p).read().strip()) / 1000.0
                            if -50 < t < 150:
                                return t
            return None
    except Exception:
        return None
    return None


def _amd_gpu_busy(pci):
    """AMD GPU 利用率：/sys/class/drm/cardN/device/gpu_busy_percent。"""
    try:
        base = (pci or "").strip()
        for name in os.listdir("/sys/class/drm"):
            if not re.match(r"^card\d+$", name):
                continue
            devdir = os.path.join("/sys/class/drm", name, "device")
            uevent = os.path.join(devdir, "uevent")
            if not os.path.exists(uevent):
                continue
            data = open(uevent).read()
            mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
            if not mm:
                continue
            dev = mm.group(1)
            if base and not (base == dev or dev.endswith(base) or base.endswith(dev)):
                continue
            p = os.path.join(devdir, "gpu_busy_percent")
            if os.path.exists(p):
                return float(open(p).read().strip())
            return None
    except Exception:
        return None
    return None


# ===================== Intel 核显占用（i915 PMU / RC6，原理同 intel_gpu_top，纯标准库） =====================
# intel_gpu_top 的做法：通过 perf_event_open 打开 i915 PMU 的各引擎 busy 计数器（单位 ns，
# 引擎忙碌时累加），两次读数之差 / 墙钟时间 = 引擎占用率。这里用 ctypes 直接发起同一个
# 系统调用（不依赖 intel-gpu-tools）。读不到 PMU（xe 驱动 / 无权限 / 非 x86）时回退到
# RC6 驻留时间：GPU 不在 RC6 省电态的时间占比 ≈ 活跃度（只是估算，不区分引擎）。
_PERF_SYSCALL_NR = {"x86_64": 298, "amd64": 298, "i386": 336, "i686": 336,
                    "aarch64": 241, "arm64": 241, "armv7l": 364, "armv6l": 364,
                    "riscv64": 241, "loongarch64": 241}
_I915_ENGINE_RE = re.compile(r"^(rcs|bcs|vcs|vecs|ccs)(\d+)-busy$")
_I915_ENGINE_KEY = {"rcs": "render", "bcs": "blitter", "vcs": "video", "vecs": "video_enhance", "ccs": "compute"}
_I915_FREQ_EVENTS = {"act": ("actual-frequency", "gt0-actual-frequency"),
                     "req": ("requested-frequency", "gt0-requested-frequency")}


def _parse_pmu_config(text):
    """PMU 事件描述（如 'config=0x1000002'）→ config 整数；i915 只用 config= 形式。"""
    m = re.search(r"(?:^|,)\s*config=(0x[0-9a-fA-F]+|\d+)\s*(?:,|$)", (text or "").strip())
    return int(m.group(1), 0) if m else None


def _first_cpu(mask):
    """cpumask 文本（'0' / '0-3' / '0,2'）→ 第一个 CPU 编号。"""
    m = re.match(r"\s*(\d+)", mask or "")
    return int(m.group(1)) if m else 0


def _pci_tag(pci):
    """lspci 的 '00:02.0' → '00_02.0'（PMU 目录名 i915_0000_00_02.0 的后缀）。"""
    return (pci or "").strip().replace(":", "_")


class _PerfOpener:
    """perf_event_open(attr, pid=-1, cpu, group_fd=-1, flags=0) 的 ctypes 封装（只读计数）。"""

    def __init__(self, machine=None):
        import platform
        self.machine = (machine or platform.machine() or "").lower()
        self.nr = _PERF_SYSCALL_NR.get(self.machine)
        self._libc = None

    def __call__(self, ptype, config, cpu):
        import ctypes
        import errno as _errno
        if not self.nr or not is_linux():
            raise OSError(_errno.ENOSYS, "perf_event_open 不支持该平台（%s）" % (self.machine or "?"))
        if self._libc is None:
            self._libc = ctypes.CDLL(None, use_errno=True)
            self._libc.syscall.restype = ctypes.c_long
        # struct perf_event_attr（PERF_ATTR_SIZE_VER0 = 64 字节）：type, size, config, sample_period,
        # sample_type, read_format(0 → read 返回单个 u64), flags(0 → 立即计数), wakeup, bp_type, config1
        import struct as _struct
        buf = ctypes.create_string_buffer(_struct.pack("=IIQQQQQIIQ", int(ptype), 64, int(config), 0, 0, 0, 0, 0, 0, 0), 64)
        fd = self._libc.syscall(ctypes.c_long(self.nr), buf, ctypes.c_int(-1), ctypes.c_int(int(cpu)),
                                ctypes.c_int(-1), ctypes.c_ulong(0))
        if fd < 0:
            e = ctypes.get_errno()
            raise OSError(e, os.strerror(e))
        return int(fd)


def _perf_read(fd):
    import struct as _struct
    data = os.read(fd, 8)
    if len(data) != 8:
        raise OSError(5, "short read")
    return _struct.unpack("=Q", data)[0]


class IntelGpuPmu:
    """i915 PMU 引擎占用采样器（线程安全，惰性初始化，失败后退避重试，进程退出时关闭 fd）。

    sysfs / opener / reader / closer / clock 均可注入，便于用夹具目录与假计数器测试。"""
    RETRY_S = 300.0       # 初始化失败后的重试间隔（不刷日志、不空转）
    REREAD_RETRY_S = 30.0  # 读数失败（如 GPU 复位）后多久重新初始化

    def __init__(self, sysfs="/sys", opener=None, reader=None, closer=None, clock=None, paranoid_path=None):
        self.sysfs = sysfs
        self.opener = opener or _PerfOpener()
        self.reader = reader or _perf_read
        self.closer = closer or os.close
        self.clock = clock or time.monotonic
        self.paranoid_path = paranoid_path or "/proc/sys/kernel/perf_event_paranoid"
        self._lock = threading.Lock()
        self._fds = {}            # 事件名 → fd
        self._events = {}         # 事件名 → (类别键, 实例号) 或 ('freq', 'act'/'req')
        self._prev = None
        self._last = None
        self._next_init = 0.0
        self._diag = {"initialized": False}

    def _pmu_dir(self, pci):
        base = os.path.join(self.sysfs, "bus", "event_source", "devices")
        try:
            names = sorted(n for n in os.listdir(base) if n == "i915" or n.startswith("i915_"))
        except OSError:
            names = []
        self._diag["pmu_candidates"] = names
        tag = _pci_tag(pci)
        for n in names:
            if tag and n.startswith("i915_") and n.endswith(tag):
                return os.path.join(base, n)
        if "i915" in names:
            return os.path.join(base, "i915")
        return os.path.join(base, names[0]) if names else None

    def _close(self):
        for fd in self._fds.values():
            try:
                self.closer(fd)
            except Exception:
                pass
        self._fds = {}
        self._prev = None

    def _init(self, pci):
        self._close()
        d = {"initialized": True, "pmu_dir": None, "events": {}, "open": {}, "error": None,
             "perf_event_paranoid": (read_text(self.paranoid_path) or "").strip() or None,
             "machine": getattr(self.opener, "machine", None), "syscall_nr": getattr(self.opener, "nr", None)}
        self._diag = d
        pdir = self._pmu_dir(pci)
        d["pmu_candidates"] = self._diag.get("pmu_candidates", [])
        if not pdir:
            d["error"] = "未找到 i915 PMU（/sys/bus/event_source/devices/i915*）"
            return
        d["pmu_dir"] = pdir
        try:
            ptype = int((read_text(os.path.join(pdir, "type")) or "").strip())
        except ValueError:
            d["error"] = "PMU type 不可读"
            return
        d["type"] = ptype
        mask = (read_text(os.path.join(pdir, "cpumask")) or "").strip()
        d["cpumask"] = mask or None
        cpu = _first_cpu(mask)
        evdir = os.path.join(pdir, "events")
        try:
            names = sorted(n for n in os.listdir(evdir) if "." not in n)
        except OSError:
            d["error"] = "PMU events 目录不可读"
            return
        wanted = {}
        for n in names:
            m = _I915_ENGINE_RE.match(n)
            if m:
                wanted[n] = (_I915_ENGINE_KEY[m.group(1)], int(m.group(2)))
        for k, cands in _I915_FREQ_EVENTS.items():
            for c in cands:
                if c in names:
                    wanted[c] = ("freq", k)
                    break
        d["events_all"] = len(names)
        for n, meaning in sorted(wanted.items()):
            cfg = _parse_pmu_config(read_text(os.path.join(evdir, n)))
            unit = (read_text(os.path.join(evdir, n + ".unit")) or "").strip() or None
            d["events"][n] = {"config": hex(cfg) if cfg is not None else None, "unit": unit}
            if cfg is None:
                d["open"][n] = {"ok": False, "error": "config 无法解析"}
                continue
            try:
                fd = self.opener(ptype, cfg, cpu)
                self._fds[n] = fd
                self._events[n] = meaning
                d["open"][n] = {"ok": True}
            except OSError as e:
                d["open"][n] = {"ok": False, "errno": e.errno, "strerror": e.strerror or str(e)}
            except Exception as e:   # ctypes 不可用等
                d["open"][n] = {"ok": False, "error": str(e)[:200]}
        if not any(m[0] != "freq" for n, m in self._events.items() if n in self._fds):
            d["error"] = d["error"] or "引擎 busy 事件均未能打开（见 open）"
            self._close()

    def sample(self, pci=None):
        """返回 {'engines', 'util', 'freq_act', 'freq_req'}；首次采样返回 {'warming': True}；
        PMU 不可用返回 None。不阻塞：只做 read()。"""
        with self._lock:
            now = self.clock()
            if not self._fds:
                if now < self._next_init:
                    return None
                self._init(pci)
                if not self._fds:
                    self._next_init = now + self.RETRY_S
                    return None
            try:
                vals = {n: int(self.reader(fd)) for n, fd in self._fds.items()}
            except Exception as e:
                self._diag["error"] = "读取计数器失败：%s" % (str(e)[:200],)
                self._close()
                self._next_init = now + self.REREAD_RETRY_S
                return None
            prev, self._prev = self._prev, (now, vals)
            self._diag["last_raw"] = {"t": round(now, 3), "values": vals}
            if not prev:
                return {"warming": True}
            dt = now - prev[0]
            if dt < 0.2:
                return self._last or {"warming": True}
            engines, freq = {}, {}
            for n, v in vals.items():
                if n not in prev[1]:
                    continue
                dv = v - prev[1][n]
                if dv < 0:                       # 计数器回绕 / GPU 复位：以本次为新基线
                    self._last = {"warming": True}
                    return self._last
                kind, sub = self._events[n]
                if kind == "freq":
                    freq[sub] = dv / dt          # MHz·s 累计 → MHz
                else:
                    pct = max(0.0, min(100.0, dv / (dt * 1e9) * 100.0))
                    engines[kind] = max(engines.get(kind, 0.0), pct)   # 同类多实例（vcs0/vcs1）取最忙的
            engines = {k: round(v, 1) for k, v in engines.items()}
            self._last = {"engines": engines, "util": max(engines.values()) if engines else None,
                          "freq_act": round(freq["act"]) if "act" in freq else None,
                          "freq_req": round(freq["req"]) if "req" in freq else None}
            return self._last

    def diag(self):
        with self._lock:
            return json.loads(json.dumps(self._diag))

    def close(self):
        with self._lock:
            self._close()


def _drm_cards(sysfs="/sys"):
    """[(card 路径, PCI 地址, 驱动名)]（只读 sysfs 链接，不访问设备）。"""
    out = []
    base = os.path.join(sysfs, "class", "drm")
    try:
        names = sorted(n for n in os.listdir(base) if re.match(r"^card\d+$", n))
    except OSError:
        return out
    for n in names:
        p = os.path.join(base, n)
        try:
            pci = os.path.basename(os.path.realpath(os.path.join(p, "device")))
        except Exception:
            pci = ""
        try:
            drv = os.path.basename(os.readlink(os.path.join(p, "device", "driver")))
        except OSError:
            drv = None
        out.append((p, pci, drv))
    return out


def _card_for(pci, sysfs="/sys"):
    cards = _drm_cards(sysfs)
    tag = (pci or "").strip()
    for c in cards:
        if tag and c[1].endswith(tag):
            return c
    for c in cards:
        if c[2] in ("i915", "xe"):
            return c
    return None


class GpuIdleResidency:
    """RC6（i915）/ gtidle（xe）驻留时间 → 活跃度估算：100 − Δ空闲 ms / Δ墙钟 ms × 100。"""
    PATHS = ("gt/gt0/rc6_residency_ms", "power/rc6_residency_ms",
             "device/tile0/gt0/gtidle/idle_residency_ms")

    def __init__(self, sysfs="/sys", clock=None):
        self.sysfs = sysfs
        self.clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._prev = None
        self.path = None

    def _find(self, pci):
        c = _card_for(pci, self.sysfs)
        if not c:
            return None
        for rel in self.PATHS:
            p = os.path.join(c[0], rel)
            if os.path.exists(p):
                return p
        return None

    def sample(self, pci=None):
        with self._lock:
            if not self.path:
                self.path = self._find(pci)
                if not self.path:
                    return None
            t = read_text(self.path).strip()
            v = int(t) if t.isdigit() else None
            now = self.clock()
            if v is None:
                self.path = None
                self._prev = None
                return None
            prev, self._prev = self._prev, (now, v)
            if not prev or now - prev[0] < 0.2:
                return {"warming": True}
            dv = v - prev[1]
            if dv < 0:                            # 32 位计数回绕：重新取基线
                return {"warming": True}
            idle = dv / ((now - prev[0]) * 1000.0) * 100.0
            return {"util": round(max(0.0, min(100.0, 100.0 - idle)), 1)}


def _intel_gpu_freq(pci, sysfs="/sys"):
    """核显当前频率 MHz（sysfs；优先实际频率 act，其次 cur）。"""
    c = _card_for(pci, sysfs)
    if not c:
        return None
    for rel in ("gt_act_freq_mhz", "gt/gt0/rps_act_freq_mhz", "device/tile0/gt0/freq0/act_freq",
                "gt_cur_freq_mhz", "gt/gt0/rps_cur_freq_mhz", "device/tile0/gt0/freq0/cur_freq"):
        v = read_int_file(os.path.join(c[0], rel))
        if v:
            return v
    return None


_INTEL_SAMPLERS = {}           # pci → (IntelGpuPmu, GpuIdleResidency)
_INTEL_SAMPLERS_LOCK = threading.Lock()


def _intel_samplers(pci):
    with _INTEL_SAMPLERS_LOCK:
        s = _INTEL_SAMPLERS.get(pci)
        if s is None:
            s = _INTEL_SAMPLERS[pci] = (IntelGpuPmu(), GpuIdleResidency())
        return s


def _close_intel_samplers():
    with _INTEL_SAMPLERS_LOCK:
        for pmu, _ in _INTEL_SAMPLERS.values():
            try:
                pmu.close()
            except Exception:
                pass


import atexit as _atexit
_atexit.register(_close_intel_samplers)


def intel_gpu_usage(pci, pmu=None, idle=None, sysfs="/sys"):
    """Intel 核显占用：PMU 引擎占用优先，否则 RC6 活跃度估算，都不可用返回 util=None。"""
    if pmu is None or idle is None:
        pmu, idle = _intel_samplers(pci)
    out = {"util": None, "util_source": None, "engines": None, "freq_mhz": None, "freq_req_mhz": None,
           "warming": False}
    r = pmu.sample(pci)
    if r is not None:
        out["util_source"] = "pmu"
        if r.get("warming"):
            out["warming"] = True
        else:
            out["engines"] = r.get("engines") or None
            out["util"] = r.get("util")
            out["freq_mhz"] = r.get("freq_act")
            out["freq_req_mhz"] = r.get("freq_req")
    else:
        q = idle.sample(pci)
        if q is not None:
            out["util_source"] = "rc6"
            out["warming"] = bool(q.get("warming"))
            out["util"] = q.get("util")
    if out["freq_mhz"] is None:
        out["freq_mhz"] = _intel_gpu_freq(pci, sysfs)
    return out


def intel_gpu_diag(pci=None, sysfs="/sys", pmu=None, idle=None):
    """/api/gpu?diag=1：核显读数诊断（PMU / RC6 / 驱动 / 内核），不含任何机密信息。"""
    if pmu is None or idle is None:
        pmu, idle = _intel_samplers(pci)
    try:
        rel = os.uname().release
    except Exception:
        rel = None
    try:
        sources = sorted(os.listdir(os.path.join(sysfs, "bus", "event_source", "devices")))
    except OSError:
        sources = []
    return {
        "kernel": rel,
        "pci": pci,
        "event_sources": sources[:64],
        "perf_event_paranoid": (read_text("/proc/sys/kernel/perf_event_paranoid") or "").strip() or None,
        "perf_syscall": {"machine": getattr(pmu.opener, "machine", None), "nr": getattr(pmu.opener, "nr", None)},
        "drm_cards": [{"card": os.path.basename(c[0]), "pci": c[1], "driver": c[2]} for c in _drm_cards(sysfs)],
        "pmu": pmu.diag(),
        "rc6_path": idle.path,
        "freq_mhz_sysfs": _intel_gpu_freq(pci, sysfs),
    }


def collect_gpu():
    """GPU 实时监控：温度 / 利用率 / 显存 / 频率（NVIDIA nvidia-smi / AMD sysfs / Intel 核显）。"""
    res = []
    for g in _gpu_ident_list():
        temp = None
        util = None
        util_avail = False
        util_proxy = False
        freq_mhz = None
        power_w = None
        mem_used = None
        mem_total = None
        mem_pct = None
        extra = {"util_source": None, "engines": None}
        try:
            if g["vendor"] == "10de":  # NVIDIA
                s, _ = run_cmd(["nvidia-smi", "--query-gpu=utilization.gpu,utilization.memory,temperature.gpu,memory.used,memory.total",
                                "--format=csv,noheader,nounits"], 3)
                parts = [x.strip() for x in s.split(",")]
                if len(parts) >= 5:
                    try:
                        util = float(parts[0])
                        util_avail = True
                    except Exception:
                        pass
                    try:
                        temp = float(parts[2])
                    except Exception:
                        pass
                    try:
                        mem_used = int(parts[3]) * 1024 * 1024
                        mem_total = int(parts[4]) * 1024 * 1024
                        if mem_total > 0:
                            mem_pct = round(mem_used / mem_total * 100, 1)
                    except Exception:
                        pass
            elif g["vendor"] == "1002":  # AMD
                b = _amd_gpu_busy(g["pci"])
                if b is not None:
                    util = b
                    util_avail = True
                t = _gpu_temp_from_sysfs_live(g["pci"])
                if t is not None:
                    temp = t
                # AMDGPU VRAM
                try:
                    base = (g["pci"] or "").strip()
                    for name in os.listdir("/sys/class/drm"):
                        if not re.match(r"^card\d+$", name):
                            continue
                        devdir = os.path.join("/sys/class/drm", name, "device")
                        uevent = os.path.join(devdir, "uevent")
                        if not os.path.exists(uevent):
                            continue
                        data = open(uevent).read()
                        mm = re.search(r"PCI_SLOT_NAME=(\S+)", data)
                        if not mm:
                            continue
                        dev = mm.group(1)
                        if base and not (base == dev or dev.endswith(base) or base.endswith(dev)):
                            continue
                        used_path = os.path.join(devdir, "mem_info_vram_used")
                        total_path = os.path.join(devdir, "mem_info_vram_total")
                        if os.path.exists(used_path) and os.path.exists(total_path):
                            mem_used = int(open(used_path).read().strip())
                            mem_total = int(open(total_path).read().strip())
                            if mem_total > 0:
                                mem_pct = round(mem_used / mem_total * 100, 1)
                            break
                except Exception:
                    pass
            elif g["vendor"] == "8086":  # Intel 核显：i915 PMU 引擎占用 / RC6 活跃度估算
                # 不再用 CPU 温度冒充 GPU 温度、用系统内存冒充显存（核显没有独立温度与显存读数）
                iu = intel_gpu_usage(g["pci"])
                extra.update(util_source=iu["util_source"], engines=iu["engines"],
                             freq_req_mhz=iu["freq_req_mhz"], warming=iu["warming"], shared_memory=True)
                freq_mhz = iu["freq_mhz"]
                if iu["util"] is not None:
                    util = iu["util"]
                    util_avail = True
        except Exception:
            traceback.print_exc()
        res.append({
            "vendor": g["vendor"], "type": g["type"], "name": g["name"], "pci": g["pci"],
            "temp": (round(temp, 1) if isinstance(temp, (int, float)) else None),
            "util": (round(util, 1) if isinstance(util, (int, float)) else None),
            "freq_mhz": (round(freq_mhz, 1) if isinstance(freq_mhz, (int, float)) else None),
            "power_w": (round(power_w, 2) if isinstance(power_w, (int, float)) else None),
            "mem_used": mem_used, "mem_total": mem_total, "mem_pct": mem_pct,
            "util_avail": util_avail, "util_proxy": util_proxy,
        })
        res[-1].update(extra)
        if res[-1]["util_source"] is None and util_avail:
            res[-1]["util_source"] = "driver"
    return res


# ===================== 内存插槽 SPD（网络主流硬件监控面板功能） =====================
def collect_memory():
    """dmidecode 读取内存插槽：品牌/容量/频率/通道/单双通道。"""
    items = []
    if not is_linux():
        return items
    out, rc = run_cmd(["dmidecode", "-t", "memory"], timeout=10)
    if rc != 0 or not out.strip():
        return items
    cur = {}
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Memory Device"):
            if cur and (cur.get("size") or cur.get("present")):
                items.append(cur)
            cur = {"present": False, "size": "", "type": "", "speed": "", "manufacturer": "", "part": "", "locator": ""}
        elif s.startswith("Memory Array Mapped") or s.startswith("Memory Device Mapped"):
            continue
        elif ":" in s:
            k, _, v = s.partition(":")
            k, v = k.strip(), v.strip()
            if k == "Size" and v != "No Module Installed":
                cur["present"] = True
                cur["size"] = v
            elif k == "Type" and v != "Unknown":
                cur["type"] = v
            elif k == "Speed" and v and v != "Unknown":
                cur["speed"] = v
            elif k == "Manufacturer" and v and v != "Unknown":
                cur["manufacturer"] = v
            elif k == "Part Number" and v and v != "Unknown" and v != "Not Specified":
                cur["part"] = v
            elif k == "Locator" and v and v != "Not Specified":
                cur["locator"] = v
            elif k == "Size":
                cur["present"] = False
    if cur and (cur.get("size") or cur.get("present")):
        items.append(cur)
    # 单双通道判定：相同 Locator 前缀（通道 A/B 等）成对出现视为双通道
    dual = False
    locators = [it.get("locator", "") for it in items if it.get("present")]
    if len(locators) >= 2:
        # 去重后的插槽位置数 >= 2 且总条数 >= 2 → 双通道
        dual = len(set(locators)) >= 2 and len(locators) >= 2
    return {"items": items, "dual": dual}


def _classify_temp(name):
    """根据传感器名启发式分类温度来源（CPU / 芯片组 / ACPI / 磁盘 / GPU / 主板）。"""
    n = name.lower()
    if any(k in n for k in ("package", "core", "tctl", "tccd", "ccd", "cpu")):
        return "cpu"
    if "pch" in n or "soc" in n:
        return "pch"
    if "acpi" in n or "acpitz" in n or "thermal zone" in n:
        return "acpi"
    if "nvme" in n or "ssd" in n or "disk" in n or "drivetemp" in n:
        return "disk"
    if "gpu" in n or "vga" in n:
        return "gpu"
    return "board"


def collect_sensors():
    """sensors -j 解析：温度分类 / 风扇转速 / 电压（参照网络主流硬件监控面板）。"""
    data = {"temps": [], "fans": [], "volts": [], "available": False, "fans_source": ""}
    if not is_linux():
        return data
    out, rc = run_cmd(["sensors", "-j"], timeout=10)
    data["available"] = (rc == 0 and bool(out.strip()))
    # 注意：sensors 不可用（fnOS 默认未装 lm-sensors）时不能提前返回——
    # 风扇通道仍需从 /sys/class/hwmon 直读补齐
    try:
        parsed = json.loads(out) if out.strip() else {}
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    for chip, sub in parsed.items():
        if not isinstance(sub, dict):
            continue
        chip_prefix = str(chip).split("-")[0]
        for key, val in sub.items():
            if not isinstance(val, dict):
                continue
            # 温度
            temp_val = None
            temp_keys = [tk for tk in sorted(val.keys()) if tk.startswith("temp") and tk.endswith("_input")]
            for tk in temp_keys:
                if isinstance(val[tk], (int, float)):
                    temp_val = val[tk]
                    break
            if temp_val is not None:
                tmax = None
                tcrit = None
                base_key = tk.replace("_input", "")
                for mk in ("_max", "_crit"):
                    ck = base_key + mk
                    if ck in val and isinstance(val[ck], (int, float)):
                        if mk == "_max":
                            tmax = val[ck]
                        else:
                            tcrit = val[ck]
                # 温度墙逻辑：coretemp 只保留 max/crit；其他芯片忽略非 ACPI 的 max（多数虚高）
                if chip_prefix != "coretemp" and chip_prefix != "acpitz":
                    tmax = tcrit = None
                if tmax is not None and (tmax < 0 or tmax > 150):
                    tmax = None
                if tcrit is not None and (tcrit < 0 or tcrit > 150):
                    tcrit = None
                # 中文名（温度墙语义）
                if chip_prefix == "coretemp":
                    nm = _temp_name_zh(key, chip_prefix)
                elif chip_prefix == "acpitz":
                    nm = "主板(ACPI)"
                elif chip_prefix.startswith("pch"):
                    nm = "PCH 芯片组"
                elif chip_prefix.startswith("it") and "temp1" in str(key):
                    nm = "主板(CPU附近)"
                elif chip_prefix.startswith("it") and "temp2" in str(key):
                    nm = "主板(系统)"
                elif chip_prefix.startswith("it"):
                    nm = "主板"
                else:
                    nm = _temp_name_zh(key, chip_prefix)
                # 主板温度归口：有 ACPI 时 SYSTIN 降级
                if "SYSTIN" in str(key):
                    nm = "主板(SYSTIN)"
                data["temps"].append({
                    "name": nm, "raw": key, "chip": chip,
                    "type": _classify_temp(key), "value": round(float(temp_val), 1),
                    "max": round(float(tmax), 1) if tmax is not None else None,
                    "crit": round(float(tcrit), 1) if tcrit is not None else None,
                })
            # 风扇转速
            fan_rpm = None
            fan_num = ""
            for fk in ("fan1_input", "fan2_input", "fan3_input", "fan4_input", "fan5_input", "fan6_input"):
                if fk in val and isinstance(val[fk], (int, float)):
                    fan_rpm = val[fk]
                    fan_num = re.sub(r"\D", "", fk)
                    break
            if fan_rpm is not None:
                # chip/num 为内部去重字段（与 sysfs hwmon 通道对齐），输出前会剔除
                data["fans"].append({"name": chip, "rpm": int(fan_rpm),
                                     "chip": chip_prefix.lower(), "num": fan_num})
            # 电压（sensors 中 inN_input 单位为 mV）
            volt = None
            for vk in ("in0_input", "in1_input", "in2_input", "in3_input", "in4_input", "in5_input", "in6_input"):
                if vk in val and isinstance(val[vk], (int, float)):
                    volt = val[vk]
                    break
            if volt is not None:
                label = key
                data["volts"].append({"name": label, "value": round(float(volt) / 1000.0, 3)})
    # ---- 风扇合并：直接读取内核 /sys/class/hwmon 风扇通道作为权威来源 ----
    # 风扇平均转速原先仅依赖外部命令 sensors -j；fnOS 等精简系统默认未安装
    # lm-sensors（sensors 命令不存在），即使 BIOS 可见转速、内核已驱动风扇芯片，
    # 面板仍读不到任何数值。sysfs 直读与 sensors 同源且无外部依赖，两者按
    # (芯片名, 通道号) 去重合并，sensors 未安装或未覆盖的通道由 sysfs 补齐。
    merged_fans = []
    seen_fans = set()
    fan_src = []
    for f in data["fans"]:
        key = (str(f.get("chip", "")).lower(), str(f.get("num", "")))
        if key in seen_fans:
            continue
        seen_fans.add(key)
        merged_fans.append({"name": f["name"], "rpm": f["rpm"]})
    if merged_fans:
        fan_src.append("sensors")
    # sysfs 枚举异常不应影响温度/电压等其它传感器数据
    try:
        sysfs_fans = _hwmon_fans()
    except Exception:
        sysfs_fans = []
    n_hw = len(merged_fans)
    for sf in sysfs_fans:
        key = (str(sf.get("chip", "")).lower(), str(sf.get("num", "")))
        if key in seen_fans:
            continue
        seen_fans.add(key)
        merged_fans.append({"name": sf.get("name") or ("风扇 " + str(sf.get("num", ""))),
                            "rpm": int(sf.get("rpm") or 0)})
    if len(merged_fans) > n_hw:
        fan_src.append("hwmon")
    # ---- 标准 hwmon / sensors 均无风扇时，启用补充数据源（自动识别）----
    # 覆盖标准内核驱动未接管的机型：IPMI/BMC 服务器、ThinkPad ACPI（均为只读）。
    # EC 硬件监控邮箱（0xA20）需要向 IO 端口 0xA20/0xA21 写入选通 / bank / 寄存器
    # 字节，属于硬件访问，默认关闭；仅在用户于设置中开启 ec_fan_probe 后才尝试。
    if not merged_fans:
        sources = [("ipmi", _ipmi_fans), ("ibm-acpi", _ibm_acpi_fans)]
        if RUNTIME.get("ec_fan_probe"):
            sources.append(("ec-hwm", _ec_hwm_fans))
        for tag, getter in sources:
            try:
                extra = getter()
            except Exception:
                extra = []
            if not extra:
                continue
            fan_src.append(tag)
            for ef in extra:
                key = (str(ef.get("chip", "")).lower(), str(ef.get("num", "")))
                if key in seen_fans:
                    continue
                seen_fans.add(key)
                merged_fans.append({"name": ef["name"], "rpm": int(ef["rpm"])})
    data["fans"] = merged_fans
    data["fans_source"] = "+".join(fan_src)
    # 温度按 CPU / 芯片组 / 主板 / ACPI 顺序排列
    order = {"cpu": 0, "pch": 1, "board": 2, "acpi": 3, "disk": 4, "gpu": 5}
    data["temps"].sort(key=lambda t: (order.get(t["type"], 9), t["name"]))
    return data


def _hwmon_resolve(hpath, dpath, fname):
    """定位 hwmon 属性文件真实路径：优先 hwmonN/ 顶层，回退 hwmonN/device/。

    新内核属性挂在 /sys/class/hwmon/hwmonN/ 下；部分 Nuvoton/ITE SuperIO 旧驱动
    把 fanN_input / pwmN 挂在 hwmonN/device/ 下，只扫顶层会完全读不到风扇。
    """
    p = os.path.join(hpath, fname)
    if os.path.exists(p):
        return p
    if dpath:
        p2 = os.path.join(dpath, fname)
        if os.path.exists(p2):
            return p2
    return p


def _hwmon_fans():
    """枚举 /sys/class/hwmon 下所有风扇通道（只读 fanN_input 转速）。"""
    fans = []
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return fans
    try:
        hw_list = sorted(os.listdir(base))
    except Exception:
        return fans
    for hw in hw_list:
        hpath = os.path.join(base, hw)
        if not os.path.isdir(hpath):
            continue
        dpath = os.path.join(hpath, "device")
        if not os.path.isdir(dpath):
            dpath = ""
        entries = set()
        for d in (hpath, dpath):
            if not d:
                continue
            try:
                entries.update(os.listdir(d))
            except Exception:
                pass
        if not entries:
            continue
        hname = (read_text(os.path.join(hpath, "name")).strip()
                 or (read_text(os.path.join(dpath, "name")).strip() if dpath else "")
                 or hw)
        fan_inputs = sorted([f for f in entries if f.startswith("fan") and f.endswith("_input")],
                            key=lambda x: int(re.sub(r"\D", "", x) or 0))
        for f in fan_inputs:
            num = re.sub(r"\D", "", f)
            rpm = read_int_file(_hwmon_resolve(hpath, dpath, f))
            fans.append({
                "chip": hname, "num": num,
                "name": hname + " 风扇 " + num, "rpm": rpm,
            })
    return fans


def _ipmi_fans():
    """通过 ipmitool 读取 BMC/IPMI 风扇转速（服务器主板、带 BMC 的准系统）。

    依赖系统已安装 ipmitool（apt install ipmitool）且内核加载了 ipmi 驱动；
    命令不存在、无 BMC 设备或执行超时时静默返回空列表，不产生额外影响。
    典型输出：Fan1A | 4320 RPM | ok；百分比 / no reading 行会被跳过。
    """
    fans = []
    if not is_linux() or not shutil.which("ipmitool"):
        return fans
    out, rc = run_cmd(["ipmitool", "sdr", "type", "Fan"], timeout=8)
    if rc != 0 or not out:
        return fans
    for line in out.splitlines():
        cols = line.split("|")
        if len(cols) < 2:
            continue
        m = re.search(r"(\d+(?:\.\d+)?)\s*rpm", cols[1].strip().lower())
        if not m:
            continue
        rpm = int(float(m.group(1)))
        if rpm <= 0:
            continue
        name = cols[0].strip() or "IPMI 风扇"
        slug = re.sub(r"[^a-z0-9]+", "", name.lower())[:12] or "0"
        fans.append({"name": name, "rpm": rpm, "chip": "ipmi", "num": slug})
    return fans


def _ibm_acpi_fans():
    """ThinkPad / ThinkCentre 的 thinkpad_acpi 风扇：/proc/acpi/ibm/fan。

    文件由内核 thinkpad_acpi 驱动提供（speed 行为 RPM），纯文件读取、无外部
    依赖；文件不存在或无转速读数时返回空列表。
    """
    path = "/proc/acpi/ibm/fan"
    if not is_linux() or not os.path.exists(path):
        return []
    m = re.search(r"speed\s*:\s*(\d+)", read_text(path), re.IGNORECASE)
    if not m:
        return []
    rpm = int(m.group(1))
    if rpm <= 0:
        return []
    return [{"name": "ThinkPad 风扇", "rpm": rpm, "chip": "ibm-acpi", "num": "0"}]


# EC 硬件监控邮箱探测状态："valid" 已确认是同协议邮箱；"invalid" 已确认不是，
# 本进程生命周期内不再访问该端口区（避免反复对未知硬件发 IO 序列）
_EC_HWM_STATE = {"checked": False, "valid": False}


def _ec_hwm_available():
    """是否允许尝试 0xA20 硬件监控邮箱探测（纯硬件闸口，不限品牌）。

    闸口：
    1) Linux 且 /dev/port 可用；
    2) /proc/ioports 中确实存在 0a20 端口区——端口不存在时芯片组直接丢弃
       对该区的 IO 读写（读回 0xFF），物理上没有任何设备会受影响；
    3) 该区由 PnP 固件设备占用（形如「pnp 00:01」）；若被已加载的内核驱动
       按名占用，说明驱动在管理该硬件，跳过以免冲突（且这种情况通常 hwmon
       里已经有风扇，也走不到这里）。
    真正「是不是同协议监控邮箱」由 _ec_hwm_probe_valid() 的传感器应答判定。
    """
    if not is_linux() or not os.path.exists("/dev/port"):
        return False
    for line in read_text("/proc/ioports").splitlines():
        m = re.match(r"\s*(0a20-0a2[0-9a-f])\s*:\s*(.*)$", line, re.I)
        if not m:
            continue
        owner = (m.group(2) or "").strip()
        if owner == "" or re.match(r"pnp\s+[0-9a-f]{2}:[0-9a-f]{2}$", owner, re.I):
            return True
    return False


def _ec_hwm_probe_valid(ec_read):
    """行为验证：向邮箱读 bank1 的 4 个温度寄存器，应答像真实 HWM 芯片才认可。

    协议机（NEC/LENOVO 及同 EC IP 的其他 ODM 机型）此处返回真实摄氏温度
    （有符号字节，负温二补码）；无关设备/空总线一般恒返回 0x00 或 0xFF。
    判据：至少 2 个寄存器落在 -20~120℃，且至少 1 个落在 20~95℃（运行中
    的整机几乎必有一个传感器在该温区）。
    """
    plausible = 0
    warm = False
    for reg in (0x00, 0x02, 0x04, 0x06):
        try:
            v = ec_read(1, reg)
        except Exception:
            return False
        if v >= 128:
            v -= 256
        if -20 <= v <= 120:
            plausible += 1
        if 20 <= v <= 95:
            warm = True
    return warm and plausible >= 2


def _ec_hwm_fans():
    """读取 EC 硬件监控邮箱（0xA20/0xA21/0xA22）的风扇转速，机型无关自动识别。

    部分品牌商用机 / ODM 主机使用这组三端口 IO 邮箱而非标准 ACPI EC，Linux 无
    现成驱动（DSDT 中 PNP0C09 EC 多为返回 0 的桩）。协议取自固件自身使用的
    GFAN/HWMG 方法：选 bank1 后，寄存器 0x40-0x47 为 4 个风扇的 16 位转速
    （RPM，高字节在前）。为保证对无关品牌主机零风险，启用需依次通过：
    端口区存在 → PnP 固件占用 → 温度应答合法（见上述两个闸口函数），且
    数据端口 0xA22 全程只做 IN 读；但每次读取都要向 0xA20/0xA21 **写入**选通复位、
    bank 与寄存器号（首末各写一次 0xFF 是固件自身的邮箱选通复位）。这不是纯只读
    操作，且与固件（ACPI/SMM）并发访问同一邮箱时没有互斥，因此默认关闭，仅在
    config.ec_fan_probe=1（设置页「实验性：EC 风扇读取」）时由 collect_sensors 调用。
    验证失败的主机结果缓存，不再访问。
    """
    if not RUNTIME.get("ec_fan_probe"):
        return []
    fans = []
    if _EC_HWM_STATE["checked"] and not _EC_HWM_STATE["valid"]:
        return fans
    if not _ec_hwm_available():
        _EC_HWM_STATE.update(checked=True, valid=False)
        return fans
    p_idx, p_dat, p_val = 0xA20, 0xA21, 0xA22
    try:
        fh = open("/dev/port", "r+b", buffering=0)
    except Exception:
        return fans
    try:
        def ec_read(bank, reg):
            fh.seek(p_idx)
            fh.write(b"\xff")       # 邮箱选通复位
            fh.seek(p_idx)
            fh.write(bytes((bank,)))
            fh.seek(p_dat)
            fh.write(bytes((reg,)))
            fh.seek(p_val)
            b = fh.read(1)
            fh.seek(p_idx)
            fh.write(b"\xff")
            return b[0] if b else 0

        if not _EC_HWM_STATE["checked"]:
            valid = _ec_hwm_probe_valid(ec_read)
            _EC_HWM_STATE.update(checked=True, valid=valid)
            if not valid:
                return fans

        n = 0
        for base in (0x40, 0x42, 0x44, 0x46):
            try:
                rpm = (ec_read(1, base) << 8) | ec_read(1, base + 1)
            except Exception:
                continue
            # 合理性校验：0 表示该通道未接风扇；正常转速 300-30000 RPM
            if 300 <= rpm <= 30000:
                n += 1
                fans.append({"name": "风扇 " + str(n), "rpm": rpm,
                             "chip": "ec-hwm", "num": str(base)})
    finally:
        try:
            fh.close()
        except Exception:
            pass
    return fans


def collect_raid():
    """mdadm RAID 阵列状态：/proc/mdstat。"""
    raids = []
    for line in read_text("/proc/mdstat").splitlines():
        line = line.strip()
        if not line or line.startswith("Personalities") or line.startswith("unused"):
            continue
        if not line.startswith("md") or "active" not in line:
            continue
        parts = line.split()
        dev = parts[0].rstrip(":")
        status = parts[2] if len(parts) > 2 else ""
        members = parts[3:] if len(parts) > 3 else []
        raids.append({"device": dev, "status": status, "members": members})
    return raids


def smart_detail(dev):
    """smartctl 读取指定盘健康/型号/转速/通电时长/告警计数。失败返回 {}。"""
    if not is_linux():
        return {}
    # -n standby：盘处于 STANDBY 时 smartctl 直接 exit(2)、不执行任何命令，
    # 从而不会唤醒休眠硬盘；此处明确返回 standby 标记供调用方复用上一次结果
    out, rc = run_cmd(["smartctl", "-n", "standby", "-i", "-H", "-A", "/dev/" + dev], timeout=8)
    # 先判 exit(2)：smartctl -n standby 命中休眠盘时执行 exit(2)，stdout 可能完全为空，
    # 若先判空输出就会把「休眠」误判成「读取失败」
    if rc == 2 or "STANDBY" in out.upper() or "SLEEP" in out.upper():
        return {"standby": True}
    if not out.strip():
        return {}
    if rc != 0:
        return {}
    d = {"health": "未知"}
    for line in out.splitlines():
        s = line.strip()
        if ":" not in s:
            continue
        k, _, v = s.partition(":")
        k, v = k.strip(), v.strip()
        if k == "SMART overall-health self-assessment test result" or k == "SMART Health Status":
            d["health"] = v
        elif k == "Device Model" or k == "Model Number" or k == "Product":
            d["model2"] = v
        elif k.lower() == "serial number":
            # SAS 设备写 "Serial number"（小写 n），SATA/NVMe 写 "Serial Number"
            d["serial"] = v
        elif k == "Rotation Rate":
            d["rpm"] = v
        elif k == "Temperature":
            try:
                d["temp_c"] = int(v.split()[0])
            except Exception:
                pass
        elif k == "Current Drive Temperature" or k == "Drive Temperature":
            # SAS/SCSI 温度写法：Current Drive Temperature:     33 C
            m = re.search(r"(-?\d+)", v)
            if m:
                d["temp_c"] = int(m.group(1))
        elif k == "SMART/Health Information" or k == "SMART Attributes":
            pass
    # ATA 告警计数（硬盘 SMART 健康：Reallocated / Pending / Uncorrectable / UDMA CRC）
    ata_attrs = {
        "5": ("reallocated_sectors", "重映射扇区"),
        "196": ("reallocated_events", "重映射事件"),
        "197": ("pending_sectors", "待重映射扇区"),
        "198": ("uncorrectable", "无法纠正错误"),
        "199": ("udma_crc", "UDMA CRC 错误"),
    }
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 10 and parts[0] in ata_attrs and "Power_On_Hours" not in parts[1]:
            key, _label = ata_attrs[parts[0]]
            try:
                d[key] = int(parts[9])
            except Exception:
                d[key] = 0
        if len(parts) >= 10 and parts[0] == "9" and "Power_On_Hours" in parts[1]:
            d["power_on_hours"] = parts[9]
    # NVMe：从 SMART/Health Information 段解析通电时长与温度
    for line in out.splitlines():
        s = line.strip()
        if "Power On Hours" in s:
            m = re.search(r"([\d,]+)\s*hours", s)
            if m:
                d["power_on_hours"] = m.group(1).replace(",", "")
        elif s.startswith("Temperature:"):
            try:
                d["temp_c"] = int(s.split()[1])
            except Exception:
                pass
    # SAS/SCSI：没有 ATA 属性表，健康数据在 grown defect list 与 Error counter log 里。
    # 前端 SMART_KEYS 只认 ATA 键名，所以同时给出 SAS 原始键和映射键——不改前端也能判红黄绿：
    #   grown_defects    -> reallocated_sectors（增长缺陷表，语义最接近 ATA 重映射）
    #   scsi_uncorrected -> uncorrectable（read/write/verify 三向不可纠正错误合计）
    scsi = False
    grown = None
    power_on = None
    ecc_fast = ecc_delayed = rereads = corrected = algo = unc = 0
    gigabytes = 0.0
    in_err_log = False
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("Error counter log"):
            in_err_log, scsi = True, True
            continue
        if in_err_log and not s:
            in_err_log = False
            continue
        m = re.match(r"Elements in grown defect list:\s*(\d+)", s)
        if m:
            grown, scsi = int(m.group(1)), True
            continue
        m = re.match(r"Pending defects:\s*(\d+)", s)
        if m:
            # 部分 SAS 盘（如 ST4000NM019B）会额外给出待定缺陷计数
            d["scsi_pending_defects"] = int(m.group(1))
            scsi = True
            continue
        m = re.match(r"Accumulated power on time,\s*hours:minutes\s+(\d+):(\d+)", s)
        if m:
            # SAS 写法：Accumulated power on time, hours:minutes 21456:23（含冒号，不能用 partition）
            power_on, scsi = int(m.group(1)), True
            continue
        m = re.match(r"Manufactured in week\s+(\d+)\s+of year\s+(\d+)", s)
        if m:
            # SAS 写法：Manufactured in week 15 of year 2019
            d["manufactured"] = "%s-W%s" % (m.group(2), m.group(1).zfill(2))
            continue
        if in_err_log:
            m = re.match(r"(read|write|verify):\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+([\d.]+)\s+(\d+)", s)
            if m:
                ecc_fast += int(m.group(2))
                ecc_delayed += int(m.group(3))
                rereads += int(m.group(4))
                corrected += int(m.group(5))
                algo += int(m.group(6))
                try:
                    gigabytes += float(m.group(7))
                except Exception:
                    pass
                unc += int(m.group(8))
    if scsi:
        d["scsi"] = True
        if grown is not None:
            d["grown_defects"] = grown
            d.setdefault("reallocated_sectors", grown)
        d["scsi_uncorrected"] = unc
        # 0 也是有意义的读数（盘干净），必须落键：否则盘位表里「不可纠正」永远显示 None，
        # 看不出到底是 0 还是没读到。ATA 侧已给出 uncorrectable 时以 ATA 为准。
        d.setdefault("uncorrectable", unc)
        if d.get("scsi_pending_defects") is not None:
            d.setdefault("pending_sectors", d["scsi_pending_defects"])
        if ecc_fast or ecc_delayed or rereads:
            # 注意：errors_corrected_by_eccfast 是 32 位计数器，会回绕到 40 亿量级，
            # 只反映设备读过多少数据、不反映盘的健康度——真机实测同型号三块盘分别为
            # 41.6 亿 / 0.26 亿 / 14.7 亿，没有一块故障。所以它单独暴露为 scsi_ecc，
            # 不映射到任何告警键；健康判定只看 grown_defects 与 scsi_uncorrected。
            d["scsi_ecc"] = ecc_fast + ecc_delayed
            d["scsi_rereads"] = rereads
        if corrected or algo or gigabytes:
            d["scsi_corrected"] = corrected
            d["scsi_gb"] = round(gigabytes, 3)
        if power_on is not None:
            d["power_on_hours"] = str(power_on)
    return d


_SMART_CACHE = {}
_SMART_TTL = 300.0
# /api/smart 只接受内核设备名，避免把任意字符串拼进 smartctl 的 argv
_DEV_NAME_RE = re.compile(r"^(?:sd[a-z]{1,3}|nvme\d+n\d+|hd[a-z]{1,3}|vd[a-z]{1,3})$")


def smart_detail_cached(dev):
    """按需读取单盘 SMART 明细（详情面板打开时才调用），5 分钟内复用上次结果。

    与 60 秒后台采集循环解耦：后台只做 lsblk / sysfs 这类轻量采集，
    重量级的 smartctl 留到用户真正打开详情时再跑，避免每轮扫全部硬盘。
    休眠保护由 smart_detail 的 -n standby 保证：命中休眠盘直接返回 standby，
    不会为了读详情把它唤醒。
    """
    now = time.time()
    hit = _SMART_CACHE.get(dev)
    if hit and now - hit[0] < _SMART_TTL:
        d = dict(hit[1])
        d["cached"] = True
        return d
    data = smart_detail(dev) or {}
    _SMART_CACHE[dev] = (now, data)
    d = dict(data)
    d["cached"] = False
    return d


# ===========================================================================
# 硬盘巡检：后台定时全盘扫 SMART → 分级入库 → 盘位卡片只读缓存
# ===========================================================================
# 为什么必须做成后台线程（而不是像 sashealth 那样在 /api/data 里懒触发）：
#   sashealth 的自动快照写在 collect() 内部，由页面请求驱动，没人打开页面就永不
#   检测——在它自己是「手动打开查看」的工具，问题不大；合进常驻监控面板后，这个
#   懒触发会直接让告警失效。所以巡检独立成线程，看数据按需、发告警定时。
#
# 分级口径（SAS / ATA 统一）：
#   crit     SMART 自检 FAIL / 不可纠正错误 > 0 / 缺陷较上次增长 ≥ 10
#   warn     增长缺陷 > 0 或待处理扇区 > 0（含小幅增长）
#   ok       计数全 0 且 SMART 给出 PASSED
#   unknown  读不到（休眠未唤醒、盘不支持 SMART、smartctl 不可用）
# 注意 ECC 计数（scsi_ecc）不参与判定：它是 32 位计数器会回绕，只反映读过多少数据。
DISK_SCAN_PRESETS_H = (6, 12, 24, 72, 168)   # 与 sashealth 自动快照档位一致
DISK_SCAN_GROWTH_CRIT = 10                   # 一次巡检内缺陷增长到这个数就判故障
_disk_scan_state = {"running": False, "last_ts": 0.0, "summary": {}, "error": ""}
_DISK_SCAN_LOCK = threading.Lock()


def disk_scan_interval_h(config):
    """巡检间隔（小时），0 表示关闭。非法值就近取档位，最小 6 小时。"""
    try:
        if int((config or {}).get("disk_scan_enabled", DEFAULT_CONFIG["disk_scan_enabled"])) == 0:
            return 0
        raw = int((config or {}).get("disk_scan_interval_h", DEFAULT_CONFIG["disk_scan_interval_h"]))
    except Exception:
        raw = DEFAULT_CONFIG["disk_scan_interval_h"]
    if raw <= 0:
        return 0
    return min(DISK_SCAN_PRESETS_H, key=lambda x: abs(x - raw))


def _nz(v):
    """把计数归一成 int / None，避免 0 与 None 在「有无变化」比较里来回跳。"""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except Exception:
        return None


def grade_disk(rec, prev=None):
    """给单盘定级，返回 (grade, reasons)。rec 为 smart_detail() 结果。

    prev 是上一次巡检记录（disk_health 行），用于判定缺陷增长。"""
    if not rec:
        return "unknown", ["未读取到 SMART"]
    if rec.get("error"):
        return "unknown", ["读取失败：%s" % rec["error"]]
    health = str(rec.get("health") or "").strip()
    unc = _nz(rec.get("uncorrectable")) or 0
    reall = _nz(rec.get("reallocated_sectors"))
    pend = _nz(rec.get("pending_sectors")) or 0
    if re.search(r"FAIL", health, re.I):
        return "crit", ["SMART 自检未通过"]
    if unc > 0:
        return "crit", ["不可纠正错误 %d 个" % unc]
    delta = None
    if reall is not None and prev is not None:
        old = _nz(prev.get("defects"))
        if old is not None:
            delta = reall - old
    if delta is not None and delta >= DISK_SCAN_GROWTH_CRIT:
        return "crit", ["缺陷增长 %+d 个（%d → %d）" % (delta, reall - delta, reall)]
    reasons = []
    if reall:
        reasons.append("增长缺陷 %d 个" % reall)
    if pend:
        reasons.append("待处理扇区 %d 个" % pend)
    if delta and delta > 0:
        reasons.append("较上次 +%d" % delta)
    if reasons:
        # 有计数但没给总体评估时也按计数判，不要因为缺 PASSED 就退回 unknown
        return "warn", reasons
    if not health:
        return "unknown", ["SMART 未提供总体评估"]
    return "ok", ["SMART 通过"]


def disk_scan_devices():
    """要巡检的物理盘：lsblk -d 只列整盘，排除分区 / loop / 只读 / 可移动介质。"""
    out, rc = run_cmd(["lsblk", "-b", "-J", "-d", "-o", "NAME,TYPE,SIZE,RO,RM,MODEL"], timeout=10)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    devs = []
    for b in data.get("blockdevices", []) or []:
        name = str(b.get("name") or "")
        if str(b.get("type") or "").lower() != "disk":
            continue
        if str(b.get("ro")) in ("1", "True", "true") or str(b.get("rm")) in ("1", "True", "true"):
            continue
        if not _DEV_NAME_RE.match(name):
            continue
        devs.append({"dev": name, "size": _nz(b.get("size")) or 0, "model": str(b.get("model") or "").strip()})
    return devs


def _health_row_rec(r):
    """disk_health 行 → smart_detail 同形状的 dict，供采集循环与详情面板复用。"""
    hrs = r.get("power_on_hours")
    return {
        "health": r.get("health") or "",
        "serial": r.get("serial") or "",
        "model2": r.get("model") or "",
        "temp_c": r.get("temp_c"),
        "power_on_hours": "" if hrs is None else str(hrs),
        "rpm": r.get("rpm") or "",
        "manufactured": r.get("manufactured") or "",
        "reallocated_sectors": r.get("defects"),
        "pending_sectors": r.get("pending_defects"),
        "uncorrectable": r.get("uncorr"),
        "scsi": (r.get("proto") or "") == "SAS",
    }


def disk_scan_once(db, config=None):
    """执行一次全盘巡检并入库。返回摘要，异常不外抛（线程里靠它活命）。"""
    if not is_linux():
        return {"ok": False, "error": "仅支持 Linux"}
    if not _DISK_SCAN_LOCK.acquire(False):
        return {"ok": False, "error": "已有巡检正在进行"}
    try:
        ts = time.time()
        _disk_scan_state["running"] = True
        try:
            prev = db.disk_health_read() if db else {}
        except Exception:
            prev = {}
        items, skipped, errors = [], [], []
        for d in disk_scan_devices():
            dev = d["dev"]
            try:
                rec = smart_detail(dev) or {}
            except Exception as e:
                errors.append("%s 读取异常：%s" % (dev, e))
                continue
            # 顺手刷新按需读的缓存：巡检刚读到的数据，详情面板不必再跑一次 smartctl
            if rec and not rec.get("standby"):
                _SMART_CACHE[dev] = (ts, rec)
                _DISK_HEALTH_MEM[dev] = rec
            old = prev.get(dev) or {}
            if rec.get("standby"):
                # 休眠盘不唤醒：沿用上次分级与计数，只标记状态
                if old:
                    item = dict(old)
                    item.update({"dev": dev, "standby": True})
                    items.append(item)
                else:
                    items.append({"dev": dev, "standby": True, "grade": "unknown",
                                  "scan_ts": ts, "detail": "休眠中，尚未读取过"})
                skipped.append(dev)
                continue
            grade, reasons = grade_disk(rec, old if old else None)
            items.append({
                "dev": dev,
                "serial": rec.get("serial") or "",
                "model": rec.get("model2") or d.get("model") or "",
                "proto": "SAS" if rec.get("scsi") else "",
                "health": str(rec.get("health") or ""),
                "grade": grade,
                "defects": _nz(rec.get("reallocated_sectors")),
                "pending_defects": _nz(rec.get("pending_sectors")),
                "uncorr": _nz(rec.get("uncorrectable")),
                "temp_c": _nz(rec.get("temp_c")),
                "power_on_hours": _nz(rec.get("power_on_hours")),
                "rpm": str(rec.get("rpm") or ""),
                "manufactured": str(rec.get("manufactured") or ""),
                "standby": False,
                "scan_ts": ts,
                "detail": "；".join(reasons),
            })
        changed = []
        if db:
            try:
                changed = db.disk_health_save(items, prev)
            except Exception as e:
                errors.append("写库失败：%s" % e)
        summary = {
            "ok": True, "ts": ts, "total": len(items), "changed": changed,
            "skipped_standby": skipped, "errors": errors,
            "counts": {g: sum(1 for i in items if i.get("grade") == g) for g in ("ok", "warn", "crit", "unknown")},
        }
        # 状态有变化才推送（新增 / 加重 / 恢复）；通知失败只记日志，不影响巡检结果
        try:
            note = notify_after_scan(items, ts, db, config)
        except Exception:
            traceback.print_exc()
            note = None
        if note:
            summary["notify"] = note
        _disk_scan_state.update({"last_ts": ts, "summary": summary, "error": ""})
        return summary
    except Exception as e:
        traceback.print_exc()
        _disk_scan_state["error"] = str(e)
        return {"ok": False, "error": str(e)}
    finally:
        _disk_scan_state["running"] = False
        _DISK_SCAN_LOCK.release()


class DiskScanner(threading.Thread):
    """巡检线程：按配置间隔全盘扫一遍，首次上电 60 秒后先补扫一次。

    60 秒延迟是刻意的：别让人刚装完/刚重启就看到一片「未巡检」，也不必等满一个周期。
    """

    STARTUP_DELAY = 60

    def __init__(self, db, config):
        super().__init__(daemon=True, name="fnmonitor-disk-scan")
        self.db = db
        self.config = config
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        # 重启后沿用库里上一次的巡检时间与结果：否则每次重启都会重新全盘扫一遍，
        # 而且首屏会退回到「只有前 8 块盘有 SMART」
        try:
            rows = self.db.disk_health_read() if self.db else {}
            last = max([float(r.get("scan_ts") or 0) for r in rows.values()] or [0])
            if last:
                _disk_scan_state["last_ts"] = last
            for dev, r in (rows or {}).items():
                if not r.get("standby") and dev not in _DISK_HEALTH_MEM:
                    _DISK_HEALTH_MEM[dev] = _health_row_rec(r)
        except Exception:
            pass
        startup = self.STARTUP_DELAY
        while not self._stop.is_set():
            try:
                hours = disk_scan_interval_h(self.config)
                if hours <= 0:
                    if self._stop.wait(60):
                        return
                    continue
                last = float(_disk_scan_state.get("last_ts") or 0)
                if last:
                    remain = last + hours * 3600 - time.time()
                    if remain > 0:
                        # 分段睡，配置改了（间隔调短/关闭）能很快生效
                        if self._stop.wait(min(remain, 300)):
                            return
                        continue
                elif startup:
                    if self._stop.wait(startup):
                        return
                    startup = 0
                    continue
                disk_scan_once(self.db, self.config)
            except Exception:
                traceback.print_exc()
                if self._stop.wait(300):
                    return


# ---------------------------------------------------------------------------
# 硬盘告警通知（SMTP 邮件 / Server酱，移植自 sashealth）
# ---------------------------------------------------------------------------
# 只在巡检发现状态变化时发一条：新增故障 / 故障加重 / 已恢复。同一块盘的同一
# 状态不重复发；发送失败不落「已通知」状态，下一轮巡检自动重试。休眠盘（沿用
# 上次分级）不参与判定，避免把「没读到」误报成故障。
# 配置独立存放在 notify.json（0600），不进 config.json，也不经过 /api/config
# 回传——授权码 / SendKey 只写不读，前端只知道「是否已填」。
NOTIFY_FILE = "notify.json"
NOTIFY_STATE_FILE = "notify_state.json"
NOTIFY_PROVIDERS = (
    ("qq", "QQ 邮箱", "smtp.qq.com", 465, "ssl"),
    ("163", "163 邮箱", "smtp.163.com", 465, "ssl"),
    ("126", "126 邮箱", "smtp.126.com", 465, "ssl"),
    ("exmail", "腾讯企业邮", "smtp.exmail.qq.com", 465, "ssl"),
    ("outlook", "Outlook / Office365", "smtp.office365.com", 587, "starttls"),
    ("custom", "自定义", "", 465, "ssl"),
)
NOTIFY_DEFAULT = {"enabled": 0, "provider": "qq", "host": "smtp.qq.com", "port": 465,
                  "secure": "ssl", "user": "", "password": "", "sender": "", "to": "",
                  "sct_enabled": 0, "sct_key": ""}
NOTIFY_SECRET_KEYS = ("password", "sct_key")
GRADE_LABEL = {"ok": "正常", "warn": "留意", "crit": "故障", "unknown": "未知"}


def notify_load(data_dir):
    """读 notify.json；缺字段回落默认值，格式错误不影响其余字段。"""
    cfg = dict(NOTIFY_DEFAULT)
    if not data_dir:
        return cfg
    try:
        with open(os.path.join(data_dir, NOTIFY_FILE), "r", encoding="utf-8", errors="ignore") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            user = {}
    except Exception:
        user = {}
    onoff = lambda x: 1 if str(x).lower() in ("1", "true", "on") else 0
    for k in ("enabled", "sct_enabled"):
        if k in user:
            cfg[k] = onoff(user[k])
    if str(user.get("provider") or "") in [p[0] for p in NOTIFY_PROVIDERS]:
        cfg["provider"] = str(user["provider"])
    for k in ("host", "user", "sender", "to", "password", "sct_key"):
        if k in user:
            cfg[k] = str(user[k] or "")
    if "port" in user:
        try:
            cfg["port"] = max(1, min(65535, int(user["port"])))
        except Exception:
            pass
    if str(user.get("secure") or "").lower() in ("ssl", "starttls", "none"):
        cfg["secure"] = str(user["secure"]).lower()
    return cfg


def notify_public(cfg):
    """对外配置：授权码 / SendKey 换成「是否已填」，绝不回传明文。"""
    out = {k: v for k, v in cfg.items() if k not in NOTIFY_SECRET_KEYS}
    out["password_set"] = bool(cfg.get("password"))
    out["sct_key_set"] = bool(cfg.get("sct_key"))
    return out


def notify_save(data_dir, data, cur=None):
    """合并保存通知配置：空值 / 未提供的密码项保留原值，返回 (配置, 错误)。"""
    cfg = dict(cur) if cur else notify_load(data_dir)
    onoff = lambda x: 1 if str(x).lower() in ("1", "true", "on") else 0
    for k in ("enabled", "sct_enabled"):
        if k in data:
            cfg[k] = onoff(data[k])
    if "provider" in data:
        p = str(data["provider"] or "")
        if p in [x[0] for x in NOTIFY_PROVIDERS]:
            cfg["provider"] = p
            for pid, _l, host, port, sec in NOTIFY_PROVIDERS:
                if pid == p and host:      # 选预设就把服务器参数一起带过来
                    cfg["host"], cfg["port"], cfg["secure"] = host, port, sec
    if "host" in data:
        cfg["host"] = clean_text(data["host"], 128)
    if "port" in data:
        try:
            cfg["port"] = max(1, min(65535, int(data["port"])))
        except Exception:
            return cfg, "端口须为 1–65535 的整数"
    if "secure" in data:
        s = str(data["secure"] or "").lower()
        if s in ("ssl", "starttls", "none"):
            cfg["secure"] = s
    for k, n in (("user", 128), ("sender", 128), ("to", 400)):
        if k in data:
            cfg[k] = clean_text(data[k], n)
    # 密码 / SendKey：只有前端真的传了新值才覆盖；传空串按「不改」处理，
    # 想清除请显式传 clear_password / clear_sct_key（前端未使用，保留 API 能力）
    if str(data.get("password") or ""):
        cfg["password"] = str(data["password"])[:256]
    if str(data.get("sct_key") or ""):
        cfg["sct_key"] = str(data["sct_key"])[:256]
    if data.get("clear_password"):
        cfg["password"] = ""
    if data.get("clear_sct_key"):
        cfg["sct_key"] = ""
    if cfg.get("enabled"):
        miss = [name for name, ok in (("SMTP 服务器", cfg.get("host")), ("发件账号", cfg.get("user")),
                                      ("授权码", cfg.get("password")), ("收件人", cfg.get("to"))) if not ok]
        if miss:
            return cfg, "邮件通知缺少：%s" % "、".join(miss)
    if cfg.get("sct_enabled") and not cfg.get("sct_key"):
        return cfg, "微信通知缺少 SendKey"
    write_json_secure(os.path.join(data_dir, NOTIFY_FILE), cfg)
    return cfg, ""


def notify_short_err(msg):
    """把 SMTP / HTTP 的原始报错翻成一句能看懂的话。"""
    m = (msg or "").strip().replace("\n", " ")
    low = m.lower()
    if "auth" in low or "535" in m or "username and password" in low:
        return "认证失败（账号或授权码错误）"
    if "timed out" in low or "timeout" in low:
        return "连接超时"
    if "getaddrinfo" in low or "name or service not known" in low or "11001" in m:
        return "无法解析服务器地址"
    if "refused" in low:
        return "服务器拒绝连接"
    if "550" in m or "recipient" in low:
        return "收件人被拒"
    if "ssl" in low or "certificate" in low:
        return "SSL/TLS 握手失败"
    if "unreachable" in low:
        return "网络不可达"
    if "400" in m or "bad request" in low or "404" in m:
        return "请求被拒（SendKey 可能无效）"
    if "证书" in m or "certificate" in m:
        return "SSL/TLS 握手失败"
    return (m[:60] + "…") if len(m) > 60 else (m or "未知错误")


def notify_mail_send(cfg, subject, body):
    """发一封纯文本邮件。返回 (成功, 错误描述)。"""
    import smtplib
    from email.mime.text import MIMEText
    from email.header import Header
    host = (cfg.get("host") or "").strip()
    user = (cfg.get("user") or "").strip()
    pwd = cfg.get("password") or ""
    sender = (cfg.get("sender") or "").strip() or user
    to = (cfg.get("to") or "").strip()
    if not host or not user or not pwd or not to:
        return False, "配置不完整（缺少服务器 / 账号 / 授权码 / 收件人）"
    try:
        port = int(cfg.get("port") or 465)
    except Exception:
        port = 465
    sec = str(cfg.get("secure") or "ssl").lower()
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = to
    srv = None
    try:
        if sec == "ssl":
            srv = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            srv = smtplib.SMTP(host, port, timeout=15)
            if sec == "starttls":
                srv.starttls()
        srv.login(user, pwd)
        rcpts = [x.strip() for x in to.split(",") if x.strip()]
        srv.sendmail(sender, rcpts, msg.as_string())
        return True, ""
    except Exception as e:
        return False, str(e)
    finally:
        if srv is not None:
            try:
                srv.quit()
            except Exception:
                pass


def notify_sct_send(cfg, title, desp):
    """Server酱：POST https://sctapi.ftqq.com/<SendKey>.send。返回 (成功, 错误描述)。"""
    key = (cfg.get("sct_key") or "").strip()
    if not key:
        return False, "未配置 SendKey"
    url = "https://sctapi.ftqq.com/%s.send" % key
    data = urllib.parse.urlencode({"title": title, "desp": desp}).encode()
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            j = json.loads(raw)
        except Exception:
            j = {}
        if j.get("code") == 0:
            return True, ""
        return False, str(j.get("message") or raw[:80] or "未知错误")
    except Exception as e:
        return False, str(e)


def notify_lan_ip():
    """本机局域网 IP（UDP connect 不实际发包），用来在通知里放一行详情链接。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except Exception:
        return ""


def notify_build(events, ts, port=8777):
    """生成通知标题与正文：按 新增 / 加重 / 恢复 分组。"""
    new = [e for e in events if e["act"] == "new"]
    worse = [e for e in events if e["act"] == "worse"]
    rec = [e for e in events if e["act"] == "recovered"]
    bad = len(new) + len(worse)
    if bad:
        subject = "[飞牛监控] 硬盘告警 · %d 块异常" % bad
    else:
        subject = "[飞牛监控] 硬盘告警解除 · %d 块恢复" % len(rec)
    lines = ["检测时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)), ""]

    def section(title, group):
        lines.append("【%s】" % title)
        for e in group:
            d = e["d"] or {}
            lines.append("%s · %s · %s · %s" % (d.get("dev") or "?", d.get("model") or "未知型号",
                                                d.get("serial") or "无序列号",
                                                GRADE_LABEL.get(d.get("grade"), d.get("grade") or "-")))
            lines.append("  %s" % (e.get("text") or ""))
            bits = []
            if d.get("defects") is not None:
                bits.append("增长缺陷 %s" % d["defects"])
            if d.get("pending_defects"):
                bits.append("待处理 %s" % d["pending_defects"])
            if d.get("uncorr"):
                bits.append("不可纠正 %s" % d["uncorr"])
            if d.get("temp_c") is not None:
                bits.append("%s°C" % d["temp_c"])
            if d.get("power_on_hours"):
                bits.append("通电 %s 小时" % d["power_on_hours"])
            if bits:
                lines.append("  " + " / ".join(bits))
            lines.append("")

    if new:
        section("新增故障", new)
    if worse:
        section("故障加重", worse)
    if rec:
        section("已恢复", rec)
    ip = notify_lan_ip()
    if ip:
        lines.append("详情：http://%s:%d" % (ip, port))
    return subject, "\n".join(lines)


def notify_state_load(data_dir):
    try:
        with open(os.path.join(data_dir, NOTIFY_STATE_FILE), "r", encoding="utf-8", errors="ignore") as f:
            j = json.load(f)
        return j if isinstance(j, dict) else {}
    except Exception:
        return {}


def notify_send_all(cfg, subject, body):
    """按已启用的通道发送，任一失败即视为失败（下一轮重试）。返回 (全部成功, 明细)。"""
    res = {"mail": None, "sct": None}
    ok_all = True
    if cfg.get("enabled"):
        ok, err = notify_mail_send(cfg, subject, body)
        res["mail"] = {"ok": ok, "err": "" if ok else notify_short_err(err)}
        ok_all = ok_all and ok
    if cfg.get("sct_enabled"):
        ok, err = notify_sct_send(cfg, subject, body)
        res["sct"] = {"ok": ok, "err": "" if ok else notify_short_err(err)}
        ok_all = ok_all and ok
    return ok_all, res


def notify_after_scan(items, ts, db, config=None):
    """巡检后按状态变化推送。返回通知摘要（未发或未配置返回 None），异常不外抛。"""
    try:
        data_dir = os.path.dirname(getattr(db, "db_path", "") or "")
        if not data_dir:
            return None
        cfg = notify_load(data_dir)
        if not (cfg.get("enabled") or cfg.get("sct_enabled")):
            return None
        st = notify_state_load(data_dir)
        notified = dict(st.get("notified") or {})
        cur = {}
        events = []
        for it in items or []:
            dev = str(it.get("dev") or "")
            if not dev or it.get("standby"):
                continue                      # 休眠盘沿用旧分级，不参与判定
            grade = str(it.get("grade") or "")
            if grade in ("warn", "crit"):
                cur[dev] = grade
            old = str(notified.get(dev) or "")
            if grade in ("warn", "crit"):
                if old == grade:
                    continue
                if old == "crit" and grade == "warn":
                    continue                  # 降级单独提醒太吵，静默更新状态
                events.append({"act": "worse" if old == "warn" else "new",
                               "dev": dev, "d": it,
                               "text": it.get("detail") or GRADE_LABEL.get(grade, grade)})
            elif grade == "ok" and old in ("warn", "crit"):
                events.append({"act": "recovered", "dev": dev, "d": it,
                               "text": "故障消失，SMART 通过"})
        if not events:
            if cur != notified:
                write_json_secure(os.path.join(data_dir, NOTIFY_STATE_FILE),
                                  {"notified": cur, "ts": ts})
            return None
        try:
            port = int((config or {}).get("port") or 0) or 8777
        except Exception:
            port = 8777
        subject, body = notify_build(events, ts, port)
        ok, res = notify_send_all(cfg, subject, body)
        summary = {"ts": ts, "ok": ok, "subject": subject,
                   "acts": [e["act"] for e in events], "channels": res}
        if ok:
            write_json_secure(os.path.join(data_dir, NOTIFY_STATE_FILE),
                              {"notified": cur, "ts": ts, "last": summary})
        else:
            # 发送失败：保留旧状态，下一轮巡检重试同一批事件
            write_json_secure(os.path.join(data_dir, NOTIFY_STATE_FILE),
                              {"notified": notified, "ts": st.get("ts") or 0, "last": summary})
            print("[fnmonitor] 硬盘告警通知发送失败：%s" % res)
        return summary
    except Exception:
        traceback.print_exc()
        return None


def notify_test(data_dir, channel="all"):
    """设置页「发送测试」：发一条样例消息验证单个通道（mail / sct）。

    默认 all 只在巡检推送路径与旧前端上使用；设置页每个通道一个按钮，传 channel
    就只发那一条，避免「点一次邮件测试、微信也跟着响」。
    """
    cfg = notify_load(data_dir)
    ch = str(channel or "all").strip().lower()
    if ch not in ("mail", "sct"):
        ch = "all"
    label = {"mail": "邮件", "sct": "微信"}
    if ch == "all":
        want = [c for c, k in (("mail", "enabled"), ("sct", "sct_enabled")) if cfg.get(k)]
        if not want:
            return {"ok": False, "error": "请先开启邮件或微信通知并保存", "channels": {}, "channel": ch}
    else:
        if not cfg.get("enabled" if ch == "mail" else "sct_enabled"):
            return {"ok": False, "channels": {}, "channel": ch,
                    "error": "%s通知当前是关闭的：先打开开关并「保存」，再点发送测试" % label[ch]}
        want = [ch]
    sample = [{"dev": "sdz", "model": "ST300MP0026（示例）", "serial": "WAE0J50J", "grade": "warn",
               "detail": "增长缺陷 49 个；待处理扇区 0 个", "defects": 49, "pending_defects": 0,
               "uncorr": 0, "temp_c": 32, "power_on_hours": 52032}]
    subject, body = notify_build([{"act": "new", "dev": "sdz", "d": sample[0],
                                   "text": sample[0]["detail"]}], time.time())
    subject = "【测试】" + subject
    body = "这是一条来自飞牛监控「设置 → 通知」的测试消息，收到即表示%s通道配置可用。\n\n" % (
        "邮件" if ch == "mail" else "微信") + body
    res = {}
    for c in want:
        if c == "mail":
            ok, err = notify_mail_send(cfg, subject, body)
        else:
            ok, err = notify_sct_send(cfg, subject, body)
        res[c] = {"ok": ok, "err": "" if ok else notify_short_err(err)}
    ok = all(v.get("ok") for v in res.values())
    return {"ok": ok, "channels": res, "channel": ch,
            "error": "" if ok else "；".join(
                "%s：%s" % (label[k], v.get("err")) for k, v in res.items() if v and not v.get("ok"))}


def read_net_dev():
    """读取 /proc/net/dev，返回接口累计字节。"""
    out = {}
    for line in read_text("/proc/net/dev").splitlines():
        if ":" not in line:
            continue
        iface, _, rest = line.partition(":")
        iface = iface.strip()
        fields = rest.split()
        if len(fields) < 16:
            continue
        try:
            out[iface] = {
                "rx_bytes": int(fields[0]),
                "rx_packets": int(fields[1]),
                "rx_errs": int(fields[2]),
                "rx_drop": int(fields[3]),
                "tx_bytes": int(fields[8]),
                "tx_packets": int(fields[9]),
                "tx_errs": int(fields[10]),
                "tx_drop": int(fields[11]),
            }
        except Exception:
            pass
    return out


def collect_net(prev, prev_ts, now):
    """计算各接口速率。prev: 上次 net_dev；返回 (最新快照, 用于下一次的 prev)。"""
    cur = read_net_dev()
    ifaces = []
    for name, c in cur.items():
        if name == "lo":
            continue
        rate_rx = 0.0
        rate_tx = 0.0
        if prev and name in prev and prev_ts:
            dt = now - prev_ts
            if dt > 0:
                rate_rx = max(0, (c["rx_bytes"] - prev[name]["rx_bytes"])) / dt
                rate_tx = max(0, (c["tx_bytes"] - prev[name]["tx_bytes"])) / dt
        ifaces.append({
            "iface": name,
            "rx_bytes": c["rx_bytes"], "tx_bytes": c["tx_bytes"],
            "rx_rate": round(rate_rx, 1), "tx_rate": round(rate_tx, 1),
            "rx_packets": c["rx_packets"], "tx_packets": c["tx_packets"],
            "rx_drop": c.get("rx_drop", 0), "tx_drop": c.get("tx_drop", 0),
            "rx_errs": c.get("rx_errs", 0), "tx_errs": c.get("tx_errs", 0),
        })
    ifaces.sort(key=lambda x: -(x["rx_rate"] + x["tx_rate"]))
    return ifaces


def read_diskstats():
    """读取各物理磁盘累计 IO 计数（/proc/diskstats 优先，失败时回退 /sys/block/*/stat）。"""
    txt = read_text("/proc/diskstats")
    if txt:
        out = {}
        for line in txt.splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if name.startswith("loop") or name.startswith("ram"):
                continue
            try:
                out[name] = {
                    "reads": int(parts[3]),
                    "sectors_read": int(parts[5]),
                    "writes": int(parts[7]),
                    "sectors_written": int(parts[9]),
                    "in_progress": int(parts[11]),
                }
            except Exception:
                pass
        return out
    # 兜底：/sys/block/*/stat（字段：reads sectors_read writes sectors_written io_ticks ...）
    out = {}
    try:
        for name in sorted(os.listdir("/sys/block")):
            if name.startswith("loop") or name.startswith("ram"):
                continue
            st = read_text(os.path.join("/sys/block", name, "stat"))
            parts = st.split()
            if len(parts) < 8:
                continue
            try:
                out[name] = {
                    "reads": int(parts[0]),
                    "sectors_read": int(parts[2]),
                    "writes": int(parts[4]),
                    "sectors_written": int(parts[6]),
                    "in_progress": int(parts[8]) if len(parts) > 8 else 0,
                }
            except Exception:
                pass
    except Exception:
        pass
    return out


def collect_disk_io(prev, prev_ts, now):
    """计算各磁盘读写速率（B/s）与 IOPS。"""
    cur = read_diskstats()
    result = []
    for name, c in cur.items():
        r_rate = w_rate = r_iops = w_iops = 0.0
        if prev and name in prev and prev_ts and now > prev_ts:
            dt = now - prev_ts
            r_rate = max(0, (c["sectors_read"] - prev[name]["sectors_read"])) * 512.0 / dt
            w_rate = max(0, (c["sectors_written"] - prev[name]["sectors_written"])) * 512.0 / dt
            r_iops = max(0, (c["reads"] - prev[name]["reads"])) / dt
            w_iops = max(0, (c["writes"] - prev[name]["writes"])) / dt
        result.append({
            "name": name,
            "read_rate": round(r_rate, 1), "write_rate": round(w_rate, 1),
            "read_iops": round(r_iops, 1), "write_iops": round(w_iops, 1),
            "in_progress": c["in_progress"],
        })
    result.sort(key=lambda x: -(x["read_rate"] + x["write_rate"]))
    return result


def smartctl_temp(dev):
    """通过 smartctl 读取硬盘温度（sysfs 无 hwmon 时的兜底）。

    -n standby：盘休眠时直接退出（exit 2），绝不为读温度把盘唤醒。"""
    out, rc = run_cmd(["smartctl", "-n", "standby", "-A", "/dev/" + dev], timeout=8)
    if rc == 2:
        return None
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        if "Temperature_Celsius" in line:
            # 标准列序：ID# NAME FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE
            # RAW_VALUE 位于第 10 列（parts[9]），且可能是 "42 (Min/Max 20/50)" 这种形式
            parts = line.split()
            if len(parts) >= 10:
                m = re.match(r"(\d+)", parts[9])
                if m:
                    return int(m.group(1))
        if line.strip().startswith("Temperature:"):
            parts = line.split()
            try:
                return int(parts[1])  # NVMe 格式
            except Exception:
                pass
        st = line.strip()
        if st.startswith("Current Drive Temperature:") or st.startswith("Drive Temperature:"):
            # SAS/SCSI 格式：Current Drive Temperature:     33 C
            m = re.search(r"(-?\d+)\s*C", st)
            if m:
                return int(m.group(1))
    return None


def read_temps():
    """读取 CPU / 主板 / 硬盘温度（摄氏度）。
    返回 {"cpu": 数值或None, "system": 数值或None, "disks": [{"name","temp"}]}
    """
    temps = {"cpu": None, "system": None, "disks": []}
    # ---- CPU / 主板 thermal zone ----
    base = "/sys/class/thermal"
    if os.path.isdir(base):
        for d in sorted(os.listdir(base)):
            if not d.startswith("thermal_zone"):
                continue
            tval = read_int_file(os.path.join(base, d, "temp"))
            ttype = read_text(os.path.join(base, d, "type")).strip().lower()
            if tval <= 0:
                continue
            t = round(tval / 1000.0, 1)
            if "x86_pkg" in ttype or "cpu" in ttype or "soc" in ttype:
                temps["cpu"] = max(temps["cpu"] or 0, t)
            elif "acpitz" in ttype or ttype.startswith("ec"):
                temps["system"] = max(temps["system"] or 0, t)
            elif temps["cpu"] is None and ttype not in ("battery",):
                temps["system"] = max(temps["system"] or 0, t)
    # ---- hwmon 兜底 CPU（无 thermal zone 时）----
    if temps["cpu"] is None:
        hw = "/sys/class/hwmon"
        if os.path.isdir(hw):
            for h in sorted(os.listdir(hw)):
                hd = os.path.join(hw, h)
                ttype = read_text(os.path.join(hd, "name")).strip().lower()
                if "coretemp" in ttype or "k10temp" in ttype or ttype == "cpu":
                    t = read_int_file(os.path.join(hd, "temp1_input"))
                    if t > 0:
                        temps["cpu"] = round(t / 1000.0, 1)
                        break
    # ---- 硬盘温度 ----
    # 休眠保护：已 STANDBY 的硬盘跳过 hwmon drivetemp 读取（旧内核该读取会
    # 唤醒硬盘）与 smartctl 兜底，沿用上次活跃时缓存的温度；休眠中的盘温度
    # 不变，展示缓存值不影响面板。
    states = disk_power_states()
    try:
        for blk in sorted(os.listdir("/sys/block")):
            if not _WHOLE_DISK_RE.match(blk):
                continue
            if states.get(blk) == "standby":
                cached = _DISK_TEMP_CACHE.get(blk)
                if cached is not None:
                    temps["disks"].append({"name": blk, "temp": cached})
                continue
            temp = None
            hwdir = os.path.join("/sys/block", blk, "device", "hwmon")
            if os.path.isdir(hwdir):
                for hw in sorted(os.listdir(hwdir)):
                    tfile = os.path.join(hwdir, hw, "temp1_input")
                    if os.path.isfile(tfile):
                        t = read_int_file(tfile)
                        if t > 0:
                            temp = round(t / 1000.0, 1)
                            break
            if temp is None:
                temp = smartctl_temp(blk)
            if temp:
                _DISK_TEMP_CACHE[blk] = temp
                temps["disks"].append({"name": blk, "temp": temp})
    except Exception:
        pass
    return temps


_FNOS_VERSION_RE = re.compile(r"^\d[\w.+~-]{0,31}$")


def read_fnos_version():
    """飞牛 fnOS 版本号（如 1.2.0800）。/etc/os-release 是底层 Debian，不用它：
    先读 /usr/trim/etc/version，读不到再找 dpkg 里 trim 包（Fn NAS）的版本；都没有返回空串，前端不显示。"""
    v = read_text("/usr/trim/etc/version").strip()
    if _FNOS_VERSION_RE.match(v):
        return v
    try:
        with open("/var/lib/dpkg/status", "r", errors="ignore") as f:
            hit = False
            for line in f:
                if line.startswith("Package: "):
                    hit = line[9:].strip() == "trim"
                elif hit and line.startswith("Version: "):
                    v = line[9:].strip()
                    return v if _FNOS_VERSION_RE.match(v) else ""
    except Exception:
        pass
    return ""


def system_info(hostname_override=None):
    info = {}
    info["hostname"] = hostname_override or socket.gethostname()
    info["kernel"] = os.uname().release if hasattr(os, "uname") else ""
    info["arch"] = os.uname().machine if hasattr(os, "uname") else ""
    info["uptime"] = read_uptime()
    info["boot_time"] = time.time() - info["uptime"] if info["uptime"] else 0
    info["cpu_model"] = read_cpu_model()
    info["fnos_version"] = read_fnos_version()
    # 内核版本与系统版本
    kv = read_text("/proc/version").strip()
    info["kernel_version"] = kv[:120] if kv else ""
    # 本机 IP
    info["ips"] = collect_ips()
    return info


def collect_ips():
    """采集每张网卡的 IPv4 / IPv6 地址。
    过滤：回环 lo、Docker / 虚拟网桥接口（docker*/veth*/br-*/virbr*/vnet*/tun*/tap*）、
    172.x 网桥网段、链路本地 169.254 / fe80:: 等无意义地址。
    返回 [{"iface":"eth0","ipv4":[...],"ipv6":[...]}, ...]"""
    result = {}
    # 优先用 ip -o addr（带接口名与地址族）
    out, rc = run_cmd(["ip", "-o", "addr"], timeout=5)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            iface = parts[1].strip().split("@")[0]
            fam = parts[2]
            addr = parts[3].split("/")[0]
            if not iface or iface == "lo":
                continue
            if iface.startswith(("docker", "veth", "virbr", "br-", "vnet", "tun", "tap", "vxlan", "wg")):
                continue
            if fam == "inet":
                if not _ip_show(addr):
                    continue
                result.setdefault(iface, {"iface": iface, "ipv4": [], "ipv6": []})["ipv4"].append(addr)
            elif fam == "inet6":
                a6 = addr.split("%")[0].lower()
                if a6.startswith("fe80") or a6 in ("::1", "::"):
                    continue  # 链路本地 / 回环
                result.setdefault(iface, {"iface": iface, "ipv4": [], "ipv6": []})["ipv6"].append(a6)
    # 兜底：hostname -I（无接口名，归到 eth0）
    if not result:
        out2, rc2 = run_cmd(["hostname", "-I"], timeout=5)
        if rc2 == 0:
            e = {"iface": "eth0", "ipv4": [], "ipv6": []}
            for tok in out2.split():
                tok = tok.strip()
                if not tok:
                    continue
                if ":" in tok:
                    a6 = tok.split("%")[0].lower()
                    if a6.startswith("fe80") or a6 in ("::1", "::"):
                        continue
                    e["ipv6"].append(a6)
                elif _ip_show(tok):
                    e["ipv4"].append(tok)
            if e["ipv4"] or e["ipv6"]:
                result["eth0"] = e
    return list(result.values())


def _ip_show(addr):
    """IPv4 地址是否展示：过滤回环、Docker 网桥网段（172.16.0.0/12）、链路本地等。"""
    try:
        if not addr or ":" in addr:
            return False  # 过滤 IPv6
        if addr.startswith("127."):
            return False
        if addr.startswith("172."):
            return False  # 过滤 Docker 网桥等 172.x
        if addr.startswith("169.254."):
            return False  # 过滤链路本地
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Docker 采集
# ---------------------------------------------------------------------------
_DK_UNIT_ZH = {"second": "秒", "minute": "分钟", "hour": "小时", "day": "天", "week": "周",
               "month": "个月", "year": "年"}
_DK_DUR_RE = re.compile(r"(?:(\d+)|an?)\s+(second|minute|hour|day|week|month|year)s?\b", re.I)


def _docker_dur_zh(text):
    """go-units HumanDuration（docker / podman 的 Status 时长）→ 中文。

    覆盖全部形式：'Less than a second' / '1 second' / '5 seconds' / 'About a minute' /
    '3 minutes' / 'About an hour' / '10 hours' / '3 days' / '2 weeks' / '4 months' / '2 years'。
    无法识别时返回 ''（调用方据此只显示状态，不残留英文）。"""
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    if low.startswith("less than a second"):
        return "不到 1 秒"
    about = low.startswith("about ")
    m = _DK_DUR_RE.search(t)
    if not m:
        return ""
    n = int(m.group(1)) if m.group(1) else 1
    unit = _DK_UNIT_ZH[m.group(2).lower()]
    return ("约 " if about else "") + "%d %s" % (n, unit)


_DK_HEALTH_ZH = {"healthy": "健康", "unhealthy": "不健康", "health: starting": "健康检查中"}


def parse_docker_status(status):
    """解析 docker ps 的 Status 字段 → dict（全部中文，不残留英文单位）。

    docker 的全部形式（moby container/state.go）：
      'Up 3 days' / 'Up About an hour' / 'Up 5 seconds (healthy)' / 'Up 2 hours (Paused)' /
      'Up 1 minute (health: starting)' / 'Restarting (1) 3 seconds ago' /
      'Exited (0) 2 weeks ago' / 'Created' / 'Dead' / 'Removal In Progress'；
    podman 另有 'Paused' / 'Stopped' / 'Configured' / 'Initialized'。
    返回 {uptime, status_zh, health, exit_code, dur}：
      uptime   列表用短文本（如「运行 3 天」「已停止 2 周」）
      status_zh 详情用完整文本（如「运行 3 天 · 健康」「已停止（退出码 0）· 2 周前」）"""
    s = (status or "").strip()
    out = {"uptime": "", "status_zh": "", "health": None, "exit_code": None, "dur": ""}
    if not s:
        return out
    low = s.lower()
    hm = re.search(r"\((healthy|unhealthy|health: starting)\)", low)
    if hm:
        out["health"] = {"healthy": "healthy", "unhealthy": "unhealthy"}.get(hm.group(1), "starting")
    health_zh = _DK_HEALTH_ZH.get(hm.group(1)) if hm else ""
    paused = "(paused)" in low
    em = re.search(r"\((-?\d+)\)", s)
    code = int(em.group(1)) if em and not low.startswith("up") else None
    out["exit_code"] = code
    body = re.sub(r"\s*\([^)]*\)", "", s).strip()          # 去掉 (healthy) / (0) 等括号
    ago = bool(re.search(r"\bago$", body, re.I))
    body = re.sub(r"\s+ago$", "", body, flags=re.I)
    if low.startswith("up "):
        dur = _docker_dur_zh(body[3:])
        out["dur"] = dur
        if paused:
            out["uptime"] = "已暂停" + (" · 已运行 " + dur if dur else "")
            out["status_zh"] = out["uptime"]
        else:
            out["uptime"] = ("运行 " + dur) if dur else "运行中"
            out["status_zh"] = out["uptime"] + (" · " + health_zh if health_zh else "")
        return out
    first = low.split(" ", 1)[0]
    rest = body.split(" ", 1)[1] if " " in body else ""
    dur = _docker_dur_zh(rest)
    out["dur"] = dur
    code_zh = "（退出码 %d）" % code if code is not None else ""
    if first in ("exited", "stopped"):
        out["uptime"] = "已停止" + (" " + dur if dur else "")
        out["status_zh"] = "已停止" + code_zh + (" · " + dur + ("前" if ago else "") if dur else "")
    elif first == "restarting":
        out["uptime"] = "重启中"
        out["status_zh"] = "重启中" + code_zh + (" · " + dur + "前退出" if dur else "")
    elif first == "paused":
        out["uptime"] = out["status_zh"] = "已暂停"
    elif first in ("created", "configured", "initialized"):
        out["uptime"] = out["status_zh"] = "已创建"
    elif first == "dead":
        out["uptime"] = out["status_zh"] = "已失效"
    elif low.startswith("removal"):
        out["uptime"] = out["status_zh"] = "删除中"
    else:
        # 未知形式：不回显英文原文，交给前端用 state 兜底
        out["uptime"] = out["status_zh"] = ""
    return out


def _docker_uptime_zh(status):
    """把 docker Status（如 'Up 3 hours' / 'Exited (0) 5 minutes ago'）转成中文运行时长。"""
    return parse_docker_status(status)["uptime"]


def collect_docker():
    result = {"available": False, "error": "", "containers": [], "images": 0}
    out, rc = run_cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=20)
    # 输出为空 = 已安装但还没有容器，不能当成不可用
    if rc != 0:
        result["error"] = "docker 命令不可用或无权限（请确认应用以 root 运行）"
        return result
    result["available"] = True
    # 镜像数量（Docker 首页计数卡）
    iout, irc = run_cmd(["docker", "images", "-q"], timeout=15)
    result["images"] = len([x for x in iout.splitlines() if x.strip()]) if irc == 0 else -1
    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
        except Exception:
            continue
        st = parse_docker_status(c.get("Status") or "")
        nets_ = [x.strip() for x in str(c.get("Networks") or "").split(",") if x.strip()]
        containers.append({
            "id": (c.get("ID") or "")[:12],
            "name": (c.get("Names") or "").lstrip("/"),
            "image": c.get("Image") or "",
            "state": c.get("State") or "",
            "status": c.get("Status") or "",
            "uptime": st["uptime"],
            "status_zh": st["status_zh"],
            "health": st["health"],
            "exit_code": st["exit_code"],
            "ports": c.get("Ports") or "",
            "created": c.get("CreatedAt") or "",
            "networks": nets_,
            # host 网络模式与宿主机共用网卡，docker 不单独统计其流量（速率恒为 None）
            "net_host": nets_ == ["host"],
            "cpu": "", "mem_usage": "", "mem_percent": "", "net": "",
            "rx_rate": None, "tx_rate": None, "net_src": None,
        })
    # 运行中容器的资源占用
    sout, src = run_cmd(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=40)
    if src == 0:
        stats_map = {}
        for line in sout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                s = json.loads(line)
                stats_map[(s.get("ID") or "")[:12]] = s
            except Exception:
                continue
        for c in containers:
            s = stats_map.get(c["id"])
            if s:
                c["cpu"] = str(s.get("CPUPerc", "")).replace("%", "").strip()
                c["mem_usage"] = s.get("MemUsage", "")
                c["mem_percent"] = str(s.get("MemPerc", "")).replace("%", "").strip()
                c["net"] = s.get("NetIO", "")
                in_b, out_b = parse_docker_netio(s.get("NetIO", ""))
                c["net_in_bytes"] = in_b
                c["net_out_bytes"] = out_b
                if not c["net_host"] and c["state"] == "running":
                    c["net_src"] = "stats"
        # 运行中容器的精确网络计数（docker inspect 取 PID → /proc/<pid>/net/dev），失败回退 stats
        run_ids = [c["id"] for c in containers if c["state"] == "running" and not c["net_host"]]
        if run_ids and is_linux():
            iout, irc = run_cmd(["docker", "inspect", "--format", "{{.Id}} {{.State.Pid}}"] + run_ids[:200], timeout=20)
            pids = {}
            for line in (iout or "").splitlines():
                parts = line.split()
                if len(parts) == 2 and re.match(r"^[0-9a-f]{64}$", parts[0]) and parts[1].isdigit():
                    pids[parts[0][:12]] = (parts[0], parts[1])
            for c in containers:
                ent = pids.get(c["id"])
                got = _proc_netns_bytes(ent[1], ent[0]) if ent else None
                if got:
                    c["net_in_bytes"], c["net_out_bytes"] = got
                    c["net_src"] = "proc"
    result["sampled_at"] = time.time()
    # 状态排序：运行中优先
    order = {"running": 0, "restarting": 1, "paused": 2, "exited": 3, "created": 4, "dead": 5}
    containers.sort(key=lambda c: order.get(c["state"], 9))
    result["containers"] = containers
    return result


def docker_action(container_id, action):
    """对 Docker 容器执行 start / restart / stop。"""
    if action not in ("start", "restart", "stop"):
        return {"ok": False, "error": "不支持的操作: %s" % action}
    # 只允许容器 ID（十六进制）或合法容器名，避免命令注入
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", container_id):
        return {"ok": False, "error": "非法的容器标识"}
    out, rc = run_cmd(["docker", action, container_id], timeout=60)
    if rc == 0:
        return {"ok": True, "action": action, "container": container_id}
    return {"ok": False, "action": action, "container": container_id,
            "error": (out or "").strip()[-500:]}


# ---------------------------------------------------------------------------
# 虚拟机（飞牛「虚拟机」基于 QEMU/KVM + libvirt，统一通过 virsh 命令行读取与操作）
# ---------------------------------------------------------------------------
# virsh domstats 的 state.state 枚举（virDomainState）
VM_STATES = {0: "nostate", 1: "running", 2: "blocked", 3: "paused", 4: "shutdown",
             5: "shutoff", 6: "crashed", 7: "pmsuspended"}
VM_STATE_ZH = {"nostate": "未知", "running": "运行中", "blocked": "运行中", "paused": "已挂起",
               "shutdown": "正在关机", "shutoff": "已关机", "crashed": "已崩溃", "pmsuspended": "已休眠"}
VM_STATE_ORDER = {"running": 0, "blocked": 0, "paused": 1, "shutdown": 2, "pmsuspended": 3,
                  "crashed": 4, "shutoff": 5, "nostate": 6}
VM_LIVE_STATES = ("running", "blocked", "paused")   # 有 QEMU 进程、计数器在走的状态
VM_ACTIONS = ("start", "shutdown", "reboot", "suspend", "resume", "destroy")
_VM_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MAC_RE = re.compile(r"^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$")
# libvirt XML 内存单位（缺省 KiB）
_XML_MEM_UNITS = {"b": 1, "bytes": 1, "kb": 1000, "k": 1024, "kib": 1024, "mb": 1000 ** 2, "m": 1024 ** 2,
                  "mib": 1024 ** 2, "gb": 1000 ** 3, "g": 1024 ** 3, "gib": 1024 ** 3, "tb": 1000 ** 4,
                  "t": 1024 ** 4, "tib": 1024 ** 4}


def virsh_path():
    """virsh 可执行文件路径；应用服务的 PATH 可能不含 sbin 等目录，逐个常见目录兜底。"""
    p = shutil.which("virsh")
    if p:
        return p
    for d in ("/usr/bin", "/usr/sbin", "/usr/local/bin", "/usr/local/sbin", "/bin", "/sbin"):
        f = os.path.join(d, "virsh")
        if os.path.isfile(f) and os.access(f, os.X_OK):
            return f
    return ""


def run_virsh(args, timeout=15):
    """执行 virsh，返回 (stdout, stderr, returncode)。固定 LC_ALL=C，输出格式不随系统语言变化。"""
    exe = virsh_path()
    if not exe:
        return "", "未找到 virsh", -1
    env = dict(os.environ, LC_ALL="C", LANG="C")
    try:
        r = subprocess.run([exe] + list(args), shell=False, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "virsh 执行超时", -1
    except Exception as e:
        return "", str(e), -1


def virsh_error_text(stderr):
    """virsh 报错去掉 'error: ' 前缀、多行合并，限制长度。"""
    lines = [re.sub(r"^error:\s*", "", x.strip()) for x in (stderr or "").splitlines() if x.strip()]
    return "；".join(lines)[-300:]


def parse_domstats(text):
    """解析 `virsh domstats --raw` → [(域名, {键: 值字符串})]，保持输出顺序。"""
    out, cur = [], None
    for line in (text or "").splitlines():
        if line.startswith("Domain: "):
            name = line[len("Domain: "):].strip()
            if len(name) >= 2 and name[0] == name[-1] == "'":
                name = name[1:-1]
            cur = {}
            out.append((name, cur))
        elif cur is not None and "=" in line:
            k, _, v = line.strip().partition("=")
            cur[k] = v
    return out


def _stat_int(st, key):
    try:
        return int(st[key])
    except Exception:
        return None


def domstats_state(st):
    return VM_STATES.get(_stat_int(st, "state.state"), "nostate")


def domstats_counters(st):
    """累计计数 (cpu 纳秒, 磁盘读字节, 磁盘写字节, 网络收字节, 网络发字节)；虚拟机未运行时为 None。

    libvirt 9 对已关机的虚拟机也会给出上次运行留下的 cpu.time / block 计数，必须按状态判断。"""
    cpu = _stat_int(st, "cpu.time")
    if cpu is None or domstats_state(st) not in VM_LIVE_STATES:
        return None
    rd = wr = rx = tx = 0
    for i in range(_stat_int(st, "block.count") or 0):
        rd += _stat_int(st, "block.%d.rd.bytes" % i) or 0
        wr += _stat_int(st, "block.%d.wr.bytes" % i) or 0
    for i in range(_stat_int(st, "net.count") or 0):
        rx += _stat_int(st, "net.%d.rx.bytes" % i) or 0
        tx += _stat_int(st, "net.%d.tx.bytes" % i) or 0
    return (cpu, rd, wr, rx, tx)


def _xml_mem_bytes(el):
    if el is None:
        return None
    try:
        return int(int((el.text or "").strip()) * _XML_MEM_UNITS.get((el.get("unit") or "KiB").lower(), 1024))
    except Exception:
        return None


def parse_domain_xml(xml_text):
    """从 `virsh dumpxml` 提取展示信息：标识、规格、固件、磁盘、网卡、图形控制台、guest agent。"""
    info = {"uuid": "", "title": "", "description": "", "vcpus": None, "mem_max": None, "arch": "",
            "machine": "", "firmware": "", "disks": [], "nics": [], "graphics": [],
            "agent": False, "agent_connected": False, "os": "", "fn_autostart": None}
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return info
    txt = lambda p: (root.findtext(p) or "").strip()
    info["uuid"] = txt("uuid").lower()
    info["title"] = txt("title")[:120]
    info["description"] = txt("description")[:500]
    # 飞牛虚拟机把系统类型 / 版本 / 开机自启写在 <metadata><customMeta> 里（各元素带自定义命名空间），
    # 开机自启由飞牛自己管理，libvirt 的 autostart 恒为关闭
    meta = {}
    for el in root.iterfind("metadata//*"):
        meta.setdefault(el.tag.rsplit("}", 1)[-1], (el.text or "").strip())
    info["os"] = " ".join(x for x in ({"linux": "Linux", "windows": "Windows", "other": "其他"}.get(
        meta.get("osType", "").lower(), meta.get("osType", "")), meta.get("osVersion", "")) if x)[:80]
    if meta.get("autostart", "").lower() in ("true", "false"):
        info["fn_autostart"] = meta["autostart"].lower() == "true"
    vcpu = root.find("vcpu")
    if vcpu is not None:
        try:
            info["vcpus"] = int(vcpu.get("current") or (vcpu.text or "").strip())
        except Exception:
            pass
    info["mem_max"] = _xml_mem_bytes(root.find("memory"))
    os_el = root.find("os")
    if os_el is not None:
        t = os_el.find("type")
        if t is not None:
            info["arch"] = t.get("arch") or ""
            info["machine"] = t.get("machine") or ""
        loader = os_el.find("loader")
        fw = (os_el.get("firmware") or "").lower()
        info["firmware"] = "UEFI" if fw == "efi" or (loader is not None and loader.get("type") == "pflash") else "BIOS"
    for d in root.findall("devices/disk"):
        tgt, src = d.find("target"), d.find("source")
        path = ""
        if src is not None:
            path = src.get("file") or src.get("dev") or src.get("name") or src.get("volume") or ""
        info["disks"].append({
            "dev": tgt.get("dev") if tgt is not None else "",
            "bus": tgt.get("bus") if tgt is not None else "",
            "device": d.get("device") or "disk",
            "path": path,
            "readonly": d.find("readonly") is not None,
            "capacity": None, "allocation": None,
        })
    for n in root.findall("devices/interface"):
        mac, src, model, tgt = n.find("mac"), n.find("source"), n.find("model"), n.find("target")
        source = ""
        if src is not None:
            source = src.get("bridge") or src.get("network") or src.get("dev") or ""
        info["nics"].append({
            "mac": (mac.get("address") or "").lower() if mac is not None else "",
            "type": n.get("type") or "",
            "source": source,
            "model": model.get("type") if model is not None else "",
            "target": tgt.get("dev") if tgt is not None else "",
            "ips": [],
        })
    for g in root.findall("devices/graphics"):
        listen = g.get("listen") or ""
        le = g.find("listen")
        if not listen and le is not None:
            listen = le.get("address") or ""
        try:
            port = int(g.get("port") or -1)
        except Exception:
            port = -1
        # 飞牛的 VNC 走本机 unix 套接字（socket 属性），没有 TCP 端口
        info["graphics"].append({"type": g.get("type") or "", "port": port if port > 0 else None, "listen": listen,
                                 "socket": g.get("socket") or ""})
    for ch in root.findall("devices/channel"):
        t = ch.find("target")
        if t is not None and t.get("name") == "org.qemu.guest_agent.0":
            info["agent"] = True
            info["agent_connected"] = t.get("state") == "connected"
    return info


def parse_domifaddr(text):
    """解析 `virsh domifaddr` 输出 → {MAC: [IP]}，去掉回环与 IPv6 链路本地地址。"""
    res, mac = {}, None
    for line in (text or "").splitlines():
        p = line.split()
        if len(p) < 4 or p[2] not in ("ipv4", "ipv6"):
            continue
        if _MAC_RE.match(p[1]):
            mac = p[1].lower()
        if not mac:
            continue
        ip = p[3].split("/")[0]
        if ip.startswith("127.") or ip == "::1" or ip.lower().startswith("fe80:"):
            continue
        lst = res.setdefault(mac, [])
        if ip not in lst:
            lst.append(ip)
    return res


def parse_domblkinfo(text):
    """`virsh domblkinfo` → (容量字节, 宿主实际占用字节)。实际占用取 Physical（镜像文件大小），没有再用 Allocation。"""
    vals = {}
    for line in (text or "").splitlines():
        k, _, v = line.partition(":")
        try:
            vals[k.strip().lower()] = int(v.strip())
        except Exception:
            pass
    phys = vals.get("physical")
    return vals.get("capacity"), phys if phys is not None else vals.get("allocation")


def arp_table():
    """/proc/net/arp：MAC → [IPv4]（只取已完成解析的条目）。桥接到宿主网卡的虚拟机靠它找 IP。"""
    res = {}
    for line in read_text("/proc/net/arp").splitlines()[1:]:
        p = line.split()
        if len(p) >= 4 and p[2] != "0x0" and p[3] != "00:00:00:00:00:00":
            res.setdefault(p[3].lower(), []).append(p[0])
    return res


def qemu_proc_info(name):
    """虚拟机 QEMU 进程的 (已运行秒数, 常驻内存字节)；读不到时对应项为 None。"""
    pid_s = read_text("/run/libvirt/qemu/%s.pid" % name).strip() if "/" not in name else ""
    if not pid_s.isdigit():
        return None, None
    pid = int(pid_s)
    up = rss = None
    try:
        stat = read_text("/proc/%d/stat" % pid)
        fields = stat[stat.rindex(")") + 2:].split()      # fields[0] 为第 3 列 state，第 22 列 starttime 即 [19]
        btime = next(int(l.split()[1]) for l in read_text("/proc/stat").splitlines() if l.startswith("btime "))
        up = max(0, int(time.time() - (btime + int(fields[19]) / os.sysconf("SC_CLK_TCK"))))
    except Exception:
        pass
    m = re.search(r"^VmRSS:\s+(\d+)\s*kB", read_text("/proc/%d/status" % pid), re.M)
    if m:
        rss = int(m.group(1)) * 1024
    return up, rss


class VmMonitor:
    """虚拟机采集：`virsh domstats` 一次拿到全部虚拟机的累计计数，与上次采样差分得到实时速率。

    按需采集（只有打开虚拟机页 / 详情时前端才轮询），4 秒内多客户端复用结果，不进后台采集循环；
    运行中的虚拟机没有新鲜的上次采样时间隔 1 秒连采两次，打开页面即有读数。
    dumpxml（磁盘 / 网卡 / 控制台）按虚拟机缓存 60 秒，状态变化时立即刷新。"""
    CACHE_TTL = 4
    PREV_MAX_AGE = 90
    XML_TTL = 60
    AUX_TTL = 30

    def __init__(self):
        self._lock = threading.Lock()
        self._cache = None
        self._cache_ts = 0.0
        self._prev = {}        # 域名 → (ts, cpu_ns, rd, wr, rx, tx)
        self._xml = {}         # 域名 → (ts, state, info)
        self._ips = {}         # 域名 → (ts, {mac: [ip]})：guest agent / DHCP 租约查询结果
        self._auto = (0.0, set())

    def invalidate(self):
        """虚拟机操作后调用：丢弃缓存，下次请求重新读取。"""
        with self._lock:
            self._cache = None
            self._xml = {}
            self._auto = (0.0, set())

    def collect(self):
        with self._lock:
            if self._cache is not None and time.time() - self._cache_ts < self.CACHE_TTL:
                return self._cache
            data = self._collect()
            self._cache, self._cache_ts = data, time.time()
            return data

    @staticmethod
    def _sample():
        out, err, rc = run_virsh(["domstats", "--raw"], timeout=20)
        return (parse_domstats(out) if rc == 0 else None), err, time.time()

    def _collect(self):
        res = {"available": False, "installed": bool(virsh_path()), "kvm": os.path.exists("/dev/kvm"),
               "error": "", "vms": [], "sampled_at": time.time()}
        if not res["installed"]:
            res["error"] = "未检测到 virsh：本机可能没有启用飞牛虚拟机功能"
            return res
        doms, err, ts = self._sample()
        if doms is None:
            res["error"] = virsh_error_text(err) or "无法连接 libvirt（虚拟机服务未运行？）"
            return res
        stale = [name for name, st in doms if domstats_counters(st) is not None
                 and ts - self._prev.get(name, (0.0,))[0] > self.PREV_MAX_AGE]
        if stale:
            self._prev = {name: (ts,) + c for name, c in ((n, domstats_counters(s)) for n, s in doms) if c}
            time.sleep(1.0)
            d2, _, ts2 = self._sample()
            if d2 is not None:
                doms, ts = d2, ts2
        res["available"] = True
        auto, arp, nxt = self._autostart(), arp_table(), {}
        vms = [self._build(name, st, ts, auto, arp, nxt) for name, st in doms]
        self._prev = nxt
        names = {name for name, _ in doms}
        self._xml = {k: v for k, v in self._xml.items() if k in names}
        self._ips = {k: v for k, v in self._ips.items() if k in names}
        vms.sort(key=lambda v: (VM_STATE_ORDER.get(v["state"], 9), (v["title"] or v["name"]).lower()))
        res["vms"] = vms
        res["sampled_at"] = ts
        return res

    def _autostart(self):
        ts, names = self._auto
        if time.time() - ts < self.AUX_TTL:
            return names
        out, _, rc = run_virsh(["list", "--all", "--autostart", "--name"], timeout=10)
        names = {x.strip() for x in out.splitlines() if x.strip()} if rc == 0 else set()
        self._auto = (time.time(), names)
        return names

    def _xml_info(self, name, state, need_blk):
        ent = self._xml.get(name)
        if ent and ent[1] == state and time.time() - ent[0] < self.XML_TTL:
            return ent[2]
        out, _, rc = run_virsh(["dumpxml", "--domain", name], timeout=10)
        info = parse_domain_xml(out) if rc == 0 else parse_domain_xml("")
        # 较老的 libvirt 对未运行的虚拟机不给 block 统计：逐块 domblkinfo 读镜像（随 XML 一起缓存）
        if need_blk and state not in VM_LIVE_STATES:
            for d in info["disks"]:
                if d["device"] == "disk" and d["dev"]:
                    bo, _, brc = run_virsh(["domblkinfo", "--domain", name, "--device", d["dev"]], timeout=10)
                    if brc == 0:
                        d["capacity"], d["allocation"] = parse_domblkinfo(bo)
        self._xml[name] = (time.time(), state, info)
        return info

    def _guest_ips(self, name, info):
        """ARP 查不到时的补充：guest agent 已连接 → 问虚拟机自己；libvirt NAT 网络 → 查 DHCP 租约。"""
        ent = self._ips.get(name)
        if ent and time.time() - ent[0] < self.AUX_TTL:
            return ent[1]
        found = {}
        sources = (["agent"] if info["agent_connected"] else []) + \
                  (["lease"] if any(n["type"] == "network" for n in info["nics"]) else [])
        for src in sources:
            out, _, rc = run_virsh(["domifaddr", "--domain", name, "--source", src], timeout=6)
            if rc == 0:
                for mac, ips in parse_domifaddr(out).items():
                    lst = found.setdefault(mac, [])
                    lst += [ip for ip in ips if ip not in lst]
        self._ips[name] = (time.time(), found)
        return found

    def _build(self, name, st, ts, auto, arp, nxt):
        state = domstats_state(st)
        running = state in VM_LIVE_STATES
        # domstats 的 block.N.name 对应 XML 的 target dev；libvirt 9 对已关机的虚拟机也会给容量。
        # 宿主实际占用取 physical（镜像文件大小）：运行中 qcow2 的 allocation 是本次开机写到的最高偏移，常为 0
        blk = {}
        for i in range(_stat_int(st, "block.count") or 0):
            cap = _stat_int(st, "block.%d.capacity" % i)
            if cap is not None:
                phys = _stat_int(st, "block.%d.physical" % i)
                blk[st.get("block.%d.name" % i, "")] = (cap, phys if phys is not None else _stat_int(st, "block.%d.allocation" % i))
        info = self._xml_info(name, state, need_blk=not blk)
        vm = {
            "uuid": info["uuid"], "name": name, "title": info["title"], "description": info["description"],
            "state": state, "state_zh": VM_STATE_ZH.get(state, state), "running": running,
            "autostart": name in auto or bool(info["fn_autostart"]), "os": info["os"],
            "vcpus": _stat_int(st, "vcpu.current") or info["vcpus"],
            "mem": None, "mem_max": info["mem_max"], "mem_rss": None, "mem_guest_used": None,
            "cpu": None, "cpu_cores": None, "rd_rate": None, "wr_rate": None, "rx_rate": None, "tx_rate": None,
            "rd_bytes": None, "wr_bytes": None, "rx_bytes": None, "tx_bytes": None, "uptime": None,
            "arch": info["arch"], "machine": info["machine"], "firmware": info["firmware"],
            "agent": info["agent"], "agent_connected": info["agent_connected"],
            "disks": [dict(d) for d in info["disks"]], "nics": [dict(n, ips=[]) for n in info["nics"]],
            "graphics": info["graphics"] if running else [], "ips": [],
        }
        for d in vm["disks"]:
            if d["dev"] in blk:
                d["capacity"], d["allocation"] = blk[d["dev"]]
        if not running:
            return vm
        kib = lambda k: (_stat_int(st, k) * 1024) if _stat_int(st, k) is not None else None
        vm["mem"] = kib("balloon.current") or info["mem_max"]
        vm["mem_max"] = kib("balloon.maximum") or info["mem_max"]
        avail, usable, unused = kib("balloon.available"), kib("balloon.usable"), kib("balloon.unused")
        if avail and (usable is not None or unused is not None):
            vm["mem_guest_used"] = max(0, avail - (usable if usable is not None else unused))
        up, rss = qemu_proc_info(name)
        vm["uptime"], vm["mem_rss"] = up, kib("balloon.rss") or rss
        # IP：先查宿主 ARP 表，查不到再问 guest agent / DHCP 租约
        for n in vm["nics"]:
            n["ips"] = list(arp.get(n["mac"], []))
        if any(n["mac"] and not n["ips"] for n in vm["nics"]):
            extra = self._guest_ips(name, info)
            for n in vm["nics"]:
                n["ips"] += [ip for ip in extra.get(n["mac"], []) if ip not in n["ips"]]
        vm["ips"] = [ip for n in vm["nics"] for ip in n["ips"]]
        # 实时速率：与上次采样的累计计数差分；计数回退（虚拟机重启）时本轮不给速率
        cur = domstats_counters(st)
        if cur is None:
            return vm
        vm["rd_bytes"], vm["wr_bytes"], vm["rx_bytes"], vm["tx_bytes"] = cur[1:]
        nxt[name] = (ts,) + cur
        p = self._prev.get(name)
        if p and 0.5 < ts - p[0] <= self.PREV_MAX_AGE and all(c >= q for c, q in zip(cur, p[1:])):
            dt = ts - p[0]
            cores = (cur[0] - p[1]) / dt / 1e9
            vm["cpu_cores"] = round(cores, 3)
            if vm["vcpus"]:
                vm["cpu"] = round(min(100.0, cores / vm["vcpus"] * 100), 1)
            vm["rd_rate"], vm["wr_rate"], vm["rx_rate"], vm["tx_rate"] = (
                round((c - q) / dt, 1) for c, q in zip(cur[1:], p[2:]))
            vm["rate_window"] = round(dt, 1)
        return vm


def vm_action(uuid, action):
    """对虚拟机执行 start / shutdown（ACPI 关机）/ reboot / suspend / resume / destroy（强制断电）。"""
    if action not in VM_ACTIONS:
        return {"ok": False, "error": "不支持的操作: %s" % action}
    # 只接受 UUID，避免命令注入与参数注入（UUID 不可能以 - 开头）
    if not _VM_UUID_RE.match(uuid or ""):
        return {"ok": False, "error": "非法的虚拟机标识"}
    out, err, rc = run_virsh([action, "--domain", uuid], timeout=60)
    if rc == 0:
        return {"ok": True, "action": action, "uuid": uuid}
    return {"ok": False, "action": action, "uuid": uuid,
            "error": virsh_error_text(err) or (out or "").strip()[-300:] or "virsh 执行失败"}


# ---------------------------------------------------------------------------
# 飞牛内置应用统计（相册 / 影视 / 音乐）
# ---------------------------------------------------------------------------
BUILTIN_APPS = [
    {"id": "photos", "name": "AI 相册", "appid": "trim.photos"},
    {"id": "media", "name": "飞牛影视", "appid": "trim.media"},
    {"id": "music", "name": "飞牛音乐", "appid": "trim.music"},
]


def _app_dirs(appid):
    """探测应用数据目录的所有可能位置。"""
    found = []
    for c in ("/usr/local/apps/@appdata/", "/vol1/@appdata/", "/vol2/@appdata/",
              "/vol3/@appdata/", "/vol4/@appdata/"):
        p = c + appid
        if os.path.isdir(p):
            found.append(p)
    return found


def _builtin_appdirs_sleeping():
    """内置应用（相册/影视/音乐）数据目录是否位于休眠硬盘上。

    周期性目录遍历 / DB COUNT 会唤醒已 STANDBY 的机械硬盘，休眠期间跳过本轮
    统计（用户手动点「更新」按钮时不受此限制）。先按挂载点判定休眠，避免
    isdir 元数据访问本身唤醒硬盘。"""
    roots = ("/usr/local/apps/@appdata", "/vol1/@appdata", "/vol2/@appdata",
             "/vol3/@appdata", "/vol4/@appdata")
    for a in BUILTIN_APPS:
        for c in roots:
            try:
                if path_on_standby(c + "/" + a["appid"]):
                    return True
            except Exception:
                pass
    return False


def _dir_size_fast(root, secs=4):
    """限时递归统计目录总大小（字节）。"""
    total = 0
    deadline = time.time() + secs
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() > deadline:
                break
            for fn in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, fn))
                except Exception:
                    pass
    except Exception:
        pass
    return total


def _find_dbs(root, secs=4):
    """递归查找目录下的 SQLite 数据库文件。"""
    dbs = []
    deadline = time.time() + secs
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            if time.time() > deadline:
                break
            for fn in filenames:
                if fn.endswith((".db", ".sqlite", ".sqlite3")):
                    dbs.append(os.path.join(dirpath, fn))
    except Exception:
        pass
    return dbs


def _read_app_db_stats(db_path):
    """只读打开 SQLite，自适应统计媒体条目数。返回 (count, total_bytes) 或 None。"""
    if not os.path.exists(db_path):
        return None
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True, timeout=5)
    except Exception:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        best = None
        for t in tables:
            tq = t.replace('"', '""')
            try:
                cur.execute('PRAGMA table_info("%s")' % tq)
                cols = [r[1].lower() for r in cur.fetchall()]
            except Exception:
                continue
            has_path = any(c in cols for c in
                           ("path", "file_path", "filename", "file_name", "uri", "src_path", "location", "filepath"))
            has_size = any(c in cols for c in
                           ("size", "file_size", "bytes", "length", "filesize"))
            if not (has_path and has_size):
                continue
            try:
                cur.execute('SELECT COUNT(*) FROM "%s"' % tq)
                cnt = cur.fetchone()[0] or 0
            except Exception:
                continue
            size_col = next((c for c in ("size", "file_size", "bytes", "length", "filesize") if c in cols), None)
            total = 0
            if size_col:
                try:
                    cur.execute('SELECT COALESCE(SUM("%s"),0) FROM "%s"' % (size_col, tq))
                    total = cur.fetchone()[0] or 0
                except Exception:
                    total = 0
            if cnt and (best is None or cnt > best[0]):
                best = (cnt, total)
        return best
    except Exception:
        return None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _wmo_text(code):
    """WMO 天气代码 -> 中文描述（含 emoji 图标）。"""
    if code is None:
        return "未知"
    code = int(code)
    if code == 0: return "☀️ 晴"
    if code == 1: return "🌤 大部晴朗"
    if code == 2: return "⛅ 局部多云"
    if code == 3: return "☁️ 阴"
    if code in (45, 48): return "🌫 雾"
    if code in (51, 53, 55): return "🌦 毛毛雨"
    if code in (56, 57): return "🌧 冻雨"
    if code in (61, 63, 65): return "🌧 小雨/中雨/大雨"
    if code in (66, 67): return "🌧 冻雨"
    if code in (71, 73, 75): return "🌨 降雪"
    if code == 77: return "❄️ 雪粒"
    if code in (80, 81, 82): return "🌦 阵雨"
    if code in (85, 86): return "🌨 阵雪"
    if code == 95: return "⛈ 雷暴"
    if code in (96, 99): return "⛈ 雷暴伴冰雹"
    return "🌡 " + str(code)


# 内置中国行政区划坐标库（省/市/县三级 3200+，来源阿里 DataV GeoAtlas，与 QWeather 同源）。
# Open-Meteo 地理编码基于 GeoNames，中国城市中文覆盖差（搜不到/错配同名地点），国内名称一律本地检索。
_CN_CITIES = None


def _cn_city_lookup(name):
    """本地检索中国城市坐标库，条目 [名称, 省份, 纬度, 经度]。
    匹配优先级：名称精确 > 名称前缀 > 名称包含（与 QWeather 一致，数据按省→市→县排序）。
    命中返回 (纬度, 经度, 显示名)，未命中返回 None。"""
    global _CN_CITIES
    if _CN_CITIES is None:
        try:
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "cities_cn.json"),
                      "r", encoding="utf-8") as f:
                _CN_CITIES = json.load(f)
        except Exception:
            _CN_CITIES = []
    q = (name or "").strip()
    if not q or not _CN_CITIES:
        return None
    for stage in (0, 1, 2):  # 0=精确 1=前缀 2=包含
        for it in _CN_CITIES:
            nm, prov = it[0], it[1]
            if (stage == 0 and nm == q) or (stage == 1 and nm.startswith(q)) or \
               (stage == 2 and q in nm):
                disp = nm if (not prov or prov == nm) else "%s（%s）" % (nm, prov)
                return (float(it[2]), float(it[3]), disp)
    return None


def fetch_weather(city_override=None):
    """获取实时天气（免费无需 key）。city_override 支持：空=公网IP自动定位；
    "城市名"（如 徐州 / Beijing）= 先查内置中国城市库，未命中走 Open-Meteo 地理编码；
    "lat,lon"（如 34.26,117.18）= 直接指定坐标。
    返回 {"ok":True,...} 或 {"ok":False,"error":...}。需外网访问，失败自动降级。"""
    import urllib.request
    import urllib.parse
    import json as _json

    def _get(url, timeout=8):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 fnmonitor"})
        return _json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "ignore"))

    try:
        # ---- 定位：自定义位置 优先 ----
        lat = lon = None
        city = ""
        override = clean_text(city_override or "", 64)
        if override:
            if "," in override:
                # 坐标模式：只接受「纬度,经度」两段数字，显示名为规范化后的坐标串
                # （绝不回显原始输入，防止存储型 XSS）
                ll = parse_coords(override)
                if not ll:
                    return {"ok": False, "error": "坐标格式应为 纬度,经度（如 34.26,117.18）"}
                lat, lon = ll
                city = "%.4f,%.4f" % (lat, lon)
            else:
                # 城市名 → 内置中国城市库本地检索（准确，秒出）；未命中再走 Open-Meteo（国际/拼音）
                hit = _cn_city_lookup(override)
                if hit:
                    lat, lon, city = hit
                else:
                    try:
                        geo = _get("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh"
                                   % urllib.parse.quote(override), timeout=8)
                        rs = (geo or {}).get("results") or []
                        if rs:
                            lat, lon = float(rs[0]["latitude"]), float(rs[0]["longitude"])
                            city = clean_text(rs[0].get("name") or override, 64)
                    except Exception:
                        pass
                if lat is None:
                    return {"ok": False, "error": "未找到城市「%s」，可改用 纬度,经度 格式" % override}
        else:
            # 公网 IP 定位：优先 HTTPS 的 ipwho.is；失败再回退 ip-api.com（免费版仅支持
            # 明文 HTTP，返回的地名一律经 clean_text 净化后才使用）
            try:
                loc = _get("https://ipwho.is/?lang=zh-CN&fields=success,latitude,longitude,city,region,country",
                           timeout=6)
                if loc and loc.get("success") is not False and loc.get("latitude") is not None \
                        and loc.get("longitude") is not None:
                    lat, lon = float(loc["latitude"]), float(loc["longitude"])
                    city = loc.get("city") or loc.get("region") or loc.get("country") or ""
            except Exception:
                pass
            if lat is None:
                try:
                    loc = _get("http://ip-api.com/json/?lang=zh-CN&fields=lat,lon,city,regionName,country", timeout=6)
                    if loc and loc.get("lat") is not None and loc.get("lon") is not None:
                        lat, lon = float(loc["lat"]), float(loc["lon"])
                        city = loc.get("city") or loc.get("regionName") or loc.get("country") or ""
                except Exception:
                    pass
            if lat is not None and not parse_coords("%s,%s" % (lat, lon)):
                lat = lon = None
            city = clean_text(city, 64) or "未知地区"
            if lat is None:
                return {"ok": False, "error": "无法定位当前网络位置"}
        # ---- Open-Meteo 当前天气 ----
        url = ("https://api.open-meteo.com/v1/forecast"
               "?latitude=%.4f&longitude=%.4f"
               "&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m,is_day"
               "&timezone=auto") % (lat, lon)
        data = _get(url)
        cur = data.get("current", {}) or {}
        temp = cur.get("temperature_2m")
        if temp is None:
            return {"ok": False, "error": "天气接口无数据"}
        return {"ok": True, "city": clean_text(city or override, 64) or "未知地区",
                "temp": round(float(temp), 1),
                "feels": round(float(cur.get("apparent_temperature") or temp), 1),
                "humidity": round(float(cur.get("relative_humidity_2m") or 0), 1),
                "wind": round(float(cur.get("wind_speed_10m") or 0), 1),
                "code": cur.get("weather_code"),
                "desc": _wmo_text(cur.get("weather_code")),
                "is_day": cur.get("is_day")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def collect_app_stats():
    """采集相册 / 影视 / 音乐三个内置应用的安装与统计信息。"""
    apps = []
    deadline = time.time() + 25  # 总时限，避免阻塞采集线程
    for a in BUILTIN_APPS:
        if time.time() > deadline:
            break
        item = {"id": a["id"], "name": a["name"], "appid": a["appid"],
                "installed": False, "data_size": 0,
                "media_count": None, "media_size": 0, "note": ""}
        dirs = _app_dirs(a["appid"])
        if not dirs:
            apps.append(item)
            continue
        item["installed"] = True
        item["data_size"] = sum(_dir_size_fast(d, secs=max(1, min(4, int(deadline - time.time())))) for d in dirs)
        # 只读数据库统计媒体条目（相册 photo.db、影视 / 音乐库）
        for d in dirs:
            if time.time() > deadline:
                break
            for dbp in _find_dbs(d, secs=max(1, min(4, int(deadline - time.time())))):
                if time.time() > deadline:
                    break
                st = _read_app_db_stats(dbp)
                if st and st[0]:
                    item["media_count"] = st[0]
                    item["media_size"] = st[1]
                    item["note"] = os.path.basename(os.path.dirname(dbp)) or os.path.basename(dbp)
                    break
            if item["media_count"]:
                break
        apps.append(item)
    return apps


# ---------------------------------------------------------------------------
# 功能模块检测
# ---------------------------------------------------------------------------
MODULES = [
    # 文件共享
    {"id": "smb", "name": "文件共享 (SMB)", "cat": "文件共享", "units": ["smbd", "nmbd", "samba"], "procs": ["smbd", "nmbd"], "containers": []},
    {"id": "nfs", "name": "文件共享 (NFS)", "cat": "文件共享", "units": ["nfs-server", "nfs-kernel-server"], "procs": ["nfsd"], "containers": []},
    {"id": "ftp", "name": "FTP 服务", "cat": "文件共享", "units": ["vsftpd", "proftpd"], "procs": ["vsftpd", "proftpd"], "containers": []},
    # 系统服务
    {"id": "docker", "name": "Docker 服务", "cat": "系统服务", "units": ["docker"], "procs": ["dockerd"], "containers": []},
    {"id": "ssh", "name": "SSH 远程", "cat": "系统服务", "units": ["ssh", "sshd"], "procs": ["sshd"], "containers": []},
    {"id": "cron", "name": "定时任务", "cat": "系统服务", "units": ["cron", "crond"], "procs": ["cron", "crond"], "containers": []},
    {"id": "nginx", "name": "Web 服务 (Nginx)", "cat": "系统服务", "units": ["nginx"], "procs": ["nginx"], "containers": []},
    {"id": "smartd", "name": "磁盘健康 (SMART)", "cat": "系统服务", "units": ["smartd"], "procs": ["smartd"], "containers": []},
    {"id": "ups", "name": "UPS 电源", "cat": "系统服务", "units": ["apcupsd", "nut-server"], "procs": ["apcupsd", "upsd"], "containers": []},
    {"id": "kvm", "name": "虚拟机 (KVM)", "cat": "系统服务", "units": ["libvirtd"], "procs": ["libvirtd"], "containers": []},
    {"id": "log", "name": "系统日志", "cat": "系统服务", "units": ["rsyslog", "syslog-ng"], "procs": ["rsyslogd", "syslog-ng"], "containers": []},
    # 飞牛内置应用（通过应用中心安装目录 / systemd 单元 / 进程 / 容器识别）
    {"id": "fn_movie", "name": "飞牛影视", "cat": "内置应用",
     "appdirs": ["/usr/local/apps/@appcenter/trim.media"],
     "units": ["trim-media", "trim.media", "fn-media", "fnos-media"], "procs": [],
     "containers": ["movie", "fn_movie", "fnmovie", "fn-movie", "fnos-movie", "fn_movie_1"]},
    {"id": "fn_photo", "name": "AI 相册", "cat": "内置应用",
     "appdirs": ["/usr/local/apps/@appcenter/trim.photos"],
     "units": ["trim-photos", "trim.photos", "fn-photos", "fnos-photos"], "procs": [],
     "containers": ["photo", "fn_photo", "fnphoto", "fn-photo", "fnos-photo", "photos", "fn_photo_1"]},
    {"id": "fn_music", "name": "飞牛音乐", "cat": "内置应用",
     "appdirs": ["/usr/local/apps/@appcenter/trim.music"],
     "units": ["trim-music", "trim.music", "fn-music", "fnos-music"], "procs": ["music", "trim.music"],
     "containers": ["music", "trim-music", "trim_music", "trim.music", "fn_music", "fnmusic", "fn-music", "fnos-music", "nas-music"]},
    {"id": "fn_download", "name": "下载中心", "cat": "内置应用",
     "appdirs": ["/usr/local/apps/@appcenter/trim.download"],
     "units": ["trim-download", "trim.download", "fn-download", "fnos-download"],
     "procs": ["aria2c", "transmission-daemon", "qbittorrent-nox"],
     "containers": ["download", "fn_download", "fndownload", "fnos-download", "aria2", "transmission", "qbittorrent"]},
    # 常用媒体应用
    {"id": "jellyfin", "name": "Jellyfin 影音", "cat": "媒体应用", "units": [], "procs": ["jellyfin"], "containers": ["jellyfin"]},
    {"id": "emby", "name": "Emby 影音", "cat": "媒体应用", "units": [], "procs": ["emby"], "containers": ["emby"]},
    {"id": "plex", "name": "Plex 影音", "cat": "媒体应用", "units": [], "procs": ["plex"], "containers": ["plex"]},
    # 常用工具
    {"id": "alist", "name": "Alist 网盘挂载", "cat": "工具应用", "units": [], "procs": ["alist"], "containers": ["alist"]},
    {"id": "portainer", "name": "Portainer 管理", "cat": "工具应用", "units": [], "procs": ["portainer"], "containers": ["portainer"]},
    {"id": "frp", "name": "内网穿透 (FRP)", "cat": "网络工具", "units": [], "procs": ["frpc", "frps"], "containers": []},
    {"id": "tailscale", "name": "Tailscale 组网", "cat": "网络工具", "units": [], "procs": ["tailscaled"], "containers": []},
    {"id": "cpolar", "name": "CPolar 内网穿透", "cat": "网络工具", "units": [], "procs": ["cpolar"], "containers": []},
]


def _installed_units():
    """获取本机所有已安装的 systemd 单元名集合（形如 xxx.service）。"""
    units = set()
    out, rc = run_cmd(["systemctl", "list-unit-files", "--no-legend", "--no-pager"], timeout=10)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split()
            if parts:
                units.add(parts[0])
    return units


def _bin_exists(name):
    """检查常见 PATH 下是否存在可执行文件。"""
    for d in ("/usr/bin", "/usr/sbin", "/bin", "/sbin", "/usr/local/bin", "/usr/local/sbin"):
        if os.path.isfile(os.path.join(d, name)):
            return True
    return False


def _docker_images():
    """获取本机已拉取的 Docker 镜像仓库名集合。"""
    imgs = set()
    out, rc = run_cmd(["docker", "images", "--format", "{{.Repository}}"], timeout=15)
    if rc == 0:
        for line in out.splitlines():
            repo = line.strip().lower()
            if repo:
                imgs.add(repo)
                if "/" in repo:
                    imgs.add(repo.split("/")[-1])
    return imgs


def _module_installed(m, units, imgs):
    """判断模块是否已安装：应用中心目录 / systemd 单元 / 二进制 / 容器镜像，任一命中即视为已安装。"""
    for d in m.get("appdirs", []):
        if os.path.isdir(d):
            return True
    for u in m.get("units", []):
        if u + ".service" in units or u in units:
            return True
    for p in m.get("procs", []):
        if _bin_exists(p):
            return True
    for c in m.get("containers", []):
        if c.lower() in imgs:
            return True
    return False


def _real_display_name(appdir, appid, mf_name):
    """manifest 的 display_name 可能是 ${common.display_name} 等模板占位符，
    从应用目录的配置文件（config/resource / conf/config.json 等）提取真实显示名。
    提取失败则回退到 manifest 里已有的非占位符名字或应用 ID。"""
    if mf_name and "${" not in mf_name and "common." not in mf_name and mf_name.strip():
        return mf_name
    # 候选配置文件（相对应用目录）
    rels = [
        "config/resource", "config/resource.json", "config/privilege",
        "resource/config", "resource/config.json", "resource/app.json",
        "etc/config.json", "etc/config", "etc/app.json",
        "var/config.json", "meta/config.json",
        "conf/config.json", "config.json", "app.json",
    ]
    # 优先 display 类字段，其次 name 类
    pats = [
        r'"(display_name|displayName|app_name|appName)"\s*[=:]\s*"([^"]+)"',
        r'"(name|title|label)"\s*[=:]\s*"([^"]+)"',
        r'"(display_name|displayName|app_name|appName)"\s*[=:]\s*([A-Za-z0-9_\-]+)',
    ]
    for rel in rels:
        p = os.path.join(appdir, rel)
        if not os.path.isfile(p):
            continue
        txt = read_text(p)
        for pat in pats:
            mm = re.search(pat, txt)
            if not mm:
                continue
            v = mm.group(2).strip()
            if v and "${" not in v and "common." not in v and len(v) < 64:
                return v
    return appid


_APP_PORTS_CACHE = {"ts": 0.0, "data": None}
_APP_PORTS_TTL = 20.0
# 应用中心安装根目录（与 _app_center_ports 内保持一致，进程归属匹配复用）
_APPCENTER_ROOTS = ("/vol1/@appcenter", "/usr/local/apps/@appcenter",
                    "/usr/trim/apps", "/var/apps", "/var/lib/fnos/apps")


def _scan_app_center_ports():
    """扫描应用中心每个已安装应用 manifest 中的配置端口与显示名。返回 {appid: {name, port}}。

    飞牛应用中心（含应用商店安装的第三方应用）实际安装在 /vol1/@appcenter/{appid}/，
    部分系统 FPK 应用在 /usr/local/apps/@appcenter，兼容多候选目录；端口字段兼容
    service_port / web_port / http_port / port。
    """
    m = {}
    for apps_dir in _APPCENTER_ROOTS:
        if not os.path.isdir(apps_dir):
            continue
        try:
            names = sorted(os.listdir(apps_dir))
        except Exception:
            continue
        for name in names:
            if name.startswith(".") or name in m:
                continue
            mf = os.path.join(apps_dir, name, "manifest")
            if not os.path.isfile(mf):
                continue
            port = ""
            dname = name
            for line in read_text(mf).splitlines():
                s = line.strip()
                if "=" not in s:
                    continue
                key, _, val = s.partition("=")
                key = key.strip().lower()
                val = val.strip().strip('"').strip("'")
                if not port and key in ("service_port", "web_port", "http_port", "port"):
                    port = val
                elif key == "display_name":
                    dname = val or dname
            if not port:
                # 兜底：从 app/ui/config 的 ".port" 字段提取
                ucfg = os.path.join(apps_dir, name, "app", "ui", "config")
                if os.path.isfile(ucfg):
                    for uline in read_text(ucfg).splitlines():
                        um = re.search(r'"\.port"\s*:\s*"?(\d+)', uline)
                        if um:
                            port = um.group(1)
                            break
            m[name] = {"name": _real_display_name(os.path.join(apps_dir, name), name, dname),
                       "port": port, "configured_port": port}
    return m


def _proc_text(pid):
    """读取进程归属判定用文本（cmdline / cgroup / exe / cwd）。纯 /proc 读取，
    不触碰数据盘内容，不会唤醒休眠硬盘。失败返回空串。"""
    parts = []
    base = "/proc/" + str(pid)
    try:
        raw = read_text(base + "/cmdline").replace("\x00", " ")
        parts.append(raw)
    except Exception:
        pass
    try:
        parts.append(read_text(base + "/cgroup"))
    except Exception:
        pass
    for link in ("exe", "cwd"):
        try:
            parts.append(os.readlink(base + "/" + link))
        except Exception:
            pass
    return " ".join(parts)


def _app_runtime_ports(appids):
    """通过 ss 监听端口 + /proc 进程归属，确定各应用当前真实监听的端口。

    manifest 里的 service_port 只是安装默认值，应用允许用户改端口（如下载应用
    Aellus 默认 8000、实际运行在 8899）；按进程 cmdline / exe / cwd / cgroup
    中是否引用 @appcenter/@appdata/{appid} 判定归属，再用 PID 反查 ss 监听端口。
    返回 {appid: [port, ...]}。只读 /proc，不访问应用数据，不唤醒休眠硬盘。"""
    if not is_linux() or not appids:
        return {}
    listeners = _ss_listeners()
    pid_ports = {}
    for l in listeners:
        if l.get("pid"):
            pid_ports.setdefault(str(l["pid"]), set()).add(int(l["port"]))
    if not pid_ports:
        return {}
    result = {}
    pats = {}
    for appid in appids:
        esc = re.escape(appid)
        # 形如 /vol1/@appcenter/aellus/…、/vol1/@appdata/aellus/…、@appcenter/aellus 结尾
        pats[appid] = re.compile(
            r"(?:@appcenter|@appdata|/apps|appcenter|appdata)[/\\]" + esc + r"(?:[/\\]|[\s\x00]|$)",
            re.IGNORECASE)
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except Exception:
        return result
    for pid in pids:
        if pid not in pid_ports:
            continue
        blob = _proc_text(pid)
        if not blob:
            continue
        for appid, pat in pats.items():
            if pat.search(blob):
                result.setdefault(appid, set()).update(pid_ports[pid])
    return {k: sorted(v) for k, v in result.items()}


def _app_center_ports(force=False):
    """应用中心端口（manifest 默认端口 + 运行时实际监听端口交叉校验），20 秒缓存。

    优先采用应用进程当前真实监听的端口；manifest 配置端口确实在监听时沿用之；
    两者不一致时把配置端口留在 default_port 字段，由前端标注「实际/默认」。"""
    now = time.time()
    if not force and _APP_PORTS_CACHE["data"] is not None \
            and now - _APP_PORTS_CACHE["ts"] <= _APP_PORTS_TTL:
        return {k: dict(v) for k, v in _APP_PORTS_CACHE["data"].items()}
    m = _scan_app_center_ports()
    try:
        runtime = _app_runtime_ports(set(m.keys()))
    except Exception:
        runtime = {}
    for appid, info in m.items():
        actual = runtime.get(appid) or []
        cfg = str(info.get("configured_port") or "").strip()
        cfg_ports = []
        for part in re.split(r"[,\s]+", cfg):
            try:
                cfg_ports.append(int(part))
            except Exception:
                pass
        if actual:
            info["runtime_ports"] = ",".join(str(p) for p in actual)
            if cfg_ports and all(p in actual for p in cfg_ports):
                info["port"] = ",".join(str(p) for p in cfg_ports)
            else:
                info["default_port"] = cfg
                info["port"] = ",".join(str(p) for p in actual)
    _APP_PORTS_CACHE["data"] = m
    _APP_PORTS_CACHE["ts"] = now
    return {k: dict(v) for k, v in m.items()}


def _ss_process_ports():
    """ss 监听端口解析为 进程名 -> 监听端口列表（用于功能模块端口标注，复用共享缓存）。"""
    pmap = {}
    for l in _ss_listeners():
        if not l.get("process"):
            continue
        pmap.setdefault(l["process"].lower(), []).append(
            {"port": int(l["port"]), "pid": str(l["pid"])})
    return pmap


def collect_modules(docker_result=None):
    # 收集当前进程名（一次）
    procs = set()
    if is_linux():
        out, _ = run_cmd(["ps", "-eo", "comm="], timeout=10)
        for p in out.splitlines():
            name = p.strip().lower()
            if name:
                procs.add(name)
    # 收集容器名（一次）
    cnames = set()
    docker_ok = bool(docker_result and docker_result.get("available"))
    if docker_ok:
        for c in docker_result.get("containers", []):
            cnames.add(c["name"].lower())

    units = _installed_units()
    imgs = _docker_images() if docker_ok else set()
    app_ports = _app_center_ports()
    ss_ports = _ss_process_ports()

    modules = []
    for m in MODULES:
        installed = _module_installed(m, units, imgs)
        running = False
        method = ""
        if installed:
            for u in m.get("units", []):
                if not is_linux():
                    continue
                _, rc = run_cmd(["systemctl", "is-active", "--quiet", u], timeout=5)
                if rc == 0:
                    running = True
                    method = "systemd:%s" % u
                    break
            if not running:
                for p in m.get("procs", []):
                    pl = p.lower()
                    if pl in procs or any(pl in x for x in procs):
                        running = True
                        method = "进程:%s" % p
                        break
            if not running:
                for c in m.get("containers", []):
                    cl = c.lower()
                    if cl in cnames or any(cl in x for x in cnames):
                        running = True
                        method = "容器:%s" % c
                        break
        # 端口 / 访问信息（实用化）：飞牛应用读 manifest，系统服务按进程名匹配 ss
        port = ""
        web = False
        proc_hint = ""
        if installed:
            for d in m.get("appdirs", []):
                appid = d.rstrip("/").split("/")[-1]
                if appid in app_ports and app_ports[appid]["port"]:
                    port = app_ports[appid]["port"]
                    web = True
                    break
            if not port:
                procs_hit = [p for p in m.get("procs", []) if p.lower() in procs]
                if procs_hit:
                    proc_hint = procs_hit[0]
                    hits = ss_ports.get(procs_hit[0].lower(), [])
                    if hits:
                        port = ",".join(str(x["port"]) for x in hits[:5])
                        # 常见 Web 服务端口视为有 Web 入口
                        web = any(x["port"] in (80, 443, 8080, 8443, 5000, 3000, 9000) for x in hits)
        modules.append({
            "id": m["id"], "name": m["name"], "category": m["cat"],
            "installed": installed, "running": running, "method": method,
            "port": port, "web": web, "proc": proc_hint,
        })
    # 按分类分组排序
    cat_order = ["文件共享", "系统服务", "内置应用", "媒体应用", "工具应用", "网络工具"]
    modules.sort(key=lambda x: (cat_order.index(x["category"]) if x["category"] in cat_order else 99, x["name"]))
    return modules


# ---------------------------------------------------------------------------
# 历史存储（SQLite）
# ---------------------------------------------------------------------------
class History:
    def __init__(self, db_path):
        self.db_path = db_path
        # 数据盘休眠期间读记忆（key -> 上次活跃时读到的行），避免前端面板轮询
        # 触发的 SELECT 把已 STANDBY 的机械硬盘唤醒
        self._read_memo = {}
        self._memo_lock = threading.Lock()
        self._init()

    def _asleep(self):
        """monitor.db 所在硬盘是否处于休眠（任何 sqlite 读都可能唤醒它）。"""
        try:
            return RUNTIME.get("standby_protect", True) and is_linux() \
                and path_on_standby(os.path.dirname(self.db_path) or "/")
        except Exception:
            return False

    def _memo(self, key, rows):
        """记录 / 取用休眠期间的读缓存（条目过多时丢弃最旧的一批）。

        仅用于面板轮询触发的查询（query），使休眠的机械硬盘不被周期性 SELECT 唤醒；
        用户主动的导出 / 报告 / 统计操作不应调用本方法——其结果可能含数百万行，
        挂入 _read_memo 会长期占用数 GB 内存且永不释放（参见 issue #3）。
        """
        with self._memo_lock:
            if rows is None:
                return self._read_memo.get(key, [])
            self._read_memo[key] = rows
            if len(self._read_memo) > 128:
                for k in list(self._read_memo)[:32]:
                    self._read_memo.pop(k, None)
        return rows

    def _no_cache(self, rows):
        """导出 / 报告路径专用：不挂入缓存，函数返回后由 Python GC 回收。

        导出是用户主动的一次性操作，下次导出可能要不同范围或最新数据，
        缓存它毫无意义且会持有数百万行导致 RSS 冲顶不回落（issue #3）。
        休眠保护仍保留：读 db 会唤醒 STANDBY 硬盘，休眠时返回空列表。"""
        return rows

    def _init(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS metrics (ts REAL NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_metric_ts ON metrics(metric, ts)")
                # 上月汇总表：cleanup 删除 metrics 前先把数据按月汇总 UPSERT 到此表，
                # 不受 retention 清理影响，即使 retention=7 月度汇总仍完整（增量叠加）
                conn.execute("""CREATE TABLE IF NOT EXISTS monthly_summary (
                    year INTEGER NOT NULL, month INTEGER NOT NULL, metric TEXT NOT NULL,
                    sum_val REAL DEFAULT 0, count_val INTEGER DEFAULT 0,
                    max_val REAL, min_val REAL, first_val REAL, last_val REAL,
                    first_ts REAL, last_ts REAL, updated_at REAL,
                    PRIMARY KEY (year, month, metric))""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_ms_ym ON monthly_summary(year DESC, month DESC)")
                # 元数据（月度归档水位线等）
                conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
                # 每日汇总（近 30 天用电 / 流量柱、全年估算）：按本地自然日累加，
                # 水位线 meta.daily_archived_until 保证每行 metrics 只计入一次；不受 retention 清理影响
                conn.execute("""CREATE TABLE IF NOT EXISTS daily_summary (
                    day TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL,
                    PRIMARY KEY (day, metric))""")
                # 硬盘巡检：最新结果（盘位卡片只读这里，不触发任何 smartctl）
                conn.execute("""CREATE TABLE IF NOT EXISTS disk_health (
                    dev TEXT PRIMARY KEY,
                    serial TEXT, model TEXT, proto TEXT, health TEXT, grade TEXT,
                    defects INTEGER, pending_defects INTEGER, uncorr INTEGER,
                    temp_c INTEGER, power_on_hours INTEGER, rpm TEXT, manufactured TEXT,
                    standby INTEGER DEFAULT 0, scan_ts REAL, changed_ts REAL, detail TEXT)""")
                # 巡检历史：只有「等级或计数发生变化」才落一行，避免 14 盘 × 每天 4 次
                # 把趋势表撑爆；无变化的巡检只更新 disk_health 的 scan_ts
                conn.execute("""CREATE TABLE IF NOT EXISTS disk_health_hist (
                    ts REAL NOT NULL, dev TEXT NOT NULL, grade TEXT,
                    defects INTEGER, pending_defects INTEGER, uncorr INTEGER)""")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_dhh ON disk_health_hist(dev, ts)")
                # 自愈：v2.8.0 前的历史写入曾把 (指标名, 时间戳) 两列互换（ts 列存了文本），
                # 这些脏行永远查不出来还会拖慢查询，启动时一次性清除
                conn.execute("DELETE FROM metrics WHERE typeof(ts) != 'real'")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()

    def disk_health_read(self):
        """全部硬盘的最新巡检结果（dev -> dict）。

        休眠时返回上一次的缓存，不为了显示盘位分级去 SELECT 把机械盘唤醒。"""
        if self._asleep():
            got = self._memo("disk_health", None)
            return got if isinstance(got, dict) else {}
        rows = {}
        try:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                for r in conn.execute("SELECT * FROM disk_health").fetchall():
                    d = dict(r)
                    dev = d.get("dev")
                    if dev:
                        rows[dev] = d
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        self._memo("disk_health", rows)
        return rows

    def disk_health_save(self, items, prev):
        """写入本次巡检结果；仅当等级或计数变化时补一条历史。

        items: [{dev, serial, model, proto, health, grade, defects, pending_defects,
                 uncorr, temp_c, power_on_hours, rpm, manufactured, standby, scan_ts, detail}]
        返回发生变化的 dev 列表（供通知使用）。"""
        changed = []
        for it in items or []:
            old = prev.get(it["dev"]) or {}
            sig = ("grade", "defects", "pending_defects", "uncorr")
            if any(old.get(k) != it.get(k) for k in sig):
                changed.append(it["dev"])
        try:
            conn = self._connect(write=True)
            try:
                conn.executemany(
                    """INSERT INTO disk_health
                       (dev, serial, model, proto, health, grade, defects, pending_defects, uncorr,
                        temp_c, power_on_hours, rpm, manufactured, standby, scan_ts, changed_ts, detail)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(dev) DO UPDATE SET
                         serial=excluded.serial, model=excluded.model, proto=excluded.proto,
                         health=excluded.health, grade=excluded.grade, defects=excluded.defects,
                         pending_defects=excluded.pending_defects, uncorr=excluded.uncorr,
                         temp_c=excluded.temp_c, power_on_hours=excluded.power_on_hours,
                         rpm=excluded.rpm, manufactured=excluded.manufactured,
                         standby=excluded.standby, scan_ts=excluded.scan_ts,
                         changed_ts=COALESCE(disk_health.changed_ts, excluded.changed_ts),
                         detail=excluded.detail""",
                    [(it.get("dev"), it.get("serial"), it.get("model"), it.get("proto"),
                      it.get("health"), it.get("grade"), it.get("defects"), it.get("pending_defects"),
                      it.get("uncorr"), it.get("temp_c"), it.get("power_on_hours"), it.get("rpm"),
                      it.get("manufactured"), 1 if it.get("standby") else 0, it.get("scan_ts"),
                      it.get("scan_ts"), it.get("detail")) for it in items])
                hist = [(it.get("scan_ts"), it.get("dev"), it.get("grade"), it.get("defects"),
                         it.get("pending_defects"), it.get("uncorr"))
                        for it in items if it["dev"] in changed and not it.get("standby")]
                if hist:
                    conn.executemany(
                        "INSERT INTO disk_health_hist(ts, dev, grade, defects, pending_defects, uncorr)"
                        " VALUES (?,?,?,?,?,?)", hist)
                # 历史只留最近 400 天，够看一年趋势又不会无限增长
                conn.execute("DELETE FROM disk_health_hist WHERE ts < ?", (time.time() - 400 * 86400,))
                conn.commit()
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        # 有新结果：清掉休眠读缓存，下次面板轮询读到的是最新一份
        with self._memo_lock:
            self._read_memo.pop("disk_health", None)
        return changed

    def disk_health_hist(self, dev, limit=60):
        """单盘巡检历史（新→旧），用于详情面板显示「较上次 +N」。"""
        if self._asleep():
            return []
        try:
            conn = self._connect()
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT * FROM disk_health_hist WHERE dev=? ORDER BY ts DESC LIMIT ?",
                    (dev, int(limit)))
                return [dict(r) for r in cur.fetchall()]
            finally:
                conn.close()
        except Exception:
            return []

    def write(self, rows):
        if not rows:
            return
        try:
            conn = self._connect(write=True)
            try:
                conn.executemany("INSERT INTO metrics(ts, metric, value) VALUES(?,?,?)", rows)
                conn.commit()
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()

    def query(self, metric, seconds, limit=7200):
        # seconds=0 表示查询全部保留期内的历史（「全部」范围），上限放大到 43200 行（30 天 × 每分钟 1 条）
        if not seconds:
            limit = 43200
        key = ("q", metric, seconds, limit)
        if self._asleep():
            return self._memo(key, None)
        cutoff = (time.time() - seconds) if seconds else 0
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            try:
                # 取最新 limit 行再反转回升序，避免超大数据量拖慢查询
                cur = conn.execute(
                    "SELECT ts, value FROM metrics WHERE metric=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                    (metric, cutoff, limit),
                )
                rows = cur.fetchall()[::-1]
            finally:
                conn.close()
        except Exception:
            return []
        return self._memo(key, rows)

    def _connect(self, write=False):
        conn = sqlite3.connect(self.db_path, timeout=15)
        if write:
            # WAL 下 synchronous=NORMAL：断电最多丢最后一个事务（监控数据可接受），
            # 显著减少 fsync，对机械硬盘更友好；限制 WAL 文件残留大小
            try:
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA journal_size_limit=67108864")
            except Exception:
                pass
        return conn

    @staticmethod
    def _prefix_hi(prefix):
        """前缀范围查询上界：'net_rx:' → 'net_rx;'。metric >= prefix AND metric < hi 可走
        idx_metric_ts 索引（LIKE 默认大小写不敏感，与 BINARY 索引不匹配，会全表扫描）。"""
        return prefix[:-1] + chr(ord(prefix[-1]) + 1)

    def _distinct_metrics(self, conn, prefix=""):
        """松散索引扫描（loose index scan）：每个不同的 metric 只做一次索引 seek，
        代价与指标个数成正比，与行数无关（替代 SELECT DISTINCT 全量扫描）。"""
        out = []
        hi = self._prefix_hi(prefix) if prefix else None
        cur = conn.execute(
            "SELECT metric FROM metrics WHERE metric >= ?" + (" AND metric < ?" if hi else "")
            + " ORDER BY metric LIMIT 1", (prefix, hi) if hi else (prefix,)).fetchone()
        while cur and len(out) < 100000:
            out.append(cur[0])
            cur = conn.execute(
                "SELECT metric FROM metrics WHERE metric > ?" + (" AND metric < ?" if hi else "")
                + " ORDER BY metric LIMIT 1", (cur[0], hi) if hi else (cur[0],)).fetchone()
        return out

    def metrics_with_prefix(self, prefix):
        """返回库中以 prefix 开头的全部指标名（面板轮询用：休眠时返回上次结果）。"""
        key = ("mp", prefix)
        if self._asleep():
            return self._memo(key, None)
        try:
            conn = self._connect()
            try:
                rows = self._distinct_metrics(conn, prefix)
            finally:
                conn.close()
        except Exception:
            return []
        return self._memo(key, rows)

    @staticmethod
    def _real_span(conn, metrics, seconds, span=None):
        """降采样桶宽所用的时间跨度：固定范围直接用 seconds；「全部」用实际最早数据
        （每个指标一次索引 MIN 查询），避免保留期很长但数据很少时点数过稀。"""
        if seconds:
            return float(seconds)
        first = None
        for m in metrics[:500]:
            r = conn.execute("SELECT MIN(ts) FROM metrics WHERE metric=?", (m,)).fetchone()
            if r and r[0] is not None and (first is None or r[0] < first):
                first = r[0]
        if first is None:
            return float(span or 86400)
        return max(60.0, time.time() - first)

    def query_series(self, metric, seconds, span=None, max_points=720):
        """单指标趋势：按时间桶在 SQL 内求平均，最多返回约 max_points 个点。
        数据点少于 max_points 时每个桶只含一个采样，结果与原始数据完全一致。"""
        key = ("qs", metric, seconds)
        if self._asleep():
            return self._memo(key, None)
        cutoff = (time.time() - seconds) if seconds else 0
        try:
            conn = self._connect()
            try:
                bucket = max(1.0, self._real_span(conn, [metric], seconds, span) / max_points)
                rows = conn.execute(
                    "SELECT MAX(ts), AVG(value) FROM metrics WHERE metric=? AND ts>=? "
                    "GROUP BY CAST(ts / ? AS INTEGER) ORDER BY 1",
                    (metric, cutoff, bucket)).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return self._memo(key, rows)

    def bucket_for(self, metrics, seconds, max_points=720):
        """多条序列共用的桶宽（保证 rx/tx 等成对序列时间戳对齐）。"""
        if seconds:
            return max(1.0, float(seconds) / max_points)
        if self._asleep():
            return None
        try:
            conn = self._connect()
            try:
                return max(1.0, self._real_span(conn, list(metrics), 0) / max_points)
            finally:
                conn.close()
        except Exception:
            return None

    def sum_series(self, metrics, seconds, span=None, max_points=720, bucket=None):
        """多指标「同一时刻求和」后按时间桶平均（网卡 / 磁盘 / 容器聚合曲线）。
        返回 [(ts, value)]；每桶的值 = 桶内各时刻总和的平均。"""
        metrics = list(metrics or [])
        if not metrics:
            return []
        key = ("ss", tuple(metrics), seconds)
        if self._asleep():
            return self._memo(key, None)
        cutoff = (time.time() - seconds) if seconds else 0
        out = []
        try:
            conn = self._connect()
            try:
                if not bucket:
                    bucket = max(1.0, self._real_span(conn, metrics, seconds, span) / max_points)
                # IN 列表分批（SQLite 变量上限 999），各批结果按桶合并
                acc = {}
                for i in range(0, len(metrics), 500):
                    part = metrics[i:i + 500]
                    q = ("SELECT CAST(ts / ? AS INTEGER) AS b, MAX(ts), SUM(value), COUNT(DISTINCT ts) "
                         "FROM metrics WHERE metric IN (%s) AND ts>=? GROUP BY b" % ",".join("?" * len(part)))
                    for b, t, sv, n in conn.execute(q, [bucket] + part + [cutoff]):
                        a = acc.setdefault(b, [t, 0.0, 0])
                        a[0] = max(a[0], t)
                        a[1] += sv or 0.0
                        a[2] = max(a[2], n or 0)
                for b in sorted(acc):
                    t, sv, n = acc[b]
                    out.append((t, sv / n if n else 0.0))
            finally:
                conn.close()
        except Exception:
            return []
        return self._memo(key, out)

    def first_last(self, metric, cutoff):
        """区间内首条与末条值（两次索引 seek）。无数据返回 None。"""
        if self._asleep():
            return self._memo(("fl", metric, int(cutoff / 60)), None) or None
        try:
            conn = self._connect()
            try:
                a = conn.execute("SELECT value FROM metrics WHERE metric=? AND ts>=? ORDER BY ts ASC LIMIT 1",
                                 (metric, cutoff)).fetchone()
                b = conn.execute("SELECT value FROM metrics WHERE metric=? AND ts>=? ORDER BY ts DESC LIMIT 1",
                                 (metric, cutoff)).fetchone()
            finally:
                conn.close()
        except Exception:
            return None
        res = (a[0], b[0]) if a and b else None
        if res:
            self._memo(("fl", metric, int(cutoff / 60)), res)
        return res

    def energy_kwh(self, metric, seconds, default_dt=60):
        """区间能耗：SUM(功率) × 采样间隔中位数（SQL 聚合覆盖完整区间，不受行数上限截断）。"""
        key = ("kwh", metric, int((time.time() - seconds) / 60))
        if self._asleep():
            v = self._memo(key, None)
            return v if isinstance(v, float) else 0.0
        cutoff = time.time() - seconds
        try:
            conn = self._connect()
            try:
                s, n = conn.execute("SELECT SUM(value), COUNT(value) FROM metrics WHERE metric=? AND ts>=?",
                                    (metric, cutoff)).fetchone()
                if not n:
                    return 0.0
                if n < 2:
                    return round((s or 0) * default_dt / 3600000.0, 4)
                ts = [r[0] for r in conn.execute(
                    "SELECT ts FROM metrics WHERE metric=? AND ts>=? ORDER BY ts DESC LIMIT 2000",
                    (metric, cutoff))]
            finally:
                conn.close()
        except Exception:
            return 0.0
        dts = sorted(ts[i] - ts[i + 1] for i in range(len(ts) - 1))
        median_dt = dts[len(dts) // 2] if dts else default_dt
        val = round((s or 0) * median_dt / 3600000.0, 4)
        self._memo(key, val)
        return val

    def export_net(self, seconds):
        """导出网卡上下行历史 (ts, metric, value)。seconds=0 表示全部（走索引范围条件）。"""
        if self._asleep():
            return []
        limit = "" if seconds else " LIMIT 1000000"
        cutoff = (time.time() - seconds) if seconds else 0
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, metric, value FROM metrics WHERE ts>=? AND "
                    "((metric >= 'net_rx:' AND metric < 'net_rx;') OR (metric >= 'net_tx:' AND metric < 'net_tx;')) "
                    "ORDER BY ts" + limit, (cutoff,)).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return self._no_cache(rows)

    def query_prefix(self, prefix, seconds):
        """按前缀批量查询历史指标，返回 [(ts, metric, value)]（索引范围条件，非 LIKE）。"""
        if self._asleep():
            return []
        cutoff = (time.time() - seconds) if seconds else 0
        limit = "" if seconds else " LIMIT 1000000"
        try:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT ts, metric, value FROM metrics WHERE metric >= ? AND metric < ? AND ts>=? ORDER BY ts"
                    + limit, (prefix, self._prefix_hi(prefix), cutoff)).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return self._no_cache(rows)

    def export_columns(self, seconds, prefixes=None):
        """导出宽表的列：区间内出现过的指标，按首次出现顺序（ts, rowid）排列，
        与旧实现「按行出现顺序」一致。prefixes 给定时按前缀分组依次排列。"""
        if self._asleep():
            return []
        cutoff = (time.time() - seconds) if seconds else 0
        cols = []
        try:
            conn = self._connect()
            try:
                for pf in (prefixes or [""]):
                    found = []
                    for m in self._distinct_metrics(conn, pf):
                        r = conn.execute("SELECT ts, rowid FROM metrics WHERE metric=? AND ts>=? "
                                         "ORDER BY ts LIMIT 1", (m, cutoff)).fetchone()
                        if r:
                            found.append((r[0], r[1], m))
                    cols.extend(m for _, _, m in sorted(found))
            finally:
                conn.close()
        except Exception:
            return []
        return cols

    def iter_rows(self, seconds, prefixes=None, metrics=None):
        """流式遍历 (ts, metric, value)（按 ts 排序），不一次性 fetchall 到内存。"""
        if self._asleep():
            return
        cutoff = (time.time() - seconds) if seconds else 0
        where, args = ["ts>=?"], [cutoff]
        if prefixes:
            where.append("(" + " OR ".join("(metric >= ? AND metric < ?)" for _ in prefixes) + ")")
            for pf in prefixes:
                args += [pf, self._prefix_hi(pf)]
        if metrics:
            where.append("metric IN (%s)" % ",".join("?" * len(metrics)))
            args += list(metrics)
        conn = self._connect()
        try:
            cur = conn.execute("SELECT ts, metric, value FROM metrics WHERE %s ORDER BY ts" % " AND ".join(where), args)
            while True:
                chunk = cur.fetchmany(5000)
                if not chunk:
                    break
                for r in chunk:
                    yield r
        finally:
            conn.close()

    def report_stats(self, seconds):
        """健康报告统计：每指标 last/max/sum/n 在 SQL 内聚合（不物化数百万行）。"""
        if self._asleep():
            return {}
        cutoff = time.time() - seconds
        out = {}
        try:
            conn = self._connect()
            try:
                for m in self._distinct_metrics(conn):
                    r = conn.execute(
                        "SELECT MAX(value), SUM(value), COUNT(value) FROM metrics "
                        "WHERE metric=? AND ts>=? AND value BETWEEN -1e308 AND 1e308", (m, cutoff)).fetchone()
                    if not r or not r[2]:
                        continue
                    last = conn.execute(
                        "SELECT value FROM metrics WHERE metric=? AND ts>=? AND value BETWEEN -1e308 AND 1e308 "
                        "ORDER BY ts DESC LIMIT 1", (m, cutoff)).fetchone()
                    out[m] = {"last": last[0] if last else r[0], "max": r[0], "sum": r[1] or 0.0, "n": r[2]}
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
        return out

    def diag(self):
        """趋势诊断信息（休眠时不读库）。"""
        info = {}
        if self._asleep():
            info["asleep"] = True
            return info
        conn = self._connect()
        try:
            metrics = self._distinct_metrics(conn)
            info["metrics"] = len(metrics)
            last = None
            for m in metrics:
                r = conn.execute("SELECT MAX(ts) FROM metrics WHERE metric=?", (m,)).fetchone()
                if r and r[0] and (last is None or r[0] > last):
                    last = r[0]
            info["last_ts"] = last
            info["rows"] = conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]
        finally:
            conn.close()
        return info

    def monthly_rows(self):
        """读取月度汇总：优先上一个自然月，没有则取最近一个月。返回 (y, m, rows) 或 None。"""
        key = ("monthly",)
        if self._asleep():
            return self._memo(key, None) or None
        import datetime
        today = datetime.date.today()
        py, pm = (today.year - 1, 12) if today.month == 1 else (today.year, today.month - 1)
        conn = self._connect()
        try:
            row = conn.execute("SELECT year, month FROM monthly_summary WHERE year=? AND month=? LIMIT 1",
                               (py, pm)).fetchone()
            if not row:
                row = conn.execute("SELECT year, month FROM monthly_summary "
                                   "ORDER BY year DESC, month DESC LIMIT 1").fetchone()
            if not row:
                return None
            y, m = row
            rows = conn.execute(
                "SELECT metric, sum_val, count_val, max_val, min_val, first_val, last_val "
                "FROM monthly_summary WHERE year=? AND month=?", (y, m)).fetchall()
        finally:
            conn.close()
        res = (y, m, rows)
        self._memo(key, res)
        return res

    # ---- 月度汇总归档（水位线，杜绝重复累加） ----
    # meta.monthly_archived_until = W 表示 ts < W 的数据都已累加进 monthly_summary。
    # 每次只归档 [W, target) 并在同一事务内推进 W，cleanup 与月初补全共用，
    # 任何数据只会被计入一次（修复旧版月初 1-3 日各累加一次 + cleanup 再加一次）。
    _UPSERT_MONTHLY = """
        INSERT INTO monthly_summary(year, month, metric, sum_val, count_val, max_val, min_val,
            first_val, last_val, first_ts, last_ts, updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(year, month, metric) DO UPDATE SET
            sum_val = monthly_summary.sum_val + excluded.sum_val,
            count_val = monthly_summary.count_val + excluded.count_val,
            max_val = MAX(monthly_summary.max_val, excluded.max_val),
            min_val = MIN(monthly_summary.min_val, excluded.min_val),
            first_val = CASE WHEN excluded.first_ts < monthly_summary.first_ts
                        THEN excluded.first_val ELSE monthly_summary.first_val END,
            first_ts = MIN(monthly_summary.first_ts, excluded.first_ts),
            last_val = CASE WHEN excluded.last_ts > monthly_summary.last_ts
                       THEN excluded.last_val ELSE monthly_summary.last_val END,
            last_ts = MAX(monthly_summary.last_ts, excluded.last_ts),
            updated_at = excluded.updated_at
    """

    def _archive_watermark(self, conn):
        r = conn.execute("SELECT value FROM meta WHERE key='monthly_archived_until'").fetchone()
        if r:
            try:
                return float(r[0])
            except Exception:
                pass
        # 首次（或从旧版升级）：旧版已归档的数据以 monthly_summary 中最大的 last_ts 为界，
        # 界线之前仍在 metrics 中的行视为已归档，避免升级后再累加一次
        r = conn.execute("SELECT MAX(last_ts) FROM monthly_summary").fetchone()
        return (float(r[0]) + 1e-3) if r and r[0] else 0.0

    def _archive_until(self, conn, target):
        wm = self._archive_watermark(conn)
        if target <= wm:
            return 0
        now = time.time()
        n = 0
        for metric in self._distinct_metrics(conn):
            groups = conn.execute(
                "SELECT CAST(strftime('%Y', ts, 'unixepoch', 'localtime') AS INTEGER), "
                "CAST(strftime('%m', ts, 'unixepoch', 'localtime') AS INTEGER), "
                "SUM(value), COUNT(value), MAX(value), MIN(value), MIN(ts), MAX(ts) "
                "FROM metrics WHERE metric=? AND ts>=? AND ts<? GROUP BY 1, 2",
                (metric, wm, target)).fetchall()
            for y, m, s, cnt, mx, mn, fts, lts in groups:
                if not cnt:
                    continue
                fv = conn.execute("SELECT value FROM metrics WHERE metric=? AND ts=? LIMIT 1", (metric, fts)).fetchone()
                lv = conn.execute("SELECT value FROM metrics WHERE metric=? AND ts=? LIMIT 1", (metric, lts)).fetchone()
                conn.execute(self._UPSERT_MONTHLY, (y, m, metric, s or 0, cnt, mx, mn,
                                                    fv[0] if fv else None, lv[0] if lv else None, fts, lts, now))
                n += 1
        conn.execute("INSERT INTO meta(key, value) VALUES('monthly_archived_until', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (repr(float(target)),))
        return n

    # ---- 每日汇总（水位线，与月度归档同一思路） ----
    # meta.daily_archived_until = W 表示 ts < W 的 metrics 行都已累加进 daily_summary。
    # 每次只汇总 [W, target) 并在同一事务内推进 W：重复调用 / 进程重启 / 反复 cleanup
    # 都不会重复累加。daily_summary 每天每指标一行（PRIMARY KEY(day, metric)）：
    #   kwh      功率 W 对时间积分（相邻采样间隔；超过 3 倍采样间隔视为停机断档，按一个间隔计）
    #   power_s  有功率数据覆盖的秒数（判断「不完整的一天」、求平均功率）
    #   rx_phys / tx_phys  物理网口（非 lo、非虚拟前缀）累计字节计数器的增量
    #   rx_virt / tx_virt  虚拟网口（docker/veth/br-…）增量；接口按 traffic_exclude_bridge 决定是否计入
    #   cpu_sum / cpu_n / cpu_max  CPU 占用 % 的和 / 采样数 / 峰值
    # 计数器回退（重启归零）时增量取新值本身（开机以来的字节），与 Prometheus increase() 相同。
    DAILY_KEEP_DAYS = 400
    DAILY_CHUNK = 7 * 86400
    _DAILY_MAX_KEYS = ("cpu_max",)
    _UPSERT_DAILY_ADD = ("INSERT INTO daily_summary(day, metric, value) VALUES(?,?,?) "
                         "ON CONFLICT(day, metric) DO UPDATE SET value = daily_summary.value + excluded.value")
    _UPSERT_DAILY_MAX = ("INSERT INTO daily_summary(day, metric, value) VALUES(?,?,?) "
                         "ON CONFLICT(day, metric) DO UPDATE SET value = MAX(daily_summary.value, excluded.value)")

    @staticmethod
    def _day_start(ts):
        lt = time.localtime(ts)
        return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))

    @staticmethod
    def _day_keyer():
        """ts → 本地日期 'YYYY-MM-DD'（缓存当天边界，逐行调用只做两次比较）。"""
        box = [1.0, 0.0, ""]

        def key(ts):
            if not (box[0] <= ts < box[1]):
                lt = time.localtime(ts)
                box[0] = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
                box[1] = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, 0, 0, 0, 0, 0, -1))
                box[2] = "%04d-%02d-%02d" % (lt.tm_year, lt.tm_mon, lt.tm_mday)
            return box[2]
        return key

    def _daily_watermark(self, conn):
        r = conn.execute("SELECT value FROM meta WHERE key='daily_archived_until'").fetchone()
        if r:
            try:
                return float(r[0]), True
            except Exception:
                pass
        # 首次：从 DAILY_KEEP_DAYS 天前的 0 点开始（库里实际只有保留期内的数据）
        return self._day_start(time.time() - self.DAILY_KEEP_DAYS * 86400), False

    def _daily_aggregate(self, conn, start, end, default_dt=60):
        """汇总 metrics 中 start <= ts < end 的行 → {(day, key): value}。只读，可用于实时尾段。"""
        acc = {}
        dk = self._day_keyer()

        def add(day, k, v):
            acc[(day, k)] = acc.get((day, k), 0.0) + v

        def prev_row(metric):
            return conn.execute("SELECT ts, value FROM metrics WHERE metric=? AND ts<? ORDER BY ts DESC LIMIT 1",
                                (metric, start)).fetchone()

        def rows(metric):
            cur = conn.execute("SELECT ts, value FROM metrics WHERE metric=? AND ts>=? AND ts<? ORDER BY ts",
                               (metric, start, end))
            while True:
                chunk = cur.fetchmany(5000)
                if not chunk:
                    return
                for r in chunk:
                    yield r

        # 功率 → kWh：与 2.18.0 月度 kWh 修复同口径（按采样间隔积分，不受行数上限截断），
        # 每个采样代表它之前的一个间隔；断档（> 3 倍间隔）只按一个典型间隔计
        pr = prev_row("power")
        pts = list(rows("power"))
        if pts:
            ts_list = ([pr[0]] if pr else []) + [t for t, _ in pts]
            dts = sorted(ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)
                         if ts_list[i + 1] > ts_list[i])
            med = dts[len(dts) // 2] if dts else float(default_dt)
            cap = 3.0 * max(med, float(default_dt or 0))
            last = pr[0] if pr else None
            for t, v in pts:
                gap = (t - last) if last is not None else None
                dt = gap if (gap is not None and 0 < gap <= cap) else med
                last = t
                day = dk(t)
                add(day, "kwh", (v or 0.0) * dt / 3600000.0)
                add(day, "power_s", dt)
        # CPU 占用：日均 / 峰值
        for t, v in rows("cpu"):
            day = dk(t)
            add(day, "cpu_sum", v or 0.0)
            add(day, "cpu_n", 1.0)
            k = (day, "cpu_max")
            if k not in acc or v > acc[k]:
                acc[k] = float(v)
        # 网口累计字节计数器 → 每日增量
        for prefix, kk in (("net_rx_bytes:", "rx"), ("net_tx_bytes:", "tx")):
            for metric in self._distinct_metrics(conn, prefix):
                iface = metric.split(":", 1)[1]
                if iface == "lo":
                    continue
                key = kk + ("_virt" if iface.startswith(VIRT_IFACE_PREFIXES) else "_phys")
                p = prev_row(metric)
                pv = p[1] if p else None
                for t, v in rows(metric):
                    if pv is not None:
                        d = v - pv
                        add(dk(t), key, d if d >= 0 else v)
                    else:
                        add(dk(t), key, 0.0)   # 首个采样：只标记「有数据」，没有基准不计增量
                    pv = v
        return acc

    def archive_daily(self, target, default_dt=60):
        """把 [水位线, target) 的 metrics 汇总进 daily_summary（分 7 天一段、每段独立事务）。"""
        conn = self._connect(write=True)
        n = 0
        try:
            wm, _ = self._daily_watermark(conn)
            while wm < target:
                end = min(float(target), wm + self.DAILY_CHUNK)
                with conn:
                    agg = self._daily_aggregate(conn, wm, end, default_dt)
                    for (day, k), v in agg.items():
                        conn.execute(self._UPSERT_DAILY_MAX if k in self._DAILY_MAX_KEYS else self._UPSERT_DAILY_ADD,
                                     (day, k, v))
                        n += 1
                    conn.execute("INSERT INTO meta(key, value) VALUES('daily_archived_until', ?) "
                                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (repr(end),))
                wm = end
            keep = time.strftime("%Y-%m-%d", time.localtime(time.time() - self.DAILY_KEEP_DAYS * 86400))
            with conn:
                conn.execute("DELETE FROM daily_summary WHERE day < ?", (keep,))
        finally:
            conn.close()
        return n

    def daily_rows(self, days, default_dt=60):
        """最近 days 天（含今天）的每日汇总：已归档部分 + 水位线之后的实时尾段（只读，不写库）。

        返回 {day: {key: value}}；休眠时返回上次结果（不读库，不唤醒硬盘）。"""
        key = ("daily", int(days))
        if self._asleep():
            return self._memo(key, None) or {}
        now = time.time()
        first = time.strftime("%Y-%m-%d", time.localtime(self._day_start(now) - (int(days) - 1) * 86400 + 3600))
        out = {}
        try:
            conn = self._connect()
            try:
                for day, k, v in conn.execute("SELECT day, metric, value FROM daily_summary WHERE day >= ?", (first,)):
                    out.setdefault(day, {})[k] = v
                wm, has = self._daily_watermark(conn)
                # 尚未做过首次归档时（刚升级、首个整点维护前）只实时汇总今天，避免请求线程扫全库
                tail_start = wm if has else max(wm, self._day_start(now))
                for (day, k), v in self._daily_aggregate(conn, tail_start, now + 1, default_dt).items():
                    if day < first:
                        continue
                    d = out.setdefault(day, {})
                    if k in self._DAILY_MAX_KEYS:
                        d[k] = max(d.get(k, v), v)
                    else:
                        d[k] = d.get(k, 0.0) + v
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()
            return {}
        out["_meta"] = {"archived_until": wm if has else None}
        return self._memo(key, out)

    def cleanup(self, retention_days, archive_enabled=False, daily_target=None, default_dt=60):
        """删除过期历史；开启上月汇总时先在同一事务内归档到水位线。
        归档目标 = max(保留期截止点, 本月 1 日 0 点)：上月数据在月初即完整归档，
        即将删除的本月早期数据也在删除前归档。按指标逐个 DELETE（走 idx_metric_ts），
        不做全表扫描。"""
        import datetime
        cutoff = time.time() - retention_days * 86400
        _n = datetime.datetime.now()
        month_start = datetime.datetime(_n.year, _n.month, 1).timestamp()
        conn = self._connect(write=True)
        try:
            if archive_enabled:
                # 归档 + 推进水位线在独立事务中原子完成；之后即使删除前进程退出，
                # 下次也不会重复归档（ts < 水位线的行不再计入）
                try:
                    with conn:
                        self._archive_until(conn, max(cutoff, month_start))
                except Exception:
                    traceback.print_exc()
                    print("[fnmonitor] 月度汇总归档失败，本次仅清理过期数据")
            # 每日汇总：始终开启（数据量极小），删除前先把即将删除与最近的数据都汇总到水位线
            try:
                tgt = time.time() - 600 if daily_target is None else daily_target
                self.archive_daily(max(tgt, cutoff), default_dt=default_dt)
            except Exception:
                traceback.print_exc()
                print("[fnmonitor] 每日汇总失败，本次仅清理过期数据")
            with conn:
                for metric in self._distinct_metrics(conn):
                    conn.execute("DELETE FROM metrics WHERE metric=? AND ts<?", (metric, cutoff))
        except Exception:
            traceback.print_exc()
        finally:
            conn.close()

    def metrics_size(self):
        try:
            return os.path.getsize(self.db_path)
        except Exception:
            return 0


# ---------------------------------------------------------------------------
# 采集线程
# ---------------------------------------------------------------------------
class Collector(threading.Thread):
    def __init__(self, data_dir, config, cfg_dir=None, updater=None):
        super().__init__(daemon=True)
        self.data_dir = data_dir
        self._cfg_dir = cfg_dir or data_dir  # config.json 固定读默认配置目录
        self.config = config
        self.updater = updater  # UpdateManager：自动检查更新（可为 None）
        self.db = History(os.path.join(data_dir, "monitor.db"))
        self.latest = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._prev_cpu = None
        self._prev_cores = None
        self._prev_net = None
        self._prev_net_ts = None
        self._prev_diskio = None
        self._prev_diskio_ts = 0.0
        self._prev_disk_percent = {}
        self._last_docker = None
        self._last_docker_ts = 0.0
        self._ctr_net_prev = {}   # 容器 ID → (ts, 累计入, 累计出, 计数来源)，用于实时网速差分
        self._last_modules = []
        self._last_modules_ts = 0.0
        self._last_apps = []
        self._last_apps_ts = 0.0
        self._last_disks_detail = []
        self._last_disks_detail_ts = 0.0
        self._last_ports = {"apps": [], "docker": [], "listeners": [], "ts": 0.0}
        self._last_ports_ts = 0.0
        self._last_hardware = {}
        self._last_hardware_ts = 0.0
        self._last_raid = []
        self._last_raid_ts = 0.0
        self._last_raidcard = {"type": "none", "label": "", "detail": ""}
        self._last_raidcard_ts = 0.0
        self._last_sensors = {"temps": [], "fans": [], "volts": [], "available": False, "fans_source": ""}
        self._last_sensors_ts = 0.0
        self._last_fans = []
        self._last_fans_ts = 0.0
        self._last_power = {"ok": False}
        self._last_power_ts = 0.0
        self._cpu_topo = None  # CPU 拓扑（物理核/逻辑线程/插槽），运行期不变只解析一次
        self._last_gpu = []
        self._last_gpu_ts = 0.0
        self._last_memory = {"items": [], "dual": False}
        self._last_memory_ts = 0.0
        self._last_hist_ts = 0.0
        self._last_cleanup_ts = 0.0
        # 数据盘休眠期间暂存未落盘的历史采样行（见 _tick 休眠保护）
        self._pending_rows = []
        self._cfg_mtime = 0.0

    def stop(self):
        self._stop.set()

    def _reload_config(self):
        """检查配置文件是否变化，变化则重载。"""
        cfg_path = os.path.join(self._cfg_dir, "config.json")
        try:
            mtime = os.path.getmtime(cfg_path)
        except Exception:
            return
        if mtime != self._cfg_mtime:
            self._cfg_mtime = mtime
            try:
                with open(cfg_path, "r", errors="ignore") as f:
                    cfg = json.load(f)
                interval = int(cfg.get("interval", DEFAULT_CONFIG["interval"]))
                retention = int(cfg.get("retention_days", DEFAULT_CONFIG["retention_days"]))
                if interval < 2:
                    interval = 2
                if retention < 1:
                    retention = 1
                self.config["interval"] = interval
                self.config["retention_days"] = retention
            except Exception:
                pass

    def run(self):
        self.db._init()
        try:
            self._tick()  # 启动后立即采集一次，避免首屏无数据
        except Exception:
            pass
        while not self._stop.wait(self.config.get("interval", 10)):
            try:
                self._tick()
            except Exception:
                pass

    def _tick(self):
        self._reload_config()
        # 同步休眠保护开关（配置保存后立即生效，无需重启）
        RUNTIME["standby_protect"] = bool(int(self.config.get("disk_standby_protect", 1) or 0))
        RUNTIME["ec_fan_probe"] = bool(int(self.config.get("ec_fan_probe", 0) or 0))
        now = time.time()
        interval = self.config.get("interval", 10)
        RUNTIME["interval"] = interval
        # 每个采集周期统一探测一次硬盘电源状态，请求线程在周期内只读缓存
        try:
            disk_power_states(force=True)
        except Exception:
            pass

        snapshot = {}
        # ---- CPU ----
        total, cores = read_cpu_times()
        cpu_percent = calc_cpu_percent(self._prev_cpu, total)
        per_core = []
        if self._prev_cores and cores and len(cores) == len(self._prev_cores):
            per_core = [calc_cpu_percent(a, b) for a, b in zip(self._prev_cores, cores)]
        self._prev_cpu = total
        self._prev_cores = cores
        cores_num = len(cores) if cores else 0  # /proc/stat 的 cpuN 条目数 = 逻辑线程数
        # CPU 拓扑只解析一次（运行期不变）；cores=物理核数，threads=逻辑线程数，与硬件面板同源
        if self._cpu_topo is None:
            try:
                self._cpu_topo = read_cpu_topology()
            except Exception:
                self._cpu_topo = (0, cores_num, 1)
        phys_cores, logical_threads, cpu_sockets = self._cpu_topo
        load = read_loadavg()
        snapshot["cpu"] = {
            "percent": cpu_percent, "per_core": per_core, "load": load,
            "cores": phys_cores or cores_num,
            "threads": logical_threads or cores_num,
            "sockets": cpu_sockets or 1,
            "logical_cpus": cores_num,
            "model": read_cpu_model(),
            "frequency_mhz": read_int_file("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq") // 1000,
        }
        # ---- 内存 ----
        snapshot["mem"] = read_meminfo()
        # ---- 磁盘 ----
        disks = collect_disks()
        snapshot["disks"] = disks
        # 「存储总使用」口径：fnOS 存储池挂载在 /vol1、/vol2…，与系统盘是不同文件系统；
        # 存在存储池时只统计存储池（与 fnOS 存储页一致，避免系统小分区混入），
        # 没有存储池（开发机/普通 Linux）时回退为全部本地磁盘。
        pools = [d for d in disks if re.match(r"^/vol\d+($|/)", d["mount"])]
        scope = pools if pools else disks
        used_all = sum(d["used"] for d in scope)
        total_all = sum(d["total"] for d in scope)
        disk_total_percent = round(used_all / total_all * 100, 1) if total_all else 0.0
        snapshot["disk_total"] = disk_total_percent
        # 字节口径与百分比同源下发，前端不再自行累加，保证两处显示永远一致
        snapshot["disk_used"] = used_all
        snapshot["disk_size"] = total_all
        snapshot["disk_pools"] = len(pools)
        # ---- 磁盘详情（物理硬盘，每 60 秒；休眠盘复用上一次结果） ----
        if now - self._last_disks_detail_ts >= 60:
            self._last_disks_detail = collect_disks_detail(self._last_disks_detail)
            self._last_disks_detail_ts = now
        snapshot["disks_detail"] = self._last_disks_detail
        # ---- 网络 ----
        ifaces = collect_net(self._prev_net, self._prev_net_ts, now)
        self._prev_net = read_net_dev()
        self._prev_net_ts = now
        snapshot["net"] = ifaces
        # ---- 磁盘 IO ----
        diskio = collect_disk_io(self._prev_diskio, self._prev_diskio_ts, now)
        self._prev_diskio = read_diskstats()
        self._prev_diskio_ts = now
        snapshot["diskio"] = diskio
        # ---- 温度 ----
        temps = read_temps()
        # 硬 RAID 物理盘温度并入总览温度卡（读自阵列卡缓存，300 秒刷新；
        # 名称形如「阵列 32:0」，经阵列卡传感器读取、不唤醒硬盘）
        for _c in ((self._last_raidcard or {}).get("controllers") or []):
            for _p in (_c.get("pds") or []):
                if _p.get("temp") is not None:
                    temps["disks"].append({"name": "阵列 " + str(_p.get("loc")),
                                           "temp": _p.get("temp")})
        snapshot["temp"] = temps
        # ---- 实时功耗（每 10 秒）：RAPL 硬件传感器优先，不可用时即时回退估算模型 ----
        # 必须在采集线程统一给出快照值：「实时功耗」与「功耗统计」两个面板都读这里，
        # 否则 RAPL 不可用的机器（多数 AMD/ARM）会出现面板显示 0W/不可用、
        # 而趋势图却有估算数据的自相矛盾。
        # 注意口径：RAPL package 只是 CPU 封装功耗（Jasper Lake N5095 等 SoC 空闲
        # 仅 2-3W，且通常没有 DRAM 域），不等于整机插座功耗——主板/内存/网卡/
        # 电源转换损耗与活动硬盘都在封装之外。整机 total = package + dram
        # + 主板等固定底座(power_base_w) + 活动硬盘数 * 单盘功耗，休眠盘不计。
        try:
            if now - self._last_power_ts >= 10:
                # 活动硬盘数：硬盘详情每 60 秒标注 state；未知(unknown)按活动计，
                # 与旧行为一致，明确 standby 的盘才剔除
                disks_active = sum(1 for d in (self._last_disks_detail or [])
                                   if d.get("state") != "standby")
                disk_w_unit = float(self.config.get("power_disk_typical_w", 8) or 0)
                disks_w = round(disks_active * disk_w_unit, 2)
                rp = get_rapl_power()
                if rp.get("ok"):
                    base_w = float(self.config.get("power_base_w", 8) or 0)
                    pkg = float(rp.get("package") or 0)
                    dram = float(rp.get("dram") or 0)
                    rp["source"] = "rapl"
                    rp["base"] = round(base_w, 2)
                    rp["disks_w"] = disks_w
                    rp["disks_active"] = disks_active
                    rp["total"] = round(pkg + dram + base_w + disks_w, 2)
                    self._last_power = rp
                else:
                    try:
                        nics_active = len([i for i in ifaces
                                           if not i["iface"].startswith(VIRT_IFACE_PREFIXES)])
                        est_w = estimate_power(
                            cpu_percent, disks_active, nics_active,
                            float(self.config.get("power_tdp_w", 65)),
                            disk_w_unit,
                            float(self.config.get("power_nic_fixed_w", 5)),
                        )
                        self._last_power = {"ok": True, "total": est_w, "source": "estimated",
                                            "package": None, "core": None, "uncore": None,
                                            "dram": None, "base": None,
                                            "disks_w": disks_w, "disks_active": disks_active}
                    except Exception:
                        self._last_power = {"ok": False, "source": "unknown"}
                self._last_power_ts = now
        except Exception:
            self._last_power = {"ok": False, "source": "unknown"}
        snapshot["power"] = self._last_power
        # ---- GPU 实时（每 10 秒） ----
        try:
            if now - self._last_gpu_ts >= 10:
                self._last_gpu = collect_gpu()
                self._last_gpu_ts = now
        except Exception:
            self._last_gpu = []
        snapshot["gpu"] = self._last_gpu
        # ---- 内存插槽 SPD（每 300 秒） ----
        try:
            if now - self._last_memory_ts >= 300:
                self._last_memory = collect_memory()
                self._last_memory_ts = now
        except Exception:
            self._last_memory = {"items": [], "dual": False}
        snapshot["memory"] = self._last_memory
        # ---- 运行时间 ----
        snapshot["uptime"] = read_uptime()
        snapshot["ts"] = now
        snapshot["interval"] = interval

        # ---- 写历史（默认每 60 秒一条，持久保存，重开应用不丢失；可在设置中改采样间隔） ----
        hist_interval = int(self.config.get("history_interval", DEFAULT_CONFIG["history_interval"]) or DEFAULT_CONFIG["history_interval"])
        if now - self._last_hist_ts >= hist_interval:
            self._last_hist_ts = now
            def _r2(v):
                try:
                    return round(float(v), 2)
                except Exception:
                    return 0.0
            # 行格式必须与 write() 的 INSERT INTO metrics(ts, metric, value) 一致，即 (时间戳, 指标名, 数值)。
            # 此前误写成 (指标名, 时间戳, 数值) 导致两列互换，历史查询 WHERE metric=? AND ts>=? 永远查不到，
            # 趋势图每次打开都只能从零开始累积（历史数据全部无法显示）。
            rows = [(now, "cpu", _r2(cpu_percent))]
            mem_percent = snapshot["mem"]["percent"]
            rows.append((now, "mem", _r2(mem_percent)))
            load1 = load[0] if load else 0.0
            rows.append((now, "load1", _r2(load1)))
            for d in disks:
                if d["total"] > 0:
                    rows.append((now, "disk:" + d["mount"], _r2(d["percent"])))
            rows.append((now, "disk_total", _r2(disk_total_percent)))
            for it in ifaces:
                rows.append((now, "net_rx:" + it["iface"], _r2(it["rx_rate"])))
                rows.append((now, "net_tx:" + it["iface"], _r2(it["tx_rate"])))
            # ---- 网卡累计字节/丢包/错误（历史维度，用于流量周期汇总） ----
            virt_prefixes = VIRT_IFACE_PREFIXES
            exclude_bridge = bool(self.config.get("traffic_exclude_bridge", 0))
            for it in ifaces:
                iname = it["iface"]
                is_virt = iname.startswith(virt_prefixes)
                if is_virt and exclude_bridge:
                    continue
                rows.append((now, "net_rx_bytes:" + iname, float(it["rx_bytes"])))
                rows.append((now, "net_tx_bytes:" + iname, float(it["tx_bytes"])))
                rows.append((now, "net_drop:" + iname, float(it.get("rx_drop", 0) + it.get("tx_drop", 0))))
                rows.append((now, "net_err:" + iname, float(it.get("rx_errs", 0) + it.get("tx_errs", 0))))
            if temps.get("cpu"):
                rows.append((now, "temp", _r2(temps["cpu"])))
            if temps.get("system"):
                rows.append((now, "temp_mb", _r2(temps["system"])))
            # ---- 功耗（历史维度）：与接口实时快照同源（RAPL 实测 / 模型估算） ----
            pw = self._last_power or {}
            if pw.get("ok") and pw.get("total"):
                rows.append((now, "power", _r2(pw.get("total"))))
                if pw.get("source") == "estimated":
                    # 估算值额外写一份 power_est，导出 CSV 时可区分实测/估算口径
                    rows.append((now, "power_est", _r2(pw.get("total"))))
            # ---- 风扇平均 RPM（历史维度） ----
            fans = self._last_sensors.get("fans", [])
            valid_rpm = [f["rpm"] for f in fans if f.get("rpm")]
            if valid_rpm:
                rows.append((now, "fan_avg", _r2(sum(valid_rpm) / len(valid_rpm))))
            # ---- 磁盘 IO（历史维度：读/写 MB/s） ----
            for it in diskio:
                rows.append((now, "diskio_r:" + str(it.get("name", "?")),
                             _r2(float(it.get("read_rate", 0) or 0) / 1048576.0)))
                rows.append((now, "diskio_w:" + str(it.get("name", "?")),
                             _r2(float(it.get("write_rate", 0) or 0) / 1048576.0)))
            # ---- 容器累计流量（历史维度） ----
            try:
                for c in (self._last_docker.get("containers") or []):
                    if c.get("state") != "running":
                        continue
                    cname = c.get("name") or ""
                    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", cname):
                        continue
                    rows.append((now, "ctr_in:" + cname, float(c.get("net_in_bytes", 0))))
                    rows.append((now, "ctr_out:" + cname, float(c.get("net_out_bytes", 0))))
            except Exception:
                pass
            # ---- 休眠保护：monitor.db 通常位于存储池 @appdata（机械硬盘）上，
            # INSERT/commit 会把已休眠的硬盘唤醒。数据盘休眠时把采样行暂存内存，
            # 等硬盘下次被唤醒（正常访问）时连同时间戳一次性补写，历史不丢点 ----
            if path_on_standby(self.data_dir):
                self._pending_rows.extend(rows)
                # 上限保护：硬盘连续休眠多日时避免内存无限增长
                if len(self._pending_rows) > 100000:
                    del self._pending_rows[:-100000]
            else:
                if self._pending_rows:
                    rows = self._pending_rows + rows
                    self._pending_rows = []
                self.db.write(rows)
        # ---- 清理过期历史（每小时一次即可；原先每 10 秒一次 DELETE，大表时白耗 CPU/IO 并放大 WAL 写入） ----
        # 数据盘休眠时同样跳过（DELETE 会唤醒硬盘），且不更新时间戳，醒来后尽快补清理
        if now - self._last_cleanup_ts >= 3600 and not path_on_standby(self.data_dir):
            self._last_cleanup_ts = now
            try:
                # 开启上月汇总时，cleanup 前先把即将删除的数据按月叠加归档
                archive = bool(int(self.config.get("monthly_summary_enabled", 0) or 0))
                # 每日汇总的水位线不能越过尚未落盘的休眠暂存行（它们稍后才以原时间戳补写）
                dtarget = now - 600
                if self._pending_rows:
                    dtarget = min(dtarget, min(r[0] for r in self._pending_rows) - 1)
                hist_iv = int(self.config.get("history_interval", DEFAULT_CONFIG["history_interval"]) or 60)
                self.db.cleanup(self.config.get("retention_days", 7), archive_enabled=archive,
                                daily_target=dtarget, default_dt=hist_iv)
            except Exception:
                pass

        # 月初补全已并入上面的 cleanup（水位线归档：上月数据在每月 1 日首次整点维护时
        # 一次性完整归档，任何数据只计入一次；旧版月初 1-3 日各累加一次的重复计数已修复）

        # ---- Docker（每 30 秒；docker ps 为外部进程调用，容器列表变化慢，无需高频） ----
        if now - self._last_docker_ts >= 30:
            self._last_docker = collect_docker()
            self._last_docker_ts = now
            # 容器实时网速：与上一轮的累计字节差分（约 30 秒窗口均值）
            try:
                self._ctr_net_prev = container_net_rates(
                    self._ctr_net_prev, self._last_docker.get("containers") or [],
                    self._last_docker.get("sampled_at") or now)
            except Exception:
                traceback.print_exc()
        snapshot["docker"] = self._last_docker

        # ---- 功能模块（每 60 秒；systemctl / docker images / ss 均为外部进程调用） ----
        try:
            if now - self._last_modules_ts >= 60:
                self._last_modules = collect_modules(self._last_docker)
                self._last_modules_ts = now
        except Exception:
            traceback.print_exc()
        snapshot["modules"] = self._last_modules

        # ---- 端口占用（每 60 秒） ----
        try:
            if now - self._last_ports_ts >= 60:
                self._last_ports = collect_ports(self._last_docker)
                self._last_ports_ts = now
        except Exception:
            traceback.print_exc()
        snapshot["ports"] = self._last_ports

        # ---- 硬件信息（每 120 秒） ----
        if now - self._last_hardware_ts >= 120:
            self._last_hardware = collect_hardware()
            self._last_hardware_ts = now
        snapshot["hardware"] = self._last_hardware

        # ---- RAID 状态（每 120 秒） ----
        if now - self._last_raid_ts >= 120:
            self._last_raid = collect_raid()
            self._last_raid_ts = now
        snapshot["raid"] = self._last_raid

        # ---- 阵列卡（每 300 秒） ----
        if now - self._last_raidcard_ts >= 300:
            self._last_raidcard = collect_raid_card()
            self._last_raidcard_ts = now
        snapshot["raidcard"] = self._last_raidcard

        # ---- 传感器：温度分类 / 风扇 / 电压（每 15 秒） ----
        if now - self._last_sensors_ts >= 15:
            self._last_sensors = collect_sensors()
            self._last_sensors_ts = now
        snapshot["sensors"] = self._last_sensors

        # ---- 风扇通道枚举（每 15 秒，供控制面板使用） ----
        if now - self._last_fans_ts >= 15:
            self._last_fans = _hwmon_fans()
            self._last_fans_ts = now
        snapshot["fans"] = self._last_fans

        # ---- 内置应用统计（相册/影视/音乐，每 10 分钟；目录遍历 + 数据库 COUNT 为重操作，手动刷新按钮可即时更新） ----
        # 数据盘休眠时跳过本轮（不更新时间戳，醒来后尽快补采），避免目录扫描唤醒机械硬盘
        if now - self._last_apps_ts >= 600:
            try:
                if RUNTIME.get("standby_protect", True) and _builtin_appdirs_sleeping():
                    pass
                else:
                    self._last_apps = collect_app_stats()
                    self._last_apps_ts = now
            except Exception:
                self._last_apps = collect_app_stats()
                self._last_apps_ts = now
        snapshot["apps"] = self._last_apps

        # ---- 在线更新自动检查（每 6 小时）----
        # update_autoupdate（自动更新）：检查到新版本后自动下载并安装（解包覆盖 + 重启服务）
        # update_autocheck/update_autodownload（旧版兼容）：仅检查 / 仅下载到数据目录
        # 网络请求（最多 5 个镜像 × 60s 超时）放到独立后台线程，绝不阻塞采集循环
        if self.updater and (self.config.get("update_autoupdate") or self.config.get("update_autocheck")):
            if now - self.updater._last_check >= UPDATE_CHECK_INTERVAL:
                self.updater.start_auto_job(bool(self.config.get("update_autoupdate")),
                                            bool(self.config.get("update_autodownload")))

        with self._lock:
            self.latest = snapshot

    def get_snapshot(self):
        with self._lock:
            return dict(self.latest or {})


# ---------------------------------------------------------------------------
# 在线更新（GitHub Release）
# ---------------------------------------------------------------------------
def _ver_tuple(v):
    """'v2.9.0' / '2.9.0' -> (2, 9, 0)，用于版本比较。"""
    try:
        return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])
    except Exception:
        return (0, 0, 0)


def _gh_open(url, timeout=30, mirrors=None):
    """打开 GitHub 下载地址：直连失败后自动尝试加速镜像（返回 response，全失败抛最后异常）。
    注意：镜像只用于传输字节，内容必须再经官方 SHA256 校验后才能使用。"""
    last = None
    for m in (GH_MIRRORS if mirrors is None else mirrors):
        u = (m + url) if m else url
        try:
            return urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "fnmonitor"}), timeout=timeout)
        except Exception as e:
            last = e
    raise last or RuntimeError("无可用下载地址")


MAX_PACKAGE_BYTES = 64 * 1024 * 1024    # 安装包正常约 300KB，上限防止恶意镜像撑爆内存
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_extract(t, dest):
    """安全解包：逐项校验——拒绝绝对路径、'..'、指向目标目录之外（或含 '..'）的链接、
    设备 / FIFO 等特殊文件，并去掉 setuid 位；Python 支持时再叠加 filter='data'。"""
    import tarfile
    root = os.path.realpath(dest)

    def _inside(p):
        rp = os.path.realpath(p)
        return rp == root or rp.startswith(root + os.sep)

    members = []
    for m in t.getmembers():
        name = m.name.replace("\\", "/")
        parts = [x for x in name.split("/") if x not in ("", ".")]
        if name.startswith("/") or re.match(r"^[A-Za-z]:", name) or ".." in parts:
            raise ValueError("安装包含非法路径: %s" % m.name)
        target = os.path.join(root, *parts) if parts else root
        if not _inside(target):
            raise ValueError("安装包路径越界: %s" % m.name)
        if m.issym() or m.islnk():
            ln = m.linkname.replace("\\", "/")
            lparts = [x for x in ln.split("/") if x not in ("", ".")]
            if ln.startswith("/") or ".." in lparts:
                raise ValueError("安装包含越界链接: %s -> %s" % (m.name, m.linkname))
            base = os.path.dirname(target) if m.issym() else root
            if not _inside(os.path.join(base, *lparts)):
                raise ValueError("安装包含越界链接: %s -> %s" % (m.name, m.linkname))
        elif not (m.isfile() or m.isdir()):
            raise ValueError("安装包含特殊文件: %s" % m.name)
        m.mode = m.mode & 0o755
        m.uid = m.gid = 0
        members.append(m)
    if hasattr(tarfile, "data_filter"):
        t.extractall(dest, members=members, filter="data")   # 逐项校验之外再叠加官方过滤器
    else:
        t.extractall(dest, members=members)


class UpdateManager:
    """基于 GitHub Releases 的在线更新：检查新版本、把安装包下载到 NAS（代理下载，
    解决浏览器直连 GitHub 慢的问题）。自动检查由采集线程按周期在后台线程中触发。

    供应链加固（v2.18.0）：
    - 每个安装包都必须有官方 SHA256：优先 GitHub Release 资产的 digest 字段；缺失时
      直连 raw.githubusercontent.com 读取仓库 fnpack.json 中该版本的 sha256（不走镜像）；
      两者都没有则拒绝下载 / 安装（fail-closed）。
    - 镜像只用于传输字节，下载结果校验通过才会落盘或发给浏览器。
    - 安装包名须精确匹配 fnmonitor-<版本>-<架构>.fpk；安全解包；安装互斥锁；拒绝降级 / 同版本重装。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._install_lock = threading.Lock()
        self._job = None
        self._last_check = 0.0
        self._cache = None            # 最近一次 check() 完整结果（30 分钟缓存）
        self._status = {"last_check": "", "latest": "", "has_update": False,
                        "downloading": False, "installing": False, "downloaded_file": "",
                        "download_dir": "", "error": ""}

    # ---- 工具 ----
    def detect_arch(self):
        """本机架构 -> 安装包平台名（x86 / arm）。"""
        try:
            m = os.uname().machine.lower()
        except Exception:
            m = ""
        return "arm" if ("aarch64" in m or m.startswith("arm")) else "x86"

    def update_dir(self):
        return os.path.join(self.data_dir, "update")

    def status(self):
        with self._lock:
            return dict(self._status)

    def _fnpack_sha256(self, version, arch):
        """从仓库 fnpack.json（直连 raw.githubusercontent.com，不经镜像）读取指定版本的 sha256。"""
        url = "https://raw.githubusercontent.com/%s/main/fnpack.json" % UPDATE_REPO
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fnmonitor"})
            with urllib.request.urlopen(req, timeout=15) as r:
                data = json.loads(r.read(4 * 1024 * 1024).decode("utf-8", "ignore"))
            for app in (data.get("apps") or {}).values():
                rel = ((app or {}).get("releases") or {}).get(version) or {}
                sha = str(((rel.get("packages") or {}).get(arch) or {}).get("sha256") or "").lower()
                if _SHA256_RE.match(sha):
                    return sha
        except Exception:
            pass
        return ""

    # ---- 检查 ----
    def check(self, force=False):
        """查询 GitHub 最新 Release。结果缓存 30 分钟；force=True 跳过缓存。"""
        with self._lock:
            if not force and self._cache and time.time() - self._last_check < 1800:
                return dict(self._cache)
        arch = self.detect_arch()
        info = {"ok": False, "current": VERSION, "arch": arch, "latest": "",
                "has_update": False, "notes": "", "published_at": "", "html_url":
                "https://github.com/%s/releases/latest" % UPDATE_REPO,
                "asset": None, "error": ""}
        try:
            req = urllib.request.Request(
                "https://api.github.com/repos/%s/releases/latest" % UPDATE_REPO,
                headers={"Accept": "application/vnd.github+json", "User-Agent": "fnmonitor"})
            with urllib.request.urlopen(req, timeout=10) as r:
                rel = json.loads(r.read(8 * 1024 * 1024).decode("utf-8", "ignore"))
            latest = str(rel.get("tag_name") or "").lstrip("vV")
            if not re.match(r"^\d+(\.\d+){1,3}$", latest):
                raise ValueError("Release 版本号格式异常: %s" % latest[:40])
            info["ok"] = True
            info["latest"] = latest
            info["has_update"] = _ver_tuple(latest) > _ver_tuple(VERSION)
            info["notes"] = str(rel.get("body") or "")[:3000]
            info["published_at"] = str(rel.get("published_at") or "")[:40]
            hu = str(rel.get("html_url") or "")
            if hu.startswith("https://github.com/"):
                info["html_url"] = hu
            # 只接受精确命名的当前架构安装包（去掉旧版「任一同平台包」兜底）
            want = "fnmonitor-%s-%s.fpk" % (latest, arch)
            for a in rel.get("assets") or []:
                if str(a.get("name")) == want:
                    url = str(a.get("browser_download_url") or "")
                    if not url.startswith("https://github.com/"):
                        break
                    digest = str(a.get("digest") or "").lower()
                    digest = digest[7:] if digest.startswith("sha256:") else ""
                    src = "github"
                    if not _SHA256_RE.match(digest):
                        digest, src = self._fnpack_sha256(latest, arch), "fnpack.json"
                    info["asset"] = {"name": want, "size": int(a.get("size") or 0),
                                     "download_url": url,
                                     "digest": digest if _SHA256_RE.match(digest or "") else "",
                                     "digest_source": src if digest else ""}
                    break
        except Exception as e:
            info["error"] = str(e)[:300]
        with self._lock:
            self._cache = dict(info)
            self._last_check = time.time()
            st = self._status
            st["error"] = info["error"]
            if info["ok"]:
                st["latest"] = info["latest"]
                st["has_update"] = info["has_update"]
                st["last_check"] = time.strftime("%Y-%m-%d %H:%M:%S")
        return info

    # ---- 下载（镜像传输 + 官方 SHA256 校验） ----
    def fetch_verified(self, asset):
        """依次尝试直连 / 各镜像下载安装包到内存，SHA256 与官方值一致才返回。
        返回 (bytes, None) 或 (None, 错误信息)。"""
        expect = str((asset or {}).get("digest") or "").lower()
        if not _SHA256_RE.match(expect):
            return None, "缺少官方 SHA256 校验值（Release digest 与 fnpack.json 均未提供），已拒绝下载"
        url = (asset or {}).get("download_url") or ""
        if not url.startswith("https://github.com/"):
            return None, "安装包下载地址异常"
        last = "下载失败"
        with self._lock:
            self._status["downloading"] = True
        try:
            for m in GH_MIRRORS:
                try:
                    with _gh_open(url, timeout=60, mirrors=[m]) as r:
                        buf = io.BytesIO()
                        while True:
                            chunk = r.read(65536)
                            if not chunk:
                                break
                            buf.write(chunk)
                            if buf.tell() > MAX_PACKAGE_BYTES:
                                raise ValueError("安装包体积异常")
                    data = buf.getvalue()
                    if hmac.compare_digest(hashlib.sha256(data).hexdigest(), expect):
                        return data, None
                    last = "安装包 SHA256 校验失败（%s）" % ("直连" if not m else "镜像 " + m)
                except Exception as e:
                    last = str(e)[:200]
            return None, last
        finally:
            with self._lock:
                self._status["downloading"] = False

    @staticmethod
    def _dest_allowed(ddir, default_dir):
        """NAS 下载目录只允许：默认 数据目录/update，或存储池 /volN/ 下的路径。"""
        if not os.path.isabs(ddir):
            return False
        rp = os.path.realpath(ddir)
        if rp == os.path.realpath(default_dir):
            return True
        return bool(re.match(r"^/vol\d+/", rp + "/")) and rp.count("/") >= 2

    def download_to_nas(self, asset, dest_dir=None):
        """把安装包下载并校验后保存到指定目录（默认 数据目录/update/）。返回 (成功?, 文件路径/错误)。"""
        name = (asset or {}).get("name") or ""
        if not re.match(r"^fnmonitor-[0-9.]+-(x86|arm)\.fpk$", name):
            return False, "安装包文件名异常"
        ddir = dest_dir or self.update_dir()
        if not self._dest_allowed(ddir, self.update_dir()):
            return False, "保存目录须为存储池下的绝对路径（如 /vol1/更新包）"
        data, err = self.fetch_verified(asset)
        if data is None:
            return False, err
        final = os.path.join(ddir, name)
        tmp = final + ".tmp"
        try:
            os.makedirs(ddir, exist_ok=True)
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, final)
            if dest_dir is None:  # 仅记录默认目录的下载状态
                with self._lock:
                    self._status["downloaded_file"] = final
                    self._status["download_dir"] = ddir
            return True, final
        except Exception as e:
            try:
                os.remove(tmp)
            except Exception:
                pass
            return False, str(e)

    # ---- 一键更新：检查 → 下载校验 → 安装（互斥、拒绝降级） ----
    def install_latest(self, force_check=False):
        if not self._install_lock.acquire(blocking=False):
            return {"ok": False, "error": "正在安装更新，请稍候"}
        try:
            with self._lock:
                self._status["installing"] = True
            info = self.check(force=force_check)
            if not info.get("ok"):
                return {"ok": False, "error": info.get("error") or "检查更新失败"}
            if _ver_tuple(info.get("latest")) <= _ver_tuple(VERSION):
                return {"ok": False, "error": "当前已是最新版本（%s），不执行降级或重复安装" % VERSION}
            asset = info.get("asset")
            if not asset:
                return {"ok": False, "error": "Release 中未找到 %s 平台安装包" % info.get("arch")}
            ok, fpk = self.download_to_nas(asset)
            if not ok:
                return {"ok": False, "error": "下载失败: %s" % fpk}
            ok, msg = self.install_fpk(fpk, expect_sha256=asset.get("digest"))
            if ok:
                return {"ok": True, "note": msg, "version": info.get("latest")}
            return {"ok": False, "error": msg}
        finally:
            with self._lock:
                self._status["installing"] = False
            self._install_lock.release()

    def start_auto_job(self, autoupdate, autodownload):
        """采集线程调用：在后台线程执行自动检查 / 下载 / 安装，已有任务在跑则跳过。"""
        if self._job and self._job.is_alive():
            return

        def _run():
            try:
                uinfo = self.check(force=True)
                if uinfo.get("has_update") and uinfo.get("asset"):
                    if autoupdate:
                        self.install_latest()
                    elif autodownload and not self.downloaded_path():
                        self.download_to_nas(uinfo["asset"])
            except Exception:
                traceback.print_exc()

        self._job = threading.Thread(target=_run, name="fnmonitor-update", daemon=True)
        self._job.start()

    # ---- 自动安装（解包 fpk 覆盖应用目录后自我重启） ----
    def install_fpk(self, fpk_path, expect_sha256=None):
        """解包 fpk（app.tgz + cmd + manifest）并覆盖应用目录（先备份，校验失败自动回滚），
        成功后延迟 1.5 秒替换当前进程重启服务。返回 (成功?, 消息)。"""
        import shutil
        import sys
        import tarfile
        if expect_sha256:
            h = hashlib.sha256()
            try:
                with open(fpk_path, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
            except Exception as e:
                return False, "读取安装包失败: %s" % e
            if not hmac.compare_digest(h.hexdigest(), str(expect_sha256).lower()):
                return False, "安装包 SHA256 校验失败，已拒绝安装"
        app_dir = os.path.dirname(os.path.abspath(__file__))       # 应用安装目录（NAS 上 server.py 平铺于 TRIM_APPDEST）
        tmp = os.path.join(self.data_dir, "update", "_extract")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        try:
            # fpk 结构：app.tgz（app 内容压缩包）+ cmd/ + manifest，均为平铺
            with tarfile.open(fpk_path, "r:*") as t:
                _safe_extract(t, tmp)
            inner = os.path.join(tmp, "app.tgz")
            if os.path.isfile(inner) and not os.path.islink(inner):
                app_src = os.path.join(tmp, "app")
                os.makedirs(app_src, exist_ok=True)
                with tarfile.open(inner, "r:*") as t2:
                    _safe_extract(t2, app_src)                     # server.py、ui/ 等平铺在内
            else:
                app_src = tmp if os.path.exists(os.path.join(tmp, "server.py")) else None
            if not app_src or not os.path.exists(os.path.join(app_src, "server.py")):
                shutil.rmtree(tmp, ignore_errors=True)
                return False, "fpk 包内未找到 app 内容（app.tgz）"
            if not os.path.exists(os.path.join(tmp, "manifest")):
                shutil.rmtree(tmp, ignore_errors=True)
                return False, "fpk 包内缺少 manifest"
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, "安装包解压失败: %s" % e
        # 备份当前 app/，失败可回滚
        bak = app_dir + ".bak"
        shutil.rmtree(bak, ignore_errors=True)
        try:
            shutil.copytree(app_dir, bak)
        except Exception as e:
            shutil.rmtree(tmp, ignore_errors=True)
            return False, "备份当前程序失败: %s" % e
        try:
            # 覆盖 app 目录内容（保留旧 __pycache__ 无碍，编译校验以新源码为准）
            shutil.copytree(app_src, app_dir, dirs_exist_ok=True)
            for item in ("manifest", "cmd", "ICON.PNG"):
                s = os.path.join(tmp, item)
                if not os.path.exists(s):
                    continue
                d = os.path.join(app_dir, item)  # 覆盖到应用目录内；此前误写上级目录导致管理中心读到的 manifest 版本不更新
                if os.path.isdir(s):
                    shutil.copytree(s, d, dirs_exist_ok=True)
                else:
                    shutil.copy2(s, d)
            # 清掉旧字节码避免 Python 误用缓存，再对新代码做语法自检，失败自动回滚
            pycache = os.path.join(app_dir, "__pycache__")
            shutil.rmtree(pycache, ignore_errors=True)
            import py_compile
            py_compile.compile(os.path.join(app_dir, "server.py"), doraise=True)
        except Exception as e:
            shutil.rmtree(app_dir, ignore_errors=True)
            shutil.copytree(bak, app_dir)
            shutil.rmtree(tmp, ignore_errors=True)
            return False, "安装失败已回滚: %s" % e
        shutil.rmtree(tmp, ignore_errors=True)
        # fnOS 应用中心从 /var/apps/{TRIM_APPNAME}/manifest 读取版本号；
        # 该目录与应用可执行文件目录（TRIM_APPDEST，server.py 所在的 target）
        # 是两个不同位置：/var/apps/{appname}/ 是应用基础目录（manifest、ICON、cmd/），
        # target/ 是软链接指向的可执行文件目录。内置更新只覆盖了 target 下的 manifest，
        # 应用基础目录下的 manifest 仍是旧版本，导致应用中心显示版本号不刷新。
        # 这里把新版 manifest / ICON 同步到应用基础目录。
        app_name = os.environ.get("TRIM_APPNAME", "") or "fnmonitor"
        app_base = "/var/apps/%s" % app_name
        if os.path.isdir(app_base) and os.path.abspath(app_base) != os.path.abspath(app_dir):
            for item in ("manifest", "ICON.PNG", "ICON_256.PNG"):
                s = os.path.join(app_dir, item)
                if not os.path.exists(s):
                    continue
                d = os.path.join(app_base, item)
                try:
                    if os.path.isdir(s):
                        shutil.copytree(s, d, dirs_exist_ok=True)
                    else:
                        shutil.copy2(s, d)
                except Exception:
                    pass
        # 升级包会覆盖 app/ui/config（type 固定为 iframe）；open_mode 不再回写
        # ui/config，打开方式由前端在 iframe 内按 /api/config 的 open_mode 自动决定；
        # 监听端口若被覆盖回默认值，进程重启后的启动逻辑会重新同步。
        # 延迟替换进程重启（先让 HTTP 响应送达前端）
        def _restart():
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                os._exit(3)  # 兜底：退出交由系统服务拉起
        timer = threading.Timer(1.5, _restart)
        timer.daemon = False
        timer.start()
        return True, "新版本已安装，服务正在重启…"

    def downloaded_path(self):
        with self._lock:
            return self._status.get("downloaded_file") or ""


# ---------------------------------------------------------------------------
# HTTP 服务
# ---------------------------------------------------------------------------
class MonitorApp:
    def __init__(self, data_dir, config, host="0.0.0.0", port=8777, cfg_dir=None):
        self.data_dir = data_dir
        self._cfg_dir = cfg_dir or data_dir  # 配置（config.json）固定写默认目录，data_dir 只存数据文件
        self.config = config
        self.host = host
        self.port = port
        self.updater = UpdateManager(data_dir)  # 在线更新（须先于 Collector 创建）
        self.collector = Collector(data_dir, config, cfg_dir=self._cfg_dir, updater=self.updater)
        self.www_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "www")
        self.db_path = os.path.join(data_dir, "monitor.db")
        self._weather_cache = None
        # ---- API 响应缓存：多客户端轮询时复用，避免重复查库 / 序列化（CPU 优化核心之一） ----
        self._overview_bytes = b""
        self._overview_ts = 0.0
        self._sysinfo_cache = None
        self._sysinfo_ts = 0.0
        self._proc_cache = None
        self._proc_ts = 0.0
        self._hist_cache = {}
        self._weather_ts = 0.0
        # 流量/功耗统计 API 缓存（5s TTL，同 overview 模式）
        self._traffic_cache = None
        self._traffic_ts = 0.0
        self._power_stats_cache = None
        self._power_stats_ts = 0.0
        self._daily_cache = {}   # days → (ts, 每日汇总)，60 秒复用
        self._disk_standby_cache = None
        self._disk_standby_ts = 0.0
        # ---- 登录鉴权：会话持久化到 配置目录/sessions.json（0600，只存 sid 哈希），内存缓存加速校验 ----
        self._sessions = {}
        self._sessions_lock = threading.RLock()
        self.limiter = LoginLimiter()
        self._load_sessions()
        self._static_cache = {}
        self._export_sem = threading.BoundedSemaphore(2)   # 报告 / 导出并发上限
        self._apps_refresh_ts = 0.0
        self.vms = VmMonitor()   # 虚拟机：按需采集（只在打开虚拟机页时轮询）

    def make_handler(self):
        app = self

        # ------------------------------------------------------------------
        # 请求安全规则（v2.18.0）
        # 1) Host 策略（防 DNS rebinding）：未启用登录口令时，Host 必须是 IP 字面量 /
        #    localhost / 单标签主机名 / .local .lan .home .internal .localdomain 等局域网
        #    后缀，否则 403（API 返回 JSON，页面返回中文说明页）；启用口令后任意 Host 均可。
        # 2) CSRF：所有改变状态的接口只接受 POST，且必须同时满足
        #    Content-Type: application/json、请求头 X-FNM-Request: 1，否则 403 JSON。
        #    自定义请求头会强制跨站请求走 CORS 预检，而本服务从不放行 CORS；不比对 Origin
        #    （飞牛官方 app 与反向代理会改写 Host，见 _csrf_reject_reason）。
        #    GET 只用于读取（例外：/api/update/download?to=browser 仅流式回传已校验的安装包）。
        # 3) 资源上限：请求体 ≤ 1 MiB（超出 413，不读取）；空闲长连接 30 秒超时；
        #    并发连接（处理线程）总数与单 IP 数量有上限，超出直接 503（见 BoundedHTTPServerMixin）。
        # ------------------------------------------------------------------
        CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
               "img-src 'self' data:; connect-src 'self'; frame-ancestors *")
        POST_ONLY = {"/api/auth/password", "/api/auth/setup", "/api/auth/logout", "/api/docker/action",
                     "/api/vm/action", "/api/config/save", "/api/update/install",
                     "/api/notify/save", "/api/notify/test", "/api/disk_scan/now"}

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 长连接：浏览器复用 TCP 连接，避免每请求新建线程；timeout 保证空闲连接
            # 30 秒内回收（并发连接数另有上限）
            protocol_version = "HTTP/1.1"
            # 长连接下必须关闭 Nagle：否则小响应要等对端延迟 ACK，每个请求白等约 40ms（浏览器轮询全走长连接）
            disable_nagle_algorithm = True
            timeout = 30
            server_version = "fnMonitor"
            sys_version = ""

            # ---- 公共：安全头 / 发送 ----
            def end_headers(self):
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("Content-Security-Policy", CSP)
                super().end_headers()

            @property
            def client_ip(self):
                return norm_ip(self.client_address[0] if self.client_address else "")

            def _accepts_gzip(self):
                return "gzip" in (self.headers.get("Accept-Encoding") or "").lower()

            def _send(self, status, body, ctype, headers=None, cache="no-store", compress=True):
                gz = False
                if compress and len(body) > 1024 and self._accepts_gzip():
                    body = gzip.compress(body, 5)
                    gz = True
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if cache:
                    self.send_header("Cache-Control", cache)
                if compress:
                    self.send_header("Vary", "Accept-Encoding")
                if gz:
                    self.send_header("Content-Encoding", "gzip")
                for k, v in (headers or []):
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _json(self, obj, status=200, cookie=None, headers=None):
                """发送 JSON。cookie=(sid, max_age) 设置会话；cookie="clear" 清除。"""
                hdrs = list(headers or [])
                if cookie == "clear":
                    hdrs.append(("Set-Cookie", "fnmp_sid=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"))
                elif cookie:
                    hdrs.append(("Set-Cookie", "fnmp_sid=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax"
                                 % (cookie[0], cookie[1])))
                self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8", hdrs)

            def _send_bytes(self, body):
                self._send(200, body, "application/json; charset=utf-8")

            def _download(self, body, ctype, filename):
                self._send(200, body, ctype, [("Content-Disposition", 'attachment; filename="%s"' % filename)],
                           compress=False)

            def _download_stream(self, chunks, ctype, filename):
                """分块传输（Transfer-Encoding: chunked）流式下载，内存占用与数据量无关。"""
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                try:
                    pending = []
                    size = 0
                    for c in chunks:
                        if not c:
                            continue
                        pending.append(c)
                        size += len(c)
                        if size >= 65536:
                            data = b"".join(pending)
                            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                            pending, size = [], 0
                    if pending:
                        data = b"".join(pending)
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                    self.wfile.write(b"0\r\n\r\n")
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                except Exception:
                    traceback.print_exc()
                    self.close_connection = True   # 已发出部分数据：只能断开连接

            def _serve_html(self, name):
                """静态页面：ETag 协商缓存（Cache-Control: no-cache，每次向服务器校验，
                更新后 ETag 变化即取新页面）+ gzip。"""
                ent = app._static_entry(name)
                if ent is None:
                    self.send_error(404, "Not Found")
                    return
                body, gz, etag = ent
                inm = self.headers.get("If-None-Match") or ""
                if etag in [x.strip() for x in inm.split(",")]:
                    self.send_response(304)
                    self.send_header("ETag", etag)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Vary", "Accept-Encoding")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                use_gz = gz is not None and self._accepts_gzip()
                data = gz if use_gz else body
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.send_header("ETag", etag)
                self.send_header("Vary", "Accept-Encoding")
                if use_gz:
                    self.send_header("Content-Encoding", "gzip")
                self.end_headers()
                self.wfile.write(data)

            def _deny(self, status, code, message, path):
                self.close_connection = True  # 可能有未读取的请求体，不复用连接
                if path.startswith("/api/") or self.command == "POST":
                    self._json({"ok": False, "error": code, "message": message}, status=status)
                else:
                    self._send(status, HOST_BLOCKED_HTML if code == "host_not_allowed" else
                               ("<h3>%s</h3>" % message).encode("utf-8"), "text/html; charset=utf-8")

            def _host_ok(self):
                if app._auth_required():
                    return True
                return host_is_lan(self.headers.get("Host") or "")

            def _unauthorized(self, path):
                if path.startswith("/api/"):
                    self._json({"ok": False, "error": "未登录", "auth_required": True}, status=401)
                else:
                    self.send_error(401, "Unauthorized")

            def _wrap(self, fn):
                try:
                    fn()
                except (BrokenPipeError, ConnectionResetError, socket.timeout):
                    self.close_connection = True
                except Exception:
                    traceback.print_exc()
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

            def do_GET(self):
                self._wrap(self._handle)

            def do_POST(self):
                self._wrap(self._handle_post)

            # ---- GET：只读接口 ----
            def _handle(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if not self._host_ok():
                    self._deny(403, "host_not_allowed", "通过域名访问前请先在局域网内开启登录口令", path)
                    return
                # 改变状态的接口不接受 GET（前端收到 405 才回退旧接口）
                if path in POST_ONLY or (path == "/api/update/download"
                                         and (qs.get("to") or ["nas"])[0] != "browser"):
                    self._json({"ok": False, "error": "method_not_allowed", "message": "该接口仅支持 POST"},
                               status=405, headers=[("Allow", "POST")])
                    return

                # ---- 鉴权：登录页与登录接口始终可访问（无需会话） ----
                if path == "/login.html":
                    self._serve_html("login.html")
                    return
                if path == "/api/auth/session":
                    self._json(app.api_auth_session(self.headers))
                    return
                if path == "/api/auth/config":
                    self._json(app.api_auth_config())
                    return
                # ---- 鉴权拦截：启用口令且未登录时，HTML 跳登录页 / API 返 401 ----
                if not app._check_auth(self.headers):
                    if path in ("/", "/index.html"):
                        self._serve_html("login.html")
                        return
                    self._unauthorized(path)
                    return

                if path in ("/", "/index.html"):
                    self._serve_html("index.html")
                    return
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path == "/api/overview":
                    self._send_bytes(app.api_overview())
                    return
                if path == "/api/smart":
                    # 按需读取单盘 SMART 明细：只在详情面板打开时由前端调用
                    self._json(app.api_smart((qs.get("dev") or [""])[0]))
                    return
                if path == "/api/disk_health":
                    # 盘位健康分级：只读巡检缓存，不触发 smartctl
                    self._json(app.api_disk_health())
                    return
                if path == "/api/notify":
                    self._json(app.api_notify_get())
                    return
                if path == "/api/docker":
                    self._json(app.api_docker())
                    return
                if path == "/api/vms":
                    # probe=1：只判断本机是否有 virsh（前端据此决定是否显示「虚拟机」页），不调用 libvirt
                    if (qs.get("probe") or ["0"])[0] in ("1", "true", "yes"):
                        self._json({"installed": bool(virsh_path()), "kvm": os.path.exists("/dev/kvm")})
                    else:
                        self._json(app.vms.collect())
                    return
                if path == "/api/modules":
                    self._json(app.api_modules())
                    return
                if path == "/api/processes":
                    self._json(app.api_processes())
                    return
                if path == "/api/history":
                    metric = (qs.get("metric") or ["cpu"])[0]
                    range_ = (qs.get("range") or ["1h"])[0]
                    self._json(app.api_history(metric, range_))
                    return
                if path == "/api/system":
                    self._json(app.api_system())
                    return
                if path == "/api/config":
                    self._json(app.api_config())
                    return
                if path == "/api/apps":
                    force = (qs.get("refresh") or ["0"])[0] in ("1", "true", "yes")
                    self._json(app.api_apps(refresh=force))
                    return
                if path == "/api/ports":
                    self._json(app.api_ports())
                    return
                if path == "/api/hardware":
                    self._json(app.api_hardware())
                    return
                if path == "/api/ui":
                    self._json(app.api_ui_get())
                    return
                if path == "/api/history/diag":
                    self._json(app.api_history_diag())
                    return
                if path == "/api/sensors":
                    self._json(app.api_sensors())
                    return
                if path == "/api/fans":
                    self._json(app.api_fans())
                    return
                if path == "/api/raidcard":
                    self._json(app.api_raidcard())
                    return
                if path == "/api/power":
                    self._json(app.api_power())
                    return
                if path == "/api/traffic":
                    self._json(app.api_traffic())
                    return
                if path == "/api/power_stats":
                    self._json(app.api_power_stats())
                    return
                if path == "/api/gpu":
                    self._json(app.api_gpu(diag=(qs.get("diag") or ["0"])[0] in ("1", "true")))
                    return
                if path == "/api/memory":
                    self._json(app.api_memory())
                    return
                if path == "/api/weather":
                    self._json(app.api_weather())
                    return
                if path == "/api/monthly_summary":
                    self._json(app.api_monthly_summary())
                    return
                if path == "/api/daily":
                    self._json(app.api_daily((qs.get("days") or ["30"])[0]))
                    return
                if path == "/api/update/check":
                    force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
                    self._json(app.api_update_check(force=force))
                    return
                if path == "/api/update/download":
                    # to=browser：NAS 端代理下载 → 官方 SHA256 校验通过后才发给浏览器
                    asset, err = app.api_update_asset()
                    data = None
                    if not err:
                        data, err = app.updater.fetch_verified(asset)
                    if err or data is None:
                        self._json({"ok": False, "error": err or "下载失败"})
                        return
                    self._send(200, data, "application/octet-stream",
                               [("Content-Disposition", 'attachment; filename="%s"' % asset["name"]),
                                ("X-Content-SHA256", asset.get("digest") or "")], compress=False)
                    return
                if path in ("/api/report", "/api/export"):
                    if not app._export_sem.acquire(blocking=False):
                        self._json({"ok": False, "error": "已有导出 / 报告任务在进行，请稍后再试"}, status=503,
                                   headers=[("Retry-After", "5")])
                        return
                    try:
                        self._handle_export(path, qs)
                    finally:
                        app._export_sem.release()
                    return
                self.send_error(404, "Not Found")

            def _handle_export(self, path, qs):
                stamp = time.strftime("%Y%m%d_%H%M%S")
                if path == "/api/report":
                    fmt = (qs.get("format") or ["json"])[0]
                    if fmt == "html":
                        self._download(app.api_report_html(), "text/html; charset=utf-8",
                                       "fnmonitor_report_%s.html" % stamp)
                    else:
                        self._json(app.api_report())
                    return
                export_type = (qs.get("type") or ["history"])[0]
                range_ = (qs.get("range") or ["7d"])[0]
                if export_type == "status":
                    body = json.dumps(app.api_export_status(), ensure_ascii=False, indent=2).encode("utf-8")
                    self._download(body, "application/json; charset=utf-8", "fnmonitor_status_%s.json" % stamp)
                elif export_type == "traffic":
                    self._download_stream(app.iter_traffic_csv(range_), "text/csv; charset=utf-8",
                                          "fnmonitor_traffic_%s.csv" % stamp)
                elif export_type == "power":
                    self._download_stream(app.iter_power_csv(range_), "text/csv; charset=utf-8",
                                          "fnmonitor_power_%s.csv" % stamp)
                else:
                    self._download_stream(app.iter_history_csv(range_), "text/csv; charset=utf-8",
                                          "fnmonitor_history_%s.csv" % stamp)

            # ---- POST：改变状态的接口 ----
            def _csrf_log(self, gate, detail):
                """CSRF 拒绝时落一行日志到 server.log：便于定位 app 内嵌 WebView 等特殊来源。"""
                print("[fnmonitor] POST 请求被 CSRF 检查拒绝（闸门 %s：%s，来源 %s，路径 %s）"
                      % (gate, detail, self.client_ip, urlparse(self.path).path))

            def _csrf_reject_reason(self):
                """CSRF 检查：通过返回空串，否则返回被拒的闸门名（content_type / header）。

                跨站防护只靠这两道闸：自定义请求头与 application/json 都会让浏览器的跨站请求
                先走 CORS 预检，而本服务从不应答预检（没有 do_OPTIONS、不发 Access-Control-*），
                跨站页面因此发不出这种请求。不比对 Origin：飞牛官方手机 app（FN Connect 远程 /
                局域网模式）与反向代理都会改写 Host，Origin 是官方域名或 app 内部来源，比对只会
                误伤；它也挡不住 DNS rebinding（由 Host 策略负责）。"""
                ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    self._csrf_log("content_type", ctype or "空")
                    return "content_type"
                if (self.headers.get("X-FNM-Request") or "").strip() != "1":
                    self._csrf_log("header", "缺少 X-FNM-Request")
                    return "header"
                return ""

            def _read_body(self):
                """读取并解析 JSON 请求体；超限 / 格式错误时已发送错误响应并返回 None。"""
                if self.headers.get("Transfer-Encoding"):
                    self._deny(411, "length_required", "请求须带 Content-Length", "/api/")
                    return None
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if length < 0 or length > MAX_BODY_BYTES:
                    self._deny(413, "payload_too_large", "请求体过大（上限 1 MiB）", "/api/")
                    return None
                body = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(body.decode("utf-8", "strict") or "{}")
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    self._json({"ok": False, "error": "bad_json", "message": "请求体须为 JSON 对象"}, status=400)
                    return None
                return data

            def _handle_post(self):
                path = urlparse(self.path).path
                if not self._host_ok():
                    self._deny(403, "host_not_allowed", "通过域名访问前请先在局域网内开启登录口令", path)
                    return
                gate = self._csrf_reject_reason()
                if gate:
                    self._deny(403, "csrf_rejected",
                               "请求被拒绝：缺少 X-FNM-Request 头或 Content-Type 不是 application/json"
                               "（闸门 %s）" % gate, path)
                    return
                data = self._read_body()
                if data is None:
                    return
                ip = self.client_ip

                # ---- 鉴权：登录 / 初始化 / 登出接口始终可访问 ----
                if path == "/api/auth/password":
                    wait = app.limiter.retry_after(ip)
                    if wait:
                        self._json({"ok": False, "error": "尝试次数过多，请 %d 秒后再试" % wait,
                                    "retry_after": wait}, status=429, headers=[("Retry-After", str(wait))])
                        return
                    resp, cookie = app.api_auth_password(data, ip)
                    self._json(resp, cookie=cookie)
                    return
                if path == "/api/auth/setup":
                    resp, cookie = app.api_auth_setup(data, ip)
                    self._json(resp, cookie=cookie)
                    return
                if path == "/api/auth/logout":
                    self._json(app.api_auth_logout(self.headers), cookie="clear")
                    return
                if not app._check_auth(self.headers):
                    self._unauthorized(path)
                    return

                if path == "/api/docker/action":
                    cid = str(data.get("id") or "").strip()
                    act = str(data.get("action") or "").strip()
                    self._json(app.api_docker_action(cid, act))
                    return
                if path == "/api/vm/action":
                    uid = str(data.get("uuid") or "").strip()
                    act = str(data.get("action") or "").strip()
                    self._json(app.api_vm_action(uid, act))
                    return
                if path == "/api/ui":
                    self._json(app.api_ui_set(data))
                    return
                if path == "/api/config/save":
                    resp, status, cookie = app.api_config_save(data, self.headers, ip)
                    hdrs = [("Retry-After", str(resp.get("retry_after")))] if status == 429 else None
                    self._json(resp, status=status, cookie=cookie, headers=hdrs)
                    return
                if path == "/api/notify/save":
                    self._json(app.api_notify_save(data))
                    return
                if path == "/api/notify/test":
                    self._json(app.api_notify_test(data))
                    return
                if path == "/api/disk_scan/now":
                    self._json(app.api_disk_scan_now())
                    return
                if path == "/api/update/install":
                    self._json(app.api_update_install())
                    return
                if path == "/api/update/download":
                    dest = str(data.get("path") or "").strip()
                    self._json(app.api_update_download_nas(dest_dir=dest or None))
                    return
                self._json({"ok": False, "error": "not_found"}, status=404)

            def log_message(self, fmt, *args):
                pass  # 静默访问日志

        return Handler

    def _static_entry(self, name):
        """静态页面缓存：(原文, gzip 版本, ETag)，文件 mtime/size 变化（升级覆盖）后自动失效。"""
        path = os.path.join(self.www_dir, name)
        try:
            st = os.stat(path)
        except Exception:
            return None
        key = (st.st_mtime_ns, st.st_size)
        ent = self._static_cache.get(name)
        if ent and ent[0] == key:
            return ent[1]
        try:
            with open(path, "rb") as f:
                body = f.read()
        except Exception:
            return None
        etag = 'W/"%s"' % hashlib.sha256(body).hexdigest()[:20]
        gz = gzip.compress(body, 6) if len(body) > 1024 else None
        val = (body, gz, etag)
        self._static_cache[name] = (key, val)
        return val

    # ---- API 实现 ----
    def _public_config(self):
        """对外配置：去掉口令哈希，附加 auth_password_set / auth_has_password（旧前端兼容）。"""
        c = public_config(self.config)
        c["auth_has_password"] = c["auth_password_set"]
        return c

    def api_smart(self, dev):
        """按需读取单块硬盘的 SMART 明细（详情面板打开时才调用，5 分钟内复用缓存）。

        只接受内核设备名形式的参数（sda / nvme0n1 / vda），避免把任意路径拼进
        smartctl 的 argv；返回值直接透出 smart_detail 的解析结果，前端按协议取用。
        """
        dev = str(dev or "")
        if not _DEV_NAME_RE.match(dev):
            return {"error": "invalid device"}
        if not is_linux():
            return {"error": "unsupported platform"}
        d = smart_detail_cached(dev)
        d["dev"] = dev
        d["ts"] = int(time.time())
        return d

    def api_disk_scan_now(self):
        """手动触发一轮巡检：立刻返回，不等结果（前端隔十几秒刷新盘位即可）。

        换盘、调间隔、刚装完想马上看到分级都靠它，不用等下一个周期。"""
        if disk_scan_interval_h(self.config) <= 0:
            return {"ok": False, "error": "disabled", "message": "硬盘巡检已关闭，先在设置里打开"}
        if _DISK_SCAN_LOCK.locked() or _disk_scan_state.get("running"):
            return {"ok": False, "error": "busy", "message": "巡检正在进行中，稍等十几秒"}
        threading.Thread(target=disk_scan_once, args=(self.collector.db, self.config),
                         name="disk-scan-manual", daemon=True).start()
        return {"ok": True, "started": True, "message": "已开始巡检，约 15–20 秒后刷新"}

    def api_disk_health(self):
        """盘位卡片的健康分级（只读巡检缓存，不触发任何 smartctl）。

        首装 / 缓存为空时全部为 unknown —— 绝不能显示「正常」，那是在骗人。"""
        now = time.time()
        rows = {}
        try:
            rows = self.collector.db.disk_health_read() or {}
        except Exception:
            rows = {}
        hours = disk_scan_interval_h(self.config)
        items, last = {}, 0.0
        for dev, r in rows.items():
            ts = float(r.get("scan_ts") or 0)
            last = max(last, ts)
            items[dev] = {
                "dev": dev,
                "grade": r.get("grade") or "unknown",
                "detail": r.get("detail") or "",
                "health": r.get("health") or "",
                "serial": r.get("serial") or "",
                "model": r.get("model") or "",
                "proto": r.get("proto") or "",
                "defects": r.get("defects"),
                "pending_defects": r.get("pending_defects"),
                "uncorr": r.get("uncorr"),
                "temp_c": r.get("temp_c"),
                "power_on_hours": r.get("power_on_hours"),
                "rpm": r.get("rpm") or "",
                "manufactured": r.get("manufactured") or "",
                "standby": bool(r.get("standby")),
                "scan_ts": ts,
                "age": int(now - ts) if ts else None,
            }
        state = dict(_disk_scan_state)
        return {
            "ok": True,
            "enabled": hours > 0,
            "interval_h": hours,
            "presets": list(DISK_SCAN_PRESETS_H),
            "last_ts": last or state.get("last_ts") or 0,
            "next_ts": ((last or state.get("last_ts") or 0) + hours * 3600) if hours else 0,
            "scanning": bool(state.get("running")),
            "summary": state.get("summary") or {},
            "error": state.get("error") or "",
            "items": items,
            "now": now,
        }

    def api_notify_get(self):
        """通知配置（不回传授权码 / SendKey，只给「是否已填」与上次发送结果）。"""
        cfg = notify_load(self.data_dir)
        out = notify_public(cfg)
        out["ok"] = True
        out["providers"] = [{"id": p[0], "label": p[1], "host": p[2], "port": p[3], "secure": p[4]}
                            for p in NOTIFY_PROVIDERS]
        st = notify_state_load(self.data_dir)
        out["last"] = st.get("last") or {}
        out["notified"] = st.get("notified") or {}
        return out

    def api_notify_save(self, data):
        """保存通知配置。空密码 / 空 SendKey 视为「不改」，需要清除时传 clear_* 开关。"""
        cfg, err = notify_save(self.data_dir, data if isinstance(data, dict) else {})
        if err:
            return {"ok": False, "error": err, "notify": notify_public(cfg)}
        print("[fnmonitor] 通知配置已更新：邮件=%s 微信=%s"
              % ("开" if cfg.get("enabled") else "关", "开" if cfg.get("sct_enabled") else "关"))
        return {"ok": True, "notify": notify_public(cfg)}

    def api_notify_test(self, data):
        """立刻发一条测试消息。channel=mail / sct 只测一个通道，不传则两个都发。

        设置页每个通道各有一个「发送测试」按钮，点哪个只发哪个。
        """
        ch = data.get("channel") if isinstance(data, dict) else ""
        return notify_test(self.data_dir, ch)

    def api_overview(self):
        """总览快照（返回预序列化 bytes，5 秒内多客户端复用同一份）。

        轮询接口每 5 秒被每个打开的页面调用一次；快照本身每 interval 秒才更新，
        序列化结果在更新周期内完全一致，直接复用可省掉每客户端的 json.dumps 与
        system_info() 的 /proc 读取。"""
        now = time.time()
        if self._overview_bytes and now - self._overview_ts < 5:
            return self._overview_bytes
        snap = self.collector.get_snapshot()
        snap["system"] = self._cached_system_info(now)
        snap["config"] = self._public_config()
        self._overview_bytes = json.dumps(snap, ensure_ascii=False).encode("utf-8")
        self._overview_ts = now
        return self._overview_bytes

    def _cached_system_info(self, now):
        if self._sysinfo_cache is None or now - self._sysinfo_ts >= 60:
            self._sysinfo_cache = system_info()
            self._sysinfo_ts = now
        return self._sysinfo_cache

    def api_docker(self):
        snap = self.collector.get_snapshot()
        if snap.get("docker"):
            return snap["docker"]
        return collect_docker()

    def api_modules(self):
        snap = self.collector.get_snapshot()
        if snap.get("modules"):
            return {"modules": snap["modules"]}
        return {"modules": collect_modules()}

    def api_processes(self):
        """进程 Top 列表（ps 外部命令结果缓存 15 秒，多客户端轮询复用）。"""
        now = time.time()
        if self._proc_cache is None or now - self._proc_ts >= 15:
            self._proc_cache = {"processes": top_processes()}
            self._proc_ts = now
        return self._proc_cache

    _RANGE_SECONDS = {"10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}
    _METRIC_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")   # 指标名含挂载路径，可能有中文

    def _span(self, seconds):
        """「全部」范围的时间跨度：按保留天数估算（用于计算降采样桶宽）。"""
        return seconds or max(1, int(self.config.get("retention_days", 7) or 7)) * 86400

    def api_history(self, metric, range_):
        """趋势查询（结果缓存 25 秒）。

        历史数据每 history_interval(默认 60) 秒才新增一个点；查询在 SQL 内按时间桶
        降采样（最多约 720 点），缓存后同周期内只查一次库。缓存项上限 48 个。"""
        if not self._METRIC_RE.match(str(metric or "")):
            return {"metric": "", "range": range_, "points": []}
        if range_ not in self._RANGE_SECONDS:
            range_ = "1h"
        key = (metric, range_)
        now = time.time()
        cached = self._hist_cache.get(key)
        if cached and now - cached[0] < 25:
            return cached[1]
        if len(self._hist_cache) > 48:
            self._hist_cache.clear()
        data = self._api_history_impl(metric, range_)
        self._hist_cache[key] = (now, data)
        return data

    def _api_history_impl(self, metric, range_):
        seconds = self._RANGE_SECONDS.get(range_, 3600)
        if metric == "net":
            return self.api_net_history(range_)
        if metric == "net_bytes":
            return self.api_net_bytes_history(range_)
        if metric == "ctr_in":
            return self.api_ctr_history(range_)
        if metric == "diskio":
            return self.api_diskio_history(range_)
        rows = self.collector.db.query_series(metric, seconds, span=self._span(seconds))
        # 实时兜底：历史为空或最新点过旧时，附加当前实时值，保证趋势图始终有数据
        now = time.time()
        latest_ts = rows[-1][0] if rows else 0
        if now - latest_ts > 60:
            val = self._live_value(metric, self.collector.get_snapshot())
            if val is not None:
                rows = list(rows) + [(now, val)]
        return {"metric": metric, "range": range_, "points": [[t, round(v, 2) if isinstance(v, float) else v]
                                                             for t, v in rows]}

    def _live_value(self, metric, snap):
        """从最新实时快照取指定指标的当前值（用于历史不足时的趋势兜底）。"""
        try:
            if metric == "cpu":
                return snap.get("cpu", {}).get("percent")
            if metric == "mem":
                return snap.get("mem", {}).get("percent")
            if metric == "disk_total":
                return snap.get("disk_total")
            if metric == "load1":
                return snap.get("cpu", {}).get("load", [None])[0]
            if metric == "temp":
                return snap.get("temp", {}).get("cpu")
            if metric == "temp_mb":
                return snap.get("temp", {}).get("system")
            if metric == "power":
                p = snap.get("power", {})
                return p.get("total") if p.get("ok") else None
            if metric == "power_est":
                p = snap.get("power", {})
                if p.get("source") == "estimated":
                    return p.get("total")
                return None
            if metric == "fan_avg":
                fans = snap.get("sensors", {}).get("fans", [])
                rpms = [f.get("rpm") for f in fans if f.get("rpm")]
                return round(sum(rpms) / len(rpms), 1) if rpms else None
        except Exception:
            pass
        return None

    def api_diskio_history(self, range_):
        """磁盘 IO 历史：聚合全部磁盘读/写速率（MB/s），返回 {r:[], w:[]}（SQL 内降采样）。"""
        seconds = self._RANGE_SECONDS.get(range_, 3600)
        db = self.collector.db
        span = self._span(seconds)
        mr, mw = db.metrics_with_prefix("diskio_r:"), db.metrics_with_prefix("diskio_w:")
        bk = db.bucket_for(mr + mw, seconds)
        r = [(t, round(v, 2)) for t, v in db.sum_series(mr, seconds, span=span, bucket=bk)]
        w = [(t, round(v, 2)) for t, v in db.sum_series(mw, seconds, span=span, bucket=bk)]
        # 实时兜底：历史为空或最新点过旧时，附加当前实时读写速率，保证趋势图始终有数据
        now = time.time()
        latest_ts = 0.0
        if r:
            latest_ts = max(latest_ts, r[-1][0])
        if w:
            latest_ts = max(latest_ts, w[-1][0])
        if now - latest_ts > 120:
            try:
                snap = self.collector.get_snapshot()
                io_now = snap.get("diskio", []) or []
                rr = sum(float(d.get("read_rate", 0) or 0) for d in io_now)
                wr = sum(float(d.get("write_rate", 0) or 0) for d in io_now)
                if rr > 0 or wr > 0:
                    r = r + [(now, round(rr / 1048576.0, 2))]
                    w = w + [(now, round(wr / 1048576.0, 2))]
            except Exception:
                pass
        return {"metric": "diskio", "range": range_, "r": r, "w": w}

    def api_history_diag(self):
        """趋势诊断：历史库状态与最近写入情况，便于定位数据不显示问题。"""
        db = self.collector.db
        info = {"data_dir": self.data_dir, "db_file": db.db_path,
                "db_exists": os.path.exists(db.db_path), "db_size": db.metrics_size()}
        try:
            info.update(db.diag())
        except Exception as e:
            info["db_error"] = str(e)
        info["snapshot_ts"] = self.collector.get_snapshot().get("ts")
        return info

    def api_net_history(self, range_):
        """聚合所有物理网卡的上下行速率历史（排除虚拟网卡），返回 {rx:[...], tx:[...]}。"""
        seconds = self._RANGE_SECONDS.get(range_, 3600)
        db = self.collector.db
        virt = ("docker", "veth", "br-", "virbr", "tun", "tap", "vnet", "lxc", "kube", "lo", "wg")

        def phys(prefix):
            return [m for m in db.metrics_with_prefix(prefix) if not m.split(":", 1)[1].startswith(virt)]

        span = self._span(seconds)
        pr, pt = phys("net_rx:"), phys("net_tx:")
        bk = db.bucket_for(pr + pt, seconds)
        rx = db.sum_series(pr, seconds, span=span, bucket=bk)
        tx = db.sum_series(pt, seconds, span=span, bucket=bk)
        return {"range": range_, "rx": [[t, round(v, 1)] for t, v in rx],
                "tx": [[t, round(v, 1)] for t, v in tx]}

    def api_system(self):
        info = system_info()
        info["version"] = VERSION
        info["db_size"] = self.collector.db.metrics_size()
        info["data_dir"] = self.data_dir
        return info

    def api_config(self):
        c = self._public_config()
        c["data_dir_actual"] = self.data_dir
        c["ui_file"] = os.path.join(self.data_dir, "ui.json")  # 界面同步设置文件实际位置
        c["version"] = VERSION
        c["arch"] = self.updater.detect_arch()
        c["update_status"] = self.updater.status()
        c["listen_port"] = self.port
        # 鉴权状态（绝不回传口令或哈希，前端只关心开关与是否已设口令）
        c["auth_enabled"] = 1 if str(self.config.get("auth_enabled") or "0") in ("1", "true", "on") else 0
        c["auth_session_days"] = int(self.config.get("auth_session_days") or 7)
        c["password_min_len"] = PASSWORD_MIN_LEN
        return {"config": c}

    # ------------------------------------------------------------------
    # 登录鉴权：会话管理 + 口令登录 / 初始化 / 登出
    # 会话文件 sessions.json 固定放在配置目录（不随自定义数据目录进入共享文件夹），
    # 权限 0600，只保存 sha256(sid)，文件泄露也无法直接冒用会话。
    # ------------------------------------------------------------------
    def _sessions_path(self):
        return os.path.join(self._cfg_dir, "sessions.json")

    @staticmethod
    def _sid_key(sid):
        return hashlib.sha256(str(sid).encode("utf-8")).hexdigest()

    def _load_sessions(self):
        """从磁盘加载会话表（兼容旧版明文 sid 键与旧位置 data_dir/sessions.json），清理过期会话。"""
        sessions, migrated = {}, False
        paths = [self._sessions_path()]
        legacy = os.path.join(self.data_dir, "sessions.json")
        if os.path.abspath(legacy) != os.path.abspath(paths[0]):
            paths.append(legacy)
        for p in paths:
            try:
                with open(p, "r", errors="ignore") as f:
                    raw = json.load(f)
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            for k, v in raw.items():
                if not isinstance(v, dict):
                    continue
                if v.get("h") == 1:
                    sessions[k] = v
                else:                       # 旧版：键为明文 sid → 转为哈希键
                    v = dict(v, h=1)
                    sessions[self._sid_key(k)] = v
                    migrated = True
            if p != paths[0]:
                migrated = True
                try:
                    os.remove(p)
                except Exception:
                    pass
        now = time.time()
        expired = [k for k, s in sessions.items() if float(s.get("expires") or 0) < now]
        for k in expired:
            sessions.pop(k, None)
        with self._sessions_lock:
            self._sessions = sessions
        if expired or migrated:
            self._save_sessions()

    def _save_sessions(self):
        """原子写入会话表（0600，临时文件 + os.replace；加锁避免并发迭代 / 写坏）。"""
        with self._sessions_lock:
            snap = dict(self._sessions)
            try:
                write_json_secure(self._sessions_path(), snap)
            except Exception:
                traceback.print_exc()

    def _create_session(self, user, via):
        """创建会话，返回 (sid, max_age_seconds)。"""
        days = max(1, min(365, int(self.config.get("auth_session_days") or 7)))
        max_age = days * 86400
        sid = hashlib.sha256(os.urandom(32)).hexdigest()
        now = time.time()
        with self._sessions_lock:
            self._sessions[self._sid_key(sid)] = {"user": user, "via": via, "h": 1,
                                                  "created": now, "expires": now + max_age}
        self._save_sessions()
        return sid, max_age

    def _check_session(self, sid):
        """校验会话有效性，返回 True/False；过期会话自动清理。"""
        if not sid or len(sid) > 128:
            return False
        key = self._sid_key(sid)
        with self._sessions_lock:
            s = self._sessions.get(key)
        if not s:
            return False
        if float(s.get("expires") or 0) < time.time():
            with self._sessions_lock:
                self._sessions.pop(key, None)
            self._save_sessions()
            return False
        return True

    def _revoke_other_sessions(self, keep_sid=None):
        keep = self._sid_key(keep_sid) if keep_sid else None
        with self._sessions_lock:
            self._sessions = {k: v for k, v in self._sessions.items() if k == keep}
        self._save_sessions()

    def _has_password(self):
        return bool(str(self.config.get("auth_password_hash") or "").strip())

    def _auth_required(self):
        """是否需要鉴权：auth_enabled=1 且已设口令时才启用。"""
        if str(self.config.get("auth_enabled") or "0") not in ("1", "true", "on"):
            return False
        return self._has_password()

    def _get_cookie_sid(self, headers):
        """从 Cookie 头解析 fnmp_sid 值。"""
        cookie = headers.get("Cookie") or ""
        for part in cookie.split(";"):
            kv = part.strip().split("=", 1)
            if len(kv) == 2 and kv[0].strip() == "fnmp_sid":
                return kv[1].strip()
        return ""

    def _check_auth(self, headers):
        """鉴权检查：返回 True=已登录或无需鉴权。"""
        if not self._auth_required():
            return True
        return self._check_session(self._get_cookie_sid(headers))

    def api_auth_session(self, headers):
        """查询当前登录态。"""
        if not self._auth_required():
            return {"ok": True, "logged_in": True, "auth_required": False}
        sid = self._get_cookie_sid(headers)
        if self._check_session(sid):
            with self._sessions_lock:
                s = dict(self._sessions.get(self._sid_key(sid), {}))
            return {"ok": True, "logged_in": True, "auth_required": True,
                    "user": s.get("user", ""), "via": s.get("via", "")}
        return {"ok": True, "logged_in": False, "auth_required": True}

    def api_auth_config(self):
        """返回鉴权配置（供登录页判断模式）。"""
        has = self._has_password()
        return {"auth_enabled": 1 if str(self.config.get("auth_enabled") or "0") in ("1", "true", "on") else 0,
                "requires_password": has, "setup_mode": not has, "password_min_len": PASSWORD_MIN_LEN}

    def _verify_with_limit(self, pw, ip):
        """口令校验（共用失败限速）。返回 True / False。"""
        with self.limiter.verify_sem:
            ok = verify_password(pw, self.config.get("auth_password_hash"))
        if ok:
            self.limiter.success(ip)
        else:
            self.limiter.fail(ip)
        return ok

    def api_auth_password(self, data, ip=""):
        """口令登录：校验 → 建会话。返回 (响应, cookie)。限速在 Handler 中先行检查。"""
        if not self._has_password():
            return {"ok": False, "error": "未配置登录口令"}, None
        inp = str((data or {}).get("password") or "")
        if not inp or len(inp) > 256 or not self._verify_with_limit(inp, ip):
            print("[fnmonitor] 登录失败（来源 %s）" % ip)
            resp = {"ok": False, "error": "口令错误"}
            wait = self.limiter.retry_after(ip)
            if wait:
                resp["retry_after"] = wait
            return resp, None
        print("[fnmonitor] 登录成功（来源 %s）" % ip)
        sid, ma = self._create_session("(登录口令)", "password")
        return {"ok": True}, (sid, ma)

    def _update_config_file(self, updates, remove=()):
        """读改写 config.json（加锁 + 0600 原子写），并清除遗留明文口令。返回写入后的内容。"""
        path = os.path.join(self._cfg_dir, "config.json")
        with _CFG_WRITE_LOCK:
            try:
                with open(path, "r", errors="ignore") as f:
                    cur = json.load(f)
                if not isinstance(cur, dict):
                    cur = {}
            except Exception:
                cur = {}
            cur.update(updates)
            for k in tuple(remove) + ("auth_password",):
                cur.pop(k, None)
            write_json_secure(path, cur)
        return cur

    def api_auth_setup(self, data, ip=""):
        """首次初始化口令：仅在未设口令时可用（口令 ≥ PASSWORD_MIN_LEN 位），设置后启用登录保护。"""
        inp = str((data or {}).get("password") or "")
        if len(inp) < PASSWORD_MIN_LEN or len(inp) > 256:
            return {"ok": False, "error": "口令至少 %d 位" % PASSWORD_MIN_LEN}, None
        with _CFG_WRITE_LOCK:
            if self._has_password():
                return {"ok": False, "error": "已设置口令，请使用登录修改"}, None
            h = hash_password(inp)
            try:
                self._update_config_file({"auth_password_hash": h, "auth_enabled": 1})
            except Exception as e:
                return {"ok": False, "error": "保存失败: %s" % e}, None
            self.config["auth_password_hash"] = h
            self.config["auth_enabled"] = 1
            self._overview_bytes = b""
        print("[fnmonitor] 已通过 /api/auth/setup 初始化登录口令并启用登录保护（来源 %s）" % ip)
        self._revoke_other_sessions()
        sid, ma = self._create_session("(初始化)", "setup")
        return {"ok": True}, (sid, ma)

    def api_auth_logout(self, headers):
        """登出：删会话 + 清 Cookie。"""
        sid = self._get_cookie_sid(headers)
        if sid:
            with self._sessions_lock:
                removed = self._sessions.pop(self._sid_key(sid), None)
            if removed:
                self._save_sessions()
        return {"ok": True}

    def api_update_check(self, force=False):
        return self.updater.check(force=force)

    def api_update_download_nas(self, dest_dir=None):
        """下载当前线上最新版安装包到 NAS 指定目录（不比较版本；必须通过官方 SHA256 校验）。"""
        info = self.updater.check()  # 走缓存，已有结果不重复请求
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "检查更新失败"}
        asset = info.get("asset")
        if not asset:
            return {"ok": False, "error": "Release 中未找到 %s 平台安装包" % info.get("arch")}
        ok, path = self.updater.download_to_nas(asset, dest_dir=dest_dir)
        if ok:
            note = "已保存到 NAS（SHA256 已校验），可在飞牛文件管理中查看；也可在应用中心「手动安装」选择该文件完成升级"
            return {"ok": True, "file": os.path.basename(path), "path": path,
                    "latest": info.get("latest"), "sha256": asset.get("digest"), "note": note}
        return {"ok": False, "error": path}

    def api_update_install(self):
        """在线更新：检查 → 下载并校验 SHA256 → 安全解包覆盖 → 自动重启服务（互斥，拒绝降级）。"""
        return self.updater.install_latest()

    def api_update_asset(self):
        """返回当前架构最新安装包资产信息（供浏览器代理下载）。"""
        info = self.updater.check()
        if not info.get("ok"):
            return None, info.get("error") or "检查更新失败"
        asset = info.get("asset")
        if not asset or not asset.get("download_url"):
            return None, "Release 中未找到 %s 平台安装包" % info.get("arch")
        return asset, None

    def api_apps(self, refresh=False):
        # 手动刷新：重新扫描内置应用（目录遍历 + 数据库 COUNT 为重操作，60 秒冷却防止被刷）
        if refresh and time.time() - self._apps_refresh_ts >= 60:
            self._apps_refresh_ts = time.time()
            try:
                self.collector._last_apps = collect_app_stats()
                self.collector._last_apps_ts = time.time()
                with self.collector._lock:
                    if self.collector.latest:
                        self.collector.latest["apps"] = self.collector._last_apps
            except Exception:
                traceback.print_exc()
        snap = self.collector.get_snapshot()
        return {"apps": snap.get("apps", [])}

    def api_ports(self):
        snap = self.collector.get_snapshot()
        return {"ports": snap.get("ports", {"apps": [], "docker": [], "listeners": []})}

    def api_hardware(self):
        snap = self.collector.get_snapshot()
        return {"hardware": snap.get("hardware", {}), "raid": snap.get("raid", [])}

    def _ui_config_path(self):
        return os.path.join(self.data_dir, "ui.json")

    def api_ui_get(self):
        # 主文件读取失败（损坏/并发写入中断）时回退备份，避免用户界面配置整体丢失
        for p in (self._ui_config_path(), self._ui_config_path() + ".bak"):
            try:
                with open(p, "r", errors="ignore") as f:
                    return {"ui": json.load(f)}
            except Exception:
                continue
        return {"ui": {}}

    def api_ui_set(self, data):
        """保存前端 UI 配置（主题/面板顺序/隐藏/模块隐藏）到本地文件，跨设备不重置。
        写入前备份上一份有效配置，并用临时文件 + 原子替换落盘（多端并发写入不会写坏 ui.json）。"""
        clean = {}
        # 注意：layoutVersion / tab 必须一并持久化，否则前端 loadUI 会因
        # 服务器端 layoutVersion 恒为 0 < 本地版本而每次刷新重置布局
        # v2.18 新界面：主色 / 点阵数字 / 动效 / 分区布局与显示 / 提醒阈值；旧字段保留以兼容回退
        for k in ("theme", "layoutVersion", "panelOrder", "hiddenPanels", "hiddenMods", "range", "tab", "sideCollapsed", "fontScale",
                  "accent", "dotMatrix", "motion", "layout", "hidden"):
            if k in data:
                clean[k] = data[k]
        if "thresholds" in data:
            clean["thresholds"] = sanitize_thresholds(data.get("thresholds"))
        if "ignored" in data:
            clean["ignored"] = sanitize_ignored(data.get("ignored"))
        # 主色只接受十六进制颜色或简单的主题色名（防止值被拼进 style / HTML 形成注入）
        if "accent" in clean and not re.match(r"^(#[0-9a-fA-F]{3,8}|[A-Za-z0-9_-]{1,32})$", str(clean["accent"])):
            clean.pop("accent", None)
        # 防止异常客户端写入超大配置（正常 ui.json 只有几 KB）
        if len(json.dumps(clean, ensure_ascii=False)) > 256 * 1024:
            return {"ok": False, "error": "ui config too large"}
        path = self._ui_config_path()
        try:
            try:
                with open(path, "rb") as f:
                    bak = f.read()
                with open(path + ".bak", "wb") as f:
                    f.write(bak)
            except Exception:
                pass
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def api_config_save(self, data, headers=None, ip=""):
        """保存配置（采集间隔/保留天数/服务端口/数据目录/口令…），端口修改需重启应用后生效。

        返回 (响应, HTTP 状态码, cookie)。口令修改规则：
        - 已设口令时必须提供 old_password 且校验通过，否则 403 {"error":"old_password_invalid"}
          （校验失败计入登录限速，锁定期间返回 429 + retry_after）；
        - 首次设置只需 auth_password；新口令至少 PASSWORD_MIN_LEN 位；
        - 修改成功后吊销其他所有会话，保留当前请求的会话。
        所有校验在写文件前完成，任一失败则整个保存不生效。"""
        headers = headers or {}
        caller_sid = self._get_cookie_sid(headers) if hasattr(headers, "get") else ""
        caller_valid = bool(caller_sid) and self._check_session(caller_sid)
        had_password = self._has_password()
        updates, remove = {}, []
        changed_port = False

        # ---- 口令（先校验，失败直接返回） ----
        new_hash = None
        if "auth_password" in data:
            pw = str(data.get("auth_password") or "")
            if pw:  # 空字符串=不改密码
                if len(pw) < PASSWORD_MIN_LEN or len(pw) > 256:
                    return {"ok": False, "error": "口令至少 %d 位" % PASSWORD_MIN_LEN}, 200, None
                if self._has_password():
                    wait = self.limiter.retry_after(ip)
                    if wait:
                        return {"ok": False, "error": "尝试次数过多，请 %d 秒后再试" % wait,
                                "retry_after": wait}, 429, None
                    old = str(data.get("old_password") or "")
                    if not old or len(old) > 256 or not self._verify_with_limit(old, ip):
                        print("[fnmonitor] 修改口令失败：当前口令校验未通过（来源 %s）" % ip)
                        return {"ok": False, "error": "old_password_invalid",
                                "message": "当前口令不正确"}, 403, None
                new_hash = hash_password(pw)
                updates["auth_password_hash"] = new_hash

        # ---- 端口：1024–65535（0 = 恢复默认端口），且不能被其他进程占用 ----
        if "port" in data:
            try:
                v = int(data["port"])
            except Exception:
                return {"ok": False, "error": "端口须为整数"}, 200, None
            if v != 0 and not (1024 <= v <= 65535):
                return {"ok": False, "error": "端口须在 1024–65535 之间（0 表示恢复默认端口）"}, 200, None
            if v != 0 and v != self.port and not port_available(v):
                return {"ok": False, "error": "端口 %d 已被其他程序占用，请换一个端口" % v}, 200, None
            cur_port = int(self.config.get("port") or 0)
            if cur_port != v:
                changed_port = True
            updates["port"] = v

        for k in ("interval", "retention_days", "history_interval"):
            if k in data:
                try:
                    v = int(data[k])
                except Exception:
                    continue
                if k == "interval":
                    v = max(2, min(v, 3600))
                elif k == "retention_days":
                    v = max(1, min(v, 365))
                else:
                    v = max(60, min(v, 86400))
                updates[k] = v
        if "weather_city" in data:
            updates["weather_city"] = clean_text(data.get("weather_city"), 64)
            self._weather_cache = None  # 位置变化后清缓存
        onoff = lambda x: 1 if str(x).lower() in ("1", "true", "on") else 0
        for k in ("update_autocheck", "update_autodownload", "update_autoupdate", "traffic_exclude_bridge",
                  "disk_standby_protect", "monthly_summary_enabled", "auth_enabled", "ec_fan_probe",
                  "disk_scan_enabled"):
            if k in data:
                updates[k] = onoff(data[k])
        # 硬盘巡检间隔：0 = 关闭；其余就近取档位（6/12/24/72/168 小时）
        if "disk_scan_interval_h" in data:
            try:
                h = int(data["disk_scan_interval_h"])
            except Exception:
                h = DEFAULT_CONFIG["disk_scan_interval_h"]
            updates["disk_scan_interval_h"] = 0 if h <= 0 else min(
                DISK_SCAN_PRESETS_H, key=lambda x: abs(x - h))
        for k in ("power_tdp_w", "power_disk_typical_w", "power_nic_fixed_w",
                  "power_rate_yuan", "power_base_w"):
            if k in data:
                try:
                    fv = float(data[k])
                    if math.isfinite(fv):
                        updates[k] = max(0.0, min(500.0, fv))
                except Exception:
                    pass
        if "data_dir" in data:
            nd = str(data.get("data_dir") or "").strip()
            if nd:
                ok_dir, why = validate_data_dir(nd)
                if not ok_dir:
                    return {"ok": False, "error": why}, 200, None
                updates["data_dir"] = os.path.normpath(nd)
            else:
                remove.append("data_dir")
        # 飞牛桌面打开方式：iframe=点击图标在飞牛窗口内打开 / url=点击图标直接在
        # 浏览器新标签页打开。保存到 config.json；同时写入桌面入口 ui/config 的
        # type 并同步应用中心数据库，使 fnOS 桌面按新模式打开（刷新桌面页后生效）
        if "open_mode" in data:
            m = str(data["open_mode"]).lower()
            if m in ("iframe", "url"):
                updates["open_mode"] = m
        if "auth_session_days" in data:
            try:
                updates["auth_session_days"] = max(1, min(365, int(data["auth_session_days"])))
            except Exception:
                pass
        try:
            cur = self._update_config_file(updates, remove)
        except Exception as e:
            return {"ok": False, "error": str(e)}, 200, None

        # ---- 同步到内存，即时生效 ----
        for k, v in updates.items():
            if k == "interval":
                self.config[k] = max(2, int(v))
            elif k == "retention_days":
                self.config[k] = max(1, int(v))
            elif k == "history_interval":
                self.config[k] = max(60, int(v))
            elif k not in ("open_mode",):
                self.config[k] = v
        for k in remove:
            self.config[k] = ""
        self._overview_bytes = b""
        open_mode_changed = False
        new_mode = str(updates.get("open_mode", "")).lower()
        if new_mode in ("iframe", "url"):
            open_mode_changed = (str(self.config.get("open_mode") or "") != new_mode)
            self.config["open_mode"] = new_mode

        # ---- 会话：改口令后吊销其他会话；保存后需要登录而调用方没有有效会话时补发一个 ----
        cookie = None
        if new_hash:
            print("[fnmonitor] 登录口令已%s（来源 %s），已吊销其他会话"
                  % ("修改" if had_password else "设置", ip))
            self._revoke_other_sessions(caller_sid if caller_valid else None)
        if self._auth_required() and not caller_valid:
            sid, ma = self._create_session("(设置页)", "config")
            cookie = (sid, ma)

        # 桌面入口同步：端口变更同步 port；打开方式变更同步 type。除写 ui/config
        # 文件外，apply_launcher_entry 内部还会同步 fnOS 应用中心数据库（真机
        # 实证桌面点击行为由数据库决定）——刷新飞牛桌面页（F5）后点击桌面图标
        # 即按新模式打开，无需重启任何系统服务。
        launcher_ok, launcher_msg = True, ""
        if changed_port:
            try:
                ok_p, msg_p = apply_launcher_entry(port=int(cur.get("port") or 0))
                print("[fnmonitor] 桌面入口端口同步: %s" % msg_p)
            except Exception as e:
                print("[fnmonitor] 桌面入口端口同步异常: %s" % e)
        if new_mode in ("iframe", "url"):
            try:
                launcher_ok, launcher_msg = apply_launcher_entry(mode=new_mode, port=int(cur.get("port") or 0))
            except Exception as e:
                launcher_ok, launcher_msg = False, str(e)
            print("[fnmonitor] 桌面入口配置: %s" % launcher_msg)
        self.collector._cfg_mtime = 0.0  # 强制采集线程下次重载
        note_parts = []
        if changed_port:
            note_parts.append("端口修改需在应用中心重启「飞牛监控」后生效")
        if open_mode_changed:
            note_parts.append("打开方式已切换，刷新飞牛桌面页面（F5）或重新登录后，点击桌面图标将直接按「%s」打开"
                              % ("飞牛窗口" if new_mode == "iframe" else "浏览器新标签页"))
            if launcher_ok:
                if launcher_msg:
                    note_parts.append(launcher_msg)
            else:
                note_parts.append("桌面入口同步失败: %s" % launcher_msg)
        return {"ok": True, "config": self._public_config(), "port_changed": changed_port,
                "open_mode_changed": open_mode_changed, "password_changed": bool(new_hash),
                "launcher_ok": launcher_ok, "launcher_msg": launcher_msg,
                "note": "；".join(note_parts)}, 200, cookie

    def api_sensors(self):
        return self.collector.get_snapshot().get("sensors", {"temps": [], "fans": [], "volts": [], "available": False, "fans_source": ""})

    def api_fans(self):
        return {"fans": self.collector.get_snapshot().get("fans", [])}

    def api_raidcard(self):
        return self.collector.get_snapshot().get("raidcard", {"type": "none", "label": "", "detail": ""})

    def api_power(self):
        """实时功耗（RAPL）。"""
        return self.collector.get_snapshot().get("power", {"ok": False})

    def api_traffic(self):
        """流量统计：实时速率 + 各网卡累计/丢包/错误 + 今日/本月/开机累计 + 容器流量 TOP。"""
        now = time.time()
        if self._traffic_cache and now - self._traffic_ts < 5:
            return self._traffic_cache
        snap = self.collector.get_snapshot()
        ifaces = snap.get("net", [])
        virt_prefixes = VIRT_IFACE_PREFIXES
        exclude_bridge = bool(self.config.get("traffic_exclude_bridge", 0))
        # 实时速率汇总
        total_rx_rate = sum(it.get("rx_rate", 0) for it in ifaces)
        total_tx_rate = sum(it.get("tx_rate", 0) for it in ifaces)
        # 开机累计（/proc/net/dev 计数器开机归零，当前值即开机以来累计）
        boot_rx = sum(it.get("rx_bytes", 0) for it in ifaces if it["iface"] != "lo" and not (it["iface"].startswith(virt_prefixes) and exclude_bridge))
        boot_tx = sum(it.get("tx_bytes", 0) for it in ifaces if it["iface"] != "lo" and not (it["iface"].startswith(virt_prefixes) and exclude_bridge))
        # 今日/本月周期汇总：从历史累计字节计算差值
        import datetime
        _now = datetime.datetime.now()
        today_start = datetime.datetime(_now.year, _now.month, _now.day).timestamp()
        month_start = datetime.datetime(_now.year, _now.month, 1).timestamp()
        today_sec = now - today_start
        month_sec = now - month_start
        def _period_totals(prefix, seconds):
            # 每个网卡只取区间首尾两行（索引 seek），不再把整月累计字节行读进 Python
            db = self.collector.db
            cutoff = now - seconds
            total = 0.0
            for metric in db.metrics_with_prefix(prefix):
                iface = metric.split(":", 1)[1] if ":" in metric else metric
                if exclude_bridge and iface.startswith(virt_prefixes):
                    continue
                if iface == "lo":
                    continue
                fl = db.first_last(metric, cutoff)
                if fl:
                    total += max(0, fl[1] - fl[0])
            return total
        # 优先用每日汇总（与「近 30 天流量」柱同源：跨重启的计数器归零也能正确累加，
        # 本月合计不受 retention_days 截断）；不可用时回退为区间首尾差
        dm = self._daily_today_month()
        if dm and dm["has_net"]:
            today_rx, today_tx = dm["today_rx"] or 0, dm["today_tx"] or 0
            month_rx, month_tx = dm["month_rx"] or 0, dm["month_tx"] or 0
            totals_basis = "daily"
        else:
            today_rx = _period_totals("net_rx_bytes:", today_sec)
            today_tx = _period_totals("net_tx_bytes:", today_sec)
            month_rx = _period_totals("net_rx_bytes:", month_sec)
            month_tx = _period_totals("net_tx_bytes:", month_sec)
            totals_basis = "history"
        # 容器流量 TOP
        containers = []
        docker_data = snap.get("docker", {})
        if docker_data.get("available"):
            for c in docker_data.get("containers", []):
                if c.get("state") != "running":
                    continue
                in_b = c.get("net_in_bytes", 0)
                out_b = c.get("net_out_bytes", 0)
                containers.append({
                    "name": c.get("name", ""),
                    "in_bytes": in_b,
                    "out_bytes": out_b,
                    "total_bytes": in_b + out_b,
                    "rx_rate": c.get("rx_rate"),
                    "tx_rate": c.get("tx_rate"),
                })
            containers.sort(key=lambda x: -x["total_bytes"])
        result = {
            "realtime": {"rx_rate": round(total_rx_rate, 1), "tx_rate": round(total_tx_rate, 1)},
            "ifaces": ifaces,
            "totals": {
                "boot_rx": boot_rx, "boot_tx": boot_tx,
                "today_rx": today_rx, "today_tx": today_tx,
                "month_rx": month_rx, "month_tx": month_tx,
                "basis": totals_basis,
            },
            "containers": containers[:20],
            "config": {"exclude_bridge": exclude_bridge},
        }
        self._traffic_cache = result
        self._traffic_ts = now
        return result

    def api_power_stats(self):
        """功耗统计：实时功耗 + kWh 能耗 + 电费 + 硬盘休眠状态。"""
        now = time.time()
        if self._power_stats_cache and now - self._power_stats_ts < 5:
            return self._power_stats_cache
        snap = self.collector.get_snapshot()
        power = snap.get("power", {"ok": False})
        source = power.get("source", "unknown")
        power_w = power.get("total", 0) if power.get("ok") else 0
        # kWh 计算：从 power 历史积分
        hist_interval = int(self.config.get("history_interval", 60))
        import datetime
        _now = datetime.datetime.now()
        today_start = datetime.datetime(_now.year, _now.month, _now.day).timestamp()
        month_start = datetime.datetime(_now.year, _now.month, 1).timestamp()
        today_sec = now - today_start
        month_sec = now - month_start
        def _kwh_from_history(seconds):
            # SQL 聚合整个区间（旧版受 query 默认 LIMIT 7200 限制，月初 5 天后本月 kWh 偏低）
            return self.collector.db.energy_kwh("power", seconds, default_dt=hist_interval)
        # 优先用每日汇总（与「近 30 天用电」柱同源；本月合计不受 retention_days 截断），
        # 汇总不可用时回退为直接对保留期内的功率历史积分
        dm = self._daily_today_month()
        if dm and dm["has_power"]:
            today_kwh = round(dm["today_kwh"] or 0.0, 4)
            month_kwh = round(dm["month_kwh"] or 0.0, 4)
            energy_basis = "daily"
        else:
            today_kwh = _kwh_from_history(today_sec)
            month_kwh = _kwh_from_history(month_sec)
            energy_basis = "history"
        # 开机累计估算
        uptime = snap.get("uptime", 0)
        boot_kwh = round(power_w * uptime / 3_600_000, 4) if uptime > 0 else 0.0
        # 电费
        rate = float(self.config.get("power_rate_yuan", 0.6))
        # 硬盘休眠状态（60s 缓存），并与「硬盘信息」(lsblk 物理盘) 校对，只保留物理硬盘
        if now - self._disk_standby_ts >= 60:
            self._disk_standby_cache = read_disk_standby_states()
            self._disk_standby_ts = now
        standby = self._disk_standby_cache or []
        phy_names = {d.get("name") for d in (getattr(self.collector, "_last_disks_detail", None) or []) if d.get("name")}
        if phy_names:
            standby = [d for d in standby if d.get("name") in phy_names]
        result = {
            # 整机口径：RAPL 模式下 power_w = CPU 封装 + DRAM + 主板等固定底座 + 活动硬盘
            "realtime": {"power_w": power_w, "source": source,
                         "package": power.get("package", 0), "core": power.get("core", 0),
                         "dram": power.get("dram", 0),
                         "base": power.get("base"),
                         "disks_w": power.get("disks_w", 0),
                         "disks_active": power.get("disks_active")},
            "energy": {"today_kwh": today_kwh, "month_kwh": month_kwh, "boot_kwh": boot_kwh,
                       "basis": energy_basis},
            "cost": {"today_yuan": round(today_kwh * rate, 2), "month_yuan": round(month_kwh * rate, 2), "rate_yuan_per_kwh": rate},
            "disks": standby,
            "standby_protect": bool(int(self.config.get("disk_standby_protect", 1) or 0)),
            "estimation_params": {
                "tdp_w": float(self.config.get("power_tdp_w", 65)),
                "disk_typical_w": float(self.config.get("power_disk_typical_w", 8)),
                "nic_fixed_w": float(self.config.get("power_nic_fixed_w", 5)),
                "base_w": float(self.config.get("power_base_w", 8)),
            },
        }
        self._power_stats_cache = result
        self._power_stats_ts = now
        return result

    def _daily_data(self, days):
        """每日汇总（60 秒缓存；多客户端 / 多面板共用）。"""
        days = max(1, min(int(days), History.DAILY_KEEP_DAYS))
        now = time.time()
        c = self._daily_cache.get(days)
        if c and now - c[0] < 60:
            return c[1]
        hist_iv = int(self.config.get("history_interval", DEFAULT_CONFIG["history_interval"]) or 60)
        data = self.collector.db.daily_rows(days, default_dt=hist_iv)
        if len(self._daily_cache) > 8:
            self._daily_cache.clear()
        self._daily_cache[days] = (now, data)
        return data

    def api_daily(self, days=30):
        """近 N 天（含今天，本地自然日）的每日汇总：用电 kWh / 电费 / 物理网口流量 / CPU。

        days 列表按日期升序、每天一项；某项没有任何数据时该字段为 null（不是 0），
        has_data=false 表示当天完全没有采样（未运行 / 早于安装）。
        kwh 为功率历史积分（RAPL 实测或模型估算，与能耗面板同源）；power_h 为当天有功率数据
        覆盖的小时数，< 24 表示当天数据不完整。rx/tx 口径与流量统计一致：排除 lo，
        traffic_exclude_bridge 开启时排除 docker/veth 等虚拟网口。"""
        try:
            days = int(days)
        except Exception:
            days = 30
        days = max(1, min(days, History.DAILY_KEEP_DAYS))
        data = self._daily_data(max(days, 31)) or {}   # 统一取 ≥31 天，图表与今日/本月合计共用一份缓存
        meta = data.get("_meta") or {}
        rate = float(self.config.get("power_rate_yuan", 0.6) or 0)
        exclude_bridge = bool(int(self.config.get("traffic_exclude_bridge", 0) or 0))
        now = time.time()
        start = History._day_start(now)
        out = []
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(start - i * 86400 + 3600))
            d = data.get(day) or {}
            kwh = d.get("kwh") if "power_s" in d else None
            has_net = any(k in d for k in ("rx_phys", "tx_phys", "rx_virt", "tx_virt"))
            rx = tx = None
            if has_net:
                rx = d.get("rx_phys", 0.0) + (0.0 if exclude_bridge else d.get("rx_virt", 0.0))
                tx = d.get("tx_phys", 0.0) + (0.0 if exclude_bridge else d.get("tx_virt", 0.0))
            n = d.get("cpu_n") or 0
            out.append({
                "day": day,
                "has_data": bool(d),
                "kwh": round(kwh, 4) if kwh is not None else None,
                "cost": round(kwh * rate, 2) if kwh is not None else None,
                "power_h": round(d["power_s"] / 3600.0, 2) if "power_s" in d else None,
                "avg_w": round(kwh * 3600000.0 / d["power_s"], 1) if kwh is not None and d.get("power_s") else None,
                "rx": int(rx) if rx is not None else None,
                "tx": int(tx) if tx is not None else None,
                "cpu_avg": round(d.get("cpu_sum", 0.0) / n, 1) if n else None,
                "cpu_max": round(d["cpu_max"], 1) if n and "cpu_max" in d else None,
            })
        return {"days": out, "rate": rate, "today": out[-1]["day"] if out else None,
                "exclude_bridge": exclude_bridge, "missing": "null",
                "archived_until": meta.get("archived_until"),
                "power_source": (self.collector.get_snapshot().get("power") or {}).get("source")}

    def _daily_today_month(self):
        """今日 / 本月合计（来自每日汇总，不受 retention 影响）。读不到返回 None。"""
        try:
            now = time.time()
            mday = time.localtime(now).tm_mday
            j = self.api_daily(max(mday, 1))
        except Exception:
            traceback.print_exc()
            return None
        days = j.get("days") or []
        if not days or not days[-1].get("has_data"):
            return None
        month = time.strftime("%Y-%m-", time.localtime(now))
        mine = [d for d in days if d["day"].startswith(month)]
        sm = lambda k: sum((d.get(k) or 0) for d in mine)
        t = days[-1]
        return {"today_kwh": t.get("kwh"), "month_kwh": sm("kwh"),
                "today_rx": t.get("rx"), "today_tx": t.get("tx"),
                "month_rx": sm("rx"), "month_tx": sm("tx"),
                "has_power": t.get("kwh") is not None, "has_net": t.get("rx") is not None}

    def api_net_bytes_history(self, range_):
        """各网卡累计字节时间序列（聚合 net_rx_bytes:* / net_tx_bytes:*，SQL 内降采样）。"""
        seconds = self._RANGE_SECONDS.get(range_, 3600)
        db = self.collector.db
        span = self._span(seconds)
        mr, mt = db.metrics_with_prefix("net_rx_bytes:"), db.metrics_with_prefix("net_tx_bytes:")
        bk = db.bucket_for(mr + mt, seconds)
        rx = db.sum_series(mr, seconds, span=span, bucket=bk)
        tx = db.sum_series(mt, seconds, span=span, bucket=bk)
        return self._merge_pair(rx, tx, "rx", "tx")

    @staticmethod
    def _merge_pair(a, b, ka, kb):
        """两条序列按时间戳对齐（缺失补 0），与旧版输出格式一致。"""
        am, bm = dict(a), dict(b)
        order = sorted(set(am) | set(bm))
        return {ka: [[t, round(am.get(t, 0), 1)] for t in order],
                kb: [[t, round(bm.get(t, 0), 1)] for t in order]}

    def api_ctr_history(self, range_):
        """容器累计流量时间序列（聚合 ctr_in:* / ctr_out:*，SQL 内降采样）。"""
        seconds = self._RANGE_SECONDS.get(range_, 3600)
        db = self.collector.db
        span = self._span(seconds)
        mi, mo = db.metrics_with_prefix("ctr_in:"), db.metrics_with_prefix("ctr_out:")
        bk = db.bucket_for(mi + mo, seconds)
        ci = db.sum_series(mi, seconds, span=span, bucket=bk)
        co = db.sum_series(mo, seconds, span=span, bucket=bk)
        return self._merge_pair(ci, co, "points", "out")

    @staticmethod
    def _csv_line(row):
        buf = io.StringIO()
        csv.writer(buf).writerow(row)
        return buf.getvalue().encode("utf-8")

    @staticmethod
    def _iso(ts):
        try:
            return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
        except Exception:
            return str(ts)

    def _iter_wide_csv(self, seconds, cols, rows, empty_lines):
        """宽表 CSV 生成器：rows 为按 ts 排序的 (ts, metric, value) 迭代器，逐时刻输出一行。"""
        if not cols:
            yield "\ufeff".encode("utf-8")
            for line in empty_lines:
                yield self._csv_line(line)
            return
        yield "\ufeff".encode("utf-8")   # 加 BOM，Excel/WPS 直接打开不乱码
        yield self._csv_line(["时间(本地)", "unix_ts"] + cols)
        cur_ts, cur = None, {}
        for ts, metric, value in rows:
            try:
                ts = float(ts)
                value = float(value)
                if not math.isfinite(value):
                    continue
            except Exception:
                continue
            if ts != cur_ts:
                if cur_ts is not None:
                    yield self._csv_line([self._iso(cur_ts), "%.3f" % cur_ts] + [cur.get(c, "") for c in cols])
                cur_ts, cur = ts, {}
            cur[metric] = value
        if cur_ts is not None:
            yield self._csv_line([self._iso(cur_ts), "%.3f" % cur_ts] + [cur.get(c, "") for c in cols])

    def iter_traffic_csv(self, range_):
        """导出流量统计 CSV（流式）。"""
        seconds = self._RANGE_SECONDS.get(range_, 604800) if range_ in self._RANGE_SECONDS else 604800
        db = self.collector.db
        pf = ["net_", "ctr_"]
        cols = db.export_columns(seconds, prefixes=pf)
        return self._iter_wide_csv(seconds, cols, db.iter_rows(seconds, prefixes=pf),
                                   [["说明", "流量历史库暂无采样数据"]])

    def iter_power_csv(self, range_):
        """导出功耗统计 CSV（流式，完整区间；旧版受 7200 行上限截断）。"""
        seconds = self._RANGE_SECONDS.get(range_, 604800) if range_ in self._RANGE_SECONDS else 604800
        db = self.collector.db

        def gen():
            yield "\ufeff".encode("utf-8")
            yield self._csv_line(["时间(本地)", "unix_ts", "功耗(W)", "估算功耗(W)"])
            cur_ts, cur = None, {}
            for ts, metric, value in db.iter_rows(seconds, metrics=["power", "power_est"]):
                if ts != cur_ts:
                    if cur_ts is not None and "power" in cur:
                        yield self._csv_line([self._iso(cur_ts), "%.3f" % cur_ts, cur["power"],
                                              cur.get("power_est", "")])
                    cur_ts, cur = ts, {}
                cur[metric] = value
            if cur_ts is not None and "power" in cur:
                yield self._csv_line([self._iso(cur_ts), "%.3f" % cur_ts, cur["power"], cur.get("power_est", "")])
        return gen()

    def api_gpu(self, diag=False):
        """GPU 实时监控；diag=True 时附带核显读数诊断（PMU / RC6 / 驱动，可直接贴到问题反馈）。"""
        gpus = self.collector.get_snapshot().get("gpu", [])
        out = {"gpu": gpus}
        if diag:
            intel = [g for g in gpus if g.get("vendor") == "8086"]
            pci = intel[0].get("pci") if intel else None
            try:
                out["diag"] = intel_gpu_diag(pci)
                out["diag"]["intel_gpu_found"] = bool(intel)
                out["diag"]["version"] = VERSION
            except Exception as e:
                out["diag"] = {"error": str(e)[:200]}
        return out

    def api_memory(self):
        """内存插槽 SPD。"""
        return self.collector.get_snapshot().get("memory", {"items": [], "dual": False})

    def api_weather(self):
        """实时天气（需外网，30 分钟缓存，支持自定义位置，失败返回降级信息）。"""
        now = time.time()
        if self._weather_cache and (now - self._weather_ts) < 1800:
            return self._weather_cache
        w = fetch_weather(self.config.get("weather_city") or "")
        self._weather_cache = w
        self._weather_ts = now
        return w

    def api_report(self):
        """一键健康报告：从历史库聚合生成 JSON / CSV / HTML。"""
        try:
            snap = self.collector.get_snapshot()
        except Exception:
            traceback.print_exc()
            snap = {}
        seconds = 86400 * 7  # 最近 7 天
        try:
            # SQL 内逐指标聚合（旧版 fetchall 7 天全部行，Docker 主机约 200 万行 / 400MB 峰值内存）
            agg = self.collector.db.report_stats(seconds)
        except Exception:
            traceback.print_exc()
            agg = {}
        summary = {}
        for metric, a in agg.items():
            summary[metric] = {
                "last": round(a["last"], 2),
                "max": round(a["max"], 2),
                "avg": round(a["sum"] / a["n"], 2) if a["n"] else None,
                "samples": a["n"],
            }
        # 阈值判定：与界面「设置 → 提醒阈值」同口径（ui.json thresholds，缺省用 THRESHOLD_SPEC 默认值）
        th = sanitize_thresholds((self.api_ui_get().get("ui") or {}).get("thresholds"))
        alerts = []

        def _judge(metric, stat, item, warn_k, crit_k, unit):
            v = (summary.get(metric) or {}).get(stat)
            if v is None:
                return
            if v >= th[crit_k]:
                alerts.append({"level": "crit", "item": item, "value": v, "threshold": "≥%s%s" % (th[crit_k], unit)})
            elif v >= th[warn_k]:
                alerts.append({"level": "warn", "item": item, "value": v, "threshold": "≥%s%s" % (th[warn_k], unit)})

        _judge("temp", "max", "CPU 温度（峰值）", "tempWarn", "tempCrit", "°C")
        _judge("cpu", "avg", "CPU 占用（平均）", "cpuWarn", "cpuCrit", "%")
        _judge("mem", "max", "内存占用（峰值）", "memWarn", "memCrit", "%")
        _judge("disk_total", "max", "存储占用（峰值）", "capWarn", "capCrit", "%")
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": VERSION,
            "summary": summary,
            "alerts": alerts,
            "thresholds": th,
            "overview": snap,
        }

    def api_report_html(self):
        """生成健康报告 HTML（内嵌迷你趋势条）。"""
        try:
            data = self.api_report()
            s = data["summary"] or {}
            rows_html = ""
            for m in ("cpu", "mem", "load1", "disk_total", "temp", "temp_mb", "power", "fan_avg"):
                a = s.get(m)
                if not isinstance(a, dict):
                    continue
                def _fmt(v):
                    try:
                        f = float(v)
                        return "--" if not math.isfinite(f) else ("%.2f" % f)
                    except Exception:
                        return "--"
                rows_html += (
                    "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                    % (_metric_label(m), _fmt(a.get("avg")), _fmt(a.get("max")),
                       _fmt(a.get("last")), a.get("samples", 0))
                )
            alerts_html = ""
            if data["alerts"]:
                for al in data["alerts"]:
                    color = "#f59e0b" if al.get("level") == "warn" else "#ef4444"
                    alerts_html += '<div style="color:%s">⚠ %s：%s（阈值 %s）</div>' % (
                        color, al.get("item", ""), al.get("value", "--"), al.get("threshold", ""))
            else:
                alerts_html = '<div style="color:#10b981">✓ 当前无活动告警</div>'
            html = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>飞牛监控 健康报告</title><style>
body{font-family:system-ui,'PingFang SC',sans-serif;background:#f1f5f9;margin:0;padding:24px;color:#0f172a}
.card{background:#fff;border-radius:16px;padding:24px;margin:16px 0;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;color:#334155;margin:0 0 12px}
table{width:100%%;border-collapse:collapse}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #e2e8f0}
th{color:#64748b;font-weight:600;font-size:13px}.meta{color:#94a3b8;font-size:13px}
.badge{display:inline-block;background:#ecfdf5;color:#059669;border-radius:999px;padding:2px 10px;font-size:12px}
</style></head><body>
<h1>飞牛监控 · 健康报告</h1><div class="meta">生成时间：%(t)s ｜ 版本 v%(v)s</div>
<div class="card"><h2>告警摘要</h2>%(alerts)s</div>
<div class="card"><h2>指标统计（最近 7 天）</h2>
<table><tr><th>指标</th><th>平均</th><th>最大</th><th>最近</th><th>样本数</th></tr>%(rows)s</table></div>
</body></html>""" % {
                "t": data["generated_at"], "v": VERSION,
                "alerts": alerts_html, "rows": rows_html,
            }
            return html.encode("utf-8")
        except Exception:
            traceback.print_exc()
            return ("<html><body><h2>报告生成失败</h2>"
                    "<p>生成健康报告时发生错误，请查看服务日志（journalctl / 应用日志）中的异常堆栈。</p>"
                    "</body></html>").encode("utf-8")

    def api_docker_action(self, cid, action):
        if not cid:
            return {"ok": False, "error": "缺少容器 ID"}
        result = docker_action(cid, action)
        # 操作成功后立即刷新 docker 缓存，前端可马上看到新状态
        if result.get("ok"):
            try:
                self.collector._last_docker = collect_docker()
                self.collector._last_docker_ts = time.time()
            except Exception:
                pass
        return result

    def api_vm_action(self, uuid, action):
        if not uuid:
            return {"ok": False, "error": "缺少虚拟机 UUID"}
        result = vm_action(uuid, action)
        # 无论成败都丢弃缓存：前端随后轮询能立刻看到新状态
        self.vms.invalidate()
        return result

    def iter_history_csv(self, range_):
        """历史指标导出为 CSV 宽表（每行一个时间点，各指标一列），流式输出。"""
        seconds = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}.get(range_, 604800)
        db = self.collector.db
        try:
            found = db.export_columns(seconds)
        except Exception:
            traceback.print_exc()
            found = []
        # 固定核心指标靠前，其余（分区/网卡等）按出现顺序追加
        fixed = ["cpu", "mem", "load1", "disk_total", "temp"]
        cols = [m for m in fixed if m in found] + [m for m in found if m not in fixed]
        return self._iter_wide_csv(seconds, cols, db.iter_rows(seconds), [
            ["说明", "历史库暂无采样数据"],
            ["提示", "历史数据每 60 秒写入一条，请等待后台采集后再导出；若持续为空，可访问 /api/history/diag 查看历史库状态"]])

    def api_monthly_summary(self):
        """返回最近的月度汇总数据（上月优先，无则取最近一条）。

        数据来自 monthly_summary 表（水位线归档：cleanup 前与月初各数据只计入一次）。
        速率型指标返回 avg/max/min；累计型指标（流量）返回月内差值 = last_val - first_val。
        """
        enabled = bool(int(self.config.get("monthly_summary_enabled", 0) or 0))
        retention = int(self.config.get("retention_days", 7))
        try:
            got = self.collector.db.monthly_rows()   # 休眠保护：硬盘休眠时返回上次结果，不读库
        except Exception:
            traceback.print_exc()
            got = None
        if not got:
            return {"available": False, "enabled": enabled,
                    "retention_days": retention,
                    "note": "暂无汇总数据（需开启开关后等待后台采集归档）"}
        y, m, rows = got
        hist_interval = int(self.config.get("history_interval", 60))
        result = {"year": y, "month": m, "available": True, "enabled": enabled,
                  "retention_days": retention}
        net_rx_total = 0
        net_tx_total = 0
        for metric, s, cnt, mx, mn, fv, lv in rows:
            avg = (s / cnt) if cnt else 0
            if metric == "cpu":
                result["cpu"] = {"avg": round(avg, 1), "max": round(mx or 0, 1), "min": round(mn or 0, 1)}
            elif metric == "mem":
                result["mem"] = {"avg": round(avg, 1), "max": round(mx or 0, 1), "min": round(mn or 0, 1)}
            elif metric == "disk_total":
                result["disk"] = {"avg": round(avg, 1), "max": round(mx or 0, 1)}
            elif metric == "temp":
                result["temp"] = {"avg": round(avg, 1), "max": round(mx or 0, 1), "min": round(mn or 0, 1)}
            elif metric == "power":
                kwh = round((s or 0) * hist_interval / 3_600_000, 2)
                rate = float(self.config.get("power_rate_yuan", 0.6))
                result["power"] = {"avg_w": round(avg, 1), "max_w": round(mx or 0, 1),
                                   "kwh": kwh, "yuan": round(kwh * rate, 2)}
            elif metric == "load1":
                result["load1"] = {"avg": round(avg, 2), "max": round(mx or 0, 2)}
            elif metric == "fan_avg":
                result["fan"] = {"avg": round(avg, 0)}
            elif metric.startswith("net_rx_bytes:"):
                net_rx_total += max(0, (lv or 0) - (fv or 0))
            elif metric.startswith("net_tx_bytes:"):
                net_tx_total += max(0, (lv or 0) - (fv or 0))
        result["net_rx"] = net_rx_total
        result["net_tx"] = net_tx_total
        # 提示：retention 不足整月时告知用户已启用叠加保存
        if enabled and retention < 31:
            result["note"] = "历史保留天数 %d 不足整月，已自动叠加保存汇总统计" % retention
        return result

    def api_export_status(self):
        """当前状态全量快照导出为 JSON。"""
        return {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": VERSION,
            "overview": json.loads(self.api_overview().decode("utf-8")),   # 已去除口令哈希等敏感字段
            "docker": self.api_docker(),
            "modules": self.api_modules(),
            "processes": self.api_processes(),
            "apps": self.api_apps(),
            "system": self.api_system(),
        }


def _metric_label(m):
    """历史指标名 → 中文标签（健康报告用）。"""
    return {
        "cpu": "CPU 使用率 %", "mem": "内存占用 %", "load1": "系统负载",
        "disk_total": "磁盘占用 %", "temp": "CPU 温度 °C", "temp_mb": "主板温度 °C",
        "power": "整机功耗 W", "fan_avg": "风扇平均 RPM", "net_rx": "网络下行", "net_tx": "网络上行",
    }.get(m, m)


_SECRET_ARG_RE = re.compile(
    r"(?i)((?:^|[\s\-])(?:[\w.-]*(?:pass(?:word|wd)?|pwd|token|secret|api[_-]?key|access[_-]?key|auth[_-]?key)"
    r"[\w.-]*)(?:=|:|\s+))(\S+)")


def _redact_args(args):
    """进程命令行脱敏：password=xxx / --token xxx 等参数值替换为 ***。"""
    try:
        return _SECRET_ARG_RE.sub(lambda m: m.group(1) + "***", args)
    except Exception:
        return args


def top_processes(limit=20):
    out, rc = run_cmd(
        ["ps", "-eo", "pid,user,pcpu,pmem,rss,comm,args", "--sort=-pcpu", "--no-headers"],
        timeout=10,
    )
    if rc != 0 or not out.strip():
        return []
    result = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 6)
        if len(parts) < 6:
            continue
        try:
            pid = int(parts[0])
            user = parts[1]
            cpu = float(parts[2])
            mem = float(parts[3])
            rss_kb = int(parts[4])
            comm = parts[5]
            args = parts[6] if len(parts) > 6 else comm
        except Exception:
            continue
        result.append({
            "pid": pid, "user": user, "cpu": cpu, "mem": mem,
            "rss": rss_kb * 1024, "comm": comm, "args": _redact_args(args)[:160],
        })
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def _launcher_cfg_candidates():
    """桌面入口 ui/config 的全部候选位置。
    关键：应用可执行目录（target，server.py 所在）下的 ui/config 与应用基础目录
    /var/apps/{appname}/ui/config 是两个不同文件——fnOS 应用中心 / 桌面从基础目录
    读取入口配置（同 v2.12.1 发现 manifest 从基础目录读取）。只写 target 下的文件
    对桌面无效（v2.16.0 切换不生效的根因）。返回 [(路径, 是否允许新建), ...]，
    允许新建仅限 /var/apps 基础目录（缺失时用应用内原始配置补建）。"""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    app_name = os.environ.get("TRIM_APPNAME", "") or "fnmonitor"
    cands = [(os.path.join(app_dir, "ui", "config"), True)]
    seen = {os.path.abspath(os.path.normpath(c[0])) for c in cands}
    bases = [os.path.dirname(app_dir),           # target 的父目录即基础目录的布局
             "/var/apps/%s" % app_name,          # fnOS 应用基础目录（桌面实际读取）
             "/usr/trim/apps/%s" % app_name,
             "/var/lib/fnos/apps/%s" % app_name]
    for base in bases:
        if not base or not base.strip("/"):
            continue
        p = os.path.join(base, "ui", "config")
        ap = os.path.abspath(os.path.normpath(p))
        if ap in seen:
            continue
        seen.add(ap)
        allow_new = ap.replace("\\", "/").startswith("/var/apps/")
        cands.append((p, allow_new))
    return cands


def apply_launcher_entry(mode=None, port=None):
    """把打开方式（type）与监听端口（port）写入桌面入口 ui/config（.url 条目），
    同步写入全部候选位置（应用可执行目录 + /var/apps/{appname} 基础目录等），
    基础目录缺失 ui/config 时用应用内原始配置补建；随后调用 sync_launcher_db
    同步 fnOS 应用中心数据库——真机实证桌面点击行为由数据库决定（磁盘 ui/config
    仅在安装/升级时被物化进库），只改文件无效。
    mode: "iframe"=点击桌面图标在飞牛内嵌窗口打开；"url"=点击桌面图标直接在浏览器
          新标签页打开（不再弹出内嵌窗口）；None=不修改 type。
    port: 服务实际监听端口（桌面图标按该端口连接）；None=不修改 port。
    用户切换打开方式时由保存接口调用本函数（改完刷新桌面页即生效）；升级包会覆盖
    ui/config 并把数据库重置为包默认 iframe，故应用启动时也按用户配置重新应用一次
    （不重启任何服务，由前端占位页兜底）。
    返回 (是否成功, 说明)，说明含各位置写入与数据库同步结果，便于真机排查。"""
    if mode is not None and mode not in ("iframe", "url"):
        return False, "不支持的打开方式: %s" % mode
    try:
        port = None if port is None else int(port)
    except Exception:
        return False, "端口无效: %r" % (port,)
    if port is not None and port <= 0:
        port = None
    if mode is None and port is None:
        return True, "无变更"
    default_raw = None
    wrote, any_change, failed = [], False, []
    for cfg_path, allow_new in _launcher_cfg_candidates():
        try:
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            elif allow_new and default_raw:
                # 基础目录缺少 ui/config：以应用可执行目录当前配置补建（桌面从此读取）
                data = json.loads(default_raw)
                os.makedirs(os.path.dirname(cfg_path), exist_ok=True)
            else:
                continue
        except Exception as e:
            failed.append("%s: %s" % (cfg_path, e))
            continue
        if default_raw is None and os.path.isfile(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    default_raw = f.read()
            except Exception:
                pass
        try:
            changed = False
            for entry in (data.get(".url") or {}).values():
                if not isinstance(entry, dict):
                    continue
                if mode is not None and entry.get("type") != mode:
                    entry["type"] = mode
                    changed = True
                if port is not None and str(entry.get("port") or "") != str(port):
                    entry["port"] = str(port)
                    changed = True
            if changed:
                any_change = True
                tmp = cfg_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=4)
                os.replace(tmp, cfg_path)
            wrote.append(cfg_path)
        except Exception as e:
            failed.append("%s: %s" % (cfg_path, e))
    file_ok = not failed
    if not wrote:
        file_msg = ("桌面入口写入失败: %s" % "; ".join(failed)) if failed else \
            "未找到任何可写的桌面入口配置 ui/config"
    elif not any_change:
        file_msg = "桌面入口已一致（%d 处，无需变更）" % len(wrote)
    else:
        file_msg = "桌面入口已更新 %d 处%s" % (len(wrote),
              ("（含 %s）" % "、".join(wrote[1:])) if len(wrote) > 1 else "")
        if failed:
            file_msg += "；部分失败: %s" % "; ".join(failed)
    db_ok, db_msg = sync_launcher_db(mode=mode, port=port)
    return (file_ok and db_ok), file_msg + (("；" + db_msg) if db_msg else "")


def _launcher_launchname():
    """桌面入口标识（manifest 的 desktop_applaunchname，即 ui/config .url 下的 key）。
    fnOS 数据库以该标识定位应用的入口记录（app_service.service_name 等）。从首个
    存在的 ui/config 候选读取。"""
    for cfg_path, _allow_new in _launcher_cfg_candidates():
        try:
            if os.path.isfile(cfg_path):
                with open(cfg_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                keys = [k for k in (data.get(".url") or {}) if k]
                if keys:
                    return keys[0]
        except Exception:
            continue
    return None


def sync_launcher_db(mode=None, port=None):
    """把打开方式/端口同步进 fnOS 的 PostgreSQL 数据库。
    真机实证的最终生效链路：磁盘 ui/config 仅在安装/升级时被物化进库，此后桌面
    点击行为完全由数据库决定——appcenter.app_service.type 是终极数据源（应用中心
    重启约 20 秒后系统据此全量重建 trim_sac.entry 表），appcenter.app_open.open_type
    同期物化；trim_sac.entry 是桌面会话当前的入口数据，直接更新后刷新桌面页即生效，
    无需重启任何系统服务。升级/重装会把库重置为包默认 iframe，故应用启动时也调用
    本函数拉齐。应用以 root 运行，经 sudo -u postgres psql 执行（SQL 走 stdin 避开
    shell 引号转义），逐表 SELECT 当前值、不一致才 UPDATE。非 fnOS 环境（Windows
    开发机）或非 root 运行时跳过并视为成功。返回 (是否成功, 说明)。"""
    if os.name == "nt":
        return True, ""
    try:
        if os.geteuid() != 0:
            return True, ""
    except Exception:
        pass
    launch = _launcher_launchname()
    if not launch:
        return True, ""

    def psql(db, sql):
        r = subprocess.run(
            ["sudo", "-u", "postgres", "psql", "-U", "postgres", "-d", db,
             "-At", "-v", "ON_ERROR_STOP=1"],
            input=sql.encode("utf-8"), capture_output=True, timeout=25)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.decode("utf-8", "ignore").strip()
                               or "psql 退出码 %d" % r.returncode)
        return r.stdout.decode("utf-8", "ignore").strip()

    def lit(v):
        # SQL 字符串字面量用 dollar-quoting，避开引号转义问题
        return "$fn$%s$fn$" % v

    changed = []
    try:
        if mode is not None:
            # 应用中心库：type 为终极数据源，open_type 与之同期物化
            cur = psql("appcenter",
                       "SELECT COALESCE((SELECT type FROM app_service WHERE service_name = %s), '')"
                       " || '|' || COALESCE((SELECT open_type FROM app_open WHERE content = %s), '');"
                       % (lit(launch), lit(launch)))
            svc_type, _, open_type = cur.partition("|")
            if svc_type != mode:
                psql("appcenter",
                     "UPDATE app_service SET type = %s WHERE service_name = %s;"
                     % (lit(mode), lit(launch)))
                changed.append("app_service.type")
            if open_type != mode:
                psql("appcenter",
                     "UPDATE app_open SET open_type = %s WHERE content = %s;"
                     % (lit(mode), lit(launch)))
                changed.append("app_open.open_type")
            # 桌面入口表：直接改即可让当前桌面会话生效（应用中心重启后会被系统重建）
            cur = psql("trim_sac",
                       "SELECT COALESCE((SELECT open_type FROM entry WHERE service_name = %s), '');"
                       % lit(launch))
            if cur != mode:
                psql("trim_sac",
                     "UPDATE entry SET open_type = %s WHERE service_name = %s;"
                     % (lit(mode), lit(launch)))
                changed.append("entry.open_type")
        if port is not None:
            # 应用中心库 url/default_url 形如 http://${host}:<port>/（保留原协议；
            # 反代等非端口型地址不匹配则不动）
            cur = psql("appcenter",
                       "SELECT COALESCE((SELECT url FROM app_service WHERE service_name = %s), '');"
                       % lit(launch))
            m = re.match(r"^([a-z]+)://\$\{host\}:(\d+)/$", cur or "")
            if m and int(m.group(2)) != port:
                new_url = "%s://${host}:%d/" % (m.group(1), port)
                psql("appcenter",
                     "UPDATE app_service SET url = %s, default_url = %s WHERE service_name = %s;"
                     % (lit(new_url), lit(new_url), lit(launch)))
                changed.append("app_service.url")
            # 桌面入口表 url 为 JSON（protocol/port/path），桌面据此拼出完整地址
            cur = psql("trim_sac",
                       "SELECT COALESCE((SELECT url FROM entry WHERE service_name = %s), '');"
                       % lit(launch))
            try:
                eurl = json.loads(cur) if cur else {}
            except Exception:
                eurl = {}
            if isinstance(eurl, dict) and str(eurl.get("port") or "") != str(port):
                new_eurl = json.dumps(
                    {"protocol": eurl.get("protocol") or "http",
                     "port": str(port),
                     "path": eurl.get("path") or "/"},
                    separators=(",", ":"), ensure_ascii=False)
                psql("trim_sac",
                     "UPDATE entry SET url = %s WHERE service_name = %s;"
                     % (lit(new_eurl), lit(launch)))
                changed.append("entry.url")
    except Exception as e:
        return False, "桌面入口数据库同步失败: %s" % e
    if changed:
        return True, "桌面入口数据库已更新: %s" % "、".join(changed)
    return True, "入口数据库已一致"


def load_config(data_dir):
    """读取 config.json（逐项容错：单个字段格式错误不影响其余字段，尤其不能让鉴权静默失效）。
    发现旧版明文 auth_password 时自动迁移为 PBKDF2 哈希、删除明文并以 0600 重写文件。"""
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = os.path.join(data_dir, "config.json")
    try:
        with open(cfg_path, "r", errors="ignore") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            user = {}
    except Exception:
        user = {}
    onoff = lambda x: 1 if str(x).lower() in ("1", "true", "on") else 0
    for k in ("interval", "retention_days", "port", "history_interval"):
        if k in user:
            try:
                cfg[k] = int(user[k])
            except Exception:
                pass
    # 开关类（在线更新 / 流量 / 休眠保护 / 上月汇总 / 鉴权 / EC 风扇探测 / 硬盘巡检）必须随配置恢复
    for k in ("update_autocheck", "update_autodownload", "update_autoupdate", "traffic_exclude_bridge",
              "disk_standby_protect", "monthly_summary_enabled", "auth_enabled", "ec_fan_probe",
              "disk_scan_enabled"):
        if k in user:
            cfg[k] = onoff(user[k])
    if "disk_scan_interval_h" in user:
        try:
            cfg["disk_scan_interval_h"] = int(user["disk_scan_interval_h"])
        except Exception:
            pass
    for k in ("power_tdp_w", "power_disk_typical_w", "power_nic_fixed_w",
              "power_rate_yuan", "power_base_w"):
        if k in user:
            try:
                cfg[k] = float(user[k])
            except Exception:
                pass
    if "weather_city" in user:
        cfg["weather_city"] = clean_text(user["weather_city"], 64)
    if "data_dir" in user:
        cfg["data_dir"] = str(user["data_dir"]).strip()
    # 飞牛桌面打开方式（iframe=飞牛窗口内打开 / url=浏览器新标签页）
    if str(user.get("open_mode", "")).lower() in ("iframe", "url"):
        cfg["open_mode"] = str(user["open_mode"]).lower()
    if "auth_session_days" in user:
        try:
            cfg["auth_session_days"] = max(1, min(365, int(user["auth_session_days"])))
        except Exception:
            pass
    h = str(user.get("auth_password_hash") or "").strip()
    if h.startswith("pbkdf2_sha256$"):
        cfg["auth_password_hash"] = h
    legacy = str(user.get("auth_password") or "")
    if "auth_password" not in user and os.path.exists(cfg_path) and _file_mode(cfg_path) & 0o077:
        try:
            os.chmod(cfg_path, 0o600)    # 旧版 / 安装脚本创建的 0644 配置文件收紧权限
        except Exception:
            pass
    if "auth_password" in user:
        # 迁移：明文 → 哈希（已有哈希时只删除明文），以 0600 原子重写
        if legacy.strip() and not cfg["auth_password_hash"]:
            cfg["auth_password_hash"] = hash_password(legacy.strip())
            print("[fnmonitor] 已将 config.json 中的明文登录口令迁移为 PBKDF2 哈希")
        try:
            with _CFG_WRITE_LOCK:
                new = dict(user)
                new.pop("auth_password", None)
                if cfg["auth_password_hash"]:
                    new["auth_password_hash"] = cfg["auth_password_hash"]
                write_json_secure(cfg_path, new)
        except Exception:
            traceback.print_exc()
    return cfg


def _file_mode(path):
    try:
        return os.stat(path).st_mode & 0o777
    except Exception:
        return 0


def port_available(port):
    """测试端口能否被本进程绑定（双栈优先；SO_REUSEADDR 与 HTTPServer 一致，TIME_WAIT 不误判）。"""
    for fam, addr in ((socket.AF_INET6, "::"), (socket.AF_INET, "0.0.0.0")):
        try:
            sk = socket.socket(fam, socket.SOCK_STREAM)
        except OSError:
            continue
        try:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if fam == socket.AF_INET6:
                try:
                    sk.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except Exception:
                    pass
            sk.bind((addr, int(port)))
            return True
        except (OSError, OverflowError, ValueError) as e:
            if fam == socket.AF_INET6 and getattr(e, "errno", None) in (97, 99, 22):  # 无 IPv6：退回 IPv4 测试
                continue
            return False
        finally:
            sk.close()
    return False


_SYSTEM_DIR_PREFIXES = ("/proc", "/sys", "/dev", "/run", "/etc", "/boot", "/usr", "/bin", "/sbin",
                        "/lib", "/lib32", "/lib64", "/libx32", "/var/apps", "/var/lib/dpkg")


def validate_data_dir(nd):
    """自定义数据目录：须为绝对路径，不能落在系统目录内。返回 (ok, 原因)。"""
    if not os.path.isabs(nd):
        return False, "数据目录须为以 / 开头的绝对路径"
    rp = os.path.realpath(os.path.normpath(nd))
    if rp == "/" or any(rp == p or rp.startswith(p + "/") for p in _SYSTEM_DIR_PREFIXES):
        return False, "数据目录不能位于系统目录（%s）" % rp
    return True, ""


class BoundedHTTPServerMixin:
    """限制并发处理线程：总数 MAX_CONN、单 IP MAX_PER_IP，超出的连接立即回 503 并关闭，
    防止半开连接 / 慢速请求耗尽线程与文件描述符。"""
    MAX_CONN = 64
    MAX_PER_IP = 24
    daemon_threads = True
    _BUSY = (b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json; charset=utf-8\r\n"
             b"Retry-After: 2\r\nConnection: close\r\nContent-Length: 27\r\n\r\n"
             b'{"ok":false,"error":"busy"}')

    def _slot_init(self):
        if not hasattr(self, "_slot_lock"):
            self._slot_lock = threading.Lock()
            self._slot_total = 0
            self._slot_ips = {}

    def process_request(self, request, client_address):
        self._slot_init()
        ip = norm_ip(client_address[0] if client_address else "")
        with self._slot_lock:
            ok = self._slot_total < self.MAX_CONN and self._slot_ips.get(ip, 0) < self.MAX_PER_IP
            if ok:
                self._slot_total += 1
                self._slot_ips[ip] = self._slot_ips.get(ip, 0) + 1
        if not ok:
            try:
                request.settimeout(1.0)
                request.sendall(self._BUSY)
            except Exception:
                pass
            self.shutdown_request(request)
            return
        t = threading.Thread(target=self._slot_run, args=(request, client_address, ip), daemon=True)
        try:
            t.start()
        except Exception:
            self._slot_release(ip)
            self.shutdown_request(request)

    def _slot_run(self, request, client_address, ip):
        try:
            self.process_request_thread(request, client_address)
        finally:
            self._slot_release(ip)

    def _slot_release(self, ip):
        with self._slot_lock:
            self._slot_total -= 1
            n = self._slot_ips.get(ip, 1) - 1
            if n <= 0:
                self._slot_ips.pop(ip, None)
            else:
                self._slot_ips[ip] = n


class BoundedHTTPServer(BoundedHTTPServerMixin, ThreadingHTTPServer):
    pass


class DualStackHTTPServer(BoundedHTTPServer):
    """双栈 HTTP 服务：同时监听 IPv4 与 IPv6（IPV6_V6ONLY=0）。

    默认 ThreadingHTTPServer 监听 0.0.0.0 仅支持 IPv4，导致 IPv6 地址 / IPv6 公网
    域名 + 端口无法直接访问。本类使用 AF_INET6 + V6ONLY=0，一个 socket 同时服务
    IPv4 与 IPv6 连接。
    """
    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except Exception:
            pass
        super().server_bind()


def _bind_server(bind_host, port, handler):
    """创建并绑定 HTTP 服务；失败抛异常。返回 (httpd, 监听说明)。"""
    if bind_host in ("0.0.0.0", "", None):
        try:
            return DualStackHTTPServer(("::", port), handler), ":: (IPv4+IPv6)"
        except (OSError, OverflowError) as e:
            if isinstance(e, OverflowError) or getattr(e, "errno", None) == 98:
                raise            # 端口越界 / 被占用：IPv4 回退同样会失败
            traceback.print_exc()
            return BoundedHTTPServer(("0.0.0.0", port), handler), "0.0.0.0 (IPv4)"
    return BoundedHTTPServer((bind_host, port), handler), bind_host


def main():
    ap = argparse.ArgumentParser(description="fnMonitor - fnOS 系统监控后端")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--data-dir", default=os.environ.get("TRIM_PKGVAR", "/tmp/fnmonitor-data"))
    args = ap.parse_args()
    # 日志行缓冲：stdout 重定向到 server.log 时登录失败等安全日志即时落盘
    for _st in (sys.stdout, sys.stderr):
        try:
            _st.reconfigure(line_buffering=True)
        except Exception:
            pass

    os.makedirs(args.data_dir, exist_ok=True)
    original_dir = args.data_dir
    # 单实例锁：同一配置目录只允许一个服务进程（防止 PID 文件失效时重复启动，
    # 第二个实例经端口回退在另一端口起来并与第一个实例同时写库）。重启时等待旧进程退出
    _lock_fh = None
    try:
        import fcntl
        _lock_fh = open(os.path.join(original_dir, ".server.lock"), "a")
        for _i in range(30):
            try:
                fcntl.flock(_lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                time.sleep(0.5)
        else:
            raise SystemExit("[fnmonitor] 已有一个飞牛监控实例在使用 %s，本次启动退出" % original_dir)
    except ImportError:
        pass
    config = load_config(args.data_dir)
    # 数据保存目录：默认 = 应用数据目录/localdata 子文件夹；用户可在设置页自定义（data_dir 配置覆盖）
    custom_dir = str(config.get("data_dir") or "").strip()
    if not (custom_dir and os.path.isabs(custom_dir) and validate_data_dir(custom_dir)[0]):
        custom_dir = os.path.join(args.data_dir, "localdata")
    try:
        os.makedirs(custom_dir, exist_ok=True)
        # 迁移历史数据 / 界面设置到目标目录（新目录缺失时才复制，不覆盖；config.json 固定留默认配置目录）
        for fn in ("monitor.db", "ui.json"):
            src = os.path.join(args.data_dir, fn)
            dst = os.path.join(custom_dir, fn)
            if os.path.exists(src) and not os.path.exists(dst):
                try:
                    shutil.copy2(src, dst)
                except Exception:
                    pass
        args.data_dir = custom_dir
        # 注意：config 仍从默认配置目录（original_dir）读取，切目录不重载
    except Exception:
        # 目录创建/迁移失败绝不能静默继续：否则 monitor.db 会因目录不存在
        # 而 "unable to open database file"，历史趋势将永远为空。回退到默认数据目录。
        traceback.print_exc()
        print("[fnmonitor] 自定义数据目录不可用，回退到 %s" % original_dir)
        args.data_dir = original_dir

    app = MonitorApp(args.data_dir, config, args.host, args.port, cfg_dir=original_dir)
    handler = app.make_handler()
    # 端口：配置文件优先（网页设置修改端口后重启生效）。配置端口越界或无法绑定时依次回退到
    # 启动参数端口（manifest service_port）与 8777，保证服务永远能起来（并记录日志）
    want = int(config.get("port") or 0) or args.port
    candidates = []
    for p in (want, args.port, 8777):
        if p not in candidates:
            candidates.append(p)
    httpd, listen_hint, port, last_err = None, "", None, None
    for p in candidates:
        if not (1 <= int(p) <= 65535):
            print("[fnmonitor] 配置端口 %s 无效（须 1–65535），尝试回退" % p)
            continue
        try:
            httpd, listen_hint = _bind_server(args.host, int(p), handler)
            port = int(p)
            break
        except Exception as e:
            last_err = e
            print("[fnmonitor] 端口 %s 绑定失败（%s），尝试回退" % (p, e))
    if httpd is None:
        raise SystemExit("[fnmonitor] 无可用端口: %s" % last_err)
    if port != want:
        print("[fnmonitor] 已回退到端口 %d（配置端口 %s 不可用）" % (port, want))
    app.port = port
    # 按用户配置同步桌面入口（升级包会覆盖 ui/config 并把应用中心数据库重置为
    # 包默认 iframe，type 与端口一并恢复；apply_launcher_entry 内部会同步 fnOS
    # 应用中心数据库——桌面点击行为由数据库决定）。使用实际监听端口。
    try:
        _m = str(config.get("open_mode") or "").lower()
        ok_l, msg_l = apply_launcher_entry(mode=(_m or None), port=port)
        if not ok_l:
            print("[fnmonitor] 桌面入口配置同步失败: %s" % msg_l)
    except Exception as e:
        print("[fnmonitor] 桌面入口配置同步异常: %s" % e)
    app.collector.start()
    # 硬盘巡检独立线程：与采集循环解耦，全盘 smartctl 不再拖慢 10 秒采样
    scanner = DiskScanner(app.collector.db, config)
    scanner.start()

    def shutdown(sig, frame):
        app.collector.stop()
        scanner.stop()
        threading.Thread(target=httpd.shutdown, daemon=True).start()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("fnMonitor %s listening on http://[%s]:%d" % (VERSION, listen_hint, port))
    print("data-dir: %s" % args.data_dir)
    if not app._auth_required():
        print("[fnmonitor] 提示：未启用登录口令，仅允许通过 IP / 局域网主机名访问（Host 白名单）")
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        app.collector.stop()


if __name__ == "__main__":
    main()
