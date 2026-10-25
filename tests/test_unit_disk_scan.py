"""硬盘巡检（disk_health）单元测试：分级口径、盘位枚举、入库与变化检测。

不依赖真实硬盘：smart_detail 与 lsblk 全部 mock，数据库用临时文件，
因此在 CI（Linux 容器）和本机 Windows 上都能跑。
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "app"))

import server as srv  # noqa: E402


def sas(**kw):
    """SAS 盘 smart_detail 结果（reallocated_sectors 来自增长缺陷表）。"""
    d = {"scsi": True, "health": "OK", "serial": "WAE0J50J", "model2": "ST300MP0026",
         "temp_c": 32, "power_on_hours": "52032", "rpm": "15000 rpm",
         "manufactured": "2018-W07", "grown_defects": 0, "reallocated_sectors": 0,
         "scsi_uncorrected": 0}
    d.update(kw)
    return d


def ata(**kw):
    d = {"health": "PASSED", "serial": "ZDE1", "model2": "WDC WD40", "temp_c": 38,
         "power_on_hours": "1200", "rpm": "7200 rpm",
         "reallocated_sectors": 0, "pending_sectors": 0, "uncorrectable": 0}
    d.update(kw)
    return d


class GradeTest(unittest.TestCase):
    """分级口径必须跨 SAS / ATA 一致，且不能把 ECC 计数当判据。"""

    def g(self, rec, prev=None):
        return srv.grade_disk(rec, prev)

    def test_healthy_sas_is_ok(self):
        self.assertEqual(self.g(sas())[0], "ok")

    def test_healthy_ata_is_ok(self):
        self.assertEqual(self.g(ata())[0], "ok")

    def test_grown_defects_is_warn(self):
        grade, why = self.g(sas(grown_defects=49, reallocated_sectors=49))
        self.assertEqual(grade, "warn")
        self.assertIn("49", why[0])

    def test_pending_sectors_are_warn(self):
        self.assertEqual(self.g(ata(pending_sectors=3))[0], "warn")

    def test_uncorrectable_is_crit(self):
        grade, why = self.g(sas(scsi_uncorrected=2, uncorrectable=2))
        self.assertEqual(grade, "crit")
        self.assertIn("不可纠正", why[0])

    def test_smart_fail_is_crit(self):
        self.assertEqual(self.g(sas(health="FAILED!"))[0], "crit")

    def test_ecc_counter_does_not_grade(self):
        # 实测同型号三块盘 ECC fast 分别是 41.6 亿 / 0.26 亿 / 14.7 亿，
        # 没有一块故障——它只是「读过多少数据」，32 位会回绕
        self.assertEqual(self.g(sas(scsi_ecc=4160000000, scsi_rereads=123))[0], "ok")

    def test_growth_past_threshold_is_crit(self):
        prev = {"defects": 49, "grade": "warn"}
        grade, why = self.g(sas(grown_defects=60, reallocated_sectors=60), prev)
        self.assertEqual(grade, "crit")
        self.assertIn("+11", why[0])

    def test_small_growth_is_warn_with_delta(self):
        prev = {"defects": 49, "grade": "warn"}
        grade, why = self.g(sas(grown_defects=51, reallocated_sectors=51), prev)
        self.assertEqual(grade, "warn")
        self.assertTrue(any("+2" in w for w in why))

    def test_first_scan_has_no_delta(self):
        grade, why = self.g(sas(grown_defects=49, reallocated_sectors=49), None)
        self.assertEqual(grade, "warn")
        self.assertFalse(any("较上次" in w for w in why))

    def test_no_health_no_counters_is_unknown(self):
        # sdj（Teclast 系统盘）所有 ATA 属性 flags 全 0、updated_online=false：
        # 盘根本不报这些属性，不能显示成「正常」
        self.assertEqual(self.g({"serial": "X", "temp_c": None})[0], "unknown")

    def test_error_is_unknown(self):
        self.assertEqual(self.g({"error": "smartctl 不可用"})[0], "unknown")

    def test_empty_is_unknown(self):
        self.assertEqual(self.g({})[0], "unknown")
        self.assertEqual(self.g(None)[0], "unknown")

    def test_standby_is_not_graded_unknown(self):
        # 休眠不是「未知」，由 disk_scan_once 沿用上次分级，不应该走到这里
        self.assertEqual(self.g({"standby": True})[0], "unknown")


class IntervalTest(unittest.TestCase):
    def test_presets(self):
        self.assertEqual(srv.DISK_SCAN_PRESETS_H, (6, 12, 24, 72, 168))

    def test_default_is_6h(self):
        self.assertEqual(srv.disk_scan_interval_h({}), 6)

    def test_off(self):
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_enabled": 0}), 0)
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_interval_h": 0}), 0)

    def test_snaps_to_nearest_preset(self):
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_interval_h": 10}), 12)
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_interval_h": 100}), 72)
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_interval_h": 1000}), 168)

    def test_garbage_falls_back(self):
        self.assertEqual(srv.disk_scan_interval_h({"disk_scan_interval_h": "abc"}), 6)


LSBLK = """{"blockdevices": [
 {"name":"sda","type":"disk","size":4000000000000,"ro":false,"rm":false,"model":"ST4000NM019B"},
 {"name":"sda1","type":"part","size":4000000000000,"ro":false,"rm":false,"model":null},
 {"name":"sdb","type":"disk","size":300000000000,"ro":false,"rm":false,"model":"ST300MP0026"},
 {"name":"sdc","type":"disk","size":2000000000,"ro":true,"rm":false,"model":"ISO"},
 {"name":"sdd","type":"disk","size":60000000000,"ro":false,"rm":true,"model":"USB"},
 {"name":"loop0","type":"loop","size":100,"ro":true,"rm":false,"model":null},
 {"name":"nvme0n1","type":"disk","size":500000000000,"ro":false,"rm":false,"model":"SN850"}
]}"""


class DevicesTest(unittest.TestCase):
    def test_filters_partitions_loop_ro_removable(self):
        with mock.patch.object(srv, "run_cmd", return_value=(LSBLK, 0)):
            devs = [d["dev"] for d in srv.disk_scan_devices()]
        self.assertEqual(devs, ["sda", "sdb", "nvme0n1"])

    def test_lsblk_failure_returns_empty(self):
        with mock.patch.object(srv, "run_cmd", return_value=("", 1)):
            self.assertEqual(srv.disk_scan_devices(), [])

    def test_bad_json_returns_empty(self):
        with mock.patch.object(srv, "run_cmd", return_value=("not json", 0)):
            self.assertEqual(srv.disk_scan_devices(), [])


class HistorySaveTest(unittest.TestCase):
    """入库与变化检测：真跑 sqlite，验证建表、UPSERT 与历史只记变化。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = srv.History(os.path.join(self.tmp, "monitor.db"))

    def tearDown(self):
        for root, _d, files in os.walk(self.tmp, topdown=False):
            for f in files:
                try:
                    os.unlink(os.path.join(root, f))
                except OSError:
                    pass
            for d in _d:
                try:
                    os.rmdir(os.path.join(root, d))
                except OSError:
                    pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    BASE = time.time() - 86400 * 30

    def item(self, dev="sdk", grade="warn", defects=49, ts=None, standby=False):
        ts = self.BASE if ts is None else ts
        return {"dev": dev, "serial": "WAE0J50J", "model": "ST300MP0026", "proto": "SAS",
                "health": "OK", "grade": grade, "defects": defects,
                "pending_defects": None, "uncorr": 0, "temp_c": 32,
                "power_on_hours": 52032, "rpm": "15000 rpm", "manufactured": "2018-W07",
                "standby": standby, "scan_ts": ts, "detail": "增长缺陷 49 个"}

    def test_tables_exist(self):
        self.assertEqual(self.db.disk_health_read(), {})

    def test_roundtrip(self):
        self.db.disk_health_save([self.item()], {})
        rows = self.db.disk_health_read()
        self.assertIn("sdk", rows)
        self.assertEqual(rows["sdk"]["grade"], "warn")
        self.assertEqual(rows["sdk"]["defects"], 49)
        self.assertEqual(rows["sdk"]["temp_c"], 32)

    def test_first_scan_counts_as_changed(self):
        changed = self.db.disk_health_save([self.item()], {})
        self.assertEqual(changed, ["sdk"])

    def test_no_change_writes_no_history(self):
        self.db.disk_health_save([self.item()], {})
        prev = self.db.disk_health_read()
        changed = self.db.disk_health_save([self.item(ts=self.BASE + 3600)], prev)
        self.assertEqual(changed, [])
        # 只有首扫那条历史
        self.assertEqual(len(self.db.disk_health_hist("sdk")), 1)

    def test_defect_growth_writes_history(self):
        self.db.disk_health_save([self.item()], {})
        prev = self.db.disk_health_read()
        changed = self.db.disk_health_save([self.item(defects=55, ts=self.BASE + 3600)], prev)
        self.assertEqual(changed, ["sdk"])
        hist = self.db.disk_health_hist("sdk")
        self.assertEqual(len(hist), 2)
        self.assertEqual(hist[0]["defects"], 55)

    def test_scan_ts_updates_even_without_change(self):
        self.db.disk_health_save([self.item(ts=self.BASE)], {})
        prev = self.db.disk_health_read()
        self.db.disk_health_save([self.item(ts=self.BASE + 7200)], prev)
        self.assertEqual(self.db.disk_health_read()["sdk"]["scan_ts"], self.BASE + 7200)

    def test_changed_ts_is_kept(self):
        self.db.disk_health_save([self.item(ts=self.BASE)], {})
        prev = self.db.disk_health_read()
        self.db.disk_health_save([self.item(ts=self.BASE + 7200)], prev)
        # 没有变化时 changed_ts 不能被刷新，否则「最后一次变化」的时间会失真
        self.assertEqual(self.db.disk_health_read()["sdk"]["changed_ts"], self.BASE)

    def test_standby_does_not_write_history(self):
        self.db.disk_health_save([self.item()], {})
        prev = self.db.disk_health_read()
        self.db.disk_health_save([self.item(grade="unknown", standby=True, ts=self.BASE + 3600)], prev)
        self.assertEqual(len(self.db.disk_health_hist("sdk")), 1)


class ScanOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = srv.History(os.path.join(self.tmp, "monitor.db"))
        srv._disk_scan_state.update({"last_ts": 0.0, "summary": {}, "error": "", "running": False})

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_scan(self, devices, detail_map):
        def fake_detail(dev):
            return detail_map.get(dev, {})
        with mock.patch.object(srv, "disk_scan_devices", return_value=devices), \
                mock.patch.object(srv, "smart_detail", side_effect=fake_detail), \
                mock.patch.object(srv, "is_linux", return_value=True):
            return srv.disk_scan_once(self.db, {})

    def test_scans_every_disk_not_just_first_eight(self):
        # 用户机器 14 块盘，sdk 排第 10 位正好落在原来 8 盘上限的盲区里
        devs = [{"dev": "sd%s" % c, "size": 0, "model": ""} for c in "abcdefghijklmn"]
        details = {d["dev"]: sas() for d in devs}
        details["sdk"] = sas(grown_defects=49, reallocated_sectors=49)
        out = self.run_scan(devs, details)
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 14)
        rows = self.db.disk_health_read()
        self.assertEqual(len(rows), 14)
        self.assertEqual(rows["sdk"]["grade"], "warn")
        self.assertEqual(out["counts"]["warn"], 1)
        self.assertEqual(out["counts"]["ok"], 13)

    def test_standby_keeps_previous_grade(self):
        self.db.disk_health_save([{"dev": "sdk", "grade": "warn", "defects": 49,
                                   "serial": "WAE0J50J", "scan_ts": time.time() - 86400,
                                   "standby": False, "uncorr": 0, "pending_defects": None}], {})
        out = self.run_scan([{"dev": "sdk", "size": 0, "model": ""}], {"sdk": {"standby": True}})
        self.assertEqual(out["skipped_standby"], ["sdk"])
        rows = self.db.disk_health_read()
        self.assertTrue(rows["sdk"]["standby"])
        self.assertEqual(rows["sdk"]["grade"], "warn")   # 不能被 standby 覆盖成 unknown
        self.assertEqual(rows["sdk"]["defects"], 49)

    def test_first_scan_of_asleep_disk_is_unknown(self):
        self.run_scan([{"dev": "sdk", "size": 0, "model": ""}], {"sdk": {"standby": True}})
        self.assertEqual(self.db.disk_health_read()["sdk"]["grade"], "unknown")

    def test_summary_records_scan_time(self):
        out = self.run_scan([{"dev": "sda", "size": 0, "model": ""}], {"sda": sas()})
        self.assertGreater(srv._disk_scan_state["last_ts"], 0)
        self.assertEqual(srv._disk_scan_state["summary"]["total"], 1)
        self.assertTrue(out["ok"])

    def test_scan_is_not_reentrant(self):
        self.assertTrue(srv._DISK_SCAN_LOCK.acquire(False))
        try:
            with mock.patch.object(srv, "is_linux", return_value=True):
                out = srv.disk_scan_once(self.db, {})
            self.assertFalse(out["ok"])
            self.assertIn("正在进行", out["error"])
        finally:
            srv._DISK_SCAN_LOCK.release()

    def test_non_linux_is_rejected(self):
        with mock.patch.object(srv, "is_linux", return_value=False):
            self.assertFalse(srv.disk_scan_once(self.db, {})["ok"])

    # ---- 手动「立即巡检」接口 ----
    def make_app(self, config):
        app = object.__new__(srv.MonitorApp)
        app.config = config
        app.collector = mock.Mock(db=self.db)
        return app

    def test_scan_now_starts_a_thread(self):
        app = self.make_app({"disk_scan_enabled": 1, "disk_scan_interval_h": 6})
        done = threading.Event()
        with mock.patch.object(srv, "disk_scan_once", side_effect=lambda *a: done.set()):
            out = app.api_disk_scan_now()
            self.assertTrue(out["ok"])
            self.assertTrue(done.wait(3), "后台线程没有真的执行巡检")
        self.assertTrue(out["message"])

    def test_scan_now_refuses_when_disabled(self):
        app = self.make_app({"disk_scan_enabled": 0, "disk_scan_interval_h": 6})
        with mock.patch.object(srv, "disk_scan_once") as once:
            out = app.api_disk_scan_now()
            once.assert_not_called()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "disabled")

    def test_scan_now_refuses_when_busy(self):
        app = self.make_app({"disk_scan_enabled": 1, "disk_scan_interval_h": 6})
        self.assertTrue(srv._DISK_SCAN_LOCK.acquire(False))
        try:
            with mock.patch.object(srv, "disk_scan_once") as once:
                out = app.api_disk_scan_now()
                once.assert_not_called()
        finally:
            srv._DISK_SCAN_LOCK.release()
        self.assertFalse(out["ok"])
        self.assertEqual(out["error"], "busy")


