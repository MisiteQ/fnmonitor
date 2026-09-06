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
import csv
import hashlib
import io
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
import urllib.request

VERSION = "2.9.4"
UPDATE_REPO = "MisiteQ/fnmonitor"          # GitHub 仓库：在线检查更新 / 下载安装包
UPDATE_CHECK_INTERVAL = 6 * 3600           # 自动更新检查周期（6 小时）
# 下载加速：直连 GitHub 下载域在国内常不可达，失败后自动依次尝试公共加速镜像
GH_MIRRORS = ["", "https://gh-proxy.com/", "https://ghfast.top/", "https://ghproxy.net/", "https://gh.llkk.cc/"]
DEFAULT_CONFIG = {"interval": 10, "retention_days": 7, "port": 0, "history_interval": 60, "weather_city": "", "data_dir": "", "update_autocheck": 1, "update_autodownload": 0, "update_autoupdate": 0}

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
    """读取 /proc/stat 的 CPU 时间。返回 (总时间列表, 每核时间列表)。"""
    total = None
    cores = []
    for line in read_text("/proc/stat").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "cpu":
            total = [int(x) for x in parts[1:8]]
        elif parts[0].startswith("cpu"):
            try:
                cores.append([int(x) for x in parts[1:8]])
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
        u = disk_usage(mp)
        if not u["total"] or u["total"] < _MIN_FS_BYTES:
            continue
        # fsid 为 0（个别文件系统不提供）时回退为设备真实路径，兼容 /dev/mapper 别名
        key = ("fsid", u["fsid"]) if u["fsid"] else ("dev", os.path.realpath(dev) or dev)
        entry = {
            "device": dev, "mount": mp, "fs": fs,
            "total": u["total"], "used": u["used"], "free": u["free"],
            "percent": u["percent"],
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


def collect_disks_detail():
    """按物理硬盘返回详情：名称/品牌/型号/容量/已用/使用率/温度/挂载点。"""
    result = []

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
    # 温度
    temp_by = {}
    for t in read_temps().get("disks", []):
        temp_by[t["name"]] = t["temp"]
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
        entry = {
            "name": name,
            "model": model or "未知型号",
            "vendor": vendor,
            "size": size,
            "used": used,
            "total": total,
            "percent": percent,
            "temp": temp_by.get(name),
            "mounts": mount_paths,
        }
        # SMART 健康 / 转速 / 通电时长（最多对前 8 块盘读取，避免拖慢循环）
        if len(result) < 8:
            sd = smart_detail(name)
            entry["health"] = sd.get("health", "未知")
            entry["rpm"] = sd.get("rpm", "")
            entry["power_on_hours"] = sd.get("power_on_hours", "")
            entry["serial"] = sd.get("serial", "")
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


def collect_ports(docker_result=None):
    """端口占用：飞牛应用中心应用端口 + Docker 容器端口映射 + 系统监听端口。"""
    result = {"apps": [], "docker": [], "listeners": [], "ts": time.time()}
    # 1) 飞牛应用中心已安装应用的 service_port（多候选目录，见 _app_center_ports）
    for appid, info in _app_center_ports().items():
        port = str(info.get("port") or "").strip()
        if port:
            result["apps"].append({"name": info.get("name") or appid,
                                   "appid": appid, "port": port})
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
    out, rc = run_cmd(["ss", "-tlnp"], timeout=10)
    if rc == 0 and out.strip():
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) < 5 or parts[0] != "LISTEN":
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
            result["listeners"].append({"addr": host, "port": port, "proto": "tcp", "process": pname, "pid": pid})
        # 去重排序
        seen = set()
        dedup = []
        for l in sorted(result["listeners"], key=lambda x: (x["port"], x["addr"])):
            k = (l["addr"], l["port"], l["process"], l["pid"])
            if k in seen:
                continue
            seen.add(k)
            dedup.append(l)
        result["listeners"] = dedup
    else:
        result["listeners"] = _read_proc_tcp_listeners()
    return result


def collect_hardware():
    """硬件信息：CPU / 主板 / BIOS / 显卡 / 网卡（参照飞牛官方资源管理器信息项）。"""
    info = {"cpu": {}, "board": {}, "gpu": [], "net": []}
    # CPU
    info["cpu"]["model"] = read_cpu_model()
    cores, threads = 0, 0
    for line in read_text("/proc/cpuinfo").splitlines():
        if line.startswith("processor"):
            cores += 1
        elif line.startswith("siblings"):
            try:
                threads = max(threads, int(line.split(":", 1)[1].strip()))
            except Exception:
                pass
    info["cpu"]["cores"] = cores
    info["cpu"]["threads"] = threads if threads else cores
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


def collect_raid_card():
    """阵列卡检测：lspci 识别 MegaRAID / HBA / 纯 SATA，MegaRAID 走 storcli（参照网络主流硬件监控面板阵列卡页）。"""
    info = {"type": "none", "label": "纯 SATA 主板（未检测到独立阵列卡）", "detail": ""}
    if not is_linux():
        return info
    out, rc = run_cmd(["lspci"], timeout=10)
    if rc != 0:
        return info
    card_line = ""
    for line in out.splitlines():
        low = line.lower()
        if any(k in low for k in ("raid", "megaraid", "hba", "sas", "storage controller", "sata controller")):
            card_line = line
            break
    if not card_line:
        return info
    low = card_line.lower()
    label = card_line.split(" ", 1)[1] if " " in card_line else card_line
    if "megaraid" in low or "raid" in low:
        info["type"] = "megaraid"
        info["label"] = label
        so, src = run_cmd(["storcli", "/c0", "show"], timeout=15)
        if src == 0:
            info["storcli"] = so
        info["detail"] = "MegaRAID 阵列卡（IR/RAID 模式），storcli 可查看完整信息"
    else:
        info["type"] = "hba"
        info["label"] = label
        info["detail"] = "HBA 直通卡（IT 模式），物理盘由系统直接识别为 /dev/sdX，SMART 见硬盘面板"
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
    if not watts:
        return {"ok": False}
    total = round(sum(watts.values()), 2)
    watts["ok"] = True
    watts["total"] = total
    return watts


# ===================== GPU 实时监控（网络主流硬件监控面板功能） =====================
def _gpu_ident_list():
    """lspci 识别显卡设备：vendor/type/name/pci。"""
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
        gtype = "igpu"
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


def _intel_igpu_util():
    """Intel 核显利用率（近似）：根据 i915 频率与 busy 采样估算。读不到返回 (None, False)。"""
    try:
        # 优先 i915 的 busy 采样（新版内核 /sys/class/drm/cardN/device/gt/gt0/rps_*）
        import glob as _glob
        busy = None
        for p in _glob.glob("/sys/class/drm/card*/device/gt/gt0/rps_busy"):
            v = read_int_file(p)
            if v is not None:
                busy = v / 10.0  # 单位 10us，占 10000 满分；近似百分比
                break
        if busy is not None:
            return (max(0.0, min(100.0, busy)), False)
        # 兜底：用 CPU 利用率近似核显负载
        return (None, True)
    except Exception:
        return (None, True)


