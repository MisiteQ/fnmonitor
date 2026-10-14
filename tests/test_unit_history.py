"""Unit tests on synthetic SQLite history DBs (temp dirs only):
- daily summary over 45 simulated days (retention 7, downtime gap, counter reset, disk standby,
  restarts, repeated cleanups) and the /api/daily, today/month energy + traffic built on it;
- energy integral not truncated (old LIMIT 7200 bug), traffic totals, bridge exclusion;
- monthly archive counted exactly once across repeated maintenance;
- history downsampling (<= ~720 points, 7d really spans 7 days), CSV exports, report thresholds,
  query plans use the (metric, ts) index.
"""
import datetime
import json
import os
import random
import shutil
import sqlite3
import tempfile
import time
import unittest
from unittest import mock

from fnm_support import load_server

srv = load_server()

_OLD_TZ = None


def setUpModule():
    # Day arithmetic below assumes a DST-free zone; fnOS boxes are typically Asia/Shanghai.
    global _OLD_TZ
    if hasattr(time, "tzset"):
        _OLD_TZ = os.environ.get("TZ")
        os.environ["TZ"] = "Asia/Shanghai"
        time.tzset()


def tearDownModule():
    if hasattr(time, "tzset"):
        if _OLD_TZ is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = _OLD_TZ
        time.tzset()


def dkey(ts):
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def new_app(data_dir, **cfg):
    c = dict(srv.DEFAULT_CONFIG)
    c.update(cfg)
    cfg_dir = os.path.join(data_dir, "cfg")
    os.makedirs(cfg_dir, exist_ok=True)
    return srv.MonitorApp(data_dir, c, cfg_dir=cfg_dir)


