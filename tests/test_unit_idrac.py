"""iDRAC（Redfish）单元测试：配置读写、地址校验、风扇/温度解析、SEL 解析与端点回退。

全程 mock，不联网、不碰真机。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
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
    """按路径返回预设 JSON，未命中按 404 处理（模拟固件不支持）。

    query 参数是给 $expand 用的，mock 里忽略——测的是解析逻辑，不是 URL 拼装。
    """
    def f(cfg, path, timeout=None, query=None):
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
        cfg, err = srv.idrac_save(self.dir, {"host": "192.0.2.10", "user": "root",
                                             "password": "calvin", "enabled": 1})
        self.assertEqual(err, "")
        pub = srv.idrac_public(cfg)
        self.assertNotIn("password", pub)
        self.assertTrue(pub["password_set"])
        self.assertEqual(pub["host"], "192.0.2.10")

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
        self.cfg = {"host": "192.0.2.10", "user": "root", "password": "pw",
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

    def test_unused_fields_dropped(self):
        """四个「解析了但界面从没用过」的字段必须不再出现在返回里。

        hostname 尤其不能留：Redfish 报的是上一个装过的系统注册进去的名字
        （真机至今是 DESKTOP-NTRSKPB，实际主机名 R730XD），fnOS 不跑
        OpenManage 就永不同步，显示出来纯属误导。
        """
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(FULL)):
            d = srv.idrac_collect(self.cfg, force=True)
        for k in ("hostname", "mem_gib", "cpu", "health_rollup", "cpu_model"):
            self.assertNotIn(k, d)
        # 界面和告警真正在用的那几个还得在
        for k in ("model", "service_tag", "bios", "health", "power_state",
                  "cpu_health", "mem_health"):
            self.assertIn(k, d)

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
        # 测点名统一翻成面板用语：CPU1 Temp → CPU1，System Board Inlet Temp → 进风口
        self.assertIn("CPU1", names)
        self.assertNotIn("Absent Sensor", names)
        inlet = [t for t in d["temps"] if t["name"] == "进风口"][0]
        self.assertEqual(inlet["c"], 22)
        self.assertEqual(inlet["crit"], 42)
        self.assertEqual(d["temps"][0]["name"], "进风口")   # 进风口排第一

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


# ---------------------------------------------------------------------------
# 慢组：Storage / Memory / Processors（$expand 一次取全的形态）
# ---------------------------------------------------------------------------
DISK_FULL = {
    "Id": "Disk.Bay.7:Enclosure.Internal.0-1:RAID.Integrated.1-1",
    "Model": "ST4000NM019B", "SerialNumber": "WCC0ABCD", "CapacityBytes": 4000661504_000,
    "MediaType": "HDD", "Protocol": "SAS", "RotationSpeedRPM": 7200, "Revision": "CSJA",
    "NegotiatedSpeedGbs": 12, "FailurePredicted": False, "PredictedMediaLifeLeftPercent": None,
    "HotspareType": "None", "EncryptionStatus": "Unencrypted",
    "Status": {"Health": "OK", "State": "Enabled"},
}
DISK_BAD = dict(DISK_FULL, Id="Disk.Bay.9:Enclosure.Internal.0-1:RAID.Integrated.1-1",
                Model="SQF SMSV2 128GCB", MediaType="SSD", FailurePredicted=True,
                PredictedMediaLifeLeftPercent=7, Status={"Health": "Critical", "State": "Enabled"})

SLOW_JSON = {
    "/Managers/iDRAC.Embedded.1": {"FirmwareVersion": "2.86.86.86", "Model": "13G Monolithic",
                                   "DateTime": "2026-10-20T16:18:27-05:00",
                                   "DateTimeLocalOffset": "-05:00", "Status": {"Health": "OK"}},
    "/Systems/System.Embedded.1/Processors": {"Members": [
        {"Id": "CPU.Socket.1", "Model": "Intel Xeon E5-2682 v4", "TotalCores": 16,
         "TotalThreads": 32, "MaxSpeedMHz": 4000, "Status": {"Health": "OK"}},
        {"Id": "CPU.Socket.2", "Model": "Intel Xeon E5-2682 v4", "TotalCores": 16,
         "TotalThreads": 32, "MaxSpeedMHz": 4000, "Status": {"Health": "Warning"}},
    ]},
    "/Systems/System.Embedded.1/Memory": {"Members": [
        {"Id": "iDRAC.Embedded.1#DIMMSLOTB2", "CapacityMiB": 16384, "MemoryDeviceType": "DDR4",
         "OperatingSpeedMhz": 2400, "Manufacturer": "Hynix Semiconductor",
         "Status": {"Health": "OK"}},
        {"Id": "iDRAC.Embedded.1#DIMMSLOTA1", "CapacityMiB": 16384, "MemoryDeviceType": "DDR4",
         "OperatingSpeedMhz": 2400, "Manufacturer": "Hynix Semiconductor",
         "Status": {"Health": "OK"}},
        {"Id": "iDRAC.Embedded.1#DIMMSLOTA2", "Status": {"Health": "OK"}},   # 没有容量：空槽
    ]},
    "/Systems/System.Embedded.1/Storage": {"Members": [
        {"Id": "RAID.Integrated.1-1", "Name": "PERC H330 Mini",
         "StorageControllers": [{"Model": "PERC H330 Mini", "FirmwareVersion": "25.5.5.0005"}],
         "Status": {"Health": "OK"},
         "Drives": [DISK_FULL, DISK_BAD],
         "Volumes": [{"Id": "Disk.Bay.7:Enclosure.Internal.0-1:RAID.Integrated.1-1",
                      "Name": "Volume 7", "RAIDType": "None", "CapacityBytes": 4000661504000,
                      "Status": {"Health": "OK"}},
                     {"Id": "VD.0", "Name": "Volume 0", "RAIDType": "RAID1",
                      "CapacityBytes": 299439751168, "Status": {"Health": "Degraded"}}]},
    ]},
}


class IdracSlowTest(unittest.TestCase):
    def setUp(self):
        self.cfg = {"host": "h", "user": "u", "password": "p"}
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_disks_and_volumes(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(SLOW_JSON)):
            d = srv.idrac_collect_slow(self.cfg, force=True)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["disks"]), 2)
        # 槽位号从 Id 里解析出来，并按槽位排序
        self.assertEqual([x["bay"] for x in d["disks"]], [7, 9])
        self.assertEqual(d["disks"][0]["cap_gb"], 4001)
        self.assertEqual(d["disks"][0]["firmware"], "CSJA")
        self.assertTrue(d["disks"][1]["fail_pred"])
        self.assertEqual(d["disk_bad"], 1)
        self.assertEqual(d["controllers"][0]["model"], "PERC H330 Mini")
        self.assertEqual(len(d["volumes"]), 2)
        self.assertEqual(d["vol_bad"], 1)

    def test_dimms_skip_empty_slot_and_sort(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(SLOW_JSON)):
            d = srv.idrac_collect_slow(self.cfg, force=True)
        self.assertEqual([x["slot"] for x in d["dimms"]], ["A1", "B2"])
        self.assertEqual(d["dimm_count"], 2)
        self.assertEqual(d["dimm_gib"], 32.0)
        self.assertEqual(d["dimm_bad"], 0)

    def test_cpu_health(self):
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(SLOW_JSON)):
            d = srv.idrac_collect_slow(self.cfg, force=True)
        self.assertEqual(len(d["cpus"]), 2)
        self.assertEqual(d["cpus"][0]["cores"], 16)
        self.assertEqual(d["idrac_fw"], "2.86.86.86")

    def test_unreachable_returns_not_ok(self):
        with mock.patch.object(srv, "_idrac_request",
                               return_value=(None, "认证失败（用户名或密码错误）")):
            d = srv.idrac_collect_slow(self.cfg, force=True)
        self.assertFalse(d["ok"])
        self.assertEqual(d["disks"], [])


class IdracProblemsTest(unittest.TestCase):
    def test_collects_only_unhealthy(self):
        fast = {"psu": [{"name": "PS1", "health": "OK"}, {"name": "PS2", "health": "Critical"}],
                "fans": [{"name": "Fan1", "rpm": 9000, "health": "OK"}]}
        slow = {"disks": [{"bay": 7, "model": "X", "health": "OK", "fail_pred": False, "life": 90},
                          {"bay": 9, "model": "Y", "health": "Critical", "fail_pred": True, "life": 7}],
                "volumes": [{"name": "Volume 0", "raid": "RAID1", "health": "Degraded"}],
                "controllers": [{"id": "c0", "model": "PERC", "health": "OK"}],
                "cpus": [{"id": "CPU.Socket.1", "health": "OK"}],
                "dimms": [{"slot": "A1", "vendor": "Hynix", "health": "Warning"}]}
        ps = srv.idrac_problems(fast, slow)
        keys = [p["key"] for p in ps]
        self.assertIn("bay:9", keys)
        self.assertIn("life:9", keys)          # 寿命 ≤10% 单独一条（warning）
        self.assertIn("psu:PS2", keys)
        self.assertIn("vol:Volume 0", keys)
        self.assertIn("dimm:A1", keys)
        self.assertNotIn("bay:7", keys)        # 健康盘不进列表
        self.assertNotIn("fan:Fan1", keys)
        life = [p for p in ps if p["key"] == "life:9"][0]
        self.assertEqual(life["severity"], "warn")

    def test_empty_when_all_ok(self):
        self.assertEqual(srv.idrac_problems({"psu": [], "fans": []}, {}), [])


class SensorMergeTest(unittest.TestCase):
    """iDRAC 带外读数并入传感器：Dell 机的内核里一个风扇通道都没有。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_fans_and_inlet_merged(self):
        fast = {"ok": True,
                "fans": [{"name": "Fan1", "rpm": 9960}, {"name": "Fan2", "rpm": 10200}],
                "temps": [{"name": "进风口", "c": 25, "crit": 47},
                          {"name": "CPU1", "c": 40, "crit": 87}]}
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=("", 1)), \
                mock.patch.object(srv, "_hwmon_fans", return_value=[]):
            s = srv.collect_sensors(fast)
        self.assertEqual([f["rpm"] for f in s["fans"]], [9960, 10200])
        self.assertIn("idrac", s["fans_source"])
        names = [t["name"] for t in s["temps"]]
        self.assertIn("进风口", names)
        # CPU 温度交给 thermal zone，带外的不重复塞进来
        self.assertNotIn("CPU1", names)

    def test_no_idrac_keeps_old_behaviour(self):
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=("", 1)), \
                mock.patch.object(srv, "_hwmon_fans", return_value=[]), \
                mock.patch.object(srv, "_ipmi_fans", return_value=[]), \
                mock.patch.object(srv, "_ibm_acpi_fans", return_value=[]):
            s = srv.collect_sensors(None)
        self.assertEqual(s["fans"], [])
        self.assertEqual(s["fans_source"], "")


