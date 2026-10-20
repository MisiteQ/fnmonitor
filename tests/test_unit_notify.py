"""硬盘告警通知单元测试：配置读写、状态机、正文生成、错误翻译。

不发真实邮件 / 不联网：notify_send_all 全部 mock，SMTP 与 Server酱只在
notify_test 的错误分支里被间接验证（未启用时不发送）。
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


def disk(dev, grade, **kw):
    d = {"dev": dev, "model": "ST300MP0026", "serial": "WAE0J50J", "grade": grade,
         "detail": "增长缺陷 49 个", "defects": 49, "pending_defects": 0, "uncorr": 0,
         "temp_c": 32, "power_on_hours": 52032, "standby": False}
    d.update(kw)
    return d


class FakeDB:
    def __init__(self, d):
        self.db_path = os.path.join(d, "history.db")


class NotifyCfgTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults(self):
        cfg = srv.notify_load(self.dir)
        self.assertEqual(cfg["enabled"], 0)
        self.assertEqual(cfg["sct_enabled"], 0)
        self.assertEqual(cfg["provider"], "qq")
        self.assertEqual(cfg["host"], "smtp.qq.com")
        self.assertEqual(cfg["port"], 465)
        self.assertEqual(cfg["secure"], "ssl")
        self.assertEqual(cfg["password"], "")

    def test_save_public_masks_secrets(self):
        cfg, err = srv.notify_save(self.dir, {"enabled": 1, "user": "a@qq.com", "to": "b@qq.com",
                                             "password": "authcode", "sct_enabled": 1,
                                             "sct_key": "SCT123"})
        self.assertEqual(err, "")
        pub = srv.notify_public(cfg)
        self.assertTrue(pub["password_set"])
        self.assertTrue(pub["sct_key_set"])
        self.assertNotIn("password", pub)
        self.assertNotIn("sct_key", pub)
        # 落盘内容确实有授权码，且权限收紧（Windows 无 POSIX 权限位，跳过）
        with open(os.path.join(self.dir, srv.NOTIFY_FILE), encoding="utf-8") as fh:
            raw = json.load(fh)
        self.assertEqual(raw["password"], "authcode")
        if os.name == "posix":
            self.assertEqual(os.stat(os.path.join(self.dir, srv.NOTIFY_FILE)).st_mode & 0o777, 0o600)

    def test_empty_secret_keeps_old_value(self):
        srv.notify_save(self.dir, {"password": "first", "sct_key": "SCT1"})
        cfg, err = srv.notify_save(self.dir, {"password": "", "sct_key": ""})
        self.assertEqual(err, "")
        self.assertEqual(cfg["password"], "first")
        self.assertEqual(cfg["sct_key"], "SCT1")

    def test_clear_secret_flags(self):
        srv.notify_save(self.dir, {"password": "first", "sct_key": "SCT1"})
        cfg, _ = srv.notify_save(self.dir, {"clear_password": 1, "clear_sct_key": 1})
        self.assertEqual(cfg["password"], "")
        self.assertEqual(cfg["sct_key"], "")

    def test_provider_preset_fills_host(self):
        cfg, err = srv.notify_save(self.dir, {"provider": "outlook"})
        self.assertEqual(err, "")
        self.assertEqual(cfg["host"], "smtp.office365.com")
        self.assertEqual(cfg["port"], 587)
        self.assertEqual(cfg["secure"], "starttls")

    def test_provider_custom_keeps_host(self):
        srv.notify_save(self.dir, {"provider": "custom", "host": "mail.example.com", "port": 2525})
        cfg, _ = srv.notify_save(self.dir, {"provider": "custom"})
        self.assertEqual(cfg["host"], "mail.example.com")
        self.assertEqual(cfg["port"], 2525)

    def test_enable_mail_without_fields_rejected(self):
        cfg, err = srv.notify_save(self.dir, {"enabled": 1})
        self.assertIn("缺少", err)
        self.assertFalse(os.path.exists(os.path.join(self.dir, srv.NOTIFY_FILE)))

    def test_enable_sct_without_key_rejected(self):
        _, err = srv.notify_save(self.dir, {"sct_enabled": 1})
        self.assertIn("SendKey", err)

    def test_bad_port_rejected(self):
        _, err = srv.notify_save(self.dir, {"port": "abc"})
        self.assertIn("端口", err)

    def test_unknown_values_ignored(self):
        cfg = srv.notify_load(self.dir)
        _, err = srv.notify_save(self.dir, {"provider": "nope", "secure": "plaintext"})
        self.assertEqual(err, "")
        cfg = srv.notify_load(self.dir)
        self.assertEqual(cfg["provider"], "qq")
        self.assertEqual(cfg["secure"], "ssl")


class NotifyStateTest(unittest.TestCase):
    """状态机：同一状态只发一次；同档计数上涨也发；失败不落状态；恢复补发一条。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.db = FakeDB(self.dir)
        srv.notify_save(self.dir, {"enabled": 1, "user": "a@qq.com", "to": "b@qq.com",
                                   "password": "x"})
        # 除首次静默那个用例，其余都从「已经建立过基线」开始
        self.mark_inited()
        self.sent = []

    def mark_inited(self):
        srv.write_json_secure(os.path.join(self.dir, srv.NOTIFY_STATE_FILE),
                              {"inited": 1, "notified": {}, "metrics": {}})

    def tearDown(self):
        self.tmp.cleanup()

    def _send(self, ok=True):
        def fake(cfg, subject, body):
            self.sent.append((subject, body))
            return ok, {"mail": {"ok": ok, "err": "" if ok else "认证失败"}}
        return mock.patch.object(srv, "notify_send_all", side_effect=fake)

    def scan(self, items, ok=True):
        with self._send(ok):
            return srv.notify_after_scan(items, 1700000000, self.db, {"port": 8777})

    def test_disabled_returns_none(self):
        os.remove(os.path.join(self.dir, srv.NOTIFY_FILE))
        self.assertIsNone(srv.notify_after_scan([disk("sda", "crit")], 1, self.db, {}))

    def test_new_fault_sends_once(self):
        r = self.scan([disk("sdk", "warn")])
        self.assertTrue(r and r["ok"])
        self.assertEqual(r["acts"], ["new"])
        self.assertIn("1 块异常", r["subject"])
        self.assertEqual(len(self.sent), 1)
        # 同状态再巡检一次：不发
        self.assertIsNone(self.scan([disk("sdk", "warn")]))
        self.assertEqual(len(self.sent), 1)

    def test_worse_then_recovered(self):
        self.scan([disk("sdk", "warn")])
        r = self.scan([disk("sdk", "crit")])
        self.assertEqual(r["acts"], ["worse"])
        r = self.scan([disk("sdk", "ok", defects=49, detail="SMART 通过")])
        self.assertEqual(r["acts"], ["recovered"])
        self.assertIn("恢复", r["subject"])
        self.assertEqual(len(self.sent), 3)

    def test_downgrade_is_silent(self):
        self.scan([disk("sdk", "crit")])
        self.assertIsNone(self.scan([disk("sdk", "warn")]))
        self.assertEqual(len(self.sent), 1)
        # 静默更新后，再次恶化仍会发（worse）
        r = self.scan([disk("sdk", "crit")])
        self.assertEqual(r["acts"], ["worse"])

    def test_send_failure_keeps_state_and_retries(self):
        r = self.scan([disk("sdk", "crit")], ok=False)
        self.assertFalse(r["ok"])
        self.assertEqual(r["channels"]["mail"]["err"], "认证失败")
        # 下一轮仍然判定为「新增」，说明状态没有被吞掉
        r2 = self.scan([disk("sdk", "crit")])
        self.assertEqual(r2["acts"], ["new"])
        self.assertEqual(len(self.sent), 2)
        # 成功后不再重复
        self.assertIsNone(self.scan([disk("sdk", "crit")]))

    def test_standby_skipped(self):
        self.scan([disk("sdk", "crit")])
        n = len(self.sent)
        self.assertIsNone(self.scan([disk("sdk", "crit", standby=True)]))
        self.assertEqual(len(self.sent), n)
        # 关键：休眠这一轮不能把已通知状态清掉，否则盘一醒又发一次「新增」
        self.assertIsNone(self.scan([disk("sdk", "crit")]))
        self.assertEqual(len(self.sent), n)

    def test_first_scan_is_silent(self):
        # 首次巡检只建基线不发信，否则刚配好就收到一封「一直都在」的清单
        os.remove(os.path.join(self.dir, srv.NOTIFY_STATE_FILE))
        r = self.scan([disk("sdk", "warn")])
        self.assertTrue(r and r.get("silent"))
        self.assertEqual(len(self.sent), 0)
        # 基线已建立：同状态再跑不发，但计数上涨要发
        self.assertIsNone(self.scan([disk("sdk", "warn")]))
        r2 = self.scan([disk("sdk", "warn", defects=61)])
        self.assertEqual(r2["acts"], ["worse"])
        self.assertIn("增长缺陷 49 → 61", self.sent[-1][1])

    def test_warn_growth_sends_worse(self):
        # warn 内部的退化也要报，不能等到跨档才提醒
        self.scan([disk("sdk", "warn")])
        r = self.scan([disk("sdk", "warn", defects=53)])
        self.assertEqual(r["acts"], ["worse"])
        self.assertIn("增长缺陷 49 → 53", self.sent[-1][1])
        # 没有继续增长就不再发
        self.assertIsNone(self.scan([disk("sdk", "warn", defects=53)]))

    def test_pending_growth_sends_worse(self):
        self.scan([disk("sdk", "warn", pending_defects=0)])
        r = self.scan([disk("sdk", "warn", pending_defects=2)])
        self.assertEqual(r["acts"], ["worse"])
        self.assertIn("待处理扇区 0 → 2", self.sent[-1][1])

    def test_metrics_baseline_missing_is_silent(self):
        # 老版本状态文件没有 metrics：这一轮只补基线，不能凭空判「恶化」
        srv.write_json_secure(os.path.join(self.dir, srv.NOTIFY_STATE_FILE),
                              {"inited": 1, "notified": {"sdk": "warn"}})
        self.assertIsNone(self.scan([disk("sdk", "warn", defects=49)]))
        r = self.scan([disk("sdk", "warn", defects=52)])
        self.assertEqual(r["acts"], ["worse"])

    def test_unknown_grade_no_event(self):
        self.assertIsNone(self.scan([disk("sdz", "unknown")]))

    def test_state_file_survives_restart(self):
        self.scan([disk("sdk", "warn")])
        with open(os.path.join(self.dir, srv.NOTIFY_STATE_FILE), encoding="utf-8") as fh:
            st = json.load(fh)
        self.assertEqual(st["notified"], {"sdk": "warn"})
        self.assertTrue(st["last"]["ok"])


