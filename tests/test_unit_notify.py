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
        self.rows = {}

    def disk_health_read(self):
        return self.rows


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
        self.assertIn("重映射扇区 49 → 61", self.sent[-1][1])

    def test_warn_growth_sends_worse(self):
        # warn 内部的退化也要报，不能等到跨档才提醒
        self.scan([disk("sdk", "warn")])
        r = self.scan([disk("sdk", "warn", defects=53)])
        self.assertEqual(r["acts"], ["worse"])
        self.assertIn("重映射扇区 49 → 53", self.sent[-1][1])
        # 没有继续增长就不再发
        self.assertIsNone(self.scan([disk("sdk", "warn", defects=53)]))

    def test_pending_growth_sends_worse(self):
        self.scan([disk("sdk", "warn", pending_defects=0)])
        r = self.scan([disk("sdk", "warn", pending_defects=2)])
        self.assertEqual(r["acts"], ["worse"])
        self.assertIn("待映射扇区 0 → 2", self.sent[-1][1])

    def test_metrics_baseline_missing_is_silent(self):
        # 老版本状态文件没有 metrics：这一轮只补基线，不能凭空判「恶化」
        srv.write_json_secure(os.path.join(self.dir, srv.NOTIFY_STATE_FILE),
                              {"inited": 1, "notified": {"sdk": "warn"}})
        self.assertIsNone(self.scan([disk("sdk", "warn", defects=49)]))
        r = self.scan([disk("sdk", "warn", defects=52)])
        self.assertEqual(r["acts"], ["worse"])

    def test_unknown_grade_no_event(self):
        self.assertIsNone(self.scan([disk("sdz", "unknown")]))

    def test_rename_after_reboot_is_silent(self):
        # 断电重启后设备名变了（sdk → sdg），序列号没变：不能当「新增故障」再报一次
        self.scan([disk("sdk", "warn")])
        self.assertEqual(len(self.sent), 1)
        self.assertIsNone(self.scan([disk("sdg", "warn")]))
        self.assertEqual(len(self.sent), 1)

    def test_legacy_state_migrates_by_serial(self):
        # 老版本按设备名存状态；迁移到序列号之后，改名也不重报
        srv.write_json_secure(os.path.join(self.dir, srv.NOTIFY_STATE_FILE),
                              {"inited": 1, "notified": {"sdk": "crit"},
                               "metrics": {"sdk": {"defects": 49}}})
        self.db.rows = {"sdk": {"serial": "WAE0J50J", "grade": "crit", "defects": 49}}
        self.assertIsNone(self.scan([disk("sdg", "crit", defects=49)]))
        self.assertEqual(len(self.sent), 0)

    def test_name_reused_by_other_disk_drops_old_state(self):
        # 老状态挂在 sdk 上，但这个名字现在给了另一块盘：不能继承旧等级，
        # 该报的还得报
        srv.write_json_secure(os.path.join(self.dir, srv.NOTIFY_STATE_FILE),
                              {"inited": 1, "notified": {"sdk": "crit"}, "metrics": {}})
        self.db.rows = {"sdk": {"serial": "WS2331Z5", "grade": "ok"}}
        r = self.scan([disk("sdk", "warn", serial="WS2331Z5")])
        self.assertEqual(r["acts"], ["new"])

    def test_state_file_survives_restart(self):
        self.scan([disk("sdk", "warn")])
        with open(os.path.join(self.dir, srv.NOTIFY_STATE_FILE), encoding="utf-8") as fh:
            st = json.load(fh)
        # 键是「sn:序列号」而不是设备名：设备名跨重启会变
        self.assertEqual(st["notified"], {"sn:WAE0J50J": "warn"})
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
        self.assertIn("重映射扇区 49", body)
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