class MemberShapeTest(unittest.TestCase):
    """$expand 只展开一层，Volumes 的三种形态都要能吃下（真机三种都出现过）。"""

    def test_expanded_collection_object(self):
        d = {"Volumes": {"@odata.context": "/redfish/v1/$metadata#VolumeCollection",
                         "@odata.id": "/redfish/v1/Systems/System.Embedded.1/Storage/RAID.Integrated.1-1/Volumes",
                         "@odata.type": "#VolumeCollection.VolumeCollection",
                         "Description": "Collection of Volumes",
                         "Members": [{"Name": "Volume 0", "RAIDType": "RAID1"}]}}
        with mock.patch.object(srv, "_idrac_request",
                               side_effect=AssertionError("已展开时不应再发请求")):
            got = srv._idrac_members({}, d, "Volumes")
        self.assertEqual([x["Name"] for x in got], ["Volume 0"])

    def test_plain_reference_fetches_once(self):
        d = {"Volumes": {"@odata.id": "/redfish/v1/X/Volumes"}}
        with mock.patch.object(srv, "_idrac_request",
                               return_value=({"Members": [{"Name": "V1"}]}, "")) as m:
            got = srv._idrac_members({"host": "h"}, d, "Volumes")
        self.assertEqual(len(got), 1)
        self.assertTrue(m.called)

    def test_list_passthrough(self):
        d = {"Drives": [{"Id": "a"}, {"Id": "b"}]}
        self.assertEqual(len(srv._idrac_members({}, d, "Drives")), 2)

    def test_missing_key(self):
        self.assertEqual(srv._idrac_members({}, {}, "Volumes"), [])
        self.assertEqual(srv._idrac_members({}, None, "Volumes"), [])


