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


def _upsc_path():
    """upsc 真实路径；找不到返回空串。"""
    for p in ("/usr/bin/upsc", "/bin/upsc", "/usr/sbin/upsc",
              "/sbin/upsc", "/usr/local/bin/upsc"):
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
def ups_list():
    """发现 UPS 实例名。"""
    exe = _upsc_path()
    if not exe:
        raise UpsError("no_nut", "未找到 upsc 命令",
                       "系统未安装 NUT；飞牛「UPS 电源管理」启用后即有")
    rc, out, err = _run([exe, "-l"], timeout=8)
    if rc != 0:
        low = (err + out).lower()
        if "connection refused" in low or "cannot connect" in low:
            raise UpsError("no_nut", "连不上 NUT 服务（127.0.0.1:3493）",
                           "nut-server 未运行；飞牛关掉 UPS 电源管理后 NUT 会随之停止")
        raise UpsError("error", "upsc -l 失败：%s" % (err.strip() or out.strip())[:200])
    names = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        names.append(line.rsplit(":", 1)[-1].strip() if ":" in line else line)
    names = [n for n in names if n]
    if not names:
        raise UpsError("no_device", "NUT 在运行但未发现 UPS",
                       "UPS 可能没接 USB 线，或驱动未启动")
    return names


def ups_pick(cfg):
    """按配置优先，否则第一台。返回 (名称, 全部名称)。"""
    names = ups_list()
    want = str((cfg or {}).get("ups_name") or "").strip()
    if want and want in names:
        return want, names
    return names[0], names


def ups_read_vars(name):
    """读全量变量。失败抛 UpsError。"""
    exe = _upsc_path()
    if not exe:
        raise UpsError("no_nut", "未找到 upsc 命令")
    rc, out, err = _run([exe, name], timeout=8)
    if rc != 0:
        low = (err + out).lower()
        if "connection refused" in low:
            raise UpsError("no_nut", "连不上 NUT 服务")
        if "data stale" in low or "driver not connected" in low:
            raise UpsError("stale", "驱动与 UPS 通信中断",
                           "USB 线可能松动，或驱动进程已退出")
        if "unknown ups" in low:
            raise UpsError("no_device", "UPS 实例 %s 不存在" % name)
        raise UpsError("error", "读取 UPS 失败：%s" % (err.strip() or out.strip())[:200])
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
    if not data:
        raise UpsError("error", "UPS 返回空数据")
    return data


def _decode_status(s):
    mask = 0
    words = (s or "").split()
    for code, _cn, bit in UPS_STATUS_BITS:
        if code in words:
            mask |= bit
    return mask


def _status_labels(mask):
    return [cn for code, cn, bit in UPS_STATUS_BITS if mask & bit]


def ups_build(name, raw):
    """原始变量 → 快照（device + live）。"""
    live = {"status": raw.get("ups.status", "")}
    live["st"] = _decode_status(live["status"])
    live["labels"] = _status_labels(live["st"])
    live["in_v"] = _fnum(raw.get("input.voltage"))
    live["in_hz"] = _fnum(raw.get("input.frequency"))
    live["out_v"] = _fnum(raw.get("output.voltage"))
    live["out_a"] = _fnum(raw.get("output.current"))
    live["out_w"] = None  # Phase 1 核心：out_v × out_a = 输出功率
    if live["out_v"] is not None and live["out_a"] is not None:
        live["out_w"] = round(live["out_v"] * live["out_a"], 1)
    live["out_hz"] = _fnum(raw.get("output.frequency"))
    live["load"] = _fnum(raw.get("ups.load"))
    live["rp"] = _fnum(raw.get("ups.realpower"))
    live["chg"] = _fnum(raw.get("battery.charge"))
    live["rt"] = _fnum(raw.get("battery.runtime"))
    live["bat_v"] = _fnum(raw.get("battery.voltage"))
    live["temp"] = _fnum(raw.get("battery.temperature"))
    live["nominal_va"] = _fnum(raw.get("ups.power.nominal"))
    live["source"] = raw.get("input.source") or ""

    # 市电切换瞬间 UPS 会吐 0，这种帧剔除
    ov = live.get("out_v")
    live["valid"] = not (ov is not None and ov < 50)
    if not live["valid"]:
        for k in ("in_v", "in_hz", "out_v", "out_a", "out_w", "out_hz", "load", "rp"):
            live[k] = None

    device = {
        "name": name,
        "mfr": raw.get("device.mfr") or raw.get("ups.mfr") or "",
        "model": raw.get("device.model") or raw.get("ups.model") or "",
        "serial": raw.get("device.serial") or raw.get("ups.serial") or "",
        "type": raw.get("ups.type") or "",
        "driver": raw.get("driver.name") or "",
    }
    return {"ts": int(time.time()), "device": device, "live": live}


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
            raw = ups_read_vars(name)
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
