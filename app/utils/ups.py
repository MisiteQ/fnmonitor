"""UPS 电源采集模块（NUT upsc 客户端）。

Phase 1 范围：只读采集 + 快照 + 基础通信告警；
Phase 2 在此基础上再加完整告警事件 + 面板。

只依赖系统自带的 NUT 客户端 upsc（飞牛「UPS 电源管理」启用后即有）。
不读 USB/HID，不写任何硬件，纯只读。
"""
import os, re, time, json, threading, subprocess, shutil
from urllib.parse import parse_qs, unquote

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
UPS_FILE = "ups.json"
UPS_STATE_FILE = "ups_state.json"

# ups.status 单词 → 位，顺序即展示顺序
UPS_STATUS_BITS = [
    ("OL", "在线", 1 << 0), ("OB", "电池供电", 1 << 1), ("LB", "低电量", 1 << 2),
    ("RB", "需换电池", 1 << 3), ("CHRG", "充电中", 1 << 4), ("DISCHRG", "放电中", 1 << 5),
    ("ALARM", "告警", 1 << 6), ("BYPASS", "旁路", 1 << 7), ("CAL", "校准中", 1 << 8),
    ("OFF", "已关闭", 1 << 9), ("OVER", "超载", 1 << 10), ("TRIM", "降压", 1 << 11),
    ("BOOST", "升压", 1 << 12), ("FSD", "强制关机", 1 << 13),
]
UPS_BIT_OB = 1 << 1  # 电池供电
UPS_BIT_LB = 1 << 2  # 低电量
UPS_BIT_RB = 1 << 3  # 需换电池
UPS_BIT_OVER = 1 << 10  # 超载

# 非标准状态词 → NUT 标准词（部分国产 UPS / 兼容驱动会吐这些写法）
UPS_STATUS_ALIAS = {
    "ONLINE": "OL", "NORMAL": "OL", "MAINS": "OL", "AC": "OL",
    "ONBATT": "OB", "ONBATTERY": "OB", "BATT": "OB", "BATTERY": "OB",
    "LOWBATT": "LB", "LOW_BATTERY": "LB",
    "CHARGING": "CHRG", "DISCHARGING": "DISCHRG",
    "OVERLOAD": "OVER",
    "REPLACEBATT": "RB", "REPLACEBATTERY": "RB", "BATTREPLACE": "RB",
    "ONBYPASS": "BYPASS",
}

# NUT 配置目录（发行版 / 源码编译两种常见布局）
NUT_CONF_DIRS = ("/etc/nut", "/usr/local/ups/etc", "/usr/local/etc/nut")
NUT_DEFAULT_PORT = 3493

# 常见 UPS 品牌识别表：(关键词, 中文品牌, 常见接入方式)
# 关键词小写；英文按单词边界匹配，中文按子串匹配
UPS_VENDOR_TABLE = [
    (("wallecube", "瓦力方程", "瓦力"), "瓦力方程 WalleCube", "usbhid-ups（APC HID 兼容）"),
    (("american power conversion", "apc", "schneider electric", "施耐德"), "APC / 施耐德", "usbhid-ups"),
    (("eaton", "mge", "powerware", "伊顿"), "伊顿 Eaton", "usbhid-ups"),
    (("cyberpower", "cps"), "CyberPower", "usbhid-ups"),
    (("santak", "山特", "tg-box"), "山特 SANTAK", "usbhid-ups / nutdrv_qx"),
    (("kstar", "科士达"), "科士达 KSTAR", "usbhid-ups / nutdrv_qx"),
    (("reddis", "雷迪司"), "雷迪司 REDDIS", "nutdrv_qx"),
    (("tripplite", "tripp lite"), "Tripp Lite", "usbhid-ups"),
    (("vertiv", "liebert", "emerson", "维谛", "艾默生"), "维谛 Vertiv", "usbhid-ups / snmp-ups"),
    (("delta", "台达"), "台达 Delta", "usbhid-ups / nutdrv_qx"),
    (("east", "易事特"), "易事特 EAST", "nutdrv_qx"),
    (("kelong", "科华"), "科华 KELONG", "nutdrv_qx"),
    (("huawei", "华为"), "华为 Huawei", "snmp-ups"),
    (("chint", "正泰"), "正泰 CHINT", "nutdrv_qx"),
    (("delixi", "德力西"), "德力西 DELIXI", "nutdrv_qx"),
    (("aoposen", "奥普森", "opti"), "奥普森 OPTI", "nutdrv_qx"),
    (("legrand", "罗格朗"), "罗格朗 Legrand", "usbhid-ups"),
    (("ugreen", "绿联"), "绿联 UGREEN", "usbhid-ups"),
    (("zspace", "极空间"), "极空间 ZSpace", "usbhid-ups"),
]

# 已知 UPS 相关 USB VID（设备字符串缺失时也能认个大概）
USB_VENDOR_IDS = {
    "051d": "APC / 施耐德",
    "0463": "伊顿 Eaton / MGE",
    "0764": "CyberPower",
    "09ae": "Tripp Lite",
    "0665": "Cypress 通用（山特 / 科士达 / 雷迪司等 Megatec 兼容）",
    "10c4": "Silicon Labs 串口桥（Megatec 兼容 UPS）",
    "04b4": "Cypress 串口桥（Megatec 兼容 UPS）",
}