class SelPathFallbackTest(unittest.TestCase):
    """iDRAC8 对 iDRAC9 的 SEL 路径会回 200 + 空列表，不能据此提前返回。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_empty_first_path_falls_through(self):
        def fake(cfg, path, timeout=None, query=None):
            if path == "/Managers/iDRAC.Embedded.1/LogServices/Sel/Entries":
                return {"Members": [], "Members@odata.count": 0}, ""
            return SEL_JSON, ""
        with mock.patch.object(srv, "_idrac_request", side_effect=fake):
            r = srv.idrac_sel({"host": "h"}, limit=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["path"], "/Managers/iDRAC.Embedded.1/Logs/Sel")
        self.assertEqual(len(r["entries"]), 3)

    def test_all_empty_is_still_ok(self):
        with mock.patch.object(srv, "_idrac_request",
                               return_value=({"Members": []}, "")):
            r = srv.idrac_sel({"host": "h"}, limit=5)
        self.assertTrue(r["ok"])
        self.assertEqual(r["entries"], [])


if __name__ == "__main__":
    unittest.main()


class SlotMapTest(unittest.TestCase):
    """序列号 → 机箱槽位：把 /dev/sdX 对上 iDRAC 的 Disk.Bay.N。

    Linux 侧（HBA 直通、背板不暴露 SES）拿不到槽位，iDRAC 侧不知道设备名，
    序列号是两边唯一的公共键，所以归一化规则要经得起大小写 / 空格 / 连字符差异。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def _put(self, disks):
        srv._IDRAC_CACHE["slow"] = {"ok": True, "ts": 0, "disks": disks}

    def test_normalize(self):
        self.assertEqual(srv._serial_norm(" wae0j50j "), "WAE0J50J")
        self.assertEqual(srv._serial_norm("Z1Z0-ABCD"), "Z1Z0ABCD")
        self.assertEqual(srv._serial_norm(None), "")
        self.assertEqual(srv._serial_norm(""), "")

    def test_map_hit(self):
        self._put([{"bay": 5, "serial": "WAE06ENM"},
                   {"bay": 7, "serial": "Z1Z0ABCD"},
                   {"bay": 9, "serial": ""}])          # 无序列号：不参与映射
        m = srv.idrac_slot_map()
        self.assertEqual(m.get("WAE06ENM"), 5)
        self.assertEqual(m.get("Z1Z0ABCD"), 7)
        self.assertEqual(len(m), 2)

    def test_map_empty_when_no_cache(self):
        self.assertEqual(srv.idrac_slot_map(), {})

    def test_map_skips_non_int_bay(self):
        # _idrac_bay 解析失败时返回字符串，这种不能当槽位用
        self._put([{"bay": "Enclosure.Internal.0-1", "serial": "WAE06ENM"}])
        self.assertEqual(srv.idrac_slot_map(), {})

    def test_map_duplicate_serial_keeps_first(self):
        self._put([{"bay": 3, "serial": "DUP"}, {"bay": 8, "serial": "dup"}])
        self.assertEqual(srv.idrac_slot_map(), {"DUP": 3})

    def test_apply_slots_sets_and_clears(self):
        disks = [{"name": "sda", "serial": " wae06enm"}, {"name": "sdb", "serial": "NOPE"}]
        srv.apply_idrac_slots(disks, {"WAE06ENM": 5})
        self.assertEqual(disks[0]["slot"], 5)
        self.assertIsNone(disks[1]["slot"])
        # iDRAC 被关掉（slots 为空）时要把上一轮的槽位清掉，不能留旧值
        srv.apply_idrac_slots(disks, {})
        self.assertIsNone(disks[0]["slot"])


