"""Unit tests: smart_detail() / smartctl_temp() 的 SAS(SCSI) 支持。

SAS 盘的 smartctl 输出字段名与 ATA/NVMe 完全不同（`Serial number` 小写 n、
`Current Drive Temperature`、`Accumulated power on time, hours:minutes`、
`Elements in grown defect list`、`Error counter log` 三向表），此前一律读不到，
导致存储页上行 SAS 盘的温度、通电时长恒为 `--`，健康分级也无法判断。

纯 fixture（不需要真实 SAS 盘）。测试通过 mock run_cmd 喂入 smartctl 文本。
"""
import time
import unittest
from unittest import mock

from fnm_support import load_server

srv = load_server()

# smartmontools 7.3 对 Seagate SAS 盘（ST1200MM0009）的真实输出格式
SAS_OK = """smartctl 7.3 2022-02-28 r5338 [x86_64-linux-6.1.0-fnOS] (local build)
Copyright (C) 2002-22, Bruce Allen, Christian Franke, www.smartmontools.org

=== START OF INFORMATION SECTION ===
Vendor:               SEAGATE
Product:              ST1200MM0009
Revision:             N003
Compliance:           SPC-4
User Capacity:        1,200,243,695,616 bytes [1.20 TB]
Logical block size:   512 bytes
Rotation Rate:        10000 rpm
Form Factor:          2.5 inches
Logical Unit id:      0x5000c5008a1b2c3d
Serial number:        W460ABCD
Device type:          disk
Transport protocol:   SAS (SPL-3)
Local Time is:        Tue Oct 20 18:10:00 2026 CST
SMART support is:     Available - device has SMART capability.
SMART support is:     Enabled
Temperature Warning:  Enabled

=== START OF READ SMART DATA SECTION ===
SMART Health Status: OK

Current Drive Temperature:     33 C
Drive Trip Temperature:        68 C

Accumulated power on time, hours:minutes 21456:23
Manufactured in week 15 of year 2019
Specified cycle count over device lifetime:  10000
Accumulated start-stop cycles:  1234
Specified load-unload count over device lifetime:  300000
Accumulated load-unload cycles:  4567
Elements in grown defect list: 2

Vendor (Seagate Cache) information
  Blocks sent to initiator = 123456789

Error counter log:
           Errors Corrected by           Total   Correction     Gigabytes    Total
               ECC          rereads/    errors   algorithm      processed    uncorrected
           fast | delayed   rewrites  corrected  invocations   [10^9 bytes]  errors
read:          0        0         0         0          0       5432.123           0
write:         0        0         0         0          0       1234.567           0
verify:        0        0         0         0          0          0.000           3

Non-medium error count:        0
"""

SAS_FAIL = """=== START OF INFORMATION SECTION ===
Product:              ST4000NM0035
Serial number:        Z1Z0ABCD
Rotation Rate:        7200 rpm

=== START OF READ SMART DATA SECTION ===
SMART Health Status: FAILURE PREDICTION THRESHOLD EXCEEDED

Current Drive Temperature:     52 C

Elements in grown defect list: 128

Error counter log:
           fast | delayed   rewrites  corrected  invocations   [10^9 bytes]  errors
read:          4        0        12         4          0       9012.000          47
write:         0        0         0         0          0        120.000           0
verify:        0        0         0         0          0          0.000           0
"""

SATA_WDC = """=== START OF INFORMATION SECTION ===
Device Model:     WDC WD40EFRX-68N32N0
Serial Number:    WD-WCC7K1234567
LU WWN Device Id: 5 0014ee 2b3a1c4d5
Firmware Version: 82.00A82
User Capacity:    4,000,787,030,016 bytes [4.00 TB]
Rotation Rate:    5400 rpm

=== START OF READ SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

ID# ATTRIBUTE_NAME          FLAG     VALUE WORST THRESH TYPE      UPDATED  WHEN_FAILED RAW_VALUE
  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       8
  9 Power_On_Hours          0x0032   077   077   000    Old_age   Always       -       17234
197 Current_Pending_Sector  0x0012   100   100   000    Old_age   Always       -       0
198 Offline_Uncorrectable   0x0010   100   100   000    Old_age   Offline      -       0
199 UDMA_CRC_Error_Count    0x003e   200   200   000    Old_age   Always       -       2
"""