# Phase 1 基础事件（Phase 2 可扩展）
UPS_EVENT_META = {
    "DEVICE_LOST": ("UPS 通信中断", "crit"),
    "DEVICE_BACK": ("UPS 恢复通信", "ok"),
    "POWER_LOSS": ("市电中断", "crit"),
    "POWER_RESTORE": ("市电恢复", "ok"),
    "LOW_BATTERY": ("电池电量过低", "crit"),
    "REPLACE_BATTERY": ("需要更换电池", "warn"),
    "OVERLOAD": ("输出过载", "crit"),
    "ALARM": ("UPS 告警", "warn"),
    "FORCED_SHUTDOWN": ("UPS 强制关机", "crit"),
}
UPS_EVENT_CHOICES = [
    "DEVICE_LOST", "POWER_LOSS", "POWER_RESTORE",
    "LOW_BATTERY", "REPLACE_BATTERY", "OVERLOAD",
    "ALARM", "FORCED_SHUTDOWN",
]

UPS_DEFAULT = {
    "enabled": 0,
    "ups_name": "",
    "nut_host": "",
    "nut_port": 0,
    "poll_interval": 5,
    "notify_enabled": 1,
    "notify_events": list(UPS_EVENT_CHOICES),
    "notify_debounce_s": 60,
    "notify_max_per_min": 8,
}

# ---------------------------------------------------------------------------
# 模块级全局（线程安全）
# ---------------------------------------------------------------------------
_UPS_LOCK = threading.Lock()
_UPS_POLLER = None
_UPS_POLLER_LOCK = threading.Lock()
_UPS_WAKE = threading.Event()
_UPS_LATEST = {}
_UPS_CFG = {}


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------
def _onoff(x):
    return 1 if str(x).lower() in ("1", "true", "on", "yes") else 0


def _fnum(v):
    """严格数值转换：读不到返回 None，绝不返回 0。"""
    if v is None:
        return None
    s = str(v).strip()
    if not s or not re.match(r"^[-+]?\d+(?:\.\d+)?$", s):
        return None
    try:
        return float(s)
    except Exception:
        return None


def _clean_text(s, maxlen=64):
    s = str(s or "").strip()
    if len(s) > maxlen:
        s = s[:maxlen]
    return s


def _clean_ident(s, maxlen=64):
    """型号/序列号：去掉引号与 N/A 之类的占位值。"""
    s = str(s or "").strip().strip("'\"")
    if s.lower() in ("n/a", "na", "unknown", "none", "null", "0", "-"):
        return ""
    return _clean_text(s, maxlen)


def _vendor_match(text):
    """按厂商/型号/驱动名识别品牌 → {brand, access} 或 None。"""
    low = str(text or "").lower()
    if not low:
        return None
    for kws, brand, access in UPS_VENDOR_TABLE:
        for kw in kws:
            if kw.isascii():
                if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", low):
                    return {"brand": brand, "access": access}
            elif kw in low:
                return {"brand": brand, "access": access}
    return None