class ParallelFetchTest(unittest.TestCase):
    """14 块盘 / 14 个卷在 iDRAC8 上只回引用，必须逐个补请求。

    串行是 70~170 秒（每请求 5 秒），槽位要等到那时候才出得来；并发后 40 秒上下。
    已展开的条目不能再发请求——那等于把整个 Storage 又读一遍。
    """

    def setUp(self):
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_expanded_items_untouched(self):
        items = [{"Id": "Disk.Bay.0", "SerialNumber": "A"},
                 {"Id": "Disk.Bay.1", "SerialNumber": "B"}]
        calls = []

        def fake(cfg, path, timeout=None, query=None):
            calls.append(path)
            return {}, ""

        with mock.patch.object(srv, "_idrac_request", fake):
            out = srv._idrac_parallel({}, items)
        self.assertEqual(out, items)
        self.assertEqual(calls, [])

    def test_refs_fetched_in_order(self):
        refs = [{"@odata.id": "/redfish/v1/x/%d" % i} for i in range(6)]

        def fake(cfg, path, timeout=None, query=None):
            return {"Id": path}, ""

        with mock.patch.object(srv, "_idrac_request", fake):
            out = srv._idrac_parallel({}, refs)
        # _idrac_try_fetch 会把 /redfish/v1 前缀削掉再请求，顺序必须与输入一致
        self.assertEqual([x.get("Id") for x in out],
                         ["/x/%d" % i for i in range(6)])

    def test_failed_fetch_keeps_original(self):
        refs = [{"@odata.id": "/a"}, {"@odata.id": "/b"}]

        def fake(cfg, path, timeout=None, query=None):
            return None, "HTTP 500"

        with mock.patch.object(srv, "_idrac_request", fake):
            out = srv._idrac_parallel({}, refs)
        self.assertEqual(out, refs)      # 取不到就保留引用，不能变成 [None, None]

    def test_empty_and_single(self):
        self.assertEqual(srv._idrac_parallel({}, []), [])
        one = [{"@odata.id": "/a"}]

        def fake(cfg, path, timeout=None, query=None):
            return {"Id": path}, ""

        with mock.patch.object(srv, "_idrac_request", fake):
            self.assertEqual(srv._idrac_parallel({}, one), [{"Id": "/a"}])


