"""UPS 采集层单元测试：实例名解析、品牌识别、变量别名适配。

覆盖常见驱动/品牌的真实变量差异（usbhid-ups 的 APC 系、nutdrv_qx / blazer_usb 的
Megatec 系、瓦力方程的 APC HID 兼容机型），不依赖真实 UPS 或 NUT 服务。
"""
import os

from fnm_support import load_server

ups = load_server()._ups_mod


def _raw(**kw):
    return dict(kw)


def test_upsc_list_parses_names_only():
    assert ups._parse_upsc_list("ups\napc\n") == ["ups", "apc"]


def test_upsc_list_parses_name_with_description():
    # 部分 NUT 版本 / 第三方封装会输出 "name: description"
    assert ups._parse_upsc_list("ups: W150\nmy-ups: APC Back-UPS\n") == ["ups", "my-ups"]


def test_upsc_list_ignores_blank_and_duplicates():
    assert ups._parse_upsc_list("\n\nups\nups\n") == ["ups"]


def test_vendor_match_common_brands():
    assert ups._vendor_match("WalleCube W150")["brand"] == "瓦力方程 WalleCube"
    assert ups._vendor_match("SANTAK TG-BOX 850")["brand"] == "山特 SANTAK"
    assert ups._vendor_match("American Power Conversion Back-UPS RS 1500")["brand"] == "APC / 施耐德"
    assert ups._vendor_match("CPS CP1500")["brand"] == "CyberPower"
    assert ups._vendor_match("未知厂商 XYZ") is None


def test_status_aliases_and_unknown_words():
    snap = ups.ups_build("ups", _raw(**{"ups.status": "ONLINE CHARGING TEST"}))
    live = snap["live"]
    assert live["st"] & 1                  # OL
    assert live["st"] & 16                 # CHRG
    assert "TEST" in live["labels"]


def test_build_wallecube_hid():
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OL",
        "device.mfr": "WalleCube",
        "device.model": "W150",
        "device.serial": "N/A",
        "ups.load": "12",
        "battery.charge": "100",
        "battery.runtime": "3600",
        "battery.voltage": "13.5",
        "ups.temperature": "25",
        "input.voltage": "230.0",
        "ups.power.nominal": "150",
        "driver.name": "usbhid-ups",
    }))
    dev, live = snap["device"], snap["live"]
    assert dev["brand"] == "瓦力方程 WalleCube"
    assert dev["model"] == "W150"
    assert dev["serial"] == ""          # N/A 视为空
    assert dev["va"] == 150
    assert live["chg"] == 100 and live["load"] == 12
    assert live["temp"] == 25           # 只有 ups.temperature 时也要能取到
    assert live["out_v"] is None and live["out_w"] is None


def test_build_apc_usbhid():
    snap = ups.ups_build("apc", _raw(**{
        "ups.status": "OL CHRG",
        "device.mfr": "American Power Conversion",
        "device.model": "Back-UPS RS 1500",
        "input.voltage": "229.0",
        "ups.load": "32",
        "ups.realpower": "240",
        "battery.charge": "97",
        "battery.runtime": "2400",
        "battery.voltage": "27.1",
        "ups.temperature": "31.4",
    }))
    live = snap["live"]
    assert snap["device"]["brand"] == "APC / 施耐德"
    assert live["out_w"] == 240         # 无输出电压/电流时回落到 ups.realpower
    assert live["temp"] == 31.4


def test_build_megatec_qx():
    snap = ups.ups_build("qx", _raw(**{
        "ups.status": "OL",
        "ups.mfr": "SANTAK",
        "ups.model": "Castle 1K",
        "input.voltage": "220.0",
        "output.voltage": "220.0",
        "output.current": "2.50",
        "ups.load": "55",
        "battery.charge": "88",
        "battery.voltage": "27.0",
        "driver.name": "nutdrv_qx",
    }))
    dev, live = snap["device"], snap["live"]
    assert dev["brand"].startswith("山特")
    assert dev["model"] == "Castle 1K"
    assert live["out_w"] == 550.0       # 220V × 2.5A


def test_zero_volt_transient_keeps_input_and_battery():
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OB",
        "input.voltage": "0",
        "output.voltage": "0",
        "output.current": "0",
        "ups.load": "40",
        "battery.charge": "76",
        "battery.runtime": "1200",
    }))
    live = snap["live"]
    assert live["valid"] is False
    assert live["out_v"] is None and live["out_w"] is None
    assert live["in_v"] == 0            # 输入 0V 是除草瞬间的真实信息，不能一起剔掉
    assert live["load"] == 40 and live["chg"] == 76


def test_three_phase_alias():
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OL",
        "input.L1-N.voltage": "230.5",
        "output.L1-N.voltage": "230.0",
        "output.L1.current": "1.20",
        "ups.load": "18",
        "battery.charge": "100",
    }))
    live = snap["live"]
    assert live["in_v"] == 230.5
    assert live["out_v"] == 230.0
    assert live["out_w"] == 276.0


def test_build_wallecube_w120_dc_output():
    # W120 为直流输出机型，正常输出约 19V，不能按交流 50V 阈值过滤
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OL",
        "ups.model": "W120",
        "output.voltage": "19.13",
        "output.current": "1.365",
        "ups.load": "20",
        "battery.charge": "100",
        "driver.name": "usbhid-ups",
    }))
    live = snap["live"]
    assert live["valid"] is True
    assert live["out_v"] == 19.13
    assert live["out_a"] == 1.365
    assert live["out_w"] == 26.1       # 19.13V × 1.365A