def _read_sys(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read().strip()
    except Exception:
        return ""


def _nut_conf(name):
    """返回第一个存在的 NUT 配置文件路径；没有返回空串。"""
    for d in NUT_CONF_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return ""


def _parse_upsd_listen():
    """解析 upsd.conf 的 LISTEN → [(host, port)]。"""
    out = []
    p = _nut_conf("upsd.conf")
    if not p:
        return out
    try:
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                parts = line.split()
                if len(parts) < 2 or parts[0].upper() != "LISTEN":
                    continue
                host = parts[1].strip("'\"")
                port = NUT_DEFAULT_PORT
                if len(parts) > 2:
                    try:
                        port = int(parts[2].strip("'\""))
                    except Exception:
                        port = NUT_DEFAULT_PORT
                out.append((host, port))
    except Exception:
        pass
    return out


def _parse_ups_conf():
    """解析 ups.conf → [{name, driver, port, desc}]。"""
    out = []
    p = _nut_conf("ups.conf")
    if not p:
        return out
    try:
        cur = None
        with open(p, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = raw.split("#", 1)[0].strip()
                if not line:
                    continue
                m = re.match(r"^\[([^\]]+)\]$", line)
                if m:
                    cur = {"name": m.group(1).strip(), "driver": "", "port": "", "desc": ""}
                    out.append(cur)
                    continue
                if cur is None or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().lower()
                if k in cur:
                    cur[k] = v.strip().strip("'\"")
    except Exception:
        pass
    return out


def _nut_targets(cfg=None):
    """可能的 upsc 连接目标（host[:port]），按优先级去重。

    先用户配置，再系统 upsd.conf 的 LISTEN，最后回环兜底。
    """
    cfg = cfg or {}
    host = str(cfg.get("nut_host") or "").strip()
    try:
        port = int(cfg.get("nut_port") or 0)
    except Exception:
        port = 0
    if host and host.count(":") == 1 and host.rsplit(":", 1)[-1].isdigit():
        h, p = host.rsplit(":", 1)
        host, port = h, port or int(p)
    if host:
        # 显式配置时只连它，避免悄悄回落到本机造成“看起来能识别但不是这台”的误解
        return ["%s:%d" % (host, port or NUT_DEFAULT_PORT)]
    cand = []
    others = []
    for h, p in _parse_upsd_listen():
        if h in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):
            cand.append(("127.0.0.1" if h in ("0.0.0.0", "::1") else h, p))
        elif h:
            others.append((h, p))
    cand.append(("127.0.0.1", NUT_DEFAULT_PORT))
    cand.append(("localhost", 0))
    cand.extend(others)
    seen, out = set(), []
    for h, p in cand:
        tgt = ("%s:%d" % (h, p)) if p else h
        if tgt not in seen:
            seen.add(tgt)
            out.append(tgt)
    return out


def _parse_upsc_list(out):
    """解析 upsc -l 输出。

    标准格式是一行一个实例名；部分 NUT 版本/第三方封装会带 “name: 描述”，
    这里兼容取冒号前的名字。
    """
    names, seen = [], set()
    for line in (out or "").splitlines():
        line = line.strip()
        if not line or line.lower().startswith("error"):
            continue
        cand = line
        if ":" in line:
            head = line.split(":", 1)[0].strip()
            if re.match(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$", head):
                cand = head
        cand = cand.strip()
        if not cand:
            continue
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$", cand):
            parts = cand.split()
            cand = parts[-1] if parts else ""
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.@-]*$", cand):
                continue
        if cand not in seen:
            seen.add(cand)
            names.append(cand)
    return names


def _norm_status_words(status):
    """把非标准状态词归一化，去重保序。"""
    out = []
    for w in str(status or "").upper().split():
        w = UPS_STATUS_ALIAS.get(w, w)
        if w not in out:
            out.append(w)
    return out


def _usb_ups_scan(limit=12):
    """/sys 扫描疑似 UPS 的 USB 设备（只读，用于识别诊断）。"""
    base = "/sys/bus/usb/devices"
    out = []
    try:
        entries = sorted(os.listdir(base))
    except Exception:
        return out
    for e in entries:
        d = os.path.join(base, e)
        vid = _read_sys(os.path.join(d, "idVendor"))
        pid = _read_sys(os.path.join(d, "idProduct"))
        if not vid or not pid:
            continue
        mfr = _read_sys(os.path.join(d, "manufacturer"))
        prod = _read_sys(os.path.join(d, "product"))
        known_vid = USB_VENDOR_IDS.get(vid.lower(), "")
        v = _vendor_match(" ".join((mfr, prod)))
        looks = bool(v or known_vid) or re.search(
            r"(?<![a-z0-9])(ups|power|battery|inverter)(?![a-z0-9])",
            (mfr + " " + prod).lower()) is not None
        if not looks:
            continue
        has_hid = False
        try:
            for iface in os.listdir(d):
                if iface.startswith(e + ":") and \
                        _read_sys(os.path.join(d, iface, "bInterfaceClass")) == "03":
                    has_hid = True
                    break
        except Exception:
            pass
        out.append({
            "bus_dev": e, "vid": vid, "pid": pid,
            "mfr": mfr, "product": prod, "hid": has_hid,
            "brand": (v or {}).get("brand") or known_vid,
        })
    return out[:limit]


def _upsc_path():
    """upsc 真实路径；找不到返回空串。"""
    for p in ("/usr/bin/upsc", "/bin/upsc", "/usr/sbin/upsc",
              "/sbin/upsc", "/usr/local/bin/upsc", "/usr/local/ups/bin/upsc",
              "/usr/lib/nut/upsc"):
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    w = shutil.which("upsc")
    return w or ""


def _run(args, timeout=8):
    """返回 (rc, stdout, stderr)。FileNotFoundError→-2，超时→-3。"""
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout,
                           env=dict(os.environ, LC_ALL="C"))
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except FileNotFoundError:
        return (-2, "", "command not found")
    except subprocess.TimeoutExpired:
        return (-3, "", "timeout")
    except Exception as e:
        return (-1, "", str(e))


class UpsError(Exception):
    """NUT 读取失败。code 用作快照的 state 字段。"""
    def __init__(self, code, msg, hint=""):
        Exception.__init__(self, msg)
        self.code = code
        self.message = msg
        self.hint = hint


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def ups_load(data_dir):
    """读 ups.json；坏文件/坏字段回落默认。"""
    user = {}
    if data_dir:
        try:
            with open(os.path.join(data_dir, UPS_FILE), "r", encoding="utf-8",
                      errors="ignore") as f:
                user = json.load(f)
            if not isinstance(user, dict):
                user = {}
        except Exception:
            user = {}
    return ups_normalize(user)


def ups_normalize(data, base=None):
    """把任意来源的配置归一化 + 夹取。"""
    cfg = dict(base or UPS_DEFAULT)
    data = data or {}
    if "enabled" in data:
        cfg["enabled"] = _onoff(data["enabled"])
    if "ups_name" in data:
        cfg["ups_name"] = _clean_text(str(data.get("ups_name") or ""), 64)
    if "nut_host" in data:
        host = _clean_text(str(data.get("nut_host") or ""), 128)
        cfg["nut_host"] = host if re.match(r"^[A-Za-z0-9_.:\[\]-]*$", host) else ""
    if "nut_port" in data:
        try:
            n = int(float(data.get("nut_port") or 0))
        except Exception:
            n = 0
        cfg["nut_port"] = max(0, min(65535, n))
    if "poll_interval" in data:
        try:
            n = int(float(data["poll_interval"]))
        except Exception:
            n = 5
        cfg["poll_interval"] = max(3, min(60, n))
    if "notify_enabled" in data:
        cfg["notify_enabled"] = _onoff(data["notify_enabled"])
    if "notify_events" in data:
        want = data.get("notify_events")
        if isinstance(want, str):
            want = [s.strip() for s in want.split(",")]
        if not isinstance(want, (list, tuple)):
            want = list(UPS_EVENT_CHOICES)
        seen, out = set(), []
        for x in want:
            code = str(x or "").strip().upper()
            if code in UPS_EVENT_META and code not in seen:
                seen.add(code)
                out.append(code)
        cfg["notify_events"] = out
    if "notify_debounce_s" in data:
        try:
            n = int(float(data["notify_debounce_s"]))
        except Exception:
            n = 60
        cfg["notify_debounce_s"] = max(0, min(3600, n))
    if "notify_max_per_min" in data:
        try:
            n = int(float(data["notify_max_per_min"]))
        except Exception:
            n = 8
        cfg["notify_max_per_min"] = max(1, min(60, n))
    return cfg