class SlowEnsureTest(unittest.TestCase):
    """兜底补读：固定信息只读一次，所以只有「还没有台账」时才该踢一轮。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()
        srv._SLOW_INFLIGHT[0] = 0.0
        self.dir = tempfile.mkdtemp()
        self.cfg = {"enabled": 1, "host": "192.168.1.120", "user": "u", "password": "p"}
        srv.write_json_secure(os.path.join(self.dir, "idrac.json"), self.cfg)

    def tearDown(self):
        srv._IDRAC_CACHE.clear()
        srv._SLOW_INFLIGHT[0] = 0.0
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_memory_ledger_no_spawn(self):
        srv._IDRAC_CACHE["slow"] = {"ok": True, "ts": srv.time.time(), "disks": [{"bay": 1}],
                                    "host": "192.168.1.120"}
        self.assertFalse(srv.idrac_ensure_slow_async(self.dir))

    def test_disk_ledger_no_spawn(self):
        srv.idrac_static_write(self.dir, self.cfg,
                               {"ok": True, "disks": [{"bay": 1}], "static_ts": 1.0})
        self.assertFalse(srv.idrac_ensure_slow_async(self.dir))
        # 顺手把文件灌回内存了，后面读槽位就不用再碰磁盘
        self.assertTrue(srv._IDRAC_CACHE["slow"]["from_disk"])

    def test_no_ledger_spawns(self):
        """没台账才踢一轮。线程体用桩替换，别真去连 192.168.1.120。

        注意：真起线程会占住 idrac_collect_slow 的单飞锁，后面的用例就会
        拿到 busy 响应，看起来像是「台账读不到」。所以这里必须等它跑完。
        """
        started = threading.Event()

        def stub(*a, **kw):
            started.set()
            return {"ok": True, "disks": [{"bay": 1}]}

        with mock.patch.object(srv, "idrac_collect_slow", stub):
            self.assertTrue(srv.idrac_ensure_slow_async(self.dir))
            started.wait(3)
            for t in threading.enumerate():
                if t.name in ("fnm-idrac-slow-once", "fnm-idrac-static-refresh"):
                    t.join(3)

    def test_disabled_config_no_spawn(self):
        srv.write_json_secure(os.path.join(self.dir, "idrac.json"), {"enabled": 0})
        self.assertFalse(srv.idrac_ensure_slow_async(self.dir))

    def test_diag_shape(self):
        srv._IDRAC_CACHE["slow"] = {"ok": True, "ts": srv.time.time() - 5,
                                    "disks": [{}, {}], "dimms": [{}], "volumes": []}
        d = srv.idrac_diag()
        self.assertEqual(d["slow_disks"], 2)
        self.assertEqual(d["slow_dimms"], 1)
        self.assertIsInstance(d["slow_age"], int)
        self.assertIsInstance(d["threads"], list)


class StaticLedgerTest(unittest.TestCase):
    """固定信息台账：读一次落盘，之后只读文件，换机器自动作废。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()
        srv._SLOW_INFLIGHT[0] = 0.0
        self.dir = tempfile.mkdtemp()
        self.cfg = {"enabled": 1, "host": "192.168.1.120", "user": "u", "password": "p"}

    def tearDown(self):
        srv._IDRAC_CACHE.clear()
        srv._SLOW_INFLIGHT[0] = 0.0
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, disks=None, host=None):
        srv.idrac_static_write(self.dir, dict(self.cfg, host=host or self.cfg["host"]),
                               {"ok": True, "disks": disks if disks is not None else [{"bay": 3}],
                                "dimms": [{"slot": "A1"}], "volumes": [], "static_ts": 1000.0})

    def test_roundtrip(self):
        self._write()
        d = srv.idrac_static_read(self.dir, self.cfg)
        self.assertTrue(d["ok"])
        self.assertEqual(d["disks"][0]["bay"], 3)
        self.assertEqual(d["host"], "192.168.1.120")
        self.assertEqual(d["static_ts"], 1000.0)

    def test_missing_file(self):
        self.assertIsNone(srv.idrac_static_read(self.dir, self.cfg))

    def test_host_changed_invalidates(self):
        self._write()
        other = dict(self.cfg, host="10.0.0.9")
        self.assertIsNone(srv.idrac_static_read(self.dir, other))
        # 换了地址不能拿旧机器的槽位充数
        self.assertFalse(srv.idrac_static_ensure_loaded(self.dir, other))

    def test_no_disks_not_accepted(self):
        """空盘列表的台账不算数：读残了宁可重读。"""
        self._write(disks=[])
        self.assertIsNone(srv.idrac_static_read(self.dir, self.cfg))

    def test_ensure_loaded_fills_cache(self):
        self._write()
        self.assertTrue(srv.idrac_static_ensure_loaded(self.dir, self.cfg))
        self.assertTrue(srv._IDRAC_CACHE["slow"]["from_disk"])
        self.assertEqual(srv._IDRAC_CACHE["slow"]["host"], "192.168.1.120")

    def test_collect_slow_reads_ledger_without_request(self):
        """有台账时一个请求都不许发——这是「减少 iDRAC 时间」的核心。"""
        self._write()
        calls = []

        def fake(cfg, path, timeout=None, query=None):
            calls.append(path)
            return None, "不该被调用"

        with mock.patch.object(srv, "_idrac_request", fake):
            d = srv.idrac_collect_slow(self.cfg, data_dir=self.dir)
        self.assertEqual(calls, [])
        self.assertTrue(d["ok"])
        self.assertEqual(d["disks"][0]["bay"], 3)

    def test_force_ignores_ledger(self):
        self._write()
        with mock.patch.object(srv, "_idrac_request", side_effect=fake_req(SLOW_JSON)):
            d = srv.idrac_collect_slow(self.cfg, force=True, data_dir=self.dir)
        self.assertTrue(d["ok"])
        self.assertEqual(len(d["disks"]), 2)      # 真的重读了
        # 重读成功 → 台账被覆盖成新的一份
        again = srv.idrac_static_read(self.dir, self.cfg)
        self.assertEqual(len(again["disks"]), 2)

    def test_failed_refresh_keeps_old_ledger(self):
        """手动刷新失败不能把上一份好台账弄丢，界面继续显示旧的。"""
        self._write(disks=[{"bay": 3}])
        with mock.patch.object(srv, "_idrac_request",
                               return_value=(None, "连接超时")):
            d = srv.idrac_collect_slow(self.cfg, force=True, data_dir=self.dir)
        self.assertEqual(d["disks"][0]["bay"], 3)     # 沿用了旧台账
        self.assertIn("refresh_error", d)
        self.assertIsNotNone(srv.idrac_static_read(self.dir, self.cfg))

    def test_write_strips_runtime_keys(self):
        srv.idrac_static_write(self.dir, self.cfg,
                               {"ok": True, "disks": [{"bay": 1}], "static_ts": 5.0,
                                "cached": True, "from_disk": True, "partial": False,
                                "busy": True, "ts": 999, "error": "x"})
        d = srv.idrac_static_read(self.dir, self.cfg)
        for k in ("cached", "from_disk", "partial", "busy", "ts", "error"):
            self.assertNotIn(k, d)
        self.assertIn("disks", d)


