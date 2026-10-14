"""HTTP integration tests against the real server (subprocess, 127.0.0.1:<free port>, temp
--data-dir; see fnm_support.ServerProcess). Network-touching endpoints (/api/weather with a
real location, /api/update/check, GET /api/update/download?to=browser) are not called."""
import http.client
import json
import os
import select
import socket
import statistics
import stat
import time
import unittest

from fnm_support import SLOW, ServerProcess, cookie_of, load_server, read_file, seed_history

PW1, PW2 = "secret-one-123", "secret-two-456"

# Every read-only GET endpoint that is safe offline (used for the "no secrets anywhere" sweep)
SAFE_GETS = ["/", "/index.html", "/login.html", "/api/overview", "/api/docker", "/api/modules", "/api/processes",
             "/api/system", "/api/config", "/api/apps", "/api/ports", "/api/hardware", "/api/ui",
             "/api/history/diag", "/api/sensors", "/api/fans", "/api/raidcard", "/api/power", "/api/traffic",
             "/api/power_stats", "/api/gpu", "/api/gpu?diag=1", "/api/memory", "/api/monthly_summary",
             "/api/daily", "/api/daily?days=400", "/api/report", "/api/report?format=html", "/api/auth/config",
             "/api/auth/session", "/api/export?type=status", "/api/export?type=history&range=24h",
             "/api/export?type=traffic&range=24h", "/api/export?type=power&range=24h"] + \
            ["/api/history?metric=%s&range=%s" % (m, r) for m in ("cpu", "mem", "temp", "power", "net", "net_bytes",
                                                                  "ctr_in", "diskio", "disk_total", "load1")
             for r in ("1h", "all")]


def assert_no_secret(tc, name, raw, pw=None):
    s = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
    tc.assertNotIn("pbkdf2", s, name)
    tc.assertNotIn("auth_password_hash", s, name)
    tc.assertNotIn('"auth_password"', s, name)
    if pw:
        tc.assertNotIn(pw, s, name)


def occupy_port():
    """Listen on a free port the same way port_available() probes (dual-stack, else IPv4)."""
    try:
        s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        s.bind(("::", 0))
    except OSError:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("0.0.0.0", 0))
    s.listen(1)
    return s, s.getsockname()[1]


class ServerTestCase(unittest.TestCase):
    CONFIG = None
    SEED = staticmethod(seed_history)

    @classmethod
    def setUpClass(cls):
        cls.srv = ServerProcess(config=cls.CONFIG, seed=cls.SEED).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.stop()

    def get(self, *a, **kw):
        return self.srv.get(*a, **kw)

    def post(self, *a, **kw):
        return self.srv.post(*a, **kw)