def _write_json(path, data):
    """原子写 JSON（先写 .tmp 再 rename）。"""
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        raise e


# ---------------------------------------------------------------------------
# NUT 采集
# ---------------------------------------------------------------------------
def ups_list(cfg=None):
    """发现 UPS 实例名。

    依次尝试：用户配置的 NUT 地址 → 系统 upsd.conf 的 LISTEN → 回环兜底。
    兼容 “name: 描述” 形式的列表输出，避免把描述当实例名。
    """
    exe = _upsc_path()
    if not exe:
        raise UpsError("no_nut", "未找到 upsc 命令",
                       "系统未安装 NUT；飞牛「UPS 电源管理」启用后即有")
    errs, connected = [], False
    for tgt in _nut_targets(cfg):
        rc, out, err = _run([exe, "-l"] + ([tgt] if tgt else []), timeout=8)
        if rc == 0:
            connected = True
            names = _parse_upsc_list(out)
            if names:
                return names
            errs.append("%s：无实例" % (tgt or "默认"))
            continue
        low = (err + out).lower()
        if "connection refused" in low or "cannot connect" in low or "no route" in low:
            errs.append("%s：连不上" % (tgt or "默认"))
            continue
        errs.append("%s：%s" % (tgt or "默认", (err.strip() or out.strip())[:100]))
    if connected:
        raise UpsError("no_device", "NUT 在运行但未发现 UPS",
                       "ups.conf 里没有可用实例；UPS 可能没接 USB 线，或驱动未启动")
    conf = _parse_ups_conf()
    if conf:
        raise UpsError(
            "no_nut", "连不上 NUT 服务（upsd 未监听）",
            "系统已配置 UPS：%s；请确认飞牛「UPS 电源管理」已开启、nut-server 正在运行"
            % "、".join(x["name"] for x in conf))
    raise UpsError("no_nut", "连不上 NUT 服务（upsd 未监听）",
                   "nut-server 未运行；飞牛关掉 UPS 电源管理后 NUT 会随之停止（%s）"
                   % "；".join(errs)[:240])


def ups_pick(cfg):
    """按配置优先，否则第一台。返回 (名称, 全部名称)。"""
    names = ups_list(cfg)
    want = str((cfg or {}).get("ups_name") or "").strip()
    if want and want in names:
        return want, names
    return names[0], names


def ups_read_vars(name, cfg=None):
    """读全量变量。失败抛 UpsError。"""
    exe = _upsc_path()
    if not exe:
        raise UpsError("no_nut", "未找到 upsc 命令")
    last = None
    for tgt in _nut_targets(cfg):
        arg = ("%s@%s" % (name, tgt)) if tgt else name
        rc, out, err = _run([exe, arg], timeout=8)
        if rc == 0:
            data = {}
            for line in out.splitlines():
                line = line.rstrip()
                if not line or ":" not in line:
                    continue
                k, v = line.split(":", 1)
                k = k.strip()
                if not k or " " in k:
                    continue
                data[k] = v.strip()
            if data:
                return data
            last = UpsError("error", "UPS 返回空数据")
            continue
        low = (err + out).lower()
        if "data stale" in low or "driver not connected" in low:
            raise UpsError("stale", "驱动与 UPS 通信中断",
                           "USB 线可能松动，或 nut-driver@%s 已退出（systemctl status "
                           "nut-driver@%s / nutdrv_qx 驱动是否支持该型号）" % (name, name))
        if "unknown ups" in low or "unknown upsname" in low:
            raise UpsError("no_device", "UPS 实例 %s 不存在" % name)
        if "access denied" in low or "username" in low or "invalid credentials" in low:
            raise UpsError("error", "upsd 拒绝访问：%s" % (err.strip() or out.strip())[:160])
        if "connection refused" in low or "cannot connect" in low or "no route" in low:
            last = UpsError("no_nut", "连不上 NUT 服务")
            continue
        last = UpsError("error", "读取 UPS 失败：%s" % (err.strip() or out.strip())[:200])
    raise last or UpsError("error", "读取 UPS 失败")


def _decode_status(s):
    mask = 0
    words = (s or "").split()
    for code, _cn, bit in UPS_STATUS_BITS:
        if code in words:
            mask |= bit
    return mask


def _status_labels(mask, words=None):
    labels = [cn for code, cn, bit in UPS_STATUS_BITS if mask & bit]
    # 未识别的状态词原样带出，便于发现厂商私有状态（如 TEST / ECO）
    known = {code for code, _cn, _bit in UPS_STATUS_BITS}
    for w in (words or []):
        if w not in known:
            labels.append(w)
    return labels