def _intel_igpu_top_sample():
    """Intel 核显顶层采样（freq/power），读不到返回空 dict。"""
    out = {}
    try:
        import glob as _glob
        for p in _glob.glob("/sys/class/drm/card*/gt_cur_freq_mhz"):
            v = read_int_file(p)
            if v is not None:
                out["freq_mhz"] = v
                break
        for p in _glob.glob("/sys/class/drm/card*/device/power/runtime_active_time"):
            pass  # 功耗需两次差分，此处省略
    except Exception:
        pass
    return out


def _gpu_memory_bytes():
    """Intel 核显共享系统内存（近似）。返回 (used, total, pct)。"""
    try:
        mem = read_meminfo()
        total = mem.get("total", 0)
        used = mem.get("used", 0)
        if total > 0:
            return (used, total, round(used / total * 100, 1))
    except Exception:
        pass
    return (None, None, None)


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
            else:  # Intel 核显 / 无核显
                temp = read_temps().get("cpu")
                sample = _intel_igpu_top_sample()
                if sample.get("freq_mhz") is not None:
                    freq_mhz = sample["freq_mhz"]
                u, u_proxy = _intel_igpu_util()
                if u is not None:
                    util = u
                    util_avail = True
                    util_proxy = u_proxy
                mu, mt, mp = _gpu_memory_bytes()
                if mt:
                    mem_used, mem_total, mem_pct = mu, mt, mp
        except Exception:
            pass
        res.append({
            "vendor": g["vendor"], "type": g["type"], "name": g["name"], "pci": g["pci"],
            "temp": (round(temp, 1) if isinstance(temp, (int, float)) else None),
            "util": (round(util, 1) if isinstance(util, (int, float)) else None),
            "freq_mhz": (round(freq_mhz, 1) if isinstance(freq_mhz, (int, float)) else None),
            "power_w": (round(power_w, 2) if isinstance(power_w, (int, float)) else None),
            "mem_used": mem_used, "mem_total": mem_total, "mem_pct": mem_pct,
            "util_avail": util_avail, "util_proxy": util_proxy,
        })
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
    data = {"temps": [], "fans": [], "volts": [], "available": False}
    if not is_linux():
        return data
    out, rc = run_cmd(["sensors", "-j"], timeout=10)
    data["available"] = (rc == 0 and bool(out.strip()))
    if rc != 0 or not out.strip():
        return data
    try:
        parsed = json.loads(out)
    except Exception:
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
            for fk in ("fan1_input", "fan2_input", "fan3_input", "fan4_input", "fan5_input", "fan6_input"):
                if fk in val and isinstance(val[fk], (int, float)):
                    fan_rpm = val[fk]
                    break
            if fan_rpm is not None:
                data["fans"].append({"name": chip, "rpm": int(fan_rpm)})
            # 电压（sensors 中 inN_input 单位为 mV）
            volt = None
            for vk in ("in0_input", "in1_input", "in2_input", "in3_input", "in4_input", "in5_input", "in6_input"):
                if vk in val and isinstance(val[vk], (int, float)):
                    volt = val[vk]
                    break
            if volt is not None:
                label = key
                data["volts"].append({"name": label, "value": round(float(volt) / 1000.0, 3)})
    # 温度按 CPU / 芯片组 / 主板 / ACPI 顺序排列
    order = {"cpu": 0, "pch": 1, "board": 2, "acpi": 3, "disk": 4, "gpu": 5}
    data["temps"].sort(key=lambda t: (order.get(t["type"], 9), t["name"]))
    return data


def _hwmon_fans():
    """枚举 /sys/class/hwmon 下所有风扇通道（fanN_input + 对应 pwmN），供控制用。"""
    fans = []
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return fans
    idx = 0
    for hw in sorted(os.listdir(base)):
        hpath = os.path.join(base, hw)
        try:
            entries = os.listdir(hpath)
        except Exception:
            continue
        hname = read_text(os.path.join(hpath, "name")).strip() or hw
        fan_inputs = sorted([f for f in entries if f.startswith("fan") and f.endswith("_input")],
                            key=lambda x: int(re.sub(r"\D", "", x)))
        for f in fan_inputs:
            num = re.sub(r"\D", "", f)
            rpm = read_int_file(os.path.join(hpath, f))
            pwm = read_int_file(os.path.join(hpath, "pwm" + num))
            enable = read_int_file(os.path.join(hpath, "pwm" + num + "_enable"))
            fans.append({
                "idx": idx, "hwmon": hw, "chip": hname, "num": num,
                "name": hname + " 风扇 " + num, "rpm": rpm,
                "duty": pwm, "enable": enable,
                "path": os.path.join(hpath, "pwm" + num),
            })
            idx += 1
    return fans


