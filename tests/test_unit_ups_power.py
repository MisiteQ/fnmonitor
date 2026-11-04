"""NUT UPS output power is optional and never invents a reading on failure."""
import json
import subprocess
import unittest
from unittest import mock

from fnm_support import load_server


srv = load_server()


class UpsOutputPowerTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