def _pick(raw, *keys):
    """按优先级取第一个非空变量值。"""
    for k in keys:
        v = raw.get(k)
        if v is not None and str(v).strip() != "":
            return v
    return None


def _pick_num(raw, *keys):
    return _fnum(_pick(raw, *keys))


def ups_build(name, raw):
    """原始变量 → 快照（device + live）。

    不同驱动（usbhid-ups / nutdrv_qx / blazer_usb / snmp-ups…）暴露的变量名不完全一致，
    这里按别名表取值，尽量把三相机型、OEM 机型也映射到同一套字段。
    """
    status = raw.get("ups.status", "") or ""
    words = _norm_status_words(status)
    live = {"status": status, "st": _decode_status(" ".join(words))}
    live["labels"] = _status_labels(live["st"], words)
    live["in_v"] = _pick_num(raw, "input.voltage", "input.L1-N.voltage",
                             "input.L1.voltage", "input.L2-N.voltage")
    live["in_hz"] = _pick_num(raw, "input.frequency", "input.L1.frequency")
    live["out_v"] = _pick_num(raw, "output.voltage", "output.L1-N.voltage",
                              "output.L1.voltage")
    live["out_a"] = _pick_num(raw, "output.current", "output.L1.current",
                              "output.L1-N.current")
    live["out_hz"] = _pick_num(raw, "output.frequency", "output.L1.frequency")
    live["load"] = _pick_num(raw, "ups.load", "output.L1.power.percent",
                             "output.power.percent", "ups.load.L1")
    live["rp"] = _pick_num(raw, "ups.realpower", "output.realpower",
                           "output.L1.realpower")
    live["out_va"] = _pick_num(raw, "ups.power", "output.power", "output.L1.power")
    live["chg"] = _pick_num(raw, "battery.charge", "ups.battery.charge",
                            "battery.charge.level")
    live["rt"] = _pick_num(raw, "battery.runtime", "ups.timer.runtime")
    live["bat_v"] = _pick_num(raw, "battery.voltage", "battery.voltage.dc")
    live["temp"] = _pick_num(raw, "battery.temperature", "ups.temperature",
                             "ambient.temperature")
    live["nominal_va"] = _pick_num(raw, "ups.power.nominal", "output.power.nominal")
    live["source"] = raw.get("input.source") or ""

    # 输出侧有效性：仅把 0V（市电切换瞬间的异常帧）视为无效。
    # 瓦力方程 W120 等直流输出机型正常工作电压约 19V，不能按交流 50V 阈值过滤
    ov = live.get("out_v")
    live["valid"] = not (ov is not None and ov <= 0.5)

    # 功率优先取设备直报的有功功率，设备未提供时用输出电压 × 输出电流
    live["out_w"] = None
    if live["rp"] is not None:
        live["out_w"] = round(live["rp"], 1)
    elif live["valid"] and ov is not None and live["out_a"] is not None:
        live["out_w"] = round(ov * live["out_a"], 1)

    if not live["valid"]:
        # 只剔除输出侧读数，输入/电量/负载仍是有效数据
        for k in ("out_v", "out_a", "out_w", "out_hz"):
            live[k] = None

    mfr = _clean_ident(_pick(raw, "device.mfr", "ups.mfr", "device.vendor",
                             "device.manufacturer"))
    model = _clean_ident(_pick(raw, "device.model", "ups.model", "device.modelname",
                               "device.product"), 80)
    serial = _clean_ident(_pick(raw, "device.serial", "ups.serial",
                                "device.serialnumber"), 80)
    driver = raw.get("driver.name") or raw.get("driver.version") or ""
    v = _vendor_match(" ".join((mfr, model, driver)))
    device = {
        "name": name,
        "mfr": mfr,
        "model": model,
        "serial": serial,
        "type": raw.get("ups.type") or "",
        "driver": driver,
        # 识别出的中文品牌 + 常见接入方式（未匹配时回落厂商名）
        "brand": (v or {}).get("brand") or mfr,
        "access": (v or {}).get("access") or "",
        "va": live["nominal_va"],
    }
    return {"ts": int(time.time()), "device": device, "live": live}


def ups_diag(cfg=None):
    """识别诊断：upsc 探测结果 + NUT 配置 + USB 设备 + 处理建议（只读）。"""
    cfg = cfg or {}
    exe = _upsc_path()
    ups_conf = _parse_ups_conf()
    targets = []
    reachable = False
    names_any = []
    if exe:
        for tgt in _nut_targets(cfg):
            rc, out, err = _run([exe, "-l"] + ([tgt] if tgt else []), timeout=6)
            names = _parse_upsc_list(out) if rc == 0 else []
            if rc == 0:
                reachable = True
            if names:
                names_any.extend(x for x in names if x not in names_any)
            targets.append({
                "target": tgt or "默认（localhost）",
                "ok": rc == 0, "names": names,
                "error": "" if rc == 0 else ((err.strip() or out.strip())[:160]
                                             or "rc=%d" % rc),
            })
    usb = _usb_ups_scan()
    listen = ["%s %d" % (h, p) for h, p in _parse_upsd_listen()]

    sug = []
    if not exe:
        sug.append("未找到 upsc：飞牛「系统设置 → 电源 → UPS」开启后系统会安装 nut-client；"
                   "若系统 UPS 功能正常但本应用找不到命令，可通过应用日志反馈。")
    else:
        if usb:
            for u in usb[:3]:
                tag = ("%s %s" % (u["brand"], u["product"])).strip() or "USB 设备"
                sug.append("检测到 USB UPS：%s（%s:%s%s）"
                           % (tag, u["vid"], u["pid"], "，HID" if u["hid"] else ""))
        if not reachable:
            sug.append("NUT 服务（upsd）不可达：先用 SSH 执行 upsc -l 验证。"
                       "若系统「UPS 电源管理」能显示 UPS 但这里连不上，说明该设备可能不是通过 "
                       "NUT 暴露的（例如系统用自己的 HID 守护进程），可在下方填写 NUT 地址/端口再试。")
        elif not names_any:
            sug.append("upsd 已运行但没有可用实例：检查 /etc/nut/ups.conf 是否配置了 UPS，"
                       "以及驱动是否启动（systemctl status nut-driver@<实例名>）。")
        if usb and names_any:
            sug.append("NUT 已有实例：%s；若读数为空，多半是驱动型号不匹配，"
                       "可尝试 usbhid-ups（APC HID 类）或 nutdrv_qx（Megatec 类）并指定 vendorid/productid。"
                       % "、".join(names_any))
    return {
        "upsc": exe,
        "targets": targets,
        "ups_conf": ups_conf,
        "upsd_listen": listen,
        "usb": usb,
        "suggestions": sug[:4],
    }


