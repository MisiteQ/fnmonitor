"""Shared helpers for the fnMonitor regression suite (stdlib only).

- load_server(): import app/server.py as a module (its ``if __name__ == "__main__"`` guard
  keeps main() from running). Loaded once per test process.
- ServerProcess: run the real server as a subprocess on a free loopback port with a temp
  --data-dir. The package is copied to a temp dir first, because main() rewrites the desktop
  launcher file ``<app>/ui/config`` with the listening port; running from the repo would modify
  a tracked file. TRIM_APPNAME is set to a unique name so the /var/apps/<name> launcher paths
  never exist.
- http(): raw http.client request helper (explicit Host header, optional gzip decode).
- shape()/compare_shape(): JSON "contract" helpers used by test_contract and update_golden.
"""
import gzip
import http.client
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTS_DIR)
SERVER_PY = os.path.join(REPO, "app", "server.py")
GOLDEN = os.path.join(TESTS_DIR, "golden", "shapes.json")

IS_LINUX = sys.platform.startswith("linux")
BENCH = os.environ.get("FNM_BENCH") == "1"
SLOW = os.environ.get("FNM_SLOW") == "1"

_SRV = None


def load_server():
    """Import app/server.py once (module name ``fnm_server``)."""
    global _SRV
    if _SRV is None:
        spec = importlib.util.spec_from_file_location("fnm_server", SERVER_PY)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["fnm_server"] = mod
        spec.loader.exec_module(mod)
        # Unit tests must never probe the host's disks for standby state
        mod.RUNTIME["standby_protect"] = False
        _SRV = mod
    return _SRV


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


# Keys written to config.json before the server starts: no update checks (network), no disk
# standby probing, nothing that reaches outside the temp dir.
TEST_CONFIG = {"update_autocheck": 0, "update_autodownload": 0, "update_autoupdate": 0,
               "disk_standby_protect": 0, "ec_fan_probe": 0, "open_mode": "url"}