class NoAuthTest(ServerTestCase):
    """Auth disabled (default install)."""

    def test_config_defaults(self):
        st, j, r, b = self.get("/api/config")
        self.assertEqual(st, 200)
        self.assertIs(j["config"]["auth_password_set"], False)
        self.assertEqual(j["config"]["ec_fan_probe"], 0)
        self.assertEqual(j["config"]["listen_port"], self.srv.port)
        self.assertEqual(j["config"]["version"], load_server().VERSION)
        assert_no_secret(self, "/api/config", b)

    # ---- CSRF ----
    def test_csrf(self):
        st, j, _, _ = self.srv.request("POST", "/api/ui", {"theme": "dark"}, {"Content-Type": "application/json"})
        self.assertEqual((st, j["error"]), (403, "csrf_rejected"))
        st, *_ = self.srv.request("POST", "/api/ui", '{"theme":"dark"}', {"Content-Type": "text/plain", "X-FNM-Request": "1"})
        self.assertEqual(st, 403)
        # 不比对 Origin：飞牛官方 app（FN Connect 远程 / 局域网模式）与反向代理会改写 Host，
        # Origin 是官方域名、局域网地址或 app 内部来源。跨站伪造由上面两道闸挡住
        cur = self.get("/api/ui")[1]["ui"]
        for origin in ("https://office.app.5ddd.com", "http://8.8.8.8:9", "http://127.0.0.1:9",
                       "http://192.168.1.66:9999", "http://nas.local:1", "null", "app://fnos"):
            st, *_ = self.post("/api/ui", cur, headers={"Origin": origin})
            self.assertEqual(st, 200, origin)
        st, j, _, _ = self.srv.request("POST", "/api/ui", cur, {"Content-Type": "application/json",
                                                                "Origin": "https://office.app.5ddd.com"})
        self.assertEqual((st, j["error"]), (403, "csrf_rejected"))
        self.assertIn("header", j["message"])
        # 两道闸成立的前提：本服务不应答 CORS 预检，跨站页面带不上自定义头
        st, _, r, _ = self.srv.request("OPTIONS", "/api/ui", None, {
            "Origin": "http://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-fnm-request"})
        self.assertGreaterEqual(st, 400)
        self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
        st, *_ = self.srv.request("POST", "/api/auth/setup", '{"password":"abcdefgh"}',
                                  {"Content-Type": "text/plain", "Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        cur = self.get("/api/ui")[1]["ui"]
        for hdr in ({"Origin": "http://%s" % self.srv.authority}, {"Origin": "https://%s" % self.srv.authority}, {}):
            st, j, *_ = self.post("/api/ui", cur, headers=hdr)
            self.assertEqual((st, j["ok"]), (200, True), hdr)
        st, j, *_ = self.post("/api/ui", cur, headers={"Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(st, 200)

    # ---- methods / bodies ----
    def test_get_on_state_changing_endpoints_405(self):
        for p in ("/api/update/install", "/api/update/download", "/api/update/download?to=nas&path=/tmp",
                  "/api/config/save", "/api/auth/setup", "/api/auth/password", "/api/auth/logout", "/api/docker/action",
                  "/api/vm/action"):
            st, j, r, _ = self.get(p)
            self.assertEqual((st, r.getheader("Allow")), (405, "POST"), p)
            self.assertEqual(j["error"], "method_not_allowed")

    def test_body_limits(self):
        s = socket.create_connection((self.srv.host, self.srv.port))
        try:
            s.sendall(("POST /api/ui HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\nX-FNM-Request: 1\r\n"
                       "Content-Length: 2000000\r\n\r\n" % self.srv.authority).encode())
            s.settimeout(5)
            resp = s.recv(4096).decode("latin1", "ignore")
        finally:
            s.close()
        self.assertTrue(resp.startswith("HTTP/1.1 413"), resp.split("\r\n")[0])
        st, j, *_ = self.post("/api/ui", "x" * 1000)
        self.assertEqual((st, j["error"]), (400, "bad_json"))
        st, *_ = self.post("/api/ui", "[1,2]")
        self.assertEqual(st, 400)
        st, *_ = self.srv.request("POST", "/api/ui", None, {"Content-Type": "application/json", "X-FNM-Request": "1",
                                                           "Transfer-Encoding": "chunked"})
        self.assertEqual(st, 411)
        st, *_ = self.post("/api/nope", {})
        self.assertEqual(st, 404)
        st, *_ = self.get("/api/nope")
        self.assertEqual(st, 404)

    # ---- Host policy ----
    def test_host_policy(self):
        p = self.srv.port
        for host, exp in (("evil.example.com", 403), ("evil.example.com:%d" % p, 403), ("192.168.1.5:%d" % p, 200),
                          ("nas", 200), ("nas.local:%d" % p, 200), ("fnos.lan", 200), ("[::1]:%d" % p, 200),
                          ("localhost:%d" % p, 200), ("10.0.0.2", 200), ("foo.home.arpa", 200), ("nas:abc", 403)):
            st, *_ = self.get("/api/overview", host=host)
            self.assertEqual(st, exp, host)
        st, j, *_ = self.get("/api/overview", host="rebind.attacker.com")
        self.assertEqual(j["error"], "host_not_allowed")
        self.assertIn("开启登录口令", j["message"])
        st, j, r, b = self.get("/", host="rebind.attacker.com")
        self.assertEqual(st, 403)
        self.assertIn("text/html", r.getheader("Content-Type"))
        self.assertIn("登录口令".encode(), b)
        st, *_ = self.post("/api/ui", {}, host="rebind.attacker.com")
        self.assertEqual(st, 403)

    # ---- caching / compression / headers ----
    def test_index_gzip_etag_304(self):
        st, j, r, b = self.get("/", headers={"Accept-Encoding": "gzip"})
        et = r.getheader("ETag")
        self.assertEqual(st, 200)
        self.assertEqual(r.getheader("Content-Encoding"), "gzip")
        self.assertEqual(r.getheader("Cache-Control"), "no-cache")
        self.assertTrue(et and et.startswith('W/"'))
        self.assertIn(b"<html", b.lower())
        self.assertEqual(b, read_file(os.path.join(self.srv.pkg, "app", "www", "index.html"), "rb"))
        st, j, r, b = self.get("/", headers={"If-None-Match": et})
        self.assertEqual((st, b), (304, b""))
        self.assertEqual(r.getheader("ETag"), et)
        st, j, r, b = self.get("/?v=123", headers={"If-None-Match": 'W/"stale"'})
        self.assertEqual(st, 200)
        self.assertGreater(len(b), 1000)
        st, j, r, b = self.get("/")
        self.assertIsNone(r.getheader("Content-Encoding"), "no gzip unless accepted")
        st, *_ = self.get("/favicon.ico")
        self.assertEqual(st, 204)
        st, j, r, b = self.get("/login.html")
        self.assertEqual(st, 200)

    def test_security_headers_everywhere(self):
        for p in ("/", "/api/overview", "/api/nope", "/login.html"):
            st, j, r, b = self.get(p)
            with self.subTest(path=p):
                self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
                self.assertEqual(r.getheader("Referrer-Policy"), "same-origin")
                csp = r.getheader("Content-Security-Policy") or ""
                self.assertIn("frame-ancestors *", csp)
                self.assertIn("default-src 'self'", csp)
                self.assertIsNone(r.getheader("X-Frame-Options"), "fnOS desktop embeds the UI in an iframe")
                self.assertIsNone(r.getheader("Access-Control-Allow-Origin"))
                self.assertEqual((r.getheader("Server") or "").strip(), "fnMonitor", "no Python version leak")

    def test_json_gzip_and_cache_headers(self):
        st, j, r, b = self.get("/api/history?metric=cpu&range=24h", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r.getheader("Content-Encoding"), "gzip", "JSON > 1 KiB is gzipped")
        self.assertEqual(r.getheader("Vary"), "Accept-Encoding")
        self.assertGreater(len(j["points"]), 100)
        st, j, r, b = self.get("/api/overview", headers={"Accept-Encoding": "gzip"})
        self.assertIsNotNone(j)
        self.assertEqual(r.getheader("Cache-Control"), "no-store")
        self.assertIn("application/json", r.getheader("Content-Type"))
        assert_no_secret(self, "/api/overview", b)

    # ---- config validation ----
    def test_port_validation(self):
        for pv in (80, 1023, 70000, -5, "abc", None):
            st, j, *_ = self.post("/api/config/save", {"port": pv})
            self.assertEqual((st, j["ok"]), (200, False), pv)
        s, busy = occupy_port()
        try:
            st, j, *_ = self.post("/api/config/save", {"port": busy})
        finally:
            s.close()
        self.assertFalse(j["ok"])
        self.assertIn("占用", j["error"])
        st, j, *_ = self.post("/api/config/save", {"port": self.srv.port})
        self.assertTrue(j["ok"], j)
        st, j, *_ = self.post("/api/config/save", {"port": 0})
        self.assertTrue(j["ok"], j)

    def test_weather_city_sanitized(self):
        try:
            self.post("/api/config/save", {"weather_city": '34.26,117.18,<img src=x onerror=alert(1)>'})
            city = self.get("/api/config")[1]["config"]["weather_city"]
            self.assertNotIn("<", city)
            self.assertNotIn(">", city)
            # 3 comma-separated parts -> rejected locally, no network request needed
            w = self.get("/api/weather")[1]
            self.assertFalse(w["ok"])
            self.assertNotIn("<", json.dumps(w, ensure_ascii=False))
            self.post("/api/config/save", {"weather_city": "x" * 300})
            self.assertLessEqual(len(self.get("/api/config")[1]["config"]["weather_city"]), 64)
        finally:
            self.post("/api/config/save", {"weather_city": ""})

    def test_config_save_clamps(self):
        try:
            st, j, *_ = self.post("/api/config/save", {"interval": 0, "retention_days": 9999, "history_interval": 1,
                                                       "power_rate_yuan": "nan", "ec_fan_probe": "on",
                                                       "data_dir": "/proc/evil"})
            self.assertFalse(j["ok"], "system data_dir rejected, nothing saved")
            st, j, *_ = self.post("/api/config/save", {"interval": 0, "retention_days": 9999, "history_interval": 1,
                                                       "power_rate_yuan": 1e9, "ec_fan_probe": "on"})
            self.assertTrue(j["ok"], j)
            c = self.get("/api/config")[1]["config"]
            self.assertEqual((c["interval"], c["retention_days"], c["history_interval"]), (2, 365, 60))
            self.assertEqual(c["power_rate_yuan"], 500.0)
            self.assertEqual(c["ec_fan_probe"], 1)
            assert_no_secret(self, "config/save", json.dumps(j))
        finally:
            self.post("/api/config/save", {"interval": 10, "retention_days": 7, "history_interval": 60,
                                           "power_rate_yuan": 0.6, "ec_fan_probe": 0})

    def test_ui_roundtrip_and_thresholds(self):
        cur = self.get("/api/ui")[1]["ui"]
        try:
            st, j, *_ = self.post("/api/ui", {"theme": "dark", "accent": "<script>", "thresholds": {"cpuWarn": 5000}})
            self.assertTrue(j["ok"])
            ui = self.get("/api/ui")[1]["ui"]
            self.assertEqual(ui["theme"], "dark")
            self.assertNotIn("accent", ui)
            self.assertEqual(ui["thresholds"]["cpuWarn"], 100)
        finally:
            self.post("/api/ui", cur)

    # ---- exports / reports / data endpoints ----
    def test_exports_and_report(self):
        st, j, r, b = self.get("/api/export?type=status")
        self.assertEqual(st, 200)
        self.assertIsInstance(j["overview"], dict)
        self.assertIn("attachment", r.getheader("Content-Disposition"))
        assert_no_secret(self, "export status", b)
        for t in ("history", "traffic", "power"):
            st, j, r, b = self.get("/api/export?type=%s&range=7d" % t)
            self.assertEqual(st, 200, t)
            self.assertTrue(b.startswith("﻿".encode()), t)
            self.assertEqual(r.getheader("Transfer-Encoding"), "chunked", t)
            self.assertGreater(b.count(b"\n"), 100, "seeded rows exported (%s)" % t)
        st, j, *_ = self.get("/api/report")
        self.assertEqual(st, 200)
        self.assertIn("summary", j)
        self.assertIn("cpu", j["summary"])
        st, j, r, b = self.get("/api/report?format=html")
        self.assertEqual(st, 200)
        self.assertIn("健康报告".encode(), b)
        self.assertNotIn("报告生成失败".encode(), b)

    def test_daily_shape(self):
        st, j, *_ = self.get("/api/daily")
        self.assertEqual(st, 200)
        self.assertEqual(len(j["days"]), 30)
        for k in ("rate", "today", "exclude_bridge", "missing", "archived_until", "power_source"):
            self.assertIn(k, j)
        last = j["days"][-1]
        self.assertEqual(last["day"], j["today"])
        self.assertTrue(last["has_data"], "seeded DB -> today has data")
        self.assertIsInstance(last["kwh"], float)
        self.assertEqual(set(last), {"day", "has_data", "kwh", "cost", "power_h", "avg_w", "rx", "tx", "cpu_avg", "cpu_max"})
        self.assertEqual(len(self.get("/api/daily?days=7")[1]["days"]), 7)
        self.assertEqual(len(self.get("/api/daily?days=abc")[1]["days"]), 30)

    def test_gpu_diag_shape(self):
        st, j, *_ = self.get("/api/gpu?diag=1")
        self.assertEqual(st, 200)
        self.assertIsInstance(j["gpu"], list)
        d = j["diag"]
        self.assertNotIn("error", d, d)
        for k in ("kernel", "pci", "event_sources", "perf_event_paranoid", "perf_syscall", "drm_cards", "pmu",
                  "rc6_path", "freq_mhz_sysfs", "intel_gpu_found", "version"):
            self.assertIn(k, d)
        self.assertEqual(d["version"], load_server().VERSION)
        self.assertNotIn("diag", self.get("/api/gpu")[1])

    def test_history_endpoint(self):
        j = self.get("/api/history?metric=cpu&range=24h")[1]
        self.assertGreater(len(j["points"]), 100)
        self.assertLessEqual(len(j["points"]), 725)
        self.assertEqual(self.get("/api/history?metric=%00x")[1]["points"], [])

    def test_auth_endpoints_when_off(self):
        j = self.get("/api/auth/config")[1]
        self.assertEqual(j, {"auth_enabled": 0, "requires_password": False, "setup_mode": True,
                             "password_min_len": load_server().PASSWORD_MIN_LEN})
        self.assertEqual(self.get("/api/auth/session")[1], {"ok": True, "logged_in": True, "auth_required": False})
        st, j, *_ = self.post("/api/auth/password", {"password": "whatever"})
        self.assertFalse(j["ok"])

    def test_no_secrets_without_auth(self):
        for p in SAFE_GETS:
            st, j, r, b = self.get(p)
            with self.subTest(path=p):
                self.assertIn(st, (200,), p)
                assert_no_secret(self, p, b)


class AuthFlowTest(ServerTestCase):
    """setup -> secrets on disk -> cookie gate -> login -> password change -> logout (one ordered flow)."""

    def test_flow(self):
        S = self.srv
        st, j, *_ = self.post("/api/auth/setup", {"password": "12345"})
        self.assertFalse(j["ok"], "short password rejected")
        st, j, r, _ = self.post("/api/auth/setup", {"password": PW1})
        sidA = cookie_of(r)
        self.assertTrue(j["ok"] and sidA, j)
        sc = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", sc)
        self.assertIn("SameSite=Lax", sc)
        self.assertFalse(self.post("/api/auth/setup", {"password": "another-pass"})[1]["ok"], "setup only once")

        cfg_path = os.path.join(S.data, "config.json")
        raw = read_file(cfg_path)
        self.assertIn("pbkdf2_sha256$", raw)
        self.assertNotIn(PW1, raw)
        self.assertNotIn('"auth_password"', raw)
        sess = os.path.join(S.data, "sessions.json")
        self.assertTrue(os.path.exists(sess))
        self.assertNotIn(sidA, read_file(sess), "only sid hashes on disk")
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(cfg_path).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.stat(sess).st_mode), 0o600)
        self.assertFalse(os.path.exists(os.path.join(S.localdata, "sessions.json")))

        # gate: no cookie -> 401 for API, login page for /
        st, j, *_ = self.get("/api/overview")
        self.assertEqual(st, 401)
        self.assertTrue(j["auth_required"])
        st, j, r, b = self.get("/")
        self.assertEqual(st, 200)
        self.assertIn(b"<html", b.lower())
        self.assertEqual(b, read_file(os.path.join(S.pkg, "app", "www", "login.html"), "rb"))
        self.assertEqual(self.post("/api/ui", {"theme": "x"})[0], 401)
        self.assertEqual(self.get("/api/auth/config")[1]["setup_mode"], False)
        self.assertEqual(self.get("/api/auth/session")[1]["logged_in"], False)
        self.assertEqual(self.get("/api/auth/session", cookie=sidA)[1]["logged_in"], True)
        self.assertEqual(self.get("/api/overview", cookie="forged")[0], 401)
        # any Host allowed once auth is on, but cookie still required
        self.assertEqual(self.get("/api/overview", host="nas.example.com")[0], 401)
        self.assertEqual(self.get("/api/overview", host="nas.example.com", cookie=sidA)[0], 200)

        # no secrets anywhere (authenticated sweep)
        for p in SAFE_GETS:
            st, j, r, b = self.get(p, cookie=sidA)
            with self.subTest(path=p):
                self.assertEqual(st, 200, p)
                assert_no_secret(self, p, b, PW1)
        st, j, r, b = self.post("/api/config/save", {"interval": 10}, cookie=sidA)
        assert_no_secret(self, "config/save", b, PW1)
        self.assertIs(self.get("/api/config", cookie=sidA)[1]["config"]["auth_password_set"], True)

        # login + password change
        st, j, r, _ = self.post("/api/auth/password", {"password": PW1})
        sidB = cookie_of(r)
        self.assertTrue(j["ok"] and sidB)
        st, j, *_ = self.post("/api/config/save", {"auth_password": PW2}, cookie=sidA)
        self.assertEqual((st, j["error"]), (403, "old_password_invalid"))
        st, j, *_ = self.post("/api/config/save", {"auth_password": PW2, "old_password": "wrong-old"}, cookie=sidA)
        self.assertEqual((st, j["error"]), (403, "old_password_invalid"))
        st, j, *_ = self.post("/api/config/save", {"auth_password": "short", "old_password": PW1}, cookie=sidA)
        self.assertFalse(j["ok"])
        st, j, r, b = self.post("/api/config/save", {"auth_password": PW2, "old_password": PW1}, cookie=sidA)
        self.assertTrue(st == 200 and j["ok"] and j["password_changed"], j)
        assert_no_secret(self, "password change response", b, PW2)
        self.assertEqual(self.get("/api/overview", cookie=sidA)[0], 200, "caller session kept")
        self.assertEqual(self.get("/api/overview", cookie=sidB)[0], 401, "other sessions revoked")
        self.assertFalse(self.post("/api/auth/password", {"password": PW1})[1]["ok"])
        st, j, r, _ = self.post("/api/auth/password", {"password": PW2})
        self.assertTrue(j["ok"] and cookie_of(r))
        self.assertNotIn(PW2, read_file(cfg_path))
        st, j, r, _ = self.post("/api/auth/logout", {}, cookie=sidA)
        self.assertTrue(j["ok"])
        self.assertIn("Max-Age=0", r.getheader("Set-Cookie"))
        self.assertEqual(self.get("/api/overview", cookie=sidA)[0], 401)


class LegacyPlaintextMigrationTest(ServerTestCase):
    CONFIG = {"auth_enabled": 1, "auth_password": "legacy-pass-1"}

    def test_migrated_on_start(self):
        raw = read_file(os.path.join(self.srv.data, "config.json"))
        self.assertNotIn("legacy-pass-1", raw)
        self.assertIn("pbkdf2_sha256$", raw)
        self.assertEqual(self.get("/api/overview")[0], 401)
        st, j, r, _ = self.post("/api/auth/password", {"password": "legacy-pass-1"})
        self.assertTrue(j["ok"])
        self.assertEqual(self.get("/api/overview", cookie=cookie_of(r))[0], 200)


class LockoutTest(ServerTestCase):
    CONFIG = {"auth_enabled": 1}

    @classmethod
    def setUpClass(cls):
        srv = load_server()
        cls.CONFIG = dict(cls.CONFIG, auth_password_hash=srv.hash_password(PW2))
        super().setUpClass()

    def test_lockout(self):
        max_fails = load_server().LoginLimiter.MAX_FAILS
        codes = [self.post("/api/auth/password", {"password": "wrong-%d" % i})[0] for i in range(max_fails + 2)]
        self.assertEqual(codes[:max_fails], [200] * max_fails, codes)
        self.assertEqual(codes[max_fails:], [429, 429], codes)
        st, j, r, _ = self.post("/api/auth/password", {"password": PW2})
        self.assertEqual(st, 429, "locked even with the right password")
        self.assertGreater(j.get("retry_after", 0), 0)
        self.assertEqual(r.getheader("Retry-After"), str(j["retry_after"]))
        self.assertIsNone(cookie_of(r))
        st, *_ = self.post("/api/config/save", {"auth_password": "zzzzzzzz", "old_password": "x"})
        self.assertEqual(st, 401, "unauthenticated config/save")


class KeepAliveTest(ServerTestCase):
    SEED = None

    def test_nagle_disabled_on_handler(self):
        srv = load_server()
        import tempfile
        d = tempfile.mkdtemp(prefix="fnm-h-")
        try:
            h = srv.MonitorApp(d, dict(srv.DEFAULT_CONFIG), cfg_dir=d).make_handler()
            self.assertEqual(h.protocol_version, "HTTP/1.1")
            self.assertTrue(getattr(h, "disable_nagle_algorithm", False),
                            "keep-alive + Nagle + delayed ACK = ~40 ms per small response")
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_keepalive_latency(self):
        """20 sequential small GETs on one keep-alive connection. With Nagle enabled Linux adds
        ~40 ms per response (headers and body are separate writes); expect well under that."""
        for path in ("/api/auth/config", "/api/config"):
            c = http.client.HTTPConnection(self.srv.host, self.srv.port, timeout=10)
            try:
                self.srv.request("GET", path, conn=c)          # warm-up
                times = []
                for _ in range(20):
                    t0 = time.perf_counter()
                    st, *_ = self.srv.request("GET", path, conn=c)
                    times.append((time.perf_counter() - t0) * 1000)
                    self.assertEqual(st, 200)
            finally:
                c.close()
            med = statistics.median(times)
            self.assertLess(med, 15, "%s median %.1f ms (Nagle?) all=%s" % (path, med, [round(x, 1) for x in times]))
            self.assertLess(sum(times), 20 * 40 / 2, path)

    def test_per_ip_connection_cap(self):
        cap = load_server().BoundedHTTPServerMixin.MAX_PER_IP
        socks, busy = [], 0
        try:
            for _ in range(cap + 6):
                socks.append(socket.create_connection((self.srv.host, self.srv.port)))
            # rejected connections get an immediate 503 + close; accepted ones stay silent
            deadline = time.time() + 2
            pending = list(socks)
            while pending and time.time() < deadline:
                ready, _, _ = select.select(pending, [], [], 0.3)
                for s in ready:
                    pending.remove(s)
                    if s.recv(200).startswith(b"HTTP/1.1 503"):
                        busy += 1
                if busy >= 6 and not ready:
                    break
        finally:
            for s in socks:
                s.close()
        self.assertEqual(busy, 6)
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                if self.get("/api/auth/config")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(0.2)
        self.assertEqual(self.get("/api/auth/config")[0], 200, "recovers after release")

    @unittest.skipUnless(SLOW, "set FNM_SLOW=1 (waits ~30 s)")
    def test_idle_keepalive_closed(self):
        s = socket.create_connection((self.srv.host, self.srv.port))
        t0 = time.time()
        s.settimeout(40)
        try:
            d = s.recv(10)
        except Exception:
            d = b"x"
        finally:
            s.close()
        self.assertEqual(d, b"")
        self.assertTrue(28 <= time.time() - t0 <= 35)


if __name__ == "__main__":
    unittest.main()