# ---------------------------------------------------------------------------
# 45-day daily summary simulation (fake clock)
# ---------------------------------------------------------------------------
class DailySummarySimulationTest(unittest.TestCase):
    DAYS = 45
    RETENTION = 7
    CLEANUP_EVERY = 3 * 3600      # collector runs hourly; 3 h keeps the test fast, still ~360 cleanups

    @classmethod
    def setUpClass(cls):
        cls.clock = [0.0]
        cls.tmp = tempfile.mkdtemp(prefix="fnm-daily-")
        with mock.patch.object(srv.time, "time", lambda: cls.clock[0]):
            cls._simulate()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self._p = mock.patch.object(srv.time, "time", lambda: self.clock[0])
        self._p.start()

    def tearDown(self):
        self._p.stop()

    @classmethod
    def _simulate(cls):
        CLOCK = cls.clock
        cls.dbp = dbp = os.path.join(cls.tmp, "monitor.db")
        H = srv.History(dbp)
        day0 = int(time.mktime((2026, 8, 20, 0, 0, 0, 0, 0, -1)))
        cls.day0 = day0
        truth = {}

        def tadd(day, k, v):
            truth.setdefault(day, {})
            truth[day][k] = truth[day].get(k, 0.0) + v
        rnd = random.Random(7)
        rx, tx, vrx, prev_rx = 10 ** 9, 5 * 10 ** 8, 0, None
        gaps = set(range(day0 + 30 + 20 * 86400 + 5 * 3600, day0 + 30 + 20 * 86400 + 8 * 3600, 60))  # 3 h down
        cls.reboot_at = reboot_at = day0 + 25 * 86400 + 12 * 3600          # counter reset
        standby = (day0 + 30 * 86400 + 2 * 3600, day0 + 30 * 86400 + 6 * 3600)  # disk asleep 4 h
        pending = []
        t = day0 + 30
        last_cleanup = -1e18
        cls.restarts = 0
        while t < day0 + cls.DAYS * 86400 - 1800:
            if t in gaps:
                t += 60
                continue
            CLOCK[0] = t + 0.5
            pw = 50 + rnd.random() * 100
            cpu = rnd.random() * 100
            if abs(t - reboot_at) < 30:
                rx, tx = 12345, 6789
                inc_rx, inc_tx = 12345, 6789
            else:
                inc_rx, inc_tx = rnd.randint(0, 5000000), rnd.randint(0, 1000000)
                rx += inc_rx
                tx += inc_tx
            vinc = rnd.randint(0, 1000)
            vrx += vinc
            day = dkey(t)
            tadd(day, "kwh", pw * 60 / 3.6e6)
            tadd(day, "power_s", 60)
            if prev_rx is not None:
                tadd(day, "rx", inc_rx)
                tadd(day, "tx", inc_tx)
                tadd(day, "vrx", vinc)
            prev_rx = rx
            tadd(day, "cpu_sum", round(cpu, 2))
            tadd(day, "cpu_n", 1)
            truth[day]["cpu_max"] = max(truth[day].get("cpu_max", 0), round(cpu, 2))
            pending += [(t, "power", round(pw, 6)), (t, "cpu", round(cpu, 2)), (t, "net_rx_bytes:eth0", float(rx)),
                        (t, "net_tx_bytes:eth0", float(tx)), (t, "net_rx_bytes:docker0", float(vrx)),
                        (t, "net_tx_bytes:docker0", 0.0), (t, "net_rx_bytes:lo", float(t))]
            asleep = standby[0] <= t < standby[1]
            due = t - last_cleanup >= cls.CLEANUP_EVERY
            if not asleep and (len(pending) >= 140 or due):
                H.write(pending)
                pending = []
            if due and not asleep:
                last_cleanup = t
                # mirrors Collector: the daily target never passes rows still buffered in memory
                tgt = t - 600
                if pending:
                    tgt = min(tgt, min(r[0] for r in pending) - 1)
                H.cleanup(cls.RETENTION, daily_target=tgt, default_dt=60)
                if rnd.random() < 0.1:   # repeated cleanup in the same tick + restart
                    H.cleanup(cls.RETENTION, daily_target=t - 600, default_dt=60)
                    H = srv.History(dbp)
                    cls.restarts += 1
                    H.archive_daily(t - 7200)       # stale lower target must be a no-op
            t += 60
        H.write(pending)
        CLOCK[0] = t
        cls.H = H
        cls.truth = truth
        cls.rows = H.daily_rows(31, default_dt=60)

    def test_retention_deleted_old_rows(self):
        conn = sqlite3.connect(self.dbp)
        try:
            oldest = conn.execute("SELECT MIN(ts) FROM metrics").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(oldest, self.clock[0] - self.RETENTION * 86400 - self.CLEANUP_EVERY - 100)

    def test_31_days_exact_despite_retention(self):
        nd = 0
        for dayn in range(self.DAYS - 31, self.DAYS):
            day = dkey(self.day0 + dayn * 86400 + 3600)
            tr, got = self.truth.get(day), self.rows.get(day)
            if not tr:
                continue
            self.assertIsNotNone(got, day)
            nd += 1
            for k, gk in (("kwh", "kwh"), ("power_s", "power_s"), ("rx", "rx_phys"), ("tx", "tx_phys"),
                          ("vrx", "rx_virt"), ("cpu_sum", "cpu_sum"), ("cpu_n", "cpu_n"), ("cpu_max", "cpu_max")):
                want, have = tr.get(k, 0.0), got.get(gk, 0.0)
                err = abs(have - want) / max(abs(want), 1e-9)
                self.assertLess(err, 1e-6, "day %s %s: got %r want %r" % (day, k, have, want))
        self.assertEqual(nd, 31)

    def test_lo_excluded(self):
        self.assertFalse(any(k.startswith("lo") for d in self.rows.values() for k in d))

    def test_gap_day_and_reboot_day(self):
        gd = dkey(self.day0 + 20 * 86400 + 3600)
        self.assertAlmostEqual(self.rows[gd]["power_s"], 21 * 3600, places=6)
        rd = dkey(self.reboot_at)
        self.assertLess(abs(self.rows[rd]["rx_phys"] - self.truth[rd]["rx"]), 1)

    def test_idempotent_across_cleanup_archive_restart(self):
        conn = sqlite3.connect(self.dbp)
        try:
            q = "SELECT day, metric, value FROM daily_summary WHERE day >= '2026-08-01' ORDER BY 1, 2"
            before = conn.execute(q).fetchall()
            H = self.H
            for _ in range(3):
                H.cleanup(self.RETENTION, daily_target=self.clock[0] - 7200, default_dt=60)
                H.archive_daily(self.clock[0] - 7200)
                H = srv.History(self.dbp)
            self.assertEqual(conn.execute(q).fetchall(), before)
        finally:
            conn.close()

    def test_400_day_prune(self):
        conn = sqlite3.connect(self.dbp)
        try:
            conn.execute("INSERT OR REPLACE INTO daily_summary VALUES('2020-01-01','kwh',1)")
            conn.commit()
            self.H.archive_daily(self.clock[0] - 7000)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM daily_summary WHERE day='2020-01-01'").fetchone()[0], 0)
        finally:
            conn.close()

    def _app(self, **cfg):
        app = new_app(tempfile.mkdtemp(dir=self.tmp), **cfg)
        app.collector.db = srv.History(self.dbp)
        return app

    def test_api_daily(self):
        app = self._app(power_rate_yuan=0.5)
        j = app.api_daily("30")
        json.dumps(j)
        self.assertEqual(len(j["days"]), 30)
        self.assertEqual(j["days"][-1]["day"], dkey(self.clock[0]))
        self.assertTrue(all(d["has_data"] for d in j["days"]))
        td, tt = j["days"][-1], self.truth[dkey(self.clock[0])]
        self.assertLess(abs(td["kwh"] - round(tt["kwh"], 4)), 1e-3, "today incl. live tail")
        self.assertLessEqual(abs(td["rx"] - (tt["rx"] + tt["vrx"])), 1, "bridge counted by default")
        self.assertLess(abs(td["cost"] - round(td["kwh"] * 0.5, 2)), 0.011)
        self.assertIsNotNone(td["cpu_avg"])
        self.assertIsNotNone(td["cpu_max"])
        for k in ("day", "has_data", "kwh", "cost", "power_h", "avg_w", "rx", "tx", "cpu_avg", "cpu_max"):
            self.assertIn(k, td)
        app.config["traffic_exclude_bridge"] = 1
        app._daily_cache.clear()
        self.assertLessEqual(abs(app.api_daily(30)["days"][-1]["rx"] - tt["rx"]), 1)
        j3 = app.api_daily("9999")
        self.assertEqual(len(j3["days"]), srv.History.DAILY_KEEP_DAYS)
        self.assertEqual(len(app.api_daily("x")["days"]), 30)
        self.assertEqual(len(app.api_daily("0")["days"]), 1)
        empty = [d for d in j3["days"] if not d["has_data"]][0]
        for k in ("kwh", "cost", "rx", "tx", "cpu_avg", "cpu_max", "power_h"):
            self.assertIsNone(empty[k], k)

    def test_month_totals_from_daily(self):
        app = self._app()
        dm = app._daily_today_month()
        mon = dkey(self.clock[0])[:8]
        mk = sum(v.get("kwh", 0) for d, v in self.truth.items() if d.startswith(mon))
        self.assertLess(abs(dm["month_kwh"] - mk), 0.01, "not truncated by retention")
        mrx = sum(v.get("rx", 0) + v.get("vrx", 0) for d, v in self.truth.items() if d.startswith(mon))
        self.assertLess(abs(dm["month_rx"] - mrx), 32)

    def test_power_stats_and_traffic_use_daily_basis(self):
        app = self._app()
        ps = app.api_power_stats()
        self.assertEqual(ps["energy"]["basis"], "daily")
        tt = self.truth[dkey(self.clock[0])]
        self.assertLess(abs(ps["energy"]["today_kwh"] - tt["kwh"]), 1e-3)
        tr = app.api_traffic()
        self.assertEqual(tr["totals"]["basis"], "daily")
        self.assertLessEqual(abs(tr["totals"]["today_rx"] - (tt["rx"] + tt["vrx"])), 1)
        self.assertLessEqual(abs(tr["totals"]["today_tx"] - tt["tx"]), 1)

    def test_upgrade_existing_db_without_watermark(self):
        d2 = tempfile.mkdtemp(dir=self.tmp)
        p2 = os.path.join(d2, "monitor.db")
        H2 = srv.History(p2)
        base = time.mktime((2026, 9, 1, 0, 0, 30, 0, 0, -1))
        H2.write([(base + i * 60, "power", 100.0) for i in range(3 * 1440)])
        saved = self.clock[0]
        try:
            self.clock[0] = base + 3 * 1440 * 60 + 100
            r0 = H2.daily_rows(5, 60)
            self.assertLessEqual(set(k for k in r0 if k != "_meta"), {dkey(self.clock[0])},
                                 "before the first archive only today is aggregated (no full scan)")
            H2.cleanup(7, daily_target=self.clock[0] - 600, default_dt=60)
            r1 = H2.daily_rows(5, 60)
            for i in range(3):
                dd = dkey(base + i * 86400 + 3600)
                self.assertAlmostEqual(r1[dd]["kwh"], 2.4, places=9)
        finally:
            self.clock[0] = saved