class ScanLogLabelTest(unittest.TestCase):
    """巡检流水里的硬盘名：有槽位写槽位（并保留设备名），没槽位退回设备名。"""

    def setUp(self):
        self._mem = dict(srv._DISK_HEALTH_MEM)
        srv._DISK_HEALTH_MEM.clear()
        srv._DISK_HEALTH_MEM.update({"sdk": {"serial": "SN-K"}, "sda": {"serial": "SN-A"}})
        self._patch = mock.patch.object(srv, "idrac_slot_map",
                                        return_value={"SNK": 6, "SNA": 0})
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        srv._DISK_HEALTH_MEM.clear()
        srv._DISK_HEALTH_MEM.update(self._mem)

    def test_slot_zero_is_valid(self):
        # 0 号槽是合法值：写成 if bay 会把它吞掉
        self.assertEqual(srv.disk_slot_of("sda"), 0)
        self.assertEqual(srv.disk_label("sda"), "插槽 0（sda）")

    def test_fallback_to_dev_name(self):
        # 对不上序列号的盘宁可显示 sdk，也不能猜一个槽位
        self.assertIsNone(srv.disk_slot_of("sdq"))
        self.assertEqual(srv.disk_label("sdq"), "sdq")

    def test_relabel_devs_is_idempotent(self):
        t = "sdk 由「正常」变为「故障」；sdq 待映射扇区 0→2"
        once = srv.relabel_disk_devs(t, {"sdk": 6})
        self.assertEqual(once, "插槽 6（sdk） 由「正常」变为「故障」；sdq 待映射扇区 0→2")
        self.assertEqual(srv.relabel_disk_devs(once, {"sdk": 6}), once)

    def test_change_text_uses_slot(self):
        prev = {"sdk": {"grade": "ok"}}
        items = [{"dev": "sdk", "serial": "SN-K", "grade": "crit"}]
        self.assertEqual(srv._scan_change_text(items, prev),
                         ["插槽 6（sdk） 由「正常」变为「故障」"])

    def test_change_text_growth_uses_slot(self):
        prev = {"sdk": {"grade": "warn", "defects": 49}}
        items = [{"dev": "sdk", "serial": "SN-K", "grade": "warn", "defects": 51}]
        self.assertEqual(srv._scan_change_text(items, prev),
                         ["插槽 6（sdk） 重映射扇区 49→51"])


if __name__ == "__main__":
    unittest.main()