# ---------------------------------------------------------------------------
# 告警事件检测（边沿）
# ---------------------------------------------------------------------------
def ups_detect(poller, snap):
    """边沿事件检测。返回本轮新事件列表。"""
    live = snap["live"]
    mask = live["st"]
    prev = poller.last_mask
    prev_s = poller.last_status
    common = {
        "ts": snap["ts"], "ups": snap["device"]["name"],
        "from_st": prev_s, "to_st": live["status"],
        "chg": live.get("chg"), "rt": live.get("rt"),
        "in_v": live.get("in_v"), "out_v": live.get("out_v"),
        "load": live.get("load"), "temp": live.get("temp"),
        "out_w": live.get("out_w"),
    }
    out = []

    def add(etype, note):
        label, level = UPS_EVENT_META.get(etype, (etype, "info"))
        out.append(dict(common, type=etype, label=label, level=level, note=note))

    if prev is not None and mask != prev:
        if (prev & UPS_BIT_OB) == 0 and (mask & UPS_BIT_OB):
            add("POWER_LOSS", "市电中断，切换电池供电")
        elif (prev & UPS_BIT_OB) and (mask & UPS_BIT_OB) == 0:
            add("POWER_RESTORE", "市电恢复")
        if (prev & UPS_BIT_LB) == 0 and (mask & UPS_BIT_LB):
            add("LOW_BATTERY", "进入低电量状态")
        if (prev & UPS_BIT_RB) == 0 and (mask & UPS_BIT_RB):
            add("REPLACE_BATTERY", "UPS 报告需要更换电池")
        if (prev & UPS_BIT_ALARM & 64) == 0 and (mask & UPS_BIT_ALARM & 64):
            add("ALARM", "UPS 告警")
        if (prev & UPS_BIT_OVER) == 0 and (mask & UPS_BIT_OVER):
            add("OVERLOAD", "输出超载")
        if (prev & (1 << 13)) == 0 and (mask & (1 << 13)):  # FSD
            add("FORCED_SHUTDOWN", "UPS 触发强制关机")

    poller.last_mask = mask
    poller.last_status = live["status"]
    return out


# ---------------------------------------------------------------------------
# 通知发送（简化版，直接调 notify_send_all）
# ---------------------------------------------------------------------------
def ups_state_read(data_dir):
    """告警状态（去抖/限流）。"""
    st = {"sent": {}, "window": []}
    if not data_dir:
        return st
    try:
        with open(os.path.join(data_dir, UPS_STATE_FILE), "r", encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict):
            st.update({k: d[k] for k in st if k in d})
    except Exception:
        pass
    if not isinstance(st.get("sent"), dict):
        st["sent"] = {}
    if not isinstance(st.get("window"), list):
        st["window"] = []
    return st


def ups_state_write(data_dir, st):
    if not data_dir:
        return
    try:
        _write_json(os.path.join(data_dir, UPS_STATE_FILE), st)
    except Exception:
        pass


def _throttled(st, etype, cfg, now):
    """同类去抖 + 全局限流。"""
    deb = int((cfg or {}).get("notify_debounce_s") or 0)
    last = float((st.get("sent") or {}).get(etype) or 0)
    if deb > 0 and now - last < deb:
        return True
    cap = int((cfg or {}).get("notify_max_per_min") or 8)
    win = [t for t in (st.get("window") or []) if now - float(t) < 60]
    st["window"] = win
    if len(win) >= cap:
        return True
    return False


def _format_num(v, unit, nd=0):
    if v is None:
        return "—"
    return ("%." + str(nd) + "f") % float(v) + unit


