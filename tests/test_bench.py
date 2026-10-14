"""Optional performance benchmarks (FNM_BENCH=1). A 7-day, 176-metric history DB (~1.8 M rows,
like a Docker-heavy NAS) is generated in a temp dir; heavy endpoints must stay within generous
budgets and streaming exports must not hold the whole result in memory.

    FNM_BENCH=1 python3 -m unittest tests/test_bench.py -v
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import tracemalloc
import unittest

from fnm_support import BENCH, load_server

srv = load_server()


@unittest.skipUnless(BENCH, "set FNM_BENCH=1 to run benchmarks")
class HistoryBench(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fnm-bench-")
        db = os.path.join(cls.tmp, "monitor.db")
        srv.History(db)
        now = time.time()
        t0 = now - 7 * 86400
        names = ["cpu", "mem", "load1", "disk_total", "temp", "power"] \
            + ["net_rx:veth%d" % i for i in range(20)] + ["net_tx:veth%d" % i for i in range(20)] \
            + ["net_rx_bytes:veth%d" % i for i in range(20)] + ["net_tx_bytes:veth%d" % i for i in range(20)] \
            + ["ctr_in:c%d" % i for i in range(20)] + ["ctr_out:c%d" % i for i in range(20)] \
            + ["diskio_r:sd%s" % c for c in "abcdefgh"] + ["diskio_w:sd%s" % c for c in "abcdefgh"] \
            + ["net_rx:eth0", "net_tx:eth0", "net_rx_bytes:eth0", "net_tx_bytes:eth0"] \
            + ["disk:/vol%d" % i for i in range(30)]
        c = sqlite3.connect(db)
        t, k, batch, n = t0, 0, [], 0
        while t < now:
            for m in names:
                batch.append((t, m, float(k % 97)))
            if len(batch) > 200000:
                c.executemany("INSERT INTO metrics VALUES(?,?,?)", batch)
                n += len(batch)
                batch = []
            t += 60
            k += 1
        c.executemany("INSERT INTO metrics VALUES(?,?,?)", batch)
        n += len(batch)
        c.commit()
        c.close()
        cls.db, cls.rows = db, n
        cfg = dict(srv.DEFAULT_CONFIG)
        cls.app = srv.MonitorApp(cls.tmp, cfg, cfg_dir=cls.tmp)
        sys.stderr.write("\n[bench] %d rows, %d metrics, %.0f MB\n" % (n, len(names), os.path.getsize(db) / 1e6))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def bench(self, label, fn, budget_s, peak_mb=None):
        tracemalloc.start()
        t = time.perf_counter()
        fn()
        el = time.perf_counter() - t
        peak = tracemalloc.get_traced_memory()[1] / 1e6
        tracemalloc.stop()
        sys.stderr.write("[bench] %-32s %7.3fs  peak %7.1f MB\n" % (label, el, peak))
        self.assertLess(el, budget_s, label)
        if peak_mb is not None:
            self.assertLess(peak, peak_mb, label)

    def test_endpoints(self):
        a = self.app
        self.bench("/api/traffic", a.api_traffic, 10)
        self.bench("/api/report", a.api_report, 30, peak_mb=100)
        self.bench("history net all", lambda: a._api_history_impl("net", "all"), 10)
        self.bench("history diskio 7d", lambda: a._api_history_impl("diskio", "7d"), 10)
        self.bench("history cpu 7d", lambda: a._api_history_impl("cpu", "7d"), 5)
        self.bench("export history 7d (stream)", lambda: sum(len(x) for x in a.iter_history_csv("7d")), 120, peak_mb=200)

    def test_z_cleanup(self):  # last: deletes a day of rows
        self.bench("cleanup(6d)", lambda: srv.History(self.db).cleanup(6), 120)


if __name__ == "__main__":
    unittest.main()