class ServerProcess:
    """Real server subprocess bound to 127.0.0.1:<free port> with a throwaway data dir."""

    def __init__(self, config=None, seed=None, server_py=None):
        self.config = dict(TEST_CONFIG, **(config or {}))
        self.seed = seed                    # callable(data_dir_of_monitor_db) run before start
        self.server_py = server_py or SERVER_PY
        self.tmp = tempfile.mkdtemp(prefix="fnm-test-")
        self.pkg = os.path.join(self.tmp, "pkg")
        self.data = os.path.join(self.tmp, "data")          # --data-dir (config.json, sessions.json)
        self.localdata = os.path.join(self.data, "localdata")  # monitor.db / ui.json
        self.port = free_port()
        self.host = "127.0.0.1"
        self.proc = None
        self.log_path = os.path.join(self.tmp, "server.log")

    # ---- lifecycle ----
    def start(self, timeout=40):
        app = os.path.join(self.pkg, "app")
        os.makedirs(app)
        shutil.copy2(self.server_py, os.path.join(app, "server.py"))
        shutil.copytree(os.path.join(REPO, "app", "www"), os.path.join(app, "www"))
        cities = os.path.join(REPO, "app", "cities_cn.json")
        if os.path.exists(cities):
            shutil.copy2(cities, os.path.join(app, "cities_cn.json"))
        shutil.copy2(os.path.join(REPO, "manifest"), os.path.join(self.pkg, "manifest"))
        os.makedirs(self.localdata)
        with open(os.path.join(self.data, "config.json"), "w") as f:
            json.dump(self.config, f)
        if self.seed:
            self.seed(self.localdata)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TRIM_")}
        env["TRIM_APPNAME"] = "fnmonitor-test-%s" % uuid.uuid4().hex[:8]
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        self._log = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(app, "server.py"), "--host", self.host,
             "--port", str(self.port), "--data-dir", self.data],
            stdout=self._log, stderr=subprocess.STDOUT, env=env, cwd=self.tmp)
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited early (%s):\n%s" % (self.proc.returncode, self.log()))
            try:
                st, _, _, _ = self.request("GET", "/api/auth/config", timeout=2)
                if st == 200:
                    return self
            except OSError as e:
                last = e
            time.sleep(0.1)
        self.stop()
        raise RuntimeError("server did not come up in %ss (%s):\n%s" % (timeout, last, self.log()))

    def wait_collected(self, timeout=40):
        """Block until the collector's first tick is visible in /api/overview (cached up to 5 s)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            st, j, _, _ = self.request("GET", "/api/overview")
            if st == 200 and j and "cpu" in j:
                return j
            time.sleep(0.5)
        raise RuntimeError("collector produced no snapshot in %ss:\n%s" % (timeout, self.log()))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        if getattr(self, "_log", None):
            self._log.close()
            self._log = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log(self):
        try:
            with open(self.log_path, "rb") as f:
                return f.read().decode("utf-8", "replace")[-4000:]
        except Exception:
            return ""

    @property
    def authority(self):
        return "%s:%d" % (self.host, self.port)

    # ---- HTTP ----
    def request(self, method, path, body=None, headers=None, host=None, timeout=60, conn=None):
        """Returns (status, parsed_json_or_None, response, raw_body_bytes)."""
        own = conn is None
        c = conn or http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        hdrs = dict(headers or {})
        hdrs.setdefault("Host", host if host is not None else self.authority)
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            if isinstance(data, str):
                data = data.encode("utf-8")
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in hdrs.items():
            c.putheader(k, v)
        if data is not None:
            c.putheader("Content-Length", str(len(data)))
        c.endheaders(data)
        r = c.getresponse()
        raw = r.read()
        if own:
            c.close()
        if r.getheader("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
        try:
            j = json.loads(raw.decode("utf-8"))
        except Exception:
            j = None
        return r.status, j, r, raw

    def get(self, path, cookie=None, host=None, headers=None):
        h = dict(headers or {})
        if cookie:
            h["Cookie"] = "fnmp_sid=" + cookie
        return self.request("GET", path, None, h, host=host)

    def post(self, path, body, cookie=None, headers=None, host=None):
        h = {"Content-Type": "application/json", "X-FNM-Request": "1"}
        h.update(headers or {})
        if cookie:
            h["Cookie"] = "fnmp_sid=" + cookie
        return self.request("POST", path, body, h, host=host)


def cookie_of(resp):
    sc = resp.getheader("Set-Cookie") or ""
    if not sc.startswith("fnmp_sid="):
        return None
    v = sc.split(";")[0].split("=", 1)[1]
    return v or None


# ---------------------------------------------------------------------------
# JSON shape contract
# ---------------------------------------------------------------------------
# Endpoints covered by tests/golden/shapes.json. Values are not compared, only keys + types.
CONTRACT_ENDPOINTS = [
    "/api/overview", "/api/config", "/api/docker", "/api/history?metric=cpu&range=1h",
    "/api/traffic", "/api/power_stats", "/api/daily", "/api/monthly_summary", "/api/ui",
    "/api/auth/config", "/api/vms",
]
# Sub-trees whose content depends on the host hardware / platform (lists of disks, sensors…).
# Only their own type is checked, not their inner keys.
OPAQUE_PATHS = {
    "/api/overview": ["disks", "disks_detail", "net", "diskio", "power", "gpu", "memory", "docker", "modules",
                      "ports", "hardware", "raid", "raidcard", "fans", "apps", "sensors.temps", "sensors.fans",
                      "sensors.volts", "temp.disks", "cpu.per_core", "system"],
    "/api/docker": ["containers"],
    "/api/vms": ["vms"],
    "/api/traffic": ["ifaces", "containers"],
    "/api/power_stats": ["disks", "realtime"],
}
# Keys that legitimately exist only on some hosts (compare-path syntax).
OPTIONAL_KEYS = {
    "/api/docker": ["$.sampled_at"],          # only when the docker CLI answered
}
MAX_DEPTH = 4


def _tname(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, (int, float)):
        return "number"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "list"
    if isinstance(v, dict):
        return "dict"
    return type(v).__name__


def shape(obj, opaque=(), path="", depth=0):
    """Reduce JSON to keys + types. List element shapes are merged over (a sample of) all
    elements so a leading null-filled entry does not hide types; opaque paths and anything deeper
    than MAX_DEPTH collapse to their type name."""
    t = _tname(obj)
    if path in opaque or depth >= MAX_DEPTH:
        return t
    if t == "dict":
        return {k: shape(v, opaque, (path + "." + k) if path else k, depth + 1) for k, v in sorted(obj.items())}
    if t == "list":
        els = [e for e in (obj[:50] + obj[-1:]) if e is not None]
        if not els:
            return []
        acc = shape(els[0], opaque, path + "[]", depth + 1)
        for e in els[1:]:
            acc = _merge(acc, shape(e, opaque, path + "[]", depth + 1))
        return [acc]
    return t


def _merge(a, b):
    """Combine two shapes of the same field: null yields to a concrete type, dict keys are unioned."""
    if a == "null":
        return b
    if b == "null":
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _merge(out[k], v) if k in out else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        if a and b:
            return [_merge(a[0], b[0])]
        return a or b
    return a


def endpoint_key(ep):
    return ep.split("?", 1)[0]


def compare_shape(golden, actual, path="$", optional=()):
    """Return a list of human-readable differences. Rules:
    - dict: same key set (missing or extra keys are both drift), except paths in `optional`;
    - "null" on either side matches any type (fields are legitimately nullable);
    - an empty list on either side matches any list (no element to inspect);
    - opaque "list"/"dict" type names match the corresponding structure."""
    if golden == "null" or actual == "null":
        return []
    gk, ak = _kind(golden), _kind(actual)
    if gk != ak:
        return ["%s: type %s != %s" % (path, _desc(golden), _desc(actual))]
    diffs = []
    if isinstance(golden, dict) and isinstance(actual, dict):
        for k in sorted(set(golden) | set(actual)):
            sub = "%s.%s" % (path, k)
            if k not in actual:
                if sub not in optional:
                    diffs.append("%s: missing (golden has it)" % sub)
            elif k not in golden:
                if sub not in optional:
                    diffs.append("%s: new key not in golden (run tests/update_golden.py if intended)" % sub)
            else:
                diffs += compare_shape(golden[k], actual[k], sub, optional)
    elif isinstance(golden, list) and isinstance(actual, list):
        if golden and actual:
            diffs += compare_shape(golden[0], actual[0], path + "[0]", optional)
    elif gk == "scalar" and golden != actual:
        diffs.append("%s: type %s != %s" % (path, golden, actual))
    return diffs


def _kind(s):
    if isinstance(s, dict) or s == "dict":
        return "dict"
    if isinstance(s, list) or s == "list":
        return "list"
    return "scalar"


def _desc(s):
    if isinstance(s, dict):
        return "dict"
    if isinstance(s, list):
        return "list"
    return str(s)


def seed_history(db_dir, hours=26, step=60, now=None):
    """Write a small synthetic monitor.db (last `hours` hours, one row per metric per `step`)
    so trend / traffic / daily endpoints have real rows to shape."""
    srv = load_server()
    now = now or time.time()
    h = srv.History(os.path.join(db_dir, "monitor.db"))
    rows = []
    t = now - hours * 3600
    i = 0
    while t < now - step:
        rows += [(t, "cpu", float(i % 100)), (t, "mem", 40.0), (t, "power", 30.0 + i % 7),
                 (t, "temp", 45.0), (t, "load1", 0.5), (t, "disk_total", 55.0),
                 (t, "net_rx_bytes:eth0", 1000.0 * i), (t, "net_tx_bytes:eth0", 400.0 * i),
                 (t, "net_rx:eth0", 16.0), (t, "net_tx:eth0", 7.0),
                 (t, "diskio_r:sda", 1.0), (t, "diskio_w:sda", 2.0),
                 (t, "ctr_in:web", 10.0 * i), (t, "ctr_out:web", 5.0 * i)]
        t += step
        i += 1
    h.write(rows)
    return len(rows)


def read_file(path, mode="r"):
    with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as f:
        return f.read()


def capture_shapes(server):
    """GET every contract endpoint on a running ServerProcess -> {endpoint: shape}."""
    server.wait_collected()
    out = {}
    for ep in CONTRACT_ENDPOINTS:
        st, j, _, raw = server.get(ep)
        if st != 200 or j is None:
            raise AssertionError("%s -> HTTP %s %r" % (ep, st, raw[:200]))
        out[ep] = shape(j, OPAQUE_PATHS.get(endpoint_key(ep), ()))
    return out


def contract_server():
    """Server used for golden capture and the contract test: seeded history, auth off."""
    return ServerProcess(seed=seed_contract)


def seed_contract(db_dir):
    """seed_history + a fixed 2020-01 monthly_summary month, so /api/monthly_summary always has
    the same (available) shape regardless of today's date (monthly archiving stays disabled)."""
    import sqlite3
    seed_history(db_dir)
    c = sqlite3.connect(os.path.join(db_dir, "monitor.db"))
    try:
        for m in ("cpu", "mem", "disk_total", "temp", "power", "load1", "fan_avg",
                  "net_rx_bytes:eth0", "net_tx_bytes:eth0"):
            c.execute("INSERT INTO monthly_summary(year, month, metric, sum_val, count_val, max_val, min_val, "
                      "first_val, last_val, first_ts, last_ts, updated_at) VALUES(2020,1,?,100,10,20,1,1,50,1,2,3)",
                      (m,))
        c.commit()
    finally:
        c.close()