def ups_build_msg(ev, snap):
    """事件 → (标题, 正文)。"""
    etype = ev.get("type") or ""
    label, level = UPS_EVENT_META.get(etype, (etype or "事件", "info"))
    dev = (snap or {}).get("device") or {}
    live = (snap or {}).get("live") or {}
    who = " ".join(x for x in (dev.get("mfr"), dev.get("model")) if x) or "UPS"
    subject = "[飞牛监控] UPS %s · %s" % (label, who)
    lines = ["检测时间：%s" % time.strftime("%Y-%m-%d %H:%M:%S")]
    if ev.get("note"):
        lines.append("说明：%s" % ev["note"])
    frm, to = ev.get("from_st") or "", ev.get("to_st") or ""
    if to and to != frm:
        lines.append("状态：%s → %s" % (frm or "—", to))
    chg = ev.get("chg")
    if chg is not None:
        lines.append("电量：%s" % _format_num(chg, "%"))
    iv, ov = ev.get("in_v"), ev.get("out_v")
    if ov is None:
        ov = live.get("out_v")
    if iv is not None or ov is not None:
        lines.append("电压：输入 %s / 输出 %s"
                     % (_format_num(iv, " V", 1), _format_num(ov, " V", 1)))
    load = ev.get("load")
    if load is not None:
        lines.append("负载：%s" % _format_num(load, "%"))
    out_w = ev.get("out_w")
    if out_w is None:
        out_w = live.get("out_w")
    if out_w is not None:
        lines.append("输出功率：%.1f W" % float(out_w))
    if dev.get("serial"):
        lines.append("序列号：%s" % dev["serial"])
    return subject, "\n".join(lines)


def ups_send_events(events, snap, cfg, data_dir, notify_load_fn, notify_send_fn):
    """发送事件。外部传入 notify_load/notify_send 两个函数以解耦对 server.py 的依赖。

    notify_load_fn(data_dir) → notify 配置 dict
    notify_send_fn(notify_cfg, subject, body) → (ok, detail)
    """
    events = events or []
    if not (cfg or {}).get("notify_enabled"):
        return
    want = set((cfg or {}).get("notify_events") or [])
    if not events:
        return
    st = ups_state_read(data_dir)
    now = time.time()
    nc = notify_load_fn(data_dir)
    sent = False
    for ev in events:
        etype = ev.get("type")
        if etype not in want:
            continue
        if _throttled(st, etype, cfg, now):
            print("[fnmonitor] UPS 事件 %s 去抖/限流跳过" % etype)
            continue
        subject, body = ups_build_msg(ev, snap)
        try:
            ok, _res = notify_send_fn(nc, subject, body)
        except Exception as e:
            print("[fnmonitor] UPS 通知发送异常：%s" % str(e)[:120])
            continue
        if ok:
            st["sent"][etype] = now
            st["window"].append(now)
            sent = True
    if sent:
        ups_state_write(data_dir, st)


# ---------------------------------------------------------------------------
# 采集线程
# ---------------------------------------------------------------------------
class UpsPoller(threading.Thread):
    """NUT 采集线程。未启用时本线程根本不存在。"""

    def __init__(self, data_dir, config):
        threading.Thread.__init__(self, name="fnm-ups", daemon=True)
        self.data_dir = data_dir
        self.config = config or {}
        self._stop = threading.Event()
        self.last_mask = None
        self.last_status = ""
        self.down_since = None
        self.down_ticks = 0
        self.down_at = ""
        self.ups_names = []
        self.ticks = 0
        self.started = int(time.time())
        self.tick_lock = threading.Lock()
        # 外部注入的通知函数（由 server.py 在启动时设置）
        self._notify_load = None
        self._notify_send = None

    def set_notify_fn(self, notify_load_fn, notify_send_fn):
        self._notify_load = notify_load_fn
        self._notify_send = notify_send_fn

    def stop(self):
        self._stop.set()
        _UPS_WAKE.set()

    def run(self):
        global _UPS_CFG
        _UPS_CFG = self.config
        cfg = ups_load(self.data_dir)
        self._stop.wait(2)  # 启动后静默一个周期
        while not self._stop.is_set():
            interval = 5
            try:
                cfg = ups_load(self.data_dir)
                interval = max(3, min(60, int(cfg.get("poll_interval") or 5)))
                if not cfg.get("enabled"):
                    self._publish_off()
                    return
                self._tick(cfg)
            except Exception as e:
                print("[fnmonitor] UPS 采集异常：%s" % str(e)[:160])
            _UPS_WAKE.wait(interval)
            _UPS_WAKE.clear()

    def _tick(self, cfg):
        if not self.tick_lock.acquire(False):
            return
        try:
            self._once(cfg)
        finally:
            self.tick_lock.release()

    def _once(self, cfg):
        self.ticks += 1
        snap = None
        try:
            name, names = ups_pick(cfg)
            self.ups_names = names
            raw = ups_read_vars(name, cfg)
            snap = ups_build(name, raw)
        except UpsError as e:
            self._set_error(e.code, e.message, e.hint)
            return
        except Exception as e:
            self._set_error("error", "读取 UPS 失败：%s" % str(e)[:160], "")
            return

        # 通信恢复事件
        if self.down_since is not None:
            lost = int(time.time()) - self.down_since
            ev = [{"ts": int(time.time()), "type": "DEVICE_BACK", "level": "ok",
                   "label": UPS_EVENT_META["DEVICE_BACK"][0],
                   "ups": snap["device"].get("name") or "",
                   "from_st": self.down_at, "to_st": snap["live"].get("status") or "",
                   "chg": snap["live"].get("chg"), "rt": snap["live"].get("rt"),
                   "in_v": snap["live"].get("in_v"), "out_v": snap["live"].get("out_v"),
                   "load": snap["live"].get("load"), "temp": snap["live"].get("temp"),
                   "out_w": snap["live"].get("out_w"),
                   "note": "通信恢复（失联 %d 秒）" % lost}]
            self.down_since = None
            self.down_ticks = 0
            self.down_at = ""
            self._notify_events(ev, snap, cfg)

        evs = ups_detect(self, snap)
        snap["state"] = "ok"
        snap["ok"] = True
        snap["enabled"] = 1
        snap["stale"] = 0
        snap["error"] = ""
        snap["ups_list"] = list(self.ups_names)
        snap["poll"] = cfg.get("poll_interval")
        snap["uptime"] = int(time.time()) - self.started
        self._publish(snap)

        if evs:
            self._notify_events(evs, snap, cfg)

    def _notify_events(self, events, snap, cfg):
        if self._notify_load and self._notify_send:
            try:
                ups_send_events(events, snap, cfg, self.data_dir,
                                self._notify_load, self._notify_send)
            except Exception as e:
                print("[fnmonitor] UPS 通知异常：%s" % str(e)[:120])

    def _set_error(self, code, msg, hint=""):
        now = int(time.time())
        old = ups_latest()
        snap = {"ok": False, "enabled": 1, "state": code or "error", "ts": now,
                "stale": 1, "error": msg, "hint": hint or "",
                "device": old.get("device") or {},
                "live": old.get("live") or {},
                "ups_list": list(self.ups_names),
                "poll": None, "uptime": int(time.time()) - self.started}
        if snap["live"]:
            snap["live"] = dict(snap["live"])
            snap["live"]["stale"] = 1
        if self.down_since is None:
            self.down_since = now
            self.down_ticks = 0
            self.down_at = (old.get("live") or {}).get("status") or ""
            ev = {"ts": now, "type": "DEVICE_LOST", "level": "crit",
                  "label": UPS_EVENT_META["DEVICE_LOST"][0],
                  "ups": (old.get("device") or {}).get("name") or "",
                  "from_st": self.down_at, "to_st": "",
                  "chg": None, "rt": None, "in_v": None, "out_v": None,
                  "load": None, "temp": None, "out_w": None,
                  "note": "%s%s" % (msg, ("（%s）" % hint) if hint else "")}
            self._notify_events([ev], snap, ups_load(self.data_dir))
        else:
            self.down_ticks += 1
        self._publish(snap)

    def _publish(self, snap):
        with _UPS_LOCK:
            _UPS_LATEST.clear()
            _UPS_LATEST.update(snap)

    def _publish_off(self):
        with _UPS_LOCK:
            _UPS_LATEST.clear()


