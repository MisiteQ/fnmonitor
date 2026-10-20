"""iDRAC（Redfish）单元测试：配置读写、地址校验、风扇/温度解析、SEL 解析与端点回退。

全程 mock，不联网、不碰真机。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

import server as srv  # noqa: E402


SYS_JSON = {
    "Model": "PowerEdge R730xd", "SKU": "CJ0SWG2", "BiosVersion": "2.19.0",
    "PowerState": "On",
    "Status": {"Health": "OK", "State": "Enabled"},
    "MemorySummary": {"TotalSystemMemoryGiB": 256},
    "ProcessorSummary": {"Count": 2},
}

THERMAL_JSON = {
    "Fans": [
        {"Name": "System Board Fan1A", "Reading": 4920, "ReadingUnits": "RPM",
         "Status": {"Health": "OK", "State": "Enabled"}, "LowerThresholdCritical": 720},
        {"Name": "System Board Fan2A", "Reading": 300, "ReadingUnits": "RPM",
         "Status": {"Health": "Critical", "State": "Enabled"}, "LowerThresholdCritical": 720},
        {"Name": "System Board Fan10A", "Reading": 4800, "ReadingUnits": "RPM",
         "Status": {"Health": "OK", "State": "Enabled"}},
        {"Name": "System Board Fan3A", "Status": {"Health": "OK"}},   # 没有 Reading：跳过
    ],
    "Temperatures": [
        {"Name": "System Board Inlet Temp", "ReadingCelsius": 22,
         "Status": {"Health": "OK"}, "UpperThresholdCritical": 42},
        {"Name": "CPU1 Temp", "ReadingCelsius": 55, "Status": {"Health": "OK"}},
        {"Name": "Absent Sensor", "Status": {"Health": "OK"}},
    ],
}

POWER_JSON = {
    "PowerControl": [{"PowerConsumedWatts": 210}],
    "PowerSupplies": [
        {"Name": "Power Supply 1", "Status": {"Health": "OK"}, "LastPowerOutputWatts": 110},
    ],
}

SEL_JSON = {"Members@odata.count": 3, "Members": [
    {"Id": "12", "Created": "2026-10-20T10:00:00-05:00", "Severity": "Critical",
     "Message": "Fan 2A RPM is less than the lower critical threshold.",
     "EntryCode": [{"Member": "Assert"}], "SensorType": "Fan"},
    {"Id": "11", "Created": "2026-10-19T09:00:00-05:00", "Severity": "OK",
     "Message": "Fan 2A RPM is within range.", "EntryCode": [{"Member": "Deassert"}]},
    {"Id": "10", "Created": "2026-10-18T09:00:00-05:00", "Severity": "Warning",
     "Message": "Power supply redundancy is lost.", "EntryCode": [{"Member": "Assert"}]},
]}

FULL = {
    "/Systems/System.Embedded.1": SYS_JSON,
    "/Chassis/System.Embedded.1/Thermal": THERMAL_JSON,
    "/Chassis/System.Embedded.1/Power": POWER_JSON,
    "/Managers/iDRAC.Embedded.1/Logs/Sel": SEL_JSON,
}


def fake_req(mapping, calls=None):
    """按路径返回预设 JSON，未命中按 404 处理（模拟固件不支持）。"""
    def f(cfg, path, timeout=None):
        if calls is not None:
            calls.append(path)
        if path in mapping:
            return mapping[path], ""
        return None, "该机型/固件不支持此接口（HTTP 404）"
    return f


class IdracCfgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        cfg = srv.idrac_load(self.dir)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["host"], "")
        self.assertEqual(cfg["sel_alert"], 1)
        self.assertEqual(cfg["verify_tls"], 0)

    def test_save_and_public_masks_password(self):
        cfg, err = srv.idrac_save(self.dir, {"host": "192.168.124.121", "user": "root",
                                             "password": "calvin", "enabled": 1})
        self.assertEqual(err, "")
        pub = srv.idrac_public(cfg)
        self.assertNotIn("password", pub)
        self.assertTrue(pub["password_set"])
        self.assertEqual(pub["host"], "192.168.124.121")

    def test_empty_password_keeps_old(self):
        srv.idrac_save(self.dir, {"host": "h", "user": "u", "password": "pw"})
        cfg, err = srv.idrac_save(self.dir, {"host": "h2", "password": ""})
        self.assertEqual(err, "")
        self.assertEqual(cfg["password"], "pw")
        self.assertEqual(cfg["host"], "h2")

    def test_clear_password(self):
        srv.idrac_save(self.dir, {"host": "h", "user": "u", "password": "pw"})
        cfg, _ = srv.idrac_save(self.dir, {"clear_password": 1})
        self.assertEqual(cfg["password"], "")

    def test_strips_scheme_and_path(self):
        cfg, _ = srv.idrac_save(self.dir, {"host": "https://192.168.1.5/redfish/v1"})
        self.assertEqual(cfg["host"], "192.168.1.5")

    def test_enabled_requires_fields(self):
        _, err = srv.idrac_save(self.dir, {"enabled": 1})
        self.assertIn("地址", err)
        _, err = srv.idrac_save(self.dir, {"enabled": 1, "host": "h"})
        self.assertIn("用户名", err)
        _, err = srv.idrac_save(self.dir, {"enabled": 1, "host": "h", "user": "u"})
        self.assertIn("密码", err)
        # 已有密码时不应再要求重填
        srv.idrac_save(self.dir, {"host": "h", "user": "u", "password": "pw"})
        _, err = srv.idrac_save(self.dir, {"enabled": 1})
        self.assertEqual(err, "")

    def test_bad_host_rejected(self):
        cfg = {"host": "http://x/y", "user": "u", "password": "p"}
        _, err = srv._idrac_request(cfg, "")
        self.assertIn("格式不对", err)

    def test_empty_host_rejected(self):
        _, err = srv._idrac_request({"host": ""}, "")
        self.assertIn("未配置", err)


class IdracCollectTest(unittest.TestCase):
    def setUp(self):
        srv._IDRAC_CACHE.clear()
        self.cfg = {"host": "192.168.124.121", "user": "root", "password": "pw",
                    "verify_tls": 0, "enabled": 1}

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_parses_overview(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            d = srv.idrac_collect(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertEqual(d["model"], "PowerEdge R730xd")
        self.assertEqual(d["service_tag"], "CJ0SWG2")
        self.assertEqual(d["health"], "OK")
        self.assertEqual(d["power_watts"], 210)
        self.assertEqual(d["mem_gib"], 256)

    def test_fans_parsed_and_sorted(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            d = srv.idrac_collect(self.cfg, force=True)
        names = [f["name"] for f in d["fans"]]
        # "System Board " 前缀去掉，且按数字自然排序（Fan10 排在 Fan2 之后）
        self.assertEqual(names, ["Fan1A", "Fan2A", "Fan10A"])
        self.assertEqual(d["fan_summary"]["count"], 3)
        self.assertEqual(d["fan_summary"]["min"], 300)
        self.assertEqual(d["fan_summary"]["max"], 4920)
        self.assertEqual(d["fan_summary"]["bad"], 1)      # Fan2A 是 Critical

    def test_zero_rpm_kept_and_missing_reading_skipped(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            d = srv.idrac_collect(self.cfg, force=True)
        self.assertNotIn("Fan3A", [f["name"] for f in d["fans"]])
        for f in d["fans"]:
            self.assertIsInstance(f["rpm"], int)

    def test_temps_skip_absent(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            d = srv.idrac_collect(self.cfg, force=True)
        names = [t["name"] for t in d["temps"]]
        self.assertIn("CPU1 Temp", names)
        self.assertNotIn("Absent Sensor", names)
        inlet = [t for t in d["temps"] if "Inlet" in t["name"]][0]
        self.assertEqual(inlet["c"], 22)
        self.assertEqual(inlet["crit"], 42)

    def test_power_error_is_not_fatal(self):
        only = {k: v for k, v in FULL.items() if "Power" not in k}
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(only)):
            d = srv.idrac_collect(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertNotIn("power_watts", d)

    def test_system_error_returns_not_ok(self):
        with mock.patch.object(srv, "_idrac_request",
                               return_value=(None, "认证失败（用户名或密码错误）")):
            d = srv.idrac_collect(self.cfg, force=True)
        self.assertFalse(d["ok"])
        self.assertIn("认证失败", d["error"])

    def test_cache_reused_within_ttl(self):
        calls = []
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL, calls)):
            srv.idrac_collect(self.cfg, force=True)
            n1 = len(calls)
            d2 = srv.idrac_collect(self.cfg)
        self.assertEqual(len(calls), n1)                  # 第二次全部走缓存
        self.assertTrue(d2.get("cached"))


class IdracSelTest(unittest.TestCase):
    def setUp(self):
        srv._IDRAC_CACHE.clear()
        self.cfg = {"host": "h", "user": "u", "password": "p"}

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_parse_and_active_only(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            r = srv.idrac_sel(self.cfg)
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 3)
        # Deassert 的 OK 条目不算活跃；Assert 的 Critical / Warning 算
        self.assertEqual(len(r["active"]), 2)
        self.assertTrue(all(a["assert"] for a in r["active"]))
        self.assertEqual(r["entries"][0]["id"], "12")     # 按时间倒序

    def test_falls_back_to_logservices(self):
        mapping = {"/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries": SEL_JSON}
        calls = []
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(mapping, calls)):
            r = srv.idrac_sel(self.cfg)
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries")
        self.assertEqual(len(calls), 1)                   # 第一个就通了，不再试第二个

    def test_both_paths_fail(self):
        with mock.patch.object(srv, "_idrac_request",
                               return_value=(None, "该机型/固件不支持此接口（HTTP 404）")):
            r = srv.idrac_sel(self.cfg)
        self.assertFalse(r["ok"])
        self.assertEqual(r["entries"], [])
        self.assertIn("404", r["error"])

    def test_members_without_detail_are_fetched(self):
        listing = {"Members": [{"@odata.id": "/redfish/v1/Managers/iDRAC.Embedded.1/Logs/Sel/9"}]}
        mapping = {"/Managers/iDRAC.Embedded.1/Logs/Sel": listing,
                   "/Managers/iDRAC.Embedded.1/Logs/Sel/9": SEL_JSON["Members"][0]}
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(mapping)):
            r = srv.idrac_sel(self.cfg)
        self.assertTrue(r["ok"])
        self.assertEqual(len(r["entries"]), 1)
        self.assertIn("Fan 2A", r["entries"][0]["message"])

    def test_entry_code_dict_form(self):
        e = srv._sel_entry({"Id": "1", "Severity": "Critical", "Message": "x",
                            "EntryCode": {"Member": "Assert"}})
        self.assertTrue(e["assert"])
        self.assertEqual(e["code"], "Assert")


class IdracErrTest(unittest.TestCase):
    def test_translate(self):
        case = lambda s: srv.idrac_short_err(Exception(s))
        self.assertIn("超时", case("timed out"))
        self.assertIn("解析", case("Name or service not known"))
        self.assertIn("拒绝连接", case("Connection refused"))
        self.assertIn("TLS", case("SSL: CERTIFICATE_VERIFY_FAILED"))

    def test_missing_reading_does_not_crash(self):
        with mock.patch.object(srv, "_idrac_request",
                               return_value=({"Fans": [{"Name": "X"}], "Temperatures": [{}]}, "")):
            d = srv.idrac_collect({"host": "h", "user": "u", "password": "p"}, force=True)
        self.assertEqual(d["fans"], [])
        self.assertEqual(d["temps"], [])


if __name__ == "__main__":
    unittest.main()