NVME_SAMSUNG = """=== START OF INFORMATION SECTION ===
Model Number:                       Samsung SSD 980 PRO 1TB
Serial Number:                      S5GXNX0R123456
Firmware Version:                   5B2QGXA7
Rotation Rate:                      Solid State Device

=== START OF SMART DATA SECTION ===
SMART overall-health self-assessment test result: PASSED

SMART/Health Information (NVMe Log 0x02)
Critical Warning:                   0x00
Temperature:                        38 Celsius
Available Spare:                    100%
Power On Hours:                     1,234 hours
"""


def _detail(text, rc=0):
    """把 smartctl 文本喂给 smart_detail，返回解析结果。"""
    with mock.patch.object(srv, "is_linux", return_value=True), \
            mock.patch.object(srv, "run_cmd", return_value=(text, rc)):
        return srv.smart_detail("sda")


class SasDetailTest(unittest.TestCase):
    """SAS 盘：此前温度/通电时长/缺陷表全部读不到。"""

    def test_temperature(self):
        d = _detail(SAS_OK)
        self.assertEqual(d.get("temp_c"), 33)

    def test_power_on_hours(self):
        # 写法是 "hours:minutes 21456:23"，含冒号，不能简单 split(":")
        self.assertEqual(_detail(SAS_OK).get("power_on_hours"), "21456")

    def test_serial_lowercase_n(self):
        # SAS 写 "Serial number"，旧代码只认 "Serial Number"
        self.assertEqual(_detail(SAS_OK).get("serial"), "W460ABCD")

    def test_model_from_product(self):
        self.assertEqual(_detail(SAS_OK).get("model2"), "ST1200MM0009")

    def test_rotation_rate(self):
        self.assertEqual(_detail(SAS_OK).get("rpm"), "10000 rpm")

    def test_health_ok(self):
        self.assertEqual(_detail(SAS_OK).get("health"), "OK")

    def test_grown_defects(self):
        d = _detail(SAS_OK)
        self.assertEqual(d.get("grown_defects"), 2)
        # 映射到前端 SMART_KEYS 认的键，零前端改动即可判色
        self.assertEqual(d.get("reallocated_sectors"), 2)

    def test_scsi_error_counters(self):
        d = _detail(SAS_OK)
        # read/write/verify 三向 uncorrected 合计 = 0+0+3
        self.assertEqual(d.get("scsi_uncorrected"), 3)
        self.assertEqual(d.get("uncorrectable"), 3)
        self.assertEqual(d.get("scsi_gb"), 6666.69)

    def test_manufactured(self):
        self.assertEqual(_detail(SAS_OK).get("manufactured"), "2019-W15")

    def test_flag_set(self):
        self.assertTrue(_detail(SAS_OK).get("scsi"))

    def test_fail_state(self):
        d = _detail(SAS_FAIL)
        self.assertIn("FAILURE", d.get("health", ""))
        self.assertEqual(d.get("temp_c"), 52)
        self.assertEqual(d.get("grown_defects"), 128)
        self.assertEqual(d.get("scsi_uncorrected"), 47)
        self.assertEqual(d.get("scsi_rereads"), 12)

    def test_no_false_positive_on_zero_defects(self):
        # 增长缺陷为 0 时必须显式给出 0，否则前端无法区分「正常」与「没读到」
        text = SAS_OK.replace("Elements in grown defect list: 2", "Elements in grown defect list: 0")
        self.assertEqual(_detail(text).get("grown_defects"), 0)

    def test_pending_defects(self):
        # 部分 SAS 盘（如 ST4000NM019B）额外给出 Pending defects 计数
        text = SAS_OK.replace("Elements in grown defect list: 2",
                              "Elements in grown defect list: 2\nPending defects: 5")
        self.assertEqual(_detail(text).get("scsi_pending_defects"), 5)


class RegressionAtaTest(unittest.TestCase):
    """原有 ATA 解析不能被 SAS 分支影响。"""

    def test_ata_attributes(self):
        d = _detail(SATA_WDC)
        self.assertEqual(d.get("reallocated_sectors"), 8)
        self.assertEqual(d.get("pending_sectors"), 0)
        self.assertEqual(d.get("udma_crc"), 2)
        self.assertEqual(d.get("power_on_hours"), "17234")

    def test_ata_identity(self):
        d = _detail(SATA_WDC)
        self.assertEqual(d.get("serial"), "WD-WCC7K1234567")
        self.assertEqual(d.get("model2"), "WDC WD40EFRX-68N32N0")
        self.assertEqual(d.get("rpm"), "5400 rpm")

    def test_ata_not_marked_scsi(self):
        self.assertNotIn("scsi", _detail(SATA_WDC))

    def test_ata_health(self):
        self.assertEqual(_detail(SATA_WDC).get("health"), "PASSED")