# ---------------------------------------------------------------------------
# 公共 API
# ---------------------------------------------------------------------------
def ups_latest():
    """最近一帧快照（浅拷贝）。"""
    with _UPS_LOCK:
        return dict(_UPS_LATEST)


def ups_public(cfg):
    """对外配置（无敏感字段）。"""
    out = dict(cfg or UPS_DEFAULT)
    out["events_meta"] = [{"code": k, "label": v[0], "level": v[1]}
                          for k, v in UPS_EVENT_META.items()]
    out["choices"] = list(UPS_EVENT_CHOICES)
    out["upsc"] = _upsc_path()
    out["nut_targets"] = _nut_targets(cfg)
    snap = ups_latest()
    out["nut_state"] = snap.get("state") or ("off" if not out.get("enabled") else "init")
    out["nut_error"] = snap.get("error") or ""
    return out


def ups_off_snapshot(cfg):
    return {"ok": False, "enabled": 0, "state": "off", "ts": 0, "stale": 0,
            "error": "", "hint": "", "device": {}, "live": {},
            "upsc": _upsc_path(), "ups_list": [],
            "config": ups_public(cfg or UPS_DEFAULT)}


def ups_save(data_dir, data):
    """合并保存。返回 (cfg, error_msg)。"""
    cur = ups_load(data_dir)
    cfg = ups_normalize(data or {}, base=cur)
    _write_json(os.path.join(data_dir, UPS_FILE), cfg)
    _UPS_WAKE.set()
    try:
        ups_poller_ensure(data_dir, _UPS_CFG or {})
    except Exception as e:
        print("[fnmonitor] UPS 线程启停异常：%s" % str(e)[:120])
    return cfg, ""


def ups_poller_ensure(data_dir, config):
    """按配置启停采集线程。"""
    global _UPS_POLLER, _UPS_CFG
    cfg = ups_load(data_dir)
    if config:
        _UPS_CFG = config
    with _UPS_POLLER_LOCK:
        if not cfg.get("enabled"):
            if _UPS_POLLER is not None:
                _UPS_POLLER.stop()
                _UPS_POLLER = None
            with _UPS_LOCK:
                _UPS_LATEST.clear()
            return None
        if _UPS_POLLER is not None and _UPS_POLLER.is_alive():
            _UPS_WAKE.set()
            return _UPS_POLLER
        _UPS_POLLER = UpsPoller(data_dir, config)
        _UPS_POLLER.start()
        return _UPS_POLLER


def ups_poller_stop():
    global _UPS_POLLER
    with _UPS_POLLER_LOCK:
        if _UPS_POLLER is not None:
            _UPS_POLLER.stop()
            _UPS_POLLER = None
    with _UPS_LOCK:
        _UPS_LATEST.clear()


def ups_apply_notify_fn(notify_load_fn, notify_send_fn):
    """注入通知函数（server.py 启动时调用一次）。"""
    global _UPS_POLLER
    p = _UPS_POLLER
    if p is not None and p.is_alive():
        p.set_notify_fn(notify_load_fn, notify_send_fn)