class VolumePrefilterTest(unittest.TestCase):
    """HBA 直通时 iDRAC 把每块盘伪装成一个「卷」，别再逐个取详情。"""

    def setUp(self):
        srv._IDRAC_CACHE.clear()

    def tearDown(self):
        srv._IDRAC_CACHE.clear()

    def test_bay_refs_skipped(self):
        storage = {"Members": [{
            "Id": "RAID.Integrated.1-1",
            "StorageControllers": [{"Model": "PERC H330 Mini"}],
            "Status": {"Health": "OK"},
            "Drives": [DISK_FULL],
            "Volumes": [
                {"@odata.id": "/redfish/v1/x/Disk.Bay.7:Enclosure.Internal.0-1:RAID.Integrated.1-1"},
                {"@odata.id": "/redfish/v1/x/Disk.Bay.8:Enclosure.Internal.0-1:RAID.Integrated.1-1"},
                {"@odata.id": "/redfish/v1/x/Disk.Virtual.0:RAID.Integrated.1-1"}]}]}
        asked = []

        def fake(cfg, path, timeout=None, query=None):
            asked.append(path)
            if path.endswith("/Storage"):
                return storage, ""
            if "Disk.Virtual" in path:
                return {"Id": "Disk.Virtual.0", "Name": "VD0", "RAIDType": "RAID1",
                        "CapacityBytes": 299439751168, "Status": {"Health": "OK"}}, ""
            if path.endswith("/Processors") or path.endswith("/Memory"):
                return {"Members": []}, ""
            return {}, ""

        with mock.patch.object(srv, "_idrac_request", fake):
            d = srv.idrac_collect_slow({"host": "h"}, force=True)
        # 两个 Disk.Bay 的假「卷」不许发请求（省 2 个请求 ≈ 10 秒），只有真卷去取
        self.assertFalse([p for p in asked if "Disk.Bay.7" in p])
        self.assertFalse([p for p in asked if "Disk.Bay.8" in p])
        self.assertTrue([p for p in asked if "Disk.Virtual.0" in p])
        self.assertEqual([v["raid"] for v in d["volumes"]], ["RAID1"])