class RegressionNvmeTest(unittest.TestCase):
    def test_nvme_temperature_and_hours(self):
        d = _detail(NVME_SAMSUNG)
        self.assertEqual(d.get("temp_c"), 38)
        self.assertEqual(d.get("power_on_hours"), "1234")
        self.assertEqual(d.get("serial"), "S5GXNX0R123456")
        self.assertNotIn("scsi", d)


class EdgeCaseTest(unittest.TestCase):
    def test_standby_not_woken(self):
        # smartctl -n standby 在休眠盘上 exit 2，必须原样上报 standby
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=("", 2)):
            self.assertEqual(srv.smart_detail("sdb"), {"standby": True})

    def test_empty_output(self):
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=("", 0)):
            self.assertEqual(srv.smart_detail("sdc"), {})

    def test_command_failure(self):
        self.assertEqual(_detail("smartctl: command not found", 127), {})


class SmartctlTempTest(unittest.TestCase):
    """温度兜底函数同样漏了 SAS 格式（面板上 SAS 盘温度列全为 --）。"""

    def _temp(self, text, rc=0):
        with mock.patch.object(srv, "run_cmd", return_value=(text, rc)):
            return srv.smartctl_temp("sda")

    def test_sas_current_drive_temperature(self):
        self.assertEqual(self._temp(SAS_OK), 33)

    def test_sas_fail_state_temperature(self):
        self.assertEqual(self._temp(SAS_FAIL), 52)

    def test_ata_temperature_unchanged(self):
        text = ("ID# ATTRIBUTE_NAME  FLAG VALUE WORST THRESH TYPE UPDATED WHEN_FAILED RAW_VALUE\n"
                "194 Temperature_Celsius 0x0022 110 095 000 Old_age Always - 42")
        self.assertEqual(self._temp(text), 42)

    def test_nvme_temperature_unchanged(self):
        self.assertEqual(self._temp("Temperature:                        38 Celsius"), 38)

    def test_standby_returns_none(self):
        self.assertIsNone(self._temp("", 2))

    def test_unknown_format_returns_none(self):
        self.assertIsNone(self._temp("no temperature here"))


if __name__ == "__main__":
    unittest.main()


class DeviceNameWhitelistTest(unittest.TestCase):
    """/api/smart 的设备名白名单：绝不能把任意字符串拼进 smartctl 的 argv。"""

    GOOD = ("sda", "sdb", "sdz", "nvme0n1", "nvme12n3", "hda", "vda", "sdaa")
    BAD = ("", "../etc/passwd", "/dev/sda", "sda/../x", "sda;rm -rf /", "sda ", "sd1", "nvme0", "sd")

    def test_accepts_kernel_device_names(self):
        for d in self.GOOD:
            self.assertIsNotNone(srv._DEV_NAME_RE.match(d), d)

    def test_rejects_injection_attempts(self):
        for d in self.BAD:
            self.assertIsNone(srv._DEV_NAME_RE.match(d), d)


class SmartDetailCacheTest(unittest.TestCase):
    """按需读取的两个承诺：5 分钟内不重复跑 smartctl；休眠盘结果不被缓存污染。"""

    def test_second_call_hits_cache(self):
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=(SAS_OK, 0)) as rc:
            srv._SMART_CACHE.clear()
            a = srv.smart_detail_cached("sdz")
            b = srv.smart_detail_cached("sdz")
            self.assertEqual(rc.call_count, 1)
            self.assertFalse(a.get("cached"))
            self.assertTrue(b.get("cached"))

    def test_expired_cache_refetches(self):
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=(SAS_OK, 0)) as rc:
            srv._SMART_CACHE.clear()
            srv.smart_detail_cached("sdz")
            srv._SMART_CACHE["sdz"] = (time.time() - srv._SMART_TTL - 1, srv._SMART_CACHE["sdz"][1])
            srv.smart_detail_cached("sdz")
            self.assertEqual(rc.call_count, 2)

    def test_cache_is_per_device(self):
        with mock.patch.object(srv, "is_linux", return_value=True), \
                mock.patch.object(srv, "run_cmd", return_value=(SAS_OK, 0)) as rc:
            srv._SMART_CACHE.clear()
            srv.smart_detail_cached("sdz")
            srv.smart_detail_cached("sdy")
            self.assertEqual(rc.call_count, 2)