class NotifyBuildTest(unittest.TestCase):
    def test_sections_and_counts(self):
        ev = [{"act": "new", "dev": "sdk", "d": disk("sdk", "crit", defects=49), "text": "缺陷增长 +12 个"},
              {"act": "recovered", "dev": "sdx", "d": disk("sdx", "ok"), "text": "故障消失，SMART 通过"}]
        subject, body = srv.notify_build(ev, 1700000000, 8777)
        self.assertIn("1 块异常", subject)
        self.assertIn("【新增故障】", body)
        self.assertIn("【已恢复】", body)
        self.assertIn("sdk", body)
        self.assertIn("增长缺陷 49", body)
        self.assertIn("检测时间", body)

    def test_recovery_only_subject(self):
        ev = [{"act": "recovered", "dev": "sdx", "d": disk("sdx", "ok"), "text": "故障消失"}]
        subject, _ = srv.notify_build(ev, 1700000000)
        self.assertIn("恢复", subject)

    def test_short_err(self):
        self.assertIn("认证失败", srv.notify_short_err("(535, b'Login fail')"))
        self.assertIn("超时", srv.notify_short_err("Connection timed out"))
        self.assertIn("无法解析", srv.notify_short_err("getaddrinfo failed"))
        self.assertIn("拒绝连接", srv.notify_short_err("Connection refused"))
        self.assertEqual(srv.notify_short_err(""), "未知错误")
        self.assertTrue(srv.notify_short_err("x" * 200).endswith("…"))


class NotifyTestEndpointTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_nothing_enabled(self):
        r = srv.notify_test(self.dir)
        self.assertFalse(r["ok"])
        self.assertIn("开启", r["error"])

    def test_ok_path_calls_both_channels(self):
        srv.notify_save(self.dir, {"enabled": 1, "user": "a@qq.com", "to": "b@qq.com", "password": "x"})
        with mock.patch.object(srv, "notify_mail_send", return_value=(True, "")) as m:
            r = srv.notify_test(self.dir)
        self.assertTrue(r["ok"])
        self.assertTrue(m.called)
        self.assertIn("测试", m.call_args[0][1])

    def _both(self):
        srv.notify_save(self.dir, {"enabled": 1, "user": "a@qq.com", "to": "b@qq.com", "password": "x",
                                   "sct_enabled": 1, "sct_key": "SCT123"})

    def test_channel_mail_only_sends_mail(self):
        # 设置页每个通道一个按钮：点邮件的「发送测试」不能连带把微信也发出去
        self._both()
        with mock.patch.object(srv, "notify_mail_send", return_value=(True, "")), \
                mock.patch.object(srv, "notify_sct_send", return_value=(True, "")) as sct:
            r = srv.notify_test(self.dir, "mail")
        self.assertTrue(r["ok"])
        self.assertEqual(r["channel"], "mail")
        self.assertEqual(list(r["channels"]), ["mail"])
        self.assertFalse(sct.called)

    def test_channel_sct_only_sends_sct(self):
        self._both()
        with mock.patch.object(srv, "notify_mail_send", return_value=(True, "")) as mail, \
                mock.patch.object(srv, "notify_sct_send", return_value=(True, "")):
            r = srv.notify_test(self.dir, "sct")
        self.assertTrue(r["ok"])
        self.assertEqual(list(r["channels"]), ["sct"])
        self.assertFalse(mail.called)

    def test_channel_disabled_is_rejected(self):
        # 只开了邮件时点微信的测试：给出「微信通知当前是关闭的」，而不是悄悄发邮件
        srv.notify_save(self.dir, {"enabled": 1, "user": "a@qq.com", "to": "b@qq.com", "password": "x"})
        with mock.patch.object(srv, "notify_mail_send", return_value=(True, "")) as mail:
            r = srv.notify_test(self.dir, "sct")
        self.assertFalse(r["ok"])
        self.assertIn("微信", r["error"])
        self.assertIn("关闭", r["error"])
        self.assertFalse(mail.called)

    def test_unknown_channel_falls_back_to_all(self):
        self._both()
        with mock.patch.object(srv, "notify_mail_send", return_value=(True, "")), \
                mock.patch.object(srv, "notify_sct_send", return_value=(True, "")):
            r = srv.notify_test(self.dir, "bogus")
        self.assertEqual(sorted(r["channels"]), ["mail", "sct"])