def fan_set(idx, duty):
    """设置指定风扇通道占空比 0-255（idx 对应 _hwmon_fans 枚举序号）。"""
    fans = _hwmon_fans()
    if idx < 0 or idx >= len(fans):
        return {"ok": False, "error": "无效的风扇序号"}
    f = fans[idx]
    try:
        duty = max(0, min(255, int(duty)))
    except Exception:
        return {"ok": False, "error": "非法占空比"}
    try:
        with open(f["path"], "w") as fh:
            fh.write(str(duty))
        return {"ok": True, "idx": idx, "duty": duty, "name": f["name"],
                "note": "已设置 %s 占空比 %d" % (f["name"], duty)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def fan_enable_auto(idx):
    """恢复该风扇为自动温控（pwmN_enable=2）。"""
    fans = _hwmon_fans()
    if idx < 0 or idx >= len(fans):
        return {"ok": False, "error": "无效的风扇序号"}
    f = fans[idx]
    epath = os.path.join("/sys/class/hwmon", f["hwmon"], "pwm" + f["num"] + "_enable")
    try:
        with open(epath, "w") as fh:
            fh.write("2")
        return {"ok": True, "idx": idx, "note": "已恢复自动温控"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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
    # -n standby：跳过待机盘，避免每次采集唤醒休眠硬盘
    out, rc = run_cmd(["smartctl", "-n", "standby", "-i", "-H", "-A", "/dev/" + dev], timeout=8)
    if rc != 0 or not out.strip():
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
        elif k == "Device Model" or k == "Model Number":
            d["model2"] = v
        elif k == "Serial Number":
            d["serial"] = v
        elif k == "Rotation Rate":
            d["rpm"] = v
        elif k == "Temperature":
            try:
                d["temp_c"] = int(v.split()[0])
            except Exception:
                pass
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
    return d


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
                "tx_bytes": int(fields[8]),
                "tx_packets": int(fields[9]),
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
    """通过 smartctl 读取硬盘温度（sysfs 无 hwmon 时的兜底）。"""
    out, rc = run_cmd(["smartctl", "-A", "/dev/" + dev], timeout=8)
    if rc != 0 or not out:
        return None
    for line in out.splitlines():
        if "Temperature_Celsius" in line:
            parts = line.split()
            if len(parts) >= 8:
                try:
                    return int(parts[7])  # ATA raw 值
                except Exception:
                    pass
        if line.strip().startswith("Temperature:"):
            parts = line.split()
            try:
                return int(parts[1])  # NVMe 格式
            except Exception:
                pass
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
    try:
        for blk in sorted(os.listdir("/sys/block")):
            if not (blk.startswith("sd") or blk.startswith("nvme")
                    or blk.startswith("vd") or blk.startswith("mmcblk")):
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
                temps["disks"].append({"name": blk, "temp": temp})
    except Exception:
        pass
    return temps


def system_info(hostname_override=None):
    info = {}
    info["hostname"] = hostname_override or socket.gethostname()
    info["kernel"] = os.uname().release if hasattr(os, "uname") else ""
    info["arch"] = os.uname().machine if hasattr(os, "uname") else ""
    info["uptime"] = read_uptime()
    info["boot_time"] = time.time() - info["uptime"] if info["uptime"] else 0
    info["cpu_model"] = read_cpu_model()
    info["os_pretty"] = ""
    for line in read_text("/etc/os-release").splitlines():
        if line.startswith("PRETTY_NAME="):
            info["os_pretty"] = line.split("=", 1)[1].strip().strip('"')
            break
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
def _docker_uptime_zh(status):
    """把 docker Status（如 'Up 3 hours' / 'Exited (0) 5 minutes ago'）转成中文运行时长。"""
    if not status:
        return ""
    s = status.strip()
    low = s.lower()
    if low.startswith("up "):
        rest = s[3:].strip()
        # 处理 "About an hour" / "2 days" 等
        rest = rest.replace("about ", "约 ")
        n = re.search(r"(\d+)", rest)
        if not n:
            return "运行中"
        num = n.group(1)
        unit = rest[n.end():].strip().lower()
        unit_zh = {"seconds": "秒", "second": "秒", "minutes": "分钟", "minute": "分钟",
                   "hours": "小时", "hour": "小时", "days": "天", "day": "天", "weeks": "周", "week": "周",
                   "months": "个月", "month": "个月"}.get(unit, unit)
        return "运行 " + num + unit_zh
    if low.startswith("exited"):
        m = re.search(r"exited.*?(\d+) (minutes|hours|days|seconds)", low)
        if m:
            return "已停止 " + m.group(1) + m.group(2)
        return "已停止"
    if low.startswith("restarting"):
        return "重启中"
    if low.startswith("paused"):
        return "已暂停"
    if low.startswith("created"):
        return "已创建"
    return s


def collect_docker():
    result = {"available": False, "error": "", "containers": [], "images": 0}
    out, rc = run_cmd(["docker", "ps", "-a", "--format", "{{json .}}"], timeout=20)
    if rc != 0 or not out.strip():
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
        containers.append({
            "id": (c.get("ID") or "")[:12],
            "name": (c.get("Names") or "").lstrip("/"),
            "image": c.get("Image") or "",
            "state": c.get("State") or "",
            "status": c.get("Status") or "",
            "uptime": _docker_uptime_zh(c.get("Status") or ""),
            "ports": c.get("Ports") or "",
            "created": c.get("CreatedAt") or "",
            "cpu": "", "mem_usage": "", "mem_percent": "", "net": "",
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


def fetch_weather(city_override=None):
    """获取实时天气（免费无需 key）。city_override 支持：空=公网IP自动定位；
    "城市名"（如 徐州 / Beijing）= Open-Meteo 地理编码解析；"lat,lon"（如 34.26,117.18）= 直接指定坐标。
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
        override = (city_override or "").strip()
        if override:
            if "," in override:
                parts = [p.strip() for p in override.split(",")]
                try:
                    lat, lon = float(parts[0]), float(parts[1])
                    city = override
                except Exception:
                    return {"ok": False, "error": "坐标格式应为 纬度,经度（如 34.26,117.18）"}
            else:
                # 城市名 → Open-Meteo 地理编码
                try:
                    geo = _get("https://geocoding-api.open-meteo.com/v1/search?name=%s&count=1&language=zh"
                               % urllib.parse.quote(override), timeout=8)
                    rs = (geo or {}).get("results") or []
                    if rs:
                        lat, lon = float(rs[0]["latitude"]), float(rs[0]["longitude"])
                        city = rs[0].get("name") or override
                except Exception:
                    pass
                if lat is None:
                    return {"ok": False, "error": "未找到城市「%s」，可改用 纬度,经度 格式" % override}
        else:
            # 公网 IP 定位（ip-api.com 免费版，仅位置信息）
            try:
                loc = _get("http://ip-api.com/json/?lang=zh-CN&fields=lat,lon,city,regionName,country", timeout=6)
                if loc and loc.get("lat") is not None and loc.get("lon") is not None:
                    lat, lon = float(loc["lat"]), float(loc["lon"])
                    city = loc.get("city") or loc.get("regionName") or loc.get("country") or "未知地区"
            except Exception:
                pass
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
        return {"ok": True, "city": city or override or "未知地区",
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


def _app_center_ports():
    """扫描应用中心每个已安装应用的 service_port 与显示名。返回 {appid: {name, port}}。

    飞牛应用中心（含应用商店安装的第三方应用）实际安装在 /vol1/@appcenter/{appid}/，
    部分系统 FPK 应用在 /usr/local/apps/@appcenter，兼容多候选目录；端口字段兼容
    service_port / web_port / http_port / port。
    """
    m = {}
    roots = ("/vol1/@appcenter", "/usr/local/apps/@appcenter",
             "/usr/trim/apps", "/var/apps", "/var/lib/fnos/apps")
    for apps_dir in roots:
        if not os.path.isdir(apps_dir):
            continue
        try:
            names = sorted(os.listdir(apps_dir))
        except Exception:
            continue
        for name in names:
            if name.startswith("."):
                continue
            if name in m:
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
            m[name] = {"name": _real_display_name(os.path.join(apps_dir, name), name, dname), "port": port}
    return m


def _ss_process_ports():
    """ss -tlnp 解析进程名 -> 监听端口列表（用于功能模块端口标注）。"""
    pmap = {}
    out, rc = run_cmd(["ss", "-tlnp"], timeout=8)
    if rc != 0 or not out.strip():
        return pmap
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 5 or parts[0] != "LISTEN":
            continue
        addr = parts[3]
        proc = parts[5] if len(parts) >= 6 else ""
        m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', proc)
        if not m:
            continue
        pname = m.group(1).lower()
        pid = m.group(2)
        if ":" not in addr:
            continue
        port = addr.rsplit(":", 1)[1]
        try:
            port = int(port)
        except Exception:
            continue
        pmap.setdefault(pname, []).append({"port": port, "pid": pid})
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
        self._init()

    def _init(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS metrics (ts REAL NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_metric_ts ON metrics(metric, ts)")
                # 自愈：v2.8.0 前的历史写入曾把 (指标名, 时间戳) 两列互换（ts 列存了文本），
                # 这些脏行永远查不出来还会拖慢查询，启动时一次性清除
                conn.execute("DELETE FROM metrics WHERE typeof(ts) != 'real'")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            traceback.print_exc()

    def write(self, rows):
        if not rows:
            return
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
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
        return rows

    def export_net(self, seconds):
        """导出网卡上下行历史 (ts, metric, value)。seconds=0 表示全部。"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            try:
                if seconds:
                    cutoff = time.time() - seconds
                    rows = conn.execute(
                        "SELECT ts, metric, value FROM metrics WHERE ts>=? "
                        "AND (metric LIKE 'net_rx:%' OR metric LIKE 'net_tx:%') ORDER BY ts",
                        (cutoff,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT ts, metric, value FROM metrics "
                        "WHERE (metric LIKE 'net_rx:%' OR metric LIKE 'net_tx:%') ORDER BY ts",
                    ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return rows

    def export_rows(self, seconds):
        """导出全部历史记录 (ts, metric, value)；seconds=0 表示全部。"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=15)
            try:
                if seconds:
                    cutoff = time.time() - seconds
                    rows = conn.execute(
                        "SELECT ts, metric, value FROM metrics WHERE ts>=? ORDER BY ts",
                        (cutoff,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT ts, metric, value FROM metrics ORDER BY ts",
                    ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []
        return rows

    def cleanup(self, retention_days):
        cutoff = time.time() - retention_days * 86400
        conn = sqlite3.connect(self.db_path, timeout=15)
        try:
            conn.execute("DELETE FROM metrics WHERE ts<?", (cutoff,))
            conn.commit()
        except Exception:
            pass
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
        self._last_sensors = {"temps": [], "fans": [], "volts": [], "available": False}
        self._last_sensors_ts = 0.0
        self._last_fans = []
        self._last_fans_ts = 0.0
        self._last_power = {"ok": False}
        self._last_power_ts = 0.0
        self._last_gpu = []
        self._last_gpu_ts = 0.0
        self._last_memory = {"items": [], "dual": False}
        self._last_memory_ts = 0.0
        self._last_hist_ts = 0.0
        self._last_cleanup_ts = 0.0
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
        now = time.time()
        interval = self.config.get("interval", 10)

        snapshot = {}
        # ---- CPU ----
        total, cores = read_cpu_times()
        cpu_percent = calc_cpu_percent(self._prev_cpu, total)
        per_core = []
        if self._prev_cores and cores and len(cores) == len(self._prev_cores):
            per_core = [calc_cpu_percent(a, b) for a, b in zip(self._prev_cores, cores)]
        self._prev_cpu = total
        self._prev_cores = cores
        cores_num = len(cores) if cores else 0
        load = read_loadavg()
        snapshot["cpu"] = {
            "percent": cpu_percent, "per_core": per_core, "load": load,
            "cores": cores_num, "model": read_cpu_model(),
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
        # ---- 磁盘详情（物理硬盘，每 60 秒） ----
        if now - self._last_disks_detail_ts >= 60:
            self._last_disks_detail = collect_disks_detail()
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
        snapshot["temp"] = temps
        # ---- 实时功耗 RAPL（每 10 秒，内部 0.25s 差分） ----
        try:
            if now - self._last_power_ts >= 10:
                self._last_power = get_rapl_power()
                self._last_power_ts = now
        except Exception:
            self._last_power = {"ok": False}
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
            if temps.get("cpu"):
                rows.append((now, "temp", _r2(temps["cpu"])))
            if temps.get("system"):
                rows.append((now, "temp_mb", _r2(temps["system"])))
            # ---- RAPL 功耗（历史维度） ----
            if self._last_power.get("ok"):
                rows.append((now, "power", _r2(self._last_power.get("total", 0))))
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
            self.db.write(rows)
        # ---- 清理过期历史（每小时一次即可；原先每 10 秒一次 DELETE，大表时白耗 CPU/IO 并放大 WAL 写入） ----
        if now - self._last_cleanup_ts >= 3600:
            self._last_cleanup_ts = now
            try:
                self.db.cleanup(self.config.get("retention_days", 7))
            except Exception:
                pass

        # ---- Docker（每 30 秒；docker ps 为外部进程调用，容器列表变化慢，无需高频） ----
        if now - self._last_docker_ts >= 30:
            self._last_docker = collect_docker()
            self._last_docker_ts = now
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
        if now - self._last_apps_ts >= 600:
            self._last_apps = collect_app_stats()
            self._last_apps_ts = now
        snapshot["apps"] = self._last_apps

        # ---- 在线更新自动检查（每 6 小时）----
        # update_autoupdate（自动更新）：检查到新版本后自动下载并安装（解包覆盖 + 重启服务）
        # update_autocheck/update_autodownload（旧版兼容）：仅检查 / 仅下载到数据目录
        if self.updater and (self.config.get("update_autoupdate") or self.config.get("update_autocheck")):
            if now - self.updater._last_check >= UPDATE_CHECK_INTERVAL:
                try:
                    uinfo = self.updater.check(force=True)
                    if uinfo.get("has_update") and uinfo.get("asset"):
                        if self.config.get("update_autoupdate"):
                            ok, fpk = self.updater.download_to_nas(uinfo["asset"])
                            if ok:
                                self.updater.install_fpk(fpk)  # 安装并自动重启
                        elif self.config.get("update_autodownload") and not self.updater.downloaded_path():
                            self.updater.download_to_nas(uinfo["asset"])
                except Exception:
                    pass

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


def _gh_open(url, timeout=30):
    """打开 GitHub 下载地址：直连失败后自动尝试加速镜像（返回 response，全失败抛最后异常）。"""
    last = None
    for m in GH_MIRRORS:
        u = (m + url) if m else url
        try:
            return urllib.request.urlopen(
                urllib.request.Request(u, headers={"User-Agent": "fnmonitor"}), timeout=timeout)
        except Exception as e:
            last = e
    raise last


class UpdateManager:
    """基于 GitHub Releases 的在线更新：检查新版本、把安装包下载到 NAS（代理下载，
    解决浏览器直连 GitHub 慢的问题）。自动检查由采集线程按周期调用。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self._lock = threading.Lock()
        self._last_check = 0.0
        self._cache = None            # 最近一次 check() 完整结果（30 分钟缓存）
        self._status = {"last_check": "", "latest": "", "has_update": False,
                        "downloading": False, "downloaded_file": "", "download_dir": "",
                        "error": ""}

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
                rel = json.loads(r.read().decode("utf-8", "ignore"))
            latest = str(rel.get("tag_name") or "").lstrip("vV")
            info["ok"] = True
            info["latest"] = latest
            info["has_update"] = _ver_tuple(latest) > _ver_tuple(VERSION)
            info["notes"] = str(rel.get("body") or "")[:3000]
            info["published_at"] = str(rel.get("published_at") or "")
            if rel.get("html_url"):
                info["html_url"] = rel["html_url"]
            # 选当前架构的 fpk 资产（x86 优先精确匹配，arm 同理）；记录官方 digest 供下载后校验
            def _mk_asset(a):
                return {"name": a.get("name"), "size": int(a.get("size") or 0),
                        "download_url": a.get("browser_download_url"),
                        "digest": str(a.get("digest") or "").replace("sha256:", "")}
            want = "fnmonitor-%s-%s.fpk" % (latest, arch)
            for a in rel.get("assets") or []:
                if str(a.get("name")) == want:
                    info["asset"] = _mk_asset(a)
                    break
            if info["asset"] is None:  # 兜底：任一同名平台包
                for a in rel.get("assets") or []:
                    if str(a.get("name", "")).endswith("-%s.fpk" % arch):
                        info["asset"] = _mk_asset(a)
                        break
        except Exception as e:
            info["error"] = str(e)
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

    # ---- 下载到 NAS ----
    def download_to_nas(self, asset, dest_dir=None):
        """把安装包下载到指定目录（默认 数据目录/update/），自动尝试镜像加速，
        下载后按官方 SHA256 校验。返回 (成功?, 文件路径/错误)。"""
        name = asset.get("name") or "fnmonitor.fpk"
        url = asset.get("download_url")
        if not url:
            return False, "资产缺少下载地址"
        ddir = dest_dir or self.update_dir()
        if not os.path.isabs(ddir):
            return False, "请填写以 / 开头的绝对路径（如 /vol1/更新包）"
        final = os.path.join(ddir, name)
        tmp = final + ".tmp"
        try:
            os.makedirs(ddir, exist_ok=True)
            with self._lock:
                self._status["downloading"] = True
            with _gh_open(url, timeout=60) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
            # 完整性校验：GitHub API 提供的官方 sha256（镜像下载也不怕被篡改）
            expect = str(asset.get("digest") or "").lower()
            if expect:
                h = hashlib.sha256()
                with open(tmp, "rb") as f:
                    for chunk in iter(lambda: f.read(65536), b""):
                        h.update(chunk)
                if h.hexdigest() != expect:
                    os.remove(tmp)
                    return False, "安装包 SHA256 校验失败（下载不完整或被篡改），请重试"
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
        finally:
            with self._lock:
                self._status["downloading"] = False

    # ---- 自动安装（解包 fpk 覆盖应用目录后自我重启） ----
    def install_fpk(self, fpk_path):
        """解包 fpk（app.tgz + cmd + manifest）并覆盖应用目录（先备份，校验失败自动回滚），
        成功后延迟 1.5 秒替换当前进程重启服务。返回 (成功?, 消息)。"""
        import shutil
        import sys
        import tarfile
        app_dir = os.path.dirname(os.path.abspath(__file__))       # <应用根>/app
        root = os.path.dirname(app_dir)                            # 应用根目录
        tmp = os.path.join(self.data_dir, "update", "_extract")
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp, exist_ok=True)
        try:
            # fpk 结构：app.tgz（app 内容压缩包）+ cmd/ + manifest，均为平铺
            with tarfile.open(fpk_path, "r:*") as t:
                t.extractall(tmp)
            inner = os.path.join(tmp, "app.tgz")
            if os.path.exists(inner):
                app_src = os.path.join(tmp, "app")
                os.makedirs(app_src, exist_ok=True)
                with tarfile.open(inner, "r:*") as t2:
                    t2.extractall(app_src)                         # server.py、ui/ 等平铺在内
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
                d = os.path.join(root, item)
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

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            # HTTP/1.1 长连接：浏览器复用 TCP 连接，避免每请求新建线程（原 HTTP/1.0
            # 每个请求都新建/销毁线程，多客户端高频轮询时线程与 glibc malloc arena
            # 反复增长，是进程 RSS 虚高的重要原因）；timeout 保证空闲连接最终回收
            protocol_version = "HTTP/1.1"
            timeout = 65

            def do_GET(self):
                try:
                    self._handle()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    traceback.print_exc()
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

            def _handle(self):
                parsed = urlparse(self.path)
                path = parsed.path
                qs = parse_qs(parsed.query)

                if path in ("/", "/index.html"):
                    self._serve_file(os.path.join(app.www_dir, "index.html"), "text/html; charset=utf-8")
                    return
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.end_headers()
                    return
                if path == "/api/overview":
                    self._send_bytes(app.api_overview())
                    return
                if path == "/api/docker":
                    self._json(app.api_docker())
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
                if path == "/api/gpu":
                    self._json(app.api_gpu())
                    return
                if path == "/api/memory":
                    self._json(app.api_memory())
                    return
                if path == "/api/weather":
                    self._json(app.api_weather())
                    return
                if path == "/api/update/check":
                    force = (qs.get("force") or ["0"])[0] in ("1", "true", "yes")
                    self._json(app.api_update_check(force=force))
                    return
                if path == "/api/update/install":
                    self._json(app.api_update_install())
                    return
                if path == "/api/update/download":
                    to = (qs.get("to") or ["nas"])[0]
                    if to == "browser":
                        # NAS 端代理下载到浏览器：用户电脑无需直连 GitHub（自动尝试加速镜像）
                        asset, err = app.api_update_asset()
                        if err:
                            self._json({"ok": False, "error": err})
                            return
                        try:
                            with _gh_open(asset["download_url"], timeout=60) as r:
                                self.send_response(200)
                                self.send_header("Content-Type", "application/octet-stream")
                                self.send_header("Content-Length", str(asset.get("size") or r.headers.get("Content-Length") or 0))
                                self.send_header("Content-Disposition",
                                                 'attachment; filename="%s"' % asset["name"].replace('"', ""))
                                self.end_headers()
                                while True:
                                    chunk = r.read(65536)
                                    if not chunk:
                                        break
                                    self.wfile.write(chunk)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        except Exception as e:
                            traceback.print_exc()
                            try:
                                self._json({"ok": False, "error": str(e)})
                            except Exception:
                                pass
                        return
                    dest = (qs.get("path") or [""])[0].strip()
                    self._json(app.api_update_download_nas(dest_dir=dest or None))
                    return
                if path == "/api/report":
                    fmt = (qs.get("format") or ["json"])[0]
                    if fmt == "html":
                        body = app.api_report_html()
                        self._download(body, "text/html; charset=utf-8",
                                       "fnmonitor_report_%s.html" % time.strftime("%Y%m%d_%H%M%S"))
                    else:
                        self._json(app.api_report())
                    return
                if path == "/api/export":
                    export_type = (qs.get("type") or ["history"])[0]
                    if export_type == "status":
                        body = json.dumps(app.api_export_status(), ensure_ascii=False, indent=2).encode("utf-8")
                        self._download(body, "application/json; charset=utf-8",
                                       "fnmonitor_status_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
                    else:
                        range_ = (qs.get("range") or ["7d"])[0]
                        body = app.api_export_history_csv(range_)
                        self._download(body, "text/csv; charset=utf-8",
                                       "fnmonitor_history_%s.csv" % time.strftime("%Y%m%d_%H%M%S"))
                    return
                self.send_error(404, "Not Found")

            def do_POST(self):
                try:
                    self._handle_post()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception:
                    traceback.print_exc()
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

            def _handle_post(self):
                parsed = urlparse(self.path)
                path = parsed.path

                def _read_json():
                    try:
                        length = int(self.headers.get("Content-Length") or 0)
                        body = self.rfile.read(length) if length else b""
                        return json.loads(body.decode("utf-8", "ignore") or "{}")
                    except Exception:
                        return {}

                if path == "/api/docker/action":
                    data = _read_json()
                    cid = str(data.get("id") or "").strip()
                    act = str(data.get("action") or "").strip()
                    self._json(app.api_docker_action(cid, act))
                    return
                if path == "/api/ui":
                    data = _read_json()
                    self._json(app.api_ui_set(data))
                    return
                if path == "/api/config/save":
                    data = _read_json()
                    self._json(app.api_config_save(data))
                    return
                if path == "/api/fan":
                    data = _read_json()
                    self._json(app.api_fan_set(data))
                    return
                self.send_error(404, "Not Found")

            def _json(self, obj):
                self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"))

            def _send_bytes(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _download(self, body, ctype, filename):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Disposition", 'attachment; filename="%s"' % filename)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _serve_file(self, path, ctype):
                try:
                    with open(path, "rb") as f:
                        body = f.read()
                except Exception:
                    self.send_error(404, "Not Found")
                    return
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt, *args):
                pass  # 静默访问日志

        return Handler

    # ---- API 实现 ----
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
        snap["config"] = dict(self.config)
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

    def api_history(self, metric, range_):
        """趋势查询（结果缓存 25 秒）。

        历史数据每 history_interval(默认 60) 秒才新增一个点，前端却每 30 秒拉 9 个
        指标、多客户端时请求成倍放大；「全部」范围单次查询最多 43200 行，缓存后
        同周期内只查一次库。缓存项上限 24 个，超出整体清空（组合数有限，足够用）。"""
        key = (metric, range_)
        now = time.time()
        cached = self._hist_cache.get(key)
        if cached and now - cached[0] < 25:
            return cached[1]
        if len(self._hist_cache) > 24:
            self._hist_cache.clear()
        data = self._api_history_impl(metric, range_)
        self._hist_cache[key] = (now, data)
        return data

    def _api_history_impl(self, metric, range_):
        seconds = {"10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}.get(range_, 3600)
        if metric == "net":
            return self.api_net_history(range_)
        if metric == "diskio":
            return self.api_diskio_history(range_)
        rows = self.collector.db.query(metric, seconds)
        # 实时兜底：历史为空或最新点过旧时，附加当前实时值，保证趋势图始终有数据
        now = time.time()
        latest_ts = rows[-1][0] if rows else 0
        if now - latest_ts > 60:
            val = self._live_value(metric, self.collector.get_snapshot())
            if val is not None:
                rows = list(rows) + [(now, val)]
        # 降采样：最多返回 ~720 个点
        step = max(1, len(rows) // 720)
        sampled = rows[::step]
        return {"metric": metric, "range": range_, "points": [[t, v] for t, v in sampled]}

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
            if metric == "fan_avg":
                fans = snap.get("sensors", {}).get("fans", [])
                rpms = [f.get("rpm") for f in fans if f.get("rpm")]
                return round(sum(rpms) / len(rpms), 1) if rpms else None
        except Exception:
            pass
        return None

    def api_diskio_history(self, range_):
        """磁盘 IO 历史：聚合全部磁盘读/写速率（MB/s），返回 {r:[], w:[]}。"""
        seconds = {"10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}.get(range_, 3600)
        cutoff = (time.time() - seconds) if seconds else 0
        r_map, w_map = {}, {}
        try:
            conn = sqlite3.connect(self.collector.db.db_path, timeout=15)
            try:
                rows = conn.execute(
                    "SELECT ts, metric, value FROM metrics WHERE ts>=? "
                    "AND (metric LIKE 'diskio_r:%' OR metric LIKE 'diskio_w:%') ORDER BY ts",
                    (cutoff,),
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            rows = []
        for ts, metric, value in rows:
            bucket = r_map if metric.startswith("diskio_r:") else w_map
            if ts not in bucket:
                bucket[ts] = 0.0
            bucket[ts] += value
        r = sorted([(t, v) for t, v in r_map.items()])
        w = sorted([(t, v) for t, v in w_map.items()])
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
        # 最近写入时间
        try:
            conn = sqlite3.connect(db.db_path, timeout=5)
            try:
                cur = conn.execute("SELECT MAX(ts) FROM metrics")
                row = cur.fetchone()
                info["last_ts"] = row[0] if row and row[0] else None
                cur = conn.execute("SELECT COUNT(*) FROM metrics")
                row = cur.fetchone()
                info["rows"] = row[0] if row else 0
                cur = conn.execute("SELECT COUNT(DISTINCT metric) FROM metrics")
                row = cur.fetchone()
                info["metrics"] = row[0] if row else 0
            finally:
                conn.close()
        except Exception as e:
            info["db_error"] = str(e)
        info["snapshot_ts"] = self.collector.get_snapshot().get("ts")
        return info

    def api_net_history(self, range_):
        """聚合所有物理网卡的上下行速率历史（排除虚拟网卡），返回 {rx:[...], tx:[...]}。"""
        seconds = {"10m": 600, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}.get(range_, 3600)
        rows = self.collector.db.export_net(seconds)
        virt = ("docker", "veth", "br-", "virbr", "tun", "tap", "vnet", "lxc", "kube", "lo", "wg")
        rx = {}
        tx = {}
        for ts, metric, value in rows:
            iface = metric.split(":", 1)[1] if ":" in metric else ""
            if iface.startswith(virt):
                continue
            if metric.startswith("net_rx:"):
                rx[ts] = rx.get(ts, 0) + value
            elif metric.startswith("net_tx:"):
                tx[ts] = tx.get(ts, 0) + value
        def sample(d):
            order = sorted(d)
            step = max(1, len(order) // 720)
            return [[t, round(d[t], 1)] for t in order[::step]]
        return {"range": range_, "rx": sample(rx), "tx": sample(tx)}

    def api_system(self):
        info = system_info()
        info["version"] = VERSION
        info["db_size"] = self.collector.db.metrics_size()
        info["data_dir"] = self.data_dir
        return info

    def api_config(self):
        c = dict(self.config)
        c["data_dir_actual"] = self.data_dir
        c["ui_file"] = os.path.join(self.data_dir, "ui.json")  # 界面同步设置文件实际位置
        c["version"] = VERSION
        c["arch"] = self.updater.detect_arch()
        c["update_status"] = self.updater.status()
        return {"config": c}

    def api_update_check(self, force=False):
        return self.updater.check(force=force)

    def api_update_download_nas(self, dest_dir=None):
        """下载当前线上最新版安装包到 NAS 指定目录（不比较版本，始终可下载）。"""
        info = self.updater.check()  # 走缓存，已有结果不重复请求
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "检查更新失败"}
        asset = info.get("asset")
        if not asset:
            return {"ok": False, "error": "Release 中未找到 %s 平台安装包" % info.get("arch")}
        ok, path = self.updater.download_to_nas(asset, dest_dir=dest_dir)
        if ok:
            note = "已保存到 NAS，可在飞牛文件管理中查看；也可在应用中心「手动安装」选择该文件完成升级"
            return {"ok": True, "file": os.path.basename(path), "path": path,
                    "latest": info.get("latest"), "note": note}
        return {"ok": False, "error": path}

    def api_update_install(self):
        """在线更新：下载最新版安装包（缓存复用）→ 校验 → 覆盖安装 → 自动重启服务。"""
        info = self.updater.check()
        if not info.get("ok"):
            return {"ok": False, "error": info.get("error") or "检查更新失败"}
        asset = info.get("asset")
        if not asset:
            return {"ok": False, "error": "Release 中未找到 %s 平台安装包" % info.get("arch")}
        ok, fpk = self.updater.download_to_nas(asset)
        if not ok:
            return {"ok": False, "error": "下载失败: %s" % fpk}
        ok, msg = self.updater.install_fpk(fpk)
        if ok:
            return {"ok": True, "note": msg, "version": info.get("latest")}
        return {"ok": False, "error": msg}

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
        if refresh:
            # 用户手动点击"更新"：绕过缓存，立即重新扫描内置应用并同步到最新快照
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
        try:
            with open(self._ui_config_path(), "r", errors="ignore") as f:
                return {"ui": json.load(f)}
        except Exception:
            return {"ui": {}}

    def api_ui_set(self, data):
        """保存前端 UI 配置（主题/面板顺序/隐藏/模块隐藏）到本地文件，跨设备不重置。"""
        clean = {}
        # 注意：layoutVersion / tab 必须一并持久化，否则前端 loadUI 会因
        # 服务器端 layoutVersion 恒为 0 < 本地版本而每次刷新重置布局
        for k in ("theme", "layoutVersion", "panelOrder", "hiddenPanels", "hiddenMods", "range", "tab", "sideCollapsed", "fontScale", "cloudBg"):
            if k in data:
                clean[k] = data[k]
        try:
            with open(self._ui_config_path(), "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=1)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def api_config_save(self, data):
        """保存配置（采集间隔/保留天数/服务端口/数据目录），端口修改需重启应用后生效。"""
        path = os.path.join(self._cfg_dir, "config.json")
        try:
            with open(path, "r", errors="ignore") as f:
                cur = json.load(f)
        except Exception:
            cur = {}
        changed_port = False
        for k in ("interval", "retention_days", "port", "history_interval"):
            if k in data:
                try:
                    v = int(data[k])
                    if k == "interval":
                        v = max(2, min(v, 3600))
                    elif k == "retention_days":
                        v = max(1, min(v, 365))
                    elif k == "port":
                        v = 0 if v <= 0 else v
                        if cur.get("port", 0) != v:
                            changed_port = True
                    elif k == "history_interval":
                        v = max(60, min(v, 86400))
                    cur[k] = v
                except Exception:
                    pass
        if "weather_city" in data:
            cur["weather_city"] = str(data["weather_city"])[:200]
            self._weather_cache = None  # 位置变化后清缓存
        for k in ("update_autocheck", "update_autodownload", "update_autoupdate"):
            if k in data:
                cur[k] = 1 if str(data[k]) in ("1", "true", "on") else 0
        if "data_dir" in data:
            nd = str(data["data_dir"]).strip()
            if nd and os.path.isabs(nd):
                cur["data_dir"] = nd
            else:
                cur.pop("data_dir", None)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cur, f, ensure_ascii=False, indent=1)
            if "interval" in cur:
                self.config["interval"] = max(2, int(cur["interval"]))
            if "retention_days" in cur:
                self.config["retention_days"] = max(1, int(cur["retention_days"]))
            if "history_interval" in cur:
                self.config["history_interval"] = max(60, int(cur["history_interval"]))
            if "weather_city" in cur:
                self.config["weather_city"] = str(cur["weather_city"])
            for k in ("update_autocheck", "update_autodownload", "update_autoupdate"):
                if k in cur:
                    self.config[k] = 1 if str(cur[k]) in ("1", "true", "on") else 0
            if "data_dir" in cur:
                self.config["data_dir"] = str(cur["data_dir"]).strip()
            self.collector._cfg_mtime = 0.0  # 强制采集线程下次重载
            return {"ok": True, "config": dict(self.config), "port_changed": changed_port,
                    "note": "端口修改需在应用中心重启「飞牛监控」后生效" if changed_port else ""}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def api_sensors(self):
        return self.collector.get_snapshot().get("sensors", {"temps": [], "fans": [], "volts": [], "available": False})

    def api_fans(self):
        return {"fans": self.collector.get_snapshot().get("fans", [])}

    def api_raidcard(self):
        return self.collector.get_snapshot().get("raidcard", {"type": "none", "label": "", "detail": ""})

    def api_power(self):
        """实时功耗（RAPL）。"""
        return self.collector.get_snapshot().get("power", {"ok": False})

    def api_gpu(self):
        """GPU 实时监控。"""
        return {"gpu": self.collector.get_snapshot().get("gpu", [])}

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
            rows = self.collector.db.export_rows(seconds)
        except Exception:
            traceback.print_exc()
            rows = []
        # 各指标最近值 / 最大 / 平均
        agg = {}
        for row in rows:
            try:
                ts, metric, value = row
                metric = str(metric)
                value = float(value)
                if not math.isfinite(value):
                    continue
            except Exception:
                continue
            a = agg.setdefault(metric, {"last": value, "max": value, "sum": 0.0, "n": 0})
            a["last"] = value
            a["max"] = max(a["max"], value)
            a["sum"] += value
            a["n"] += 1
        summary = {}
        for metric, a in agg.items():
            summary[metric] = {
                "last": round(a["last"], 2),
                "max": round(a["max"], 2),
                "avg": round(a["sum"] / a["n"], 2) if a["n"] else None,
                "samples": a["n"],
            }
        # 阈值判定
        alerts = []
        if "temp" in summary and summary["temp"]["max"] and summary["temp"]["max"] >= 80:
            alerts.append({"level": "warn", "item": "CPU 温度", "value": summary["temp"]["max"], "threshold": "≥80°C"})
        if "mem" in summary and summary["mem"]["max"] and summary["mem"]["max"] >= 90:
            alerts.append({"level": "warn", "item": "内存占用", "value": summary["mem"]["max"], "threshold": "≥90%"})
        if "disk_total" in summary and summary["disk_total"]["max"] and summary["disk_total"]["max"] >= 90:
            alerts.append({"level": "warn", "item": "磁盘占用", "value": summary["disk_total"]["max"], "threshold": "≥90%"})
        return {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": VERSION,
            "summary": summary,
            "alerts": alerts,
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
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px 12px;border-bottom:1px solid #e2e8f0}
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

    def api_fan_set(self, data):
        """风扇控制：设置占空比或恢复自动温控。{idx, duty} 或 {idx, auto:true}。"""
        try:
            idx = int(data.get("idx", -1))
        except Exception:
            return {"ok": False, "error": "缺少风扇序号"}
        if data.get("auto"):
            return fan_enable_auto(idx)
        duty = data.get("duty")
        if duty is None:
            return {"ok": False, "error": "缺少占空比"}
        return fan_set(idx, duty)

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

    def api_export_history_csv(self, range_):
        """历史指标导出为 CSV 宽表（每行一个时间点，各指标一列）。"""
        try:
            return self._export_history_csv_impl(range_)
        except Exception:
            traceback.print_exc()
            # 兜底：导出失败时返回带说明的 CSV，避免 500 空错误页
            b2 = io.StringIO()
            w2 = csv.writer(b2)
            w2.writerow(["导出失败", "生成 CSV 时发生错误，请查看服务日志（journalctl / 应用日志）中的异常堆栈"])
            return ("\ufeff" + b2.getvalue()).encode("utf-8")

    def _export_history_csv_impl(self, range_):
        seconds = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "all": 0}.get(range_, 604800)
        rows = self.collector.db.export_rows(seconds)
        by_ts = {}
        order = []
        metric_set = []
        for row in rows:
            try:
                ts, metric, value = row
                ts = float(ts)
                value = float(value)
                metric = str(metric)
                if not math.isfinite(value):
                    continue
            except Exception:
                continue
            if ts not in by_ts:
                by_ts[ts] = {}
                order.append(ts)
            by_ts[ts][metric] = value
            if metric not in metric_set:
                metric_set.append(metric)
        # 历史库无数据时返回带说明的 CSV，明确原因而非空表头
        if not order:
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["说明", "历史库暂无采样数据"])
            w.writerow(["提示", "历史数据每 60 秒写入一条，请等待后台采集后再导出；若持续为空，可访问 /api/history/diag 查看历史库状态"])
            return ("\ufeff" + buf.getvalue()).encode("utf-8")
        # 固定核心指标靠前，其余（分区/网卡等）按出现顺序追加
        fixed = ["cpu", "mem", "load1", "disk_total", "temp"]
        cols = [m for m in fixed if m in metric_set] + [m for m in metric_set if m not in fixed]
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["时间(本地)", "unix_ts"] + cols)
        for ts in order:
            row = by_ts[ts]
            try:
                iso = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            except Exception:
                iso = str(ts)
            w.writerow([iso, "%.3f" % ts] + [row.get(c, "") for c in cols])
        return ("\ufeff" + buf.getvalue()).encode("utf-8")  # 加 BOM，Excel/WPS 直接打开不乱码

    def api_export_status(self):
        """当前状态全量快照导出为 JSON。"""
        return {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": VERSION,
            "overview": self.api_overview(),
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
            "rss": rss_kb * 1024, "comm": comm, "args": args[:160],
        })
        if len(result) >= limit:
            break
    return result


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def load_config(data_dir):
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = os.path.join(data_dir, "config.json")
    try:
        with open(cfg_path, "r", errors="ignore") as f:
            user = json.load(f)
        for k in ("interval", "retention_days", "port", "history_interval"):
            if k in user:
                cfg[k] = int(user[k])
        if "weather_city" in user:
            cfg["weather_city"] = str(user["weather_city"])
        if "data_dir" in user:
            cfg["data_dir"] = str(user["data_dir"]).strip()
    except Exception:
        pass
    return cfg


class DualStackHTTPServer(ThreadingHTTPServer):
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


def main():
    ap = argparse.ArgumentParser(description="fnMonitor - fnOS 系统监控后端")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--data-dir", default=os.environ.get("TRIM_PKGVAR", "/tmp/fnmonitor-data"))
    args = ap.parse_args()

    os.makedirs(args.data_dir, exist_ok=True)
    original_dir = args.data_dir
    config = load_config(args.data_dir)
    # 数据保存目录：默认 = 应用数据目录/localdata 子文件夹；用户可在设置页自定义（data_dir 配置覆盖）
    custom_dir = str(config.get("data_dir") or "").strip()
    if not (custom_dir and os.path.isabs(custom_dir)):
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
    # 配置文件里指定了端口则优先（网页设置修改端口后重启生效）
    port = int(config.get("port") or 0) or args.port

    app = MonitorApp(args.data_dir, config, args.host, port, cfg_dir=original_dir)
    handler = app.make_handler()
    # 双栈监听：默认 0.0.0.0 时同时监听 IPv4 + IPv6（IPv6 地址 / IPv6 公网域名+端口可直连）
    bind_host = args.host
    if bind_host in ("0.0.0.0", "", None):
        try:
            httpd = DualStackHTTPServer(("::", port), handler)
            listen_hint = ":: (IPv4+IPv6)"
        except Exception:
            traceback.print_exc()
            httpd = ThreadingHTTPServer(("0.0.0.0", port), handler)
            listen_hint = "0.0.0.0 (IPv4)"
    else:
        httpd = ThreadingHTTPServer((bind_host, port), handler)
        listen_hint = bind_host
    app.collector.start()

    def shutdown(sig, frame):
        app.collector.stop()
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    print("fnMonitor %s listening on http://[%s]:%d" % (VERSION, listen_hint, port))
    print("data-dir: %s" % args.data_dir)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.collector.stop()


if __name__ == "__main__":
    main()