# ---------------------------------------------------------------------------
# Real-clock synthetic datasets
# ---------------------------------------------------------------------------
class EnergyTrafficTest(unittest.TestCase):
    """10 days of 60 s samples ending now: 14400 power rows (> the old 7200-row query limit)."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fnm-energy-")
        cls.now = time.time()
        H = srv.History(os.path.join(cls.tmp, "monitor.db"))
        rows, truth = [], {}
        t = cls.now - 10 * 86400
        i = 0
        while t < cls.now - 60:
            day = dkey(t)
            d = truth.setdefault(day, {"kwh": 0.0, "rx": 0, "tx": 0, "vrx": 0})
            d["kwh"] += 100.0 * 60 / 3.6e6
            if i:
                d["rx"] += 1000
                d["tx"] += 500
                d["vrx"] += 10
            rows += [(t, "power", 100.0), (t, "cpu", float(i % 100)),
                     (t, "net_rx_bytes:eth0", 1000.0 * i), (t, "net_tx_bytes:eth0", 500.0 * i),
                     (t, "net_rx_bytes:veth1", 10.0 * i), (t, "net_tx_bytes:veth1", 0.0),
                     (t, "net_rx_bytes:lo", 1e6 * i)]
            t += 60
            i += 1
        H.write(rows)
        cls.n = i
        cls.truth = truth
        H.cleanup(31, daily_target=cls.now - 600, default_dt=60)
        cls.H = H

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def app(self, **cfg):
        a = new_app(self.tmp, history_interval=60, retention_days=31, **cfg)
        return a

    def test_energy_integral_not_truncated(self):
        kwh = self.H.energy_kwh("power", 11 * 86400, default_dt=60)
        self.assertGreater(self.n, 7200)
        self.assertAlmostEqual(kwh, round(100.0 * self.n * 60 / 3.6e6, 4), places=3)

    def test_power_stats_today_month(self):
        ps = self.app().api_power_stats()
        today = dkey(time.time())
        month = today[:8]
        self.assertAlmostEqual(ps["energy"]["today_kwh"], self.truth[today]["kwh"], places=3)
        mk = sum(v["kwh"] for d, v in self.truth.items() if d.startswith(month))
        self.assertAlmostEqual(ps["energy"]["month_kwh"], mk, places=2)
        self.assertEqual(ps["cost"]["today_yuan"], round(ps["energy"]["today_kwh"] * 0.6, 2))

    def test_traffic_totals(self):
        today = dkey(time.time())
        month = today[:8]
        tr = self.app().api_traffic()["totals"]
        tt = self.truth[today]
        self.assertEqual(tr["today_rx"], tt["rx"] + tt["vrx"], "lo excluded, bridge counted")
        self.assertEqual(tr["today_tx"], tt["tx"])
        self.assertEqual(tr["month_rx"], sum(v["rx"] + v["vrx"] for d, v in self.truth.items() if d.startswith(month)))
        tr2 = self.app(traffic_exclude_bridge=1).api_traffic()["totals"]
        self.assertEqual(tr2["today_rx"], tt["rx"])


class MonthlyArchiveTest(unittest.TestCase):
    """Previous month + this month at 10-minute steps; repeated maintenance must not double count."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fnm-monthly-")
        dn = datetime.datetime.now()
        cls.month_start = datetime.datetime(dn.year, dn.month, 1).timestamp()
        prev = datetime.datetime(dn.year, dn.month, 1) - datetime.timedelta(days=1)
        cls.prev_start = datetime.datetime(prev.year, prev.month, 1).timestamp()
        cls.pk = (prev.year, prev.month)
        cls.cur = (dn.year, dn.month)
        cls.db = os.path.join(cls.tmp, "monitor.db")
        H = srv.History(cls.db)
        rows = []
        now = time.time()
        t = cls.prev_start + 15
        while t < now:
            rows += [(t, "power", 50.0), (t, "cpu", 10.0)]
            t += 600
        H.write(rows)
        truth = {}
        for ts, m, v in rows:
            d = datetime.datetime.fromtimestamp(ts)
            k = (d.year, d.month, m)
            s, c = truth.get(k, (0.0, 0))
            truth[k] = (s + v, c + 1)
        cls.rows, cls.truth = rows, truth
        for _ in range(5):
            H.cleanup(30, archive_enabled=True)
        H.cleanup(7, archive_enabled=True)
        H.cleanup(7, archive_enabled=True)
        srv.History(cls.db).cleanup(7, archive_enabled=True)   # "restart" then maintain again
        cls.cutoff7 = time.time() - 7 * 86400

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def summary(self):
        c = sqlite3.connect(self.db)
        try:
            return {(y, m, mt): (s, n) for y, m, mt, s, n in
                    c.execute("SELECT year, month, metric, sum_val, count_val FROM monthly_summary")}
        finally:
            c.close()

    def test_previous_month_counted_once(self):
        S = self.summary()
        for m in ("power", "cpu"):
            self.assertEqual(S.get(self.pk + (m,), (0, 0))[1], self.truth[self.pk + (m,)][1], m)

    def test_current_month_archived_once(self):
        S = self.summary()
        want = sum(1 for ts, m, v in self.rows if m == "cpu" and self.month_start <= ts < self.cutoff7)
        got = S.get(self.cur + ("cpu",), (0, 0))[1]
        # rows that crossed the cutoff between setUpClass maintenance and now may differ by one sample
        self.assertLessEqual(abs(got - want), 1, (got, want))

    def test_monthly_api(self):
        app = new_app(self.tmp, monthly_summary_enabled=1)
        ms = app.api_monthly_summary()
        self.assertTrue(ms["available"])
        self.assertEqual((ms["year"], ms["month"]), self.pk)
        self.assertEqual(ms["power"]["kwh"], round(self.truth[self.pk + ("power",)][0] * 60 / 3600000, 2))
        self.assertIn("note", ms)
        json.dumps(ms)

    def test_monthly_api_empty_db(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        ms = new_app(d).api_monthly_summary()
        self.assertFalse(ms["available"])


class HistoryQueryTest(unittest.TestCase):
    """8 days of 60 s samples for ~16 metrics; downsampling, ranges, CSV exports, report, plans."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fnm-hist-")
        H = srv.History(os.path.join(cls.tmp, "monitor.db"))
        now = time.time()
        cls.t_end = now - 30
        rows = []
        t = now - 8 * 86400
        i = 0
        while t < now - 30:
            rows += [(t, "cpu", float(i % 100)), (t, "mem", 40.0), (t, "temp", 50.0 + i % 5), (t, "power", 30.0),
                     (t, "net_rx:eth0", 5.0), (t, "net_tx:eth0", 6.0), (t, "net_rx:veth1", 50.0),
                     (t, "net_tx:veth1", 60.0), (t, "net_rx_bytes:eth0", 1000.0 * i),
                     (t, "net_tx_bytes:eth0", 500.0 * i), (t, "diskio_r:sda", 1.5), (t, "diskio_w:sda", 2.5),
                     (t, "diskio_r:sdb", 0.5), (t, "diskio_w:sdb", 0.25), (t, "ctr_in:web", 100.0 * i),
                     (t, "ctr_out:web", 50.0 * i)]
            t += 60
            i += 1
        H.write(rows)
        cls.n = i
        cls.H = H
        cls.app = new_app(cls.tmp, history_interval=60, retention_days=8)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def series(self, res):
        return {k: v for k, v in res.items() if isinstance(v, list)}

    def test_downsampling_bounds(self):
        for metric in ("cpu", "net", "net_bytes", "ctr_in", "diskio"):
            for rng in ("1h", "6h", "24h", "7d", "all"):
                res = self.app._api_history_impl(metric, rng)
                json.dumps(res)
                ser = self.series(res)
                self.assertTrue(ser, (metric, rng))
                for k, pts in ser.items():
                    with self.subTest(metric=metric, range=rng, series=k):
                        self.assertGreater(len(pts), 0)
                        self.assertLessEqual(len(pts), 725)

    def test_1h_raw_values(self):
        pts = self.app._api_history_impl("cpu", "1h")["points"]
        self.assertTrue(55 <= len(pts) <= 62, len(pts))
        raw = self.H.query("cpu", 3600)
        self.assertEqual([round(v, 2) for _, v in raw][-len(pts):], [p[1] for p in pts][-len(raw):])

    def test_7d_spans_7_days(self):
        pts = self.app._api_history_impl("cpu", "7d")["points"]
        self.assertGreater((pts[-1][0] - pts[0][0]) / 86400, 6.5)

    def test_aggregated_values(self):
        # downsampled diskio sums disks: r = 1.5 + 0.5, w = 2.5 + 0.25 (any bucket)
        res = self.app._api_history_impl("diskio", "7d")
        ser = self.series(res)
        flat = [p[1] for pts in ser.values() for p in pts]
        self.assertTrue(any(abs(v - 2.0) < 1e-6 for v in flat) and any(abs(v - 2.75) < 1e-6 for v in flat), ser.keys())

    def test_api_history_input_validation(self):
        self.assertEqual(self.app.api_history("cpu\x00", "1h")["points"], [])
        self.assertEqual(self.app.api_history("", "1h")["points"], [])
        self.assertEqual(self.app.api_history("cpu", "bogus")["range"], "1h")
        self.assertEqual(self.app.api_history("no_such_metric", "24h")["points"], [])

    def test_csv_exports(self):
        h = b"".join(self.app.iter_history_csv("24h"))
        self.assertTrue(h.startswith("﻿".encode("utf-8")))
        lines = h.decode("utf-8-sig").splitlines()
        self.assertIn("cpu", lines[0])
        self.assertTrue(1400 <= len(lines) - 1 <= 1445, len(lines))
        p7 = b"".join(self.app.iter_power_csv("7d")).decode("utf-8-sig").splitlines()
        self.assertGreaterEqual(len(p7) - 1, 7 * 1440 - 2, "power CSV 7d not truncated")
        tr = b"".join(self.app.iter_traffic_csv("24h")).decode("utf-8-sig").splitlines()
        self.assertTrue(any("net_rx_bytes:eth0" in c for c in tr[0].split(",")), tr[0])
        self.assertGreater(len(tr), 1000)

    def test_csv_empty_db(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        out = b"".join(new_app(d).iter_history_csv("7d")).decode("utf-8-sig")
        self.assertIn("暂无", out)

    def test_report_thresholds(self):
        r = self.app.api_report()
        json.dumps(r)
        self.assertAlmostEqual(r["summary"]["cpu"]["avg"], 49.5, delta=1)
        self.assertEqual(r["alerts"], [])
        d = tempfile.mkdtemp(dir=self.tmp)
        shutil.copy(os.path.join(self.tmp, "monitor.db"), os.path.join(d, "monitor.db"))
        app2 = new_app(d)
        self.assertTrue(app2.api_ui_set({"thresholds": {"cpuWarn": 30, "cpuCrit": 90, "tempWarn": 40, "tempCrit": 52}})["ok"])
        alerts = {a["item"]: a["level"] for a in app2.api_report()["alerts"]}
        self.assertEqual(alerts.get("CPU 占用（平均）"), "warn")
        self.assertEqual(alerts.get("CPU 温度（峰值）"), "crit")
        html = app2.api_report_html()
        self.assertIn("健康报告".encode("utf-8"), html)
        self.assertNotIn("报告生成失败".encode("utf-8"), html)

    def test_query_plans_use_index(self):
        c = sqlite3.connect(os.path.join(self.tmp, "monitor.db"))
        try:
            plans = {
                "prefix range": "SELECT ts, metric, value FROM metrics WHERE metric >= 'net_rx_bytes:' "
                                "AND metric < 'net_rx_bytes;' AND ts>=0 ORDER BY ts",
                "loose scan": "SELECT metric FROM metrics WHERE metric > 'a' ORDER BY metric LIMIT 1",
                "first_last": "SELECT value FROM metrics WHERE metric='x' AND ts>=0 ORDER BY ts DESC LIMIT 1",
                "delete": "DELETE FROM metrics WHERE metric='x' AND ts<0",
            }
            for k, q in plans.items():
                p = " | ".join(str(r[-1]) for r in c.execute("EXPLAIN QUERY PLAN " + q))
                self.assertIn("idx_metric_ts", p, k)
                self.assertNotIn("SCAN metrics ", p + " ", k)
        finally:
            c.close()

    def test_ui_set_filters_and_backup(self):
        d = tempfile.mkdtemp(dir=self.tmp)
        app = new_app(d)
        self.assertTrue(app.api_ui_set({"theme": "dark", "accent": "#12abef", "evil": 1})["ok"])
        self.assertTrue(app.api_ui_set({"theme": "light", "accent": "red;}</style><script>"})["ok"])
        ui = app.api_ui_get()["ui"]
        self.assertEqual(ui, {"theme": "light"})
        with open(os.path.join(d, "ui.json"), "w") as f:
            f.write("{broken")
        self.assertEqual(app.api_ui_get()["ui"].get("accent"), "#12abef", "falls back to ui.json.bak")
        self.assertFalse(app.api_ui_set({"layout": "x" * (300 * 1024)})["ok"])


if __name__ == "__main__":
    unittest.main()
