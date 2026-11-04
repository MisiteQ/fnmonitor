"""NUT UPS output power is optional and never invents a reading on failure."""
import json
import os
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from fnm_support import load_server


srv = load_server()


class UpsOutputPowerTest(unittest.TestCase):
    def test_local_device_list_exposes_names_and_models(self):
        listed = subprocess.CompletedProcess(["upsc", "-l"], 0, "2340161\nother\n", "")
        first = subprocess.CompletedProcess(["upsc", "2340161", "ups.model"], 0, "W120\n", "")
        second = subprocess.CompletedProcess(["upsc", "other", "ups.model"], 0, "Other\n", "")
        with mock.patch.object(srv.subprocess, "run", side_effect=[listed, first, second]):
            result = srv.list_ups_devices()
        self.assertEqual(result["devices"], [{"name": "2340161", "model": "W120"},
                                             {"name": "other", "model": "Other"}])

    def test_device_list_failure_is_not_an_empty_success(self):
        with mock.patch.object(srv.subprocess, "run", side_effect=OSError("missing")):
            result = srv.list_ups_devices()
        self.assertFalse(result["ok"])

    def test_single_device_uses_output_voltage_and_current(self):
        listed = subprocess.CompletedProcess(["upsc", "-l"], 0, "2340161\n", "")
        readings = subprocess.CompletedProcess(
            ["upsc", "2340161"], 0,
            "battery.charge: 100\noutput.voltage: 19.13\noutput.current: 1.365\n",
            "Init SSL without certificate database\n",
        )
        with mock.patch.object(srv.subprocess, "run", side_effect=[listed, readings]) as run:
            result = srv.read_ups_output_power()
        self.assertEqual(result["total"], 26.11)
        self.assertEqual(result["source"], "ups")
        self.assertEqual(result["device"], "2340161")
        self.assertEqual(run.call_args_list[0].args[0], ["upsc", "-l"])
        self.assertEqual(run.call_args_list[1].args[0], ["upsc", "2340161"])

    def test_multiple_devices_require_explicit_selection(self):
        listed = subprocess.CompletedProcess(["upsc", "-l"], 0, "one\ntwo\n", "")
        with mock.patch.object(srv.subprocess, "run", return_value=listed):
            result = srv.read_ups_output_power()
        self.assertEqual(result["reason"], "choose_device")
        self.assertFalse(result["ok"])

    def test_missing_or_invalid_reading_is_unavailable(self):
        for output in ("output.voltage: 19.13\n", "output.voltage: nan\noutput.current: 1\n",
                       "output.voltage: 19\noutput.current: -1\n"):
            reading = subprocess.CompletedProcess(["upsc", "ups1"], 0, output, "")
            with self.subTest(output=output), mock.patch.object(srv.subprocess, "run", return_value=reading):
                result = srv.read_ups_output_power("ups1")
                self.assertFalse(result["ok"])
                self.assertNotIn("total", result)

    def test_command_failure_is_unavailable(self):
        with mock.patch.object(srv.subprocess, "run", side_effect=subprocess.TimeoutExpired(["upsc"], 3)):
            self.assertEqual(srv.read_ups_output_power("ups1")["reason"], "read_failed")

    def test_source_selection_survives_config_reload(self):
        data = json.dumps({"power_use_ups": 1, "power_ups_name": "2340161"})
        with mock.patch("builtins.open", mock.mock_open(read_data=data)), \
             mock.patch.object(srv.os.path, "exists", return_value=False):
            cfg = srv.load_config("unused")
        self.assertEqual(cfg["power_use_ups"], 1)
        self.assertEqual(cfg["power_ups_name"], "2340161")
        self.assertEqual(srv.DEFAULT_CONFIG["power_use_ups"], 0)

    def test_switch_starts_new_trend_without_deleting_energy_history(self):
        with tempfile.TemporaryDirectory() as path:
            cfg = dict(srv.DEFAULT_CONFIG, update_autocheck=0, disk_standby_protect=0)
            app = srv.MonitorApp(path, cfg, cfg_dir=path)
            now = time.time()
            app.collector.db.write([(now - 120, "power", 100), (now - 30, "power", 30)])
            before = app.collector.db.energy_kwh("power", 3600, default_dt=60)
            result, status, _ = app.api_config_save({"power_use_ups": 1})
            self.assertTrue(result["ok"])
            self.assertEqual(status, 200)
            self.assertGreater(cfg["power_trend_since"], now - 1)
            self.assertEqual(app.api_history("power", "1h")["points"], [])
            self.assertEqual(app.collector.db.energy_kwh("power", 3600, default_dt=60), before)
            app.collector.db.write([(time.time(), "power", 25)])
            app._hist_cache.clear()
            self.assertEqual(app.api_history("power", "1h")["points"][-1][1], 25)
            energy_after_sample = app.collector.db.energy_kwh("power", 3600, default_dt=60)
            loaded = srv.load_config(path)
            self.assertEqual(loaded["power_trend_since"], cfg["power_trend_since"])
            first_since = cfg["power_trend_since"]
            self.assertTrue(app.api_config_save({"power_ups_name": "2340161"})[0]["ok"])
            self.assertGreater(cfg["power_trend_since"], first_since)
            self.assertEqual(app.api_history("power", "1h")["points"], [])
            second_since = cfg["power_trend_since"]
            self.assertTrue(app.api_config_save({"power_use_ups": 0})[0]["ok"])
            self.assertGreater(cfg["power_trend_since"], second_since)
            self.assertEqual(app.api_history("power", "1h")["points"], [])
            self.assertEqual(app.collector.db.energy_kwh("power", 3600, default_dt=60), energy_after_sample)

    def test_upgrade_with_enabled_ups_starts_one_persistent_segment(self):
        with tempfile.TemporaryDirectory() as path:
            with open(os.path.join(path, "config.json"), "w", encoding="utf-8") as f:
                json.dump({"power_use_ups": 1}, f)
            cfg = srv.load_config(path)
            app = srv.MonitorApp(path, cfg, cfg_dir=path)
            first_since = app.config["power_trend_since"]
            self.assertGreater(first_since, 0)
            reloaded = srv.load_config(path)
            self.assertEqual(reloaded["power_trend_since"], first_since)


if __name__ == "__main__":
    unittest.main()
