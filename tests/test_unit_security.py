"""Unit tests: password hashing / migration, config secrets, Host policy helpers, sanitizers,
thresholds, login rate limiting, data-dir validation, weather sanitizing (network mocked)."""
import io
import json
import os
import shutil
import stat
import tempfile
import unittest
from unittest import mock

from fnm_support import IS_LINUX, load_server, read_file

srv = load_server()


class PasswordHashTest(unittest.TestCase):
    def test_hash_format_and_verify(self):
        h = srv.hash_password("secret-one-123", iterations=1000)
        algo, iters, salt, dk = h.split("$")
        self.assertEqual(algo, "pbkdf2_sha256")
        self.assertEqual(iters, "1000")
        self.assertTrue(srv.verify_password("secret-one-123", h))
        self.assertFalse(srv.verify_password("secret-one-124", h))
        self.assertFalse(srv.verify_password("", h))

    def test_default_iterations(self):
        self.assertGreaterEqual(srv.PBKDF2_ITERATIONS, 200000)
        h = srv.hash_password("x" * 8)
        self.assertEqual(int(h.split("$")[1]), srv.PBKDF2_ITERATIONS)

    def test_salted(self):
        self.assertNotEqual(srv.hash_password("same", 1000), srv.hash_password("same", 1000))

    def test_malformed_hashes_rejected(self):
        good = srv.hash_password("pw", 1000)
        for bad in ("", None, "plain", "md5$1000$a$b", "pbkdf2_sha256$999$a$b",
                    "pbkdf2_sha256$99999999$a$b", "pbkdf2_sha256$abc$a$b", "pbkdf2_sha256$1000$!!$??",
                    good.replace("pbkdf2_sha256", "pbkdf2_sha1")):
            self.assertFalse(srv.verify_password("pw", bad), bad)


class ConfigSecretsTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="fnm-cfg-")
        self.path = os.path.join(self.d, "config.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write(self, obj, mode=0o644):
        with open(self.path, "w") as f:
            json.dump(obj, f)
        os.chmod(self.path, mode)

    def test_public_config_strips_secrets(self):
        cfg = dict(srv.DEFAULT_CONFIG, auth_password_hash="pbkdf2_sha256$1$a$b", auth_password="x")
        pub = srv.public_config(cfg)
        self.assertNotIn("auth_password_hash", pub)
        self.assertNotIn("auth_password", pub)
        self.assertTrue(pub["auth_password_set"])
        self.assertFalse(srv.public_config(srv.DEFAULT_CONFIG)["auth_password_set"])

    def test_legacy_plaintext_migrated_to_hash(self):
        self._write({"auth_password": "legacy-pass", "auth_enabled": 1, "interval": 5})
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            cfg = srv.load_config(self.d)
        self.assertTrue(cfg["auth_password_hash"].startswith("pbkdf2_sha256$"))
        self.assertTrue(srv.verify_password("legacy-pass", cfg["auth_password_hash"]))
        self.assertEqual(cfg["interval"], 5)
        raw = read_file(self.path)
        self.assertNotIn("legacy-pass", raw)
        self.assertNotIn('"auth_password"', raw)
        self.assertIn("pbkdf2_sha256$", raw)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        # second load: stable, no re-hash
        cfg2 = srv.load_config(self.d)
        self.assertEqual(cfg2["auth_password_hash"], cfg["auth_password_hash"])

    def test_existing_hash_wins_over_plaintext(self):
        h = srv.hash_password("real-pass", 1000)
        self._write({"auth_password": "stale-plain", "auth_password_hash": h})
        cfg = srv.load_config(self.d)
        self.assertEqual(cfg["auth_password_hash"], h)
        self.assertNotIn("stale-plain", read_file(self.path))

    def test_non_pbkdf2_hash_ignored(self):
        self._write({"auth_password_hash": "md5$deadbeef"})
        self.assertEqual(srv.load_config(self.d)["auth_password_hash"], "")

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_world_readable_config_tightened(self):
        self._write({"interval": 10}, mode=0o644)
        srv.load_config(self.d)
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_per_field_tolerance(self):
        self._write({"interval": "bad", "retention_days": 3, "auth_enabled": "on", "power_rate_yuan": "x",
                     "weather_city": "<b>Xuzhou</b>", "auth_session_days": 9999, "open_mode": "IFRAME"})
        cfg = srv.load_config(self.d)
        self.assertEqual(cfg["interval"], srv.DEFAULT_CONFIG["interval"])
        self.assertEqual(cfg["retention_days"], 3)
        self.assertEqual(cfg["auth_enabled"], 1)
        self.assertEqual(cfg["power_rate_yuan"], srv.DEFAULT_CONFIG["power_rate_yuan"])
        self.assertNotIn("<", cfg["weather_city"])
        self.assertEqual(cfg["auth_session_days"], 365)
        self.assertEqual(cfg["open_mode"], "iframe")

    def test_corrupt_config_falls_back_to_defaults(self):
        with open(self.path, "w") as f:
            f.write("{not json")
        self.assertEqual(srv.load_config(self.d), srv.DEFAULT_CONFIG)

    @unittest.skipUnless(os.name == "posix", "POSIX permissions")
    def test_write_json_secure_mode_and_atomic(self):
        p = os.path.join(self.d, "sub", "x.json")
        srv.write_json_secure(p, {"a": "中文"})
        self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual(json.loads(read_file(p)), {"a": "中文"})
        self.assertEqual([f for f in os.listdir(os.path.dirname(p)) if ".tmp." in f], [])


class HostPolicyHelpersTest(unittest.TestCase):
    def test_split_host_port(self):
        cases = {
            "": ("", None), "NAS:8777": ("nas", 8777), "nas.": ("nas", None), "[::1]:8777": ("::1", 8777),
            "[fe80::1%eth0]": ("fe80::1%eth0", None), "::1": ("::1", None), "1.2.3.4:x": ("1.2.3.4", -1),
            "[::1": ("[::1", None),
        }
        for h, want in cases.items():
            self.assertEqual(srv.split_host_port(h), want, h)

    def test_host_is_lan(self):
        allow = ["", "192.168.1.5:8777", "10.0.0.2", "nas", "NAS:8777", "nas.local:8777", "fnos.lan",
                 "[::1]:8777", "localhost:8777", "foo.home.arpa", "x.internal", "box.localdomain", "a.localhost"]
        deny = ["evil.example.com", "evil.example.com:8777", "rebind.attacker.com", "nas.example.com",
                "192.168.1.5:abc", "local", "x.local.evil.com"]
        for h in allow:
            self.assertTrue(srv.host_is_lan(h), h)
        for h in deny:
            if h == "local":       # single label: allowed by design
                self.assertTrue(srv.host_is_lan(h))
                continue
            self.assertFalse(srv.host_is_lan(h), h)

    def test_norm_ip(self):
        self.assertEqual(srv.norm_ip("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertEqual(srv.norm_ip("fe80::1%eth0"), "fe80::1")
        self.assertEqual(srv.norm_ip("127.0.0.1"), "127.0.0.1")
        self.assertEqual(srv.norm_ip("garbage"), "garbage")
        self.assertEqual(srv.norm_ip(None), "")


class SanitizerTest(unittest.TestCase):
    def test_clean_text(self):
        self.assertEqual(srv.clean_text('<img src=x onerror="alert(1)">'), "img src=x onerror=alert(1)")
        self.assertEqual(srv.clean_text("a\x00b\x1fc\x7f  d \n e"), "abc d e")
        self.assertEqual(len(srv.clean_text("x" * 300)), 64)
        self.assertEqual(len(srv.clean_text("x" * 300, 10)), 10)
        self.assertEqual(srv.clean_text(None), "")
        for ch in "<>\"'&`\\":
            self.assertNotIn(ch, srv.clean_text("a%sb" % ch))

    def test_parse_coords(self):
        self.assertEqual(srv.parse_coords("34.26,117.18"), (34.26, 117.18))
        self.assertEqual(srv.parse_coords(" -90 , 180 "), (-90.0, 180.0))
        for bad in ("", "1", "1,2,3", "91,0", "0,181", "nan,1", "inf,1", "a,b", None,
                    "34.26,117.18,<img src=x>"):
            self.assertIsNone(srv.parse_coords(bad), bad)

    def test_thresholds(self):
        d = srv.sanitize_thresholds({})
        self.assertEqual(d, {k: v[0] for k, v in srv.THRESHOLD_SPEC.items()})
        self.assertEqual(srv.sanitize_thresholds(None), d)
        t = srv.sanitize_thresholds({"cpuWarn": 999, "cpuCrit": 10, "memWarn": "abc", "tempWarn": float("nan"),
                                     "capWarn": 87.25, "diskTempWarn": "55", "bogus": 1})
        self.assertEqual(t["cpuWarn"], 100)
        self.assertEqual(t["cpuCrit"], 100)          # crit >= warn after clamping
        self.assertEqual(t["memWarn"], 85)
        self.assertEqual(t["tempWarn"], 80)
        self.assertEqual(t["capWarn"], 87.2)         # one decimal place (round half to even)
        self.assertEqual(t["diskTempWarn"], 55)
        self.assertNotIn("bogus", t)
        for k, (dv, lo, hi) in srv.THRESHOLD_SPEC.items():
            self.assertTrue(lo <= t[k] <= hi, k)

    def test_validate_data_dir(self):
        self.assertFalse(srv.validate_data_dir("relative/path")[0])
        self.assertFalse(srv.validate_data_dir("/")[0])
        for p in ("/proc/x", "/sys", "/dev/shm/x", "/usr/lib/x", "/boot"):
            self.assertFalse(srv.validate_data_dir(p)[0], p)
        if IS_LINUX:   # on macOS /etc and /var resolve to /private/...
            for p in ("/etc/fnm", "/var/apps/x", "/run/x"):
                self.assertFalse(srv.validate_data_dir(p)[0], p)
        self.assertTrue(srv.validate_data_dir("/vol1/1000/fnmonitor")[0])


class LoginLimiterTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000000.0]
        self.p = mock.patch.object(srv.time, "time", lambda: self.now[0])
        self.p.start()
        self.lim = srv.LoginLimiter()

    def tearDown(self):
        self.p.stop()

    def test_lock_after_max_fails_and_backoff(self):
        ip = "192.168.1.9"
        for _ in range(srv.LoginLimiter.MAX_FAILS - 1):
            self.lim.fail(ip)
        self.assertEqual(self.lim.retry_after(ip), 0)
        self.lim.fail(ip)
        self.assertEqual(self.lim.retry_after(ip), srv.LoginLimiter.BASE_LOCK)
        self.assertEqual(self.lim.retry_after("192.168.1.10"), 0, "other IPs unaffected")
        self.now[0] += srv.LoginLimiter.BASE_LOCK + 1
        self.assertEqual(self.lim.retry_after(ip), 0)
        for _ in range(srv.LoginLimiter.MAX_FAILS):
            self.lim.fail(ip)
        self.assertEqual(self.lim.retry_after(ip), 2 * srv.LoginLimiter.BASE_LOCK, "doubles on relock")
        for _ in range(10):
            self.now[0] += srv.LoginLimiter.MAX_LOCK + 1
            for _ in range(srv.LoginLimiter.MAX_FAILS):
                self.lim.fail(ip)
        self.assertLessEqual(self.lim.retry_after(ip), srv.LoginLimiter.MAX_LOCK)

    def test_success_resets(self):
        ip = "10.0.0.1"
        for _ in range(4):
            self.lim.fail(ip)
        self.lim.success(ip)
        self.lim.fail(ip)
        self.assertEqual(self.lim.retry_after(ip), 0)

    def test_window_expiry(self):
        ip = "10.0.0.2"
        for _ in range(4):
            self.lim.fail(ip)
        self.now[0] += srv.LoginLimiter.WINDOW + 1
        self.lim.fail(ip)
        self.assertEqual(self.lim.retry_after(ip), 0)

    def test_global_lock(self):
        for i in range(srv.LoginLimiter.GLOBAL_MAX):
            self.lim.fail("172.16.0.%d" % i)
        self.assertEqual(self.lim.retry_after("172.16.9.9"), srv.LoginLimiter.GLOBAL_LOCK)

    def test_memory_bound(self):
        for i in range(5000):
            self.lim.fail("10.%d.%d.1" % (i // 256, i % 256))
            self.now[0] += 0.001
        self.assertLessEqual(len(self.lim._ips), 4097)


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class WeatherSanitizeTest(unittest.TestCase):
    """fetch_weather with urllib mocked: never echoes raw input, sanitizes remote names."""

    FORECAST = {"current": {"temperature_2m": 21.34, "apparent_temperature": 20.0, "relative_humidity_2m": 50,
                            "weather_code": 1, "wind_speed_10m": 3.2, "is_day": 1}}

    def _run(self, city, routes):
        calls = []

        def fake(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            for frag, payload in routes.items():
                if frag in url:
                    return _FakeResp(json.dumps(payload).encode())
            raise OSError("network disabled in tests: %s" % url)

        with mock.patch("urllib.request.urlopen", fake):
            return srv.fetch_weather(city), calls

    def test_coords_with_payload_rejected_without_network(self):
        w, calls = self._run(srv.clean_text("34.26,117.18,<img src=x onerror=alert(1)>"), {})
        self.assertFalse(w["ok"])
        self.assertEqual(calls, [])
        self.assertNotIn("<", json.dumps(w, ensure_ascii=False))

    def test_coords_normalized(self):
        w, calls = self._run("34.26,117.18", {"api.open-meteo.com/v1/forecast": self.FORECAST})
        self.assertTrue(w["ok"], w)
        self.assertEqual(w["city"], "34.2600,117.1800")
        self.assertEqual(w["temp"], 21.3)
        self.assertEqual(len(calls), 1)

    def test_out_of_range_coords(self):
        w, calls = self._run("100,200", {})
        self.assertFalse(w["ok"])
        self.assertEqual(calls, [])

    def test_geocode_name_sanitized(self):
        w, calls = self._run("Zzyzxville", {
            "geocoding-api": {"results": [{"latitude": 1.0, "longitude": 2.0, "name": '<script>alert(1)</script>"X'}]},
            "api.open-meteo.com/v1/forecast": self.FORECAST})
        self.assertTrue(w["ok"], w)
        for ch in "<>\"'":
            self.assertNotIn(ch, w["city"])

    def test_unknown_city_error_is_sanitized(self):
        w, _ = self._run("<b>nowhere</b>", {"geocoding-api": {"results": []}})
        self.assertFalse(w["ok"])
        self.assertNotIn("<", w["error"])

    def test_ip_geolocation_name_sanitized(self):
        w, calls = self._run("", {
            "ipwho.is": {"success": True, "latitude": 31.2, "longitude": 121.5, "city": "<img src=x>Shanghai"},
            "api.open-meteo.com/v1/forecast": self.FORECAST})
        self.assertTrue(w["ok"], w)
        self.assertNotIn("<", w["city"])

    def test_ip_geolocation_bad_coords(self):
        w, _ = self._run("", {"ipwho.is": {"success": True, "latitude": 999, "longitude": 0, "city": "x"},
                              "ip-api.com": {"lat": None}})
        self.assertFalse(w["ok"])

    def test_builtin_city_lookup(self):
        if not os.path.exists(os.path.join(os.path.dirname(srv.__file__), "cities_cn.json")):
            self.skipTest("cities_cn.json not present")
        w, calls = self._run("徐州", {"api.open-meteo.com/v1/forecast": self.FORECAST})
        self.assertTrue(w["ok"], w)
        self.assertFalse(any("geocoding" in c for c in calls), "built-in DB hit must not geocode")


if __name__ == "__main__":
    unittest.main()