class SelPairTest(unittest.TestCase):
    """SEL 配对：把「发生」与「恢复」配成一对，配上的不再算活跃告警。

    夹具是 R730XD 上实测的最近 20 条 SEL（PowerEdge + iDRAC8，2026-10-21 抓取）。
    这些条目里原来有 10 条被判为活跃，实际全是早已恢复的历史事件：
    8 月两次风扇低速、多次开关盖。配对逻辑上线后应该是 0 条。
    """

    # (严重级别, EntryCode, MessageId, 消息)   —— 顺序即 SEL 的返回顺序：最新的在前
    REAL_20 = [
        ("OK", "Deassert", "801ff", "The chassis is closed while the power is on."),
        ("Critical", "Assert", "801ff", "The chassis is open while the power is on."),
        ("OK", "Deassert", "801ff", "The chassis is closed while the power is on."),
        ("OK", "Lower Non-critical - going low", "50915", "Fan 1 RPM is within range."),
        ("Warning", "Lower Critical - going low", "52913", "Fan 1 RPM is less than the lower warning threshold."),
        ("Critical", "Lower Critical - going low", "5203", "Fan 1 RPM is less than the lower critical threshold."),
        ("Warning", "Lower Non-critical - going low", "5005", "Fan 1 RPM is less than the lower warning threshold."),
        ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
        ("OK", "Assert", "a015", "Drive 5 is installed in disk drive bay 1."),
        ("Critical", "Deassert", "a015", "Drive 5 is removed from disk drive bay 1."),
        ("OK", "Deassert", "801ff", "The chassis is closed while the power is on."),
        ("Critical", "Assert", "801ff", "The chassis is open while the power is on."),
        ("OK", "Deassert", "802ff", "The chassis is closed while the power is off."),
        ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
        ("OK", "Deassert", "802ff", "The chassis is closed while the power is off."),
        ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
        ("OK", "Deassert", "802ff", "The chassis is closed while the power is off."),
        ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
        ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
        ("OK", "Deassert", "3ffff", "The input power for power supply 2 has been restored."),
    ]

    def _rows(self, items):
        rows = []
        for i, (sev, code, mid, msg) in enumerate(items):
            rows.append({"id": str(9000 - i),
                         "created": "2026-10-21T00:%02d:00Z" % (59 - i),
                         "severity": sev, "message": msg, "code": code,
                         "assert": code.lower() != "deassert",
                         "sensor": "[{'Member': 'x'}]", "msg_id": mid})
        rows.sort(key=lambda x: (x["created"], x["id"]), reverse=True)
        return rows

    def test_real_machine_collapses_to_zero(self):
        """真机 20 条：原来 10 条活跃，配对后必须一条不剩。"""
        rows = self._rows(self.REAL_20)
        self.assertEqual(len(srv.sel_pair_active(rows)), 0)

    def test_unrecovered_warning_stays(self):
        """只有告警、没有任何恢复记录 → 必须留着（宁可多报）。"""
        rows = self._rows([
            ("Critical", "Assert", "802ff", "The chassis is open while the power is off."),
            ("OK", "Assert", "a015", "Drive 5 is installed in disk drive bay 1."),
        ])
        act = srv.sel_pair_active(rows)
        self.assertEqual(len(act), 1)
        self.assertIn("chassis is open", act[0]["message"])

    def test_deassert_by_same_msg_id(self):
        rows = self._rows([
            ("OK", "Deassert", "801ff", "The chassis is closed while the power is on."),
            ("Critical", "Assert", "801ff", "The chassis is open while the power is on."),
        ])
        self.assertEqual(srv.sel_pair_active(rows), [])

    def test_semantic_pairing_across_msg_ids(self):
        """风扇：告警与恢复的 MessageId 不同，只能靠「同一对象 + within range」。"""
        rows = self._rows([
            ("OK", "Lower Non-critical - going low", "50915", "Fan 1 RPM is within range."),
            ("Critical", "Lower Critical - going low", "5203", "Fan 1 RPM is less than the lower critical threshold."),
        ])
        self.assertEqual(srv.sel_pair_active(rows), [])

    def test_semantic_pairing_is_object_scoped(self):
        """恢复记录只配对同一个对象，不能把别的传感器的告警一起消掉。"""
        rows = self._rows([
            ("OK", "Lower Non-critical - going low", "50915", "Fan 1 RPM is within range."),
            ("Critical", "Lower Critical - going low", "5203", "Fan 2 RPM is less than the lower critical threshold."),
        ])
        act = srv.sel_pair_active(rows)
        self.assertEqual(len(act), 1)
        self.assertIn("Fan 2", act[0]["message"])

    def test_info_events_never_active(self):
        """OK 级别的信息事件（装盘/拔盘）不算告警。"""
        rows = self._rows([
            ("OK", "Assert", "a015", "Drive 5 is installed in disk drive bay 1."),
            ("Critical", "Deassert", "a015", "Drive 5 is removed from disk drive bay 1."),
        ])
        self.assertEqual(srv.sel_pair_active(rows), [])

    def test_recovery_must_be_newer(self):
        """恢复记录比告警更旧，说明是「先恢复、后发生」，不能算恢复。"""
        rows = self._rows([
            ("Critical", "Assert", "801ff", "The chassis is open while the power is on."),
            ("OK", "Deassert", "801ff", "The chassis is closed while the power is on."),
        ])
        self.assertEqual(len(srv.sel_pair_active(rows)), 1)

    def test_object_extraction(self):
        self.assertEqual(srv._sel_object("Fan 3 RPM is less than the lower warning threshold."), "fan:3")
        self.assertEqual(srv._sel_object("The chassis is open while the power is on."), "chassis")
        self.assertEqual(srv._sel_object("The input power for power supply 2 has been restored."), "psu:2")
        self.assertEqual(srv._sel_object("Drive 12 is removed from disk drive bay 1."), "drive:12")
        self.assertEqual(srv._sel_object("完全看不懂的一句话"), "")