def test_build_prefers_reported_realpower():
    # 设备直报有功功率时优先使用，不再用电压 × 电流估算
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OL",
        "output.voltage": "19.13",
        "output.current": "1.365",
        "ups.realpower": "24",
        "battery.charge": "100",
    }))
    assert snap["live"]["out_w"] == 24


def test_low_voltage_without_current_has_no_power():
    snap = ups.ups_build("ups", _raw(**{
        "ups.status": "OL",
        "output.voltage": "19.13",
        "battery.charge": "100",
    }))
    live = snap["live"]
    assert live["valid"] is True and live["out_v"] == 19.13
    assert live["out_w"] is None


def test_nut_targets_user_config_first():
    t = ups._nut_targets({"nut_host": "192.168.1.9", "nut_port": 3494})
    assert t == ["192.168.1.9:3494"]      # 显式配置只连它，不回落到本机


def test_nut_targets_host_with_port():
    t = ups._nut_targets({"nut_host": "10.0.0.2:3500"})
    assert t[0] == "10.0.0.2:3500"


def test_nut_targets_defaults():
    t = ups._nut_targets({})
    assert t[0] in ("127.0.0.1:3493", "localhost")
    assert any("127.0.0.1" in x for x in t)


# ---------------------------------------------------------------------------
# 笔记本电池：upower / acpi 补充解析（issue #40）
# ---------------------------------------------------------------------------
_UPOWER_DUMP = """Daemon:
  daemon-version:  0.99.20
  on-battery:      yes

Device: /org/freedesktop/UPower/devices/line_power_AC
  native-path:          AC
  power supply:         yes
  line-power:
    online:              no

Device: /org/freedesktop/UPower/devices/battery_BAT0
  native-path:          BAT0
  vendor:               LGC
  model:                DELL XYZ
  serial:               1234
  power supply:         yes
  battery:
    present:             yes
    state:               discharging
    energy-full:         45.2 Wh
    energy-full-design:  52 Wh
    energy-rate:         9.5 W
    voltage:             12.5 V
    charge-cycles:       120
    time to empty:       2.5 hours
    percentage:          67%
    temperature:         30.5 degrees C
    technology:          lithium-ion
"""


def test_parse_upower_dump_battery_fields():
    a = ups._parse_upower_dump(_UPOWER_DUMP)
    assert a["state"] == "discharging"
    assert a["capacity"] == 67.0
    assert a["power_now_upower"] == 9.5
    assert a["voltage_now_upower"] == 12.5
    assert a["temp_upower"] == 30.5
    assert a["cycle_count"] == 120
    assert a["time_to_empty"] == 9000          # "2.5 hours"
    assert a["energy_full_upower"] == 45.2
    assert a["energy_full_design_upower"] == 52.0
    assert a["model"] == "DELL XYZ"            # 设备级属性也要读到
    assert a["technology"] == "lithium-ion"


def test_parse_upower_dump_ignores_line_power_section():
    a = ups._parse_upower_dump(_UPOWER_DUMP)
    assert "online" not in a                   # line-power 小节不能混进电池字段


def test_upower_time_to_seconds_formats():
    assert ups._upower_time_to_seconds("2.5 hours") == 9000
    assert ups._upower_time_to_seconds("1 hour 30 minutes") == 5400
    assert ups._upower_time_to_seconds("02:30:00") == 9000
    assert ups._upower_time_to_seconds("15 mins") == 900
    assert ups._upower_time_to_seconds("unknown") is None
    assert ups._upower_time_to_seconds("") is None


def test_parse_acpi_battery_discharging():
    # "Discharging" 含子串 "charging"，必须判定为放电而不是充电
    a = ups._parse_acpi_battery("Battery 0: Discharging, 42%, 01:20:00 remaining\n")
    assert a["status_acpi"] == "discharging"
    assert a["capacity_acpi"] == 42.0
    a2 = ups._parse_acpi_battery("Battery 0: Charging, 88%, 00:12:00 until charged\n")
    assert a2["status_acpi"] == "charging"


def test_battery_info_with_upower_fills_missing_only():
    sysfs = {"name": "BAT0", "status": "Unknown", "capacity": "67",
             "voltage_now": "12500000", "power_now": "0", "energy_full": "45200000"}
    orig_path, orig_run = ups._upower_path, ups._run
    ups._upower_path = lambda: "/usr/bin/upower"
    ups._run = lambda args, timeout=8: (0, _UPOWER_DUMP, "")
    try:
        info = ups._battery_info_with_upower(sysfs)
    finally:
        ups._upower_path, ups._run = orig_path, orig_run
    assert info["status"] == "discharging"     # sysfs 是 Unknown，采用 upower
    assert info["time_to_empty"] == 9000       # sysfs 没有，upower 补
    assert info["temp"] == 305.0               # °C → 0.1°C（与 sysfs 同口径）
    assert info["power_now"] == "9500000"      # sysfs 为 0，采用 upower 的 9.5W
    assert info["cycle_count"] == 120
    assert info["model_name"] == "DELL XYZ"
    assert info["source_hint"] == "sysfs+upower"


def test_battery_info_without_tools_keeps_sysfs():
    sysfs = {"name": "BAT0", "status": "Discharging", "capacity": "50"}
    orig_path, orig_acpi = ups._upower_path, ups._acpi_path
    ups._upower_path = lambda: ""
    ups._acpi_path = lambda: ""
    try:
        info = ups._battery_info_with_upower(sysfs)
    finally:
        ups._upower_path, ups._acpi_path = orig_path, orig_acpi
    assert info["status"] == "Discharging" and info["capacity"] == "50"
    assert info["source_hint"] == "sysfs"