class DiskScanNotifyHookTest(unittest.TestCase):
    """巡检结束后必须调用通知判定，且通知异常不能影响巡检结果。"""

    def test_notify_called_after_scan(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            captured = {}

            def fake_notify(items, ts, db, config=None):
                captured["items"] = items
                captured["config"] = config
                return {"ok": True, "acts": ["new"], "subject": "s"}

            with mock.patch.object(srv, "is_linux", return_value=True), \
                 mock.patch.object(srv, "disk_scan_devices", return_value=[{"dev": "sda", "size": 1, "model": "M"}]), \
                 mock.patch.object(srv, "smart_detail", return_value={"health": "OK", "serial": "S1"}), \
                 mock.patch.object(srv, "grade_disk", return_value=("ok", ["SMART 通过"])), \
                 mock.patch.object(srv, "notify_after_scan", side_effect=fake_notify):
                s = srv.disk_scan_once(FakeDB(tmp.name), {"port": 8777})
            self.assertTrue(s["ok"])
            self.assertEqual(s["notify"]["subject"], "s")
            self.assertEqual(len(captured["items"]), 1)
            self.assertEqual(captured["config"], {"port": 8777})
        finally:
            tmp.cleanup()

    def test_notify_crash_does_not_break_scan(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            with mock.patch.object(srv, "is_linux", return_value=True), \
                 mock.patch.object(srv, "disk_scan_devices", return_value=[{"dev": "sda", "size": 1, "model": "M"}]), \
                 mock.patch.object(srv, "smart_detail", return_value={"health": "OK", "serial": "S1"}), \
                 mock.patch.object(srv, "grade_disk", return_value=("ok", ["SMART 通过"])), \
                 mock.patch.object(srv, "notify_after_scan", side_effect=RuntimeError("boom")):
                # notify_after_scan 自身带 try/except 兜底，这里验证外层也不受影响
                s = srv.disk_scan_once(FakeDB(tmp.name), {"port": 8777})
            self.assertTrue(s["ok"])
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