class HwNotifyTest(unittest.TestCase):
    """硬件告警：攒批 + 每日定时发送。

    与硬盘巡检通知的区别：硬盘「有变化立刻发」，硬件「到点统一发，
    到点前恢复的整条不发，一直不修的每 7 天再提醒，发过的恢复了补一封」。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        srv._HW_CHECK_TS[0] = 0.0
        srv._HW_JOB[0] = None
        self._cfg(hw_time="00:00")
        # iDRAC 配置与快照全部用桩：这些用例只验告警批次的状态机，不该联网
        p = mock.patch.object(srv, "idrac_load",
                              return_value={"enabled": 1, "host": "h", "user": "u",
                                            "password": "p", "sel_alert": 1})
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        srv._HW_CHECK_TS[0] = 0.0
        srv._HW_JOB[0] = None
        for f in ("notify.json", "notify-hw.json"):
            p = os.path.join(self.dir, f)
            if os.path.exists(p):
                os.remove(p)
        os.rmdir(self.dir)

    def _cfg(self, **kw):
        cfg = {"enabled": 1, "provider": "qq", "host": "smtp.qq.com", "port": 465,
               "secure": "ssl", "user": "u@qq.com", "password": "pw",
               "sender": "u@qq.com", "to": "d@qq.com", "sct_enabled": 0, "sct_key": "",
               "hw_enabled": 1, "hw_time": "00:00"}
        cfg.update(kw)
        with open(os.path.join(self.dir, "notify.json"), "w", encoding="utf-8") as f:
            json.dump(cfg, f)

    def _state(self, **kw):
        st = {"active": {}, "recovered": [], "day": "", "ts": 0}
        st.update(kw)
        with open(os.path.join(self.dir, "notify-hw.json"), "w", encoding="utf-8") as f:
            json.dump(st, f)
        return st

    def _read_state(self):
        with open(os.path.join(self.dir, "notify-hw.json"), encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _item(msg="The chassis is open while the power is on.", sev="Critical"):
        return {"message": msg, "sensor": "硬件日志", "severity": sev}

    # ---- 配置解析 ----
    def test_time_parse(self):
        self.assertEqual(srv.notify_hw_time({"hw_time": "8:05"}), (8, 5))
        self.assertEqual(srv.notify_hw_time({"hw_time": "23:59"}), (23, 59))
        self.assertEqual(srv.notify_hw_time({"hw_time": "25:00"}), (8, 0))
        self.assertEqual(srv.notify_hw_time({"hw_time": ""}), (8, 0))
        self.assertEqual(srv.notify_hw_time({}), (8, 0))

    # ---- 合并逻辑 ----
    def test_merge_new_keeps_first_seen(self):
        now = 1000.0
        hw = srv.notify_hw_merge(None, {"k1": self._item()}, now)
        self.assertEqual(hw["active"]["k1"]["first"], now)
        self.assertEqual(hw["active"]["k1"]["sent"], 0)
        hw2 = srv.notify_hw_merge(hw, {"k1": self._item()}, now + 100)
        self.assertEqual(hw2["active"]["k1"]["first"], now)      # 首次时间不被刷新
        self.assertEqual(hw2["active"]["k1"]["last"], now + 100)

    def test_merge_recovered_only_when_notified(self):
        now = 1000.0
        hw = srv.notify_hw_merge(None, {"k1": self._item(), "k2": self._item("x")}, now)
        hw["active"]["k1"]["sent"] = 1        # 通知过的
        hw = srv.notify_hw_merge(hw, {}, now + 50)
        # 只给通知过的补「已恢复」，没通知过的不打扰
        self.assertEqual([r["key"] for r in hw["recovered"]], ["k1"])
        self.assertEqual(hw["active"], {})

    def test_merge_reappeared_cancels_recovered(self):
        hw = {"active": {}, "recovered": [{"key": "k1", "message": "m", "first": 1}]}
        hw = srv.notify_hw_merge(hw, {"k1": self._item()}, 2000.0)
        self.assertEqual(hw["recovered"], [])
        self.assertIn("k1", hw["active"])

    # ---- 定时发送 ----
    def test_first_run_is_silent_baseline(self):
        """首次接入只记基线：机器上现存的异常不倒出来。"""
        with mock.patch.object(srv, "notify_hw_collect", return_value={"k1": self._item()}):
            with mock.patch.object(srv, "notify_send_all") as send:
                r = srv.notify_hw_tick(self.dir, {})
        self.assertTrue(r.get("baseline"))
        send.assert_not_called()
        st = self._read_state()
        self.assertEqual(st["active"]["k1"]["sent"], 1)
        self.assertTrue(st["day"])

    def test_send_new_with_first_seen(self):
        """到点仍没恢复 → 发一封，正文必须带发生时间。"""
        self._state(day="2000-01-01", active={
            "k1": {"message": "The chassis is open while the power is on.", "sensor": "硬件日志",
                   "severity": "Critical", "first": 1700000000, "last": 1700000000,
                   "sent": 0, "sent_ts": 0}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={"k1": self._item()}):
            with mock.patch.object(srv, "notify_send_all", return_value=(True, {})) as send:
                with mock.patch.object(srv, "notify_build",
                                       wraps=srv.notify_build) as build:
                    r = srv.notify_hw_tick(self.dir, {})
        self.assertTrue(r.get("ok"))
        send.assert_called_once()
        _args, kwargs = send.call_args
        subject, body = _args[1], _args[2]
        self.assertIn("硬件告警", subject)
        self.assertIn("发生时间", body)
        st = self._read_state()
        self.assertEqual(st["active"]["k1"]["sent"], 1)
        self.assertGreater(st["active"]["k1"]["sent_ts"], 0)

    def test_recovered_but_unnotified_is_silent(self):
        """到点前就恢复了、而且从没通知过 → 一条都不发。"""
        self._state(day="2000-01-01", active={
            "k1": {"message": "m", "sensor": "硬件日志", "severity": "Critical",
                   "first": 1000, "last": 1000, "sent": 0, "sent_ts": 0}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={}):
            with mock.patch.object(srv, "notify_send_all") as send:
                r = srv.notify_hw_tick(self.dir, {})
        send.assert_not_called()
        self.assertEqual(r.get("events"), 0)
        self.assertEqual(self._read_state()["active"], {})

    def test_recovered_after_notified_sends_notice(self):
        """通知过的告警后来恢复了 → 补一封「已恢复」。"""
        self._state(day="2000-01-01", active={
            "k1": {"message": "The chassis is open while the power is on.", "sensor": "硬件日志",
                   "severity": "Critical", "first": 1000, "last": 1000,
                   "sent": 1, "sent_ts": 1000}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={}):
            with mock.patch.object(srv, "notify_send_all", return_value=(True, {})) as send:
                r = srv.notify_hw_tick(self.dir, {})
        send.assert_called_once()
        subject = send.call_args[0][1]
        self.assertIn("解除", subject)
        self.assertEqual(self._read_state()["recovered"], [])

    def test_remind_every_7_days(self):
        """一直不修：7 天内不重复，满 7 天再提醒一次。"""
        import time as _t
        now = _t.time()
        self._state(day="2000-01-01", active={
            "k1": {"message": "m", "sensor": "硬件日志", "severity": "Critical",
                   "first": now - 86400 * 30, "last": now, "sent": 1,
                   "sent_ts": now - 86400 * 3}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={"k1": self._item("m")}):
            with mock.patch.object(srv, "notify_send_all") as send:
                r = srv.notify_hw_tick(self.dir, {})
        send.assert_not_called()               # 才 3 天，不提醒
        self.assertEqual(r.get("events"), 0)

        self._state(day="2000-01-01", active={
            "k1": {"message": "m", "sensor": "硬件日志", "severity": "Critical",
                   "first": now - 86400 * 30, "last": now, "sent": 1,
                   "sent_ts": now - 86400 * 8}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={"k1": self._item("m")}):
            with mock.patch.object(srv, "notify_send_all", return_value=(True, {})) as send:
                srv.notify_hw_tick(self.dir, {})
        send.assert_called_once()              # 满 7 天，再提醒

    def test_same_day_never_twice(self):
        import time as _t
        today = _t.strftime("%Y-%m-%d", _t.localtime())
        self._state(day=today, active={
            "k1": {"message": "m", "sensor": "x", "severity": "Critical",
                   "first": 1, "last": 1, "sent": 0, "sent_ts": 0}})
        with mock.patch.object(srv, "notify_hw_collect") as collect:
            with mock.patch.object(srv, "notify_send_all") as send:
                srv.notify_hw_tick(self.dir, {})
        collect.assert_not_called()
        send.assert_not_called()

    def test_send_failure_keeps_pending(self):
        """发送失败不能把 sent 置位，否则明天就不发了。"""
        self._state(day="2000-01-01", active={
            "k1": {"message": "m", "sensor": "x", "severity": "Critical",
                   "first": 1, "last": 1, "sent": 0, "sent_ts": 0}})
        with mock.patch.object(srv, "notify_hw_collect", return_value={"k1": self._item("m")}):
            with mock.patch.object(srv, "notify_send_all", return_value=(False, "smtp 超时")):
                r = srv.notify_hw_tick(self.dir, {})
        self.assertFalse(r.get("ok"))
        st = self._read_state()
        self.assertEqual(st["active"]["k1"]["sent"], 0)
        self.assertNotEqual(st.get("day"), srv.time.strftime("%Y-%m-%d"))

    def test_idrac_unreadable_keeps_state(self):
        """iDRAC 读不到时绝不能被当成「全部恢复」。"""
        self._state(day="2000-01-01", active={
            "k1": {"message": "m", "sensor": "x", "severity": "Critical",
                   "first": 1, "last": 1, "sent": 1, "sent_ts": 1}})
        with mock.patch.object(srv, "notify_hw_collect", return_value=None):
            with mock.patch.object(srv, "notify_send_all") as send:
                r = srv.notify_hw_tick(self.dir, {})
        self.assertIsNone(r)
        send.assert_not_called()
        self.assertIn("k1", self._read_state()["active"])

    # ---- 到点判断 ----
    def test_due_respects_time_and_disabled(self):
        import time as _t
        srv._HW_CHECK_TS[0] = 0.0
        early = _t.mktime((2026, 10, 21, 7, 0, 0, 0, 0, -1))
        self._cfg(hw_time="08:00")
        self.assertFalse(srv._hw_due(self.dir, early))
        srv._HW_CHECK_TS[0] = 0.0
        late = _t.mktime((2026, 10, 21, 8, 1, 0, 0, 0, -1))
        self.assertTrue(srv._hw_due(self.dir, late))
        srv._HW_CHECK_TS[0] = 0.0
        self._cfg(hw_time="08:00", hw_enabled=0)
        self.assertFalse(srv._hw_due(self.dir, late))


if __name__ == "__main__":
    unittest.main()
