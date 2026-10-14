"""Unit tests: docker status -> Chinese, NetIO units, /proc container netns counters (fixture
tree), container rate diffing, small parsers; plus live smoke tests that only run where the real
/proc or a reachable docker daemon exist."""
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from fnm_support import IS_LINUX, load_server

srv = load_server()


class DockerStatusZhTest(unittest.TestCase):
    CASES = {
        "Up 3 days": ("运行 3 天", "运行 3 天"),
        "Up About an hour": ("运行 约 1 小时", "运行 约 1 小时"),
        "Up About a minute": ("运行 约 1 分钟", None),
        "Up Less than a second": ("运行 不到 1 秒", None),
        "Up 1 second": ("运行 1 秒", None),
        "Up 5 seconds (healthy)": ("运行 5 秒", "运行 5 秒 · 健康"),
        "Up 2 minutes (unhealthy)": ("运行 2 分钟", "运行 2 分钟 · 不健康"),
        "Up 1 minute (health: starting)": ("运行 1 分钟", "运行 1 分钟 · 健康检查中"),
        "Up 2 hours (Paused)": ("已暂停 · 已运行 2 小时", None),
        "Up 10 hours": ("运行 10 小时", None),
        "Up 3 weeks": ("运行 3 周", None),
        "Up 4 months": ("运行 4 个月", None),
        "Up 2 years": ("运行 2 年", None),
        "Exited (0) 2 weeks ago": ("已停止 2 周", "已停止（退出码 0） · 2 周前"),
        "Exited (137) 10 hours ago": ("已停止 10 小时", "已停止（退出码 137） · 10 小时前"),
        "Exited (2) About an hour ago": ("已停止 约 1 小时", None),
        "Exited (0) Less than a second ago": ("已停止 不到 1 秒", None),
        "Restarting (1) 3 seconds ago": ("重启中", "重启中（退出码 1） · 3 秒前退出"),
        "Paused": ("已暂停", None),
        "Created": ("已创建", None),
        "Dead": ("已失效", None),
        "Removal In Progress": ("删除中", None),
        "Stopped": ("已停止", None),
        "": ("", ""),
    }

    def test_cases(self):
        for st, (up, full) in self.CASES.items():
            with self.subTest(status=st):
                r = srv.parse_docker_status(st)
                self.assertEqual(r["uptime"], up)
                if full is not None:
                    self.assertEqual(r["status_zh"], full)
                for k in ("uptime", "status_zh"):
                    self.assertIsNone(re.search(r"[A-Za-z]", r[k]), "English left in %r: %r" % (st, r[k]))
                self.assertEqual(srv._docker_uptime_zh(st), up)

    def test_fields(self):
        self.assertEqual(srv.parse_docker_status("Exited (137) 1 day ago")["exit_code"], 137)
        r = srv.parse_docker_status("Up 5 seconds (healthy)")
        self.assertEqual(r["health"], "healthy")
        self.assertIsNone(r["exit_code"])


class NetIOTest(unittest.TestCase):
    def test_units(self):
        cases = {"145kB / 1.21MB": (145000, 1210000), "0B / 0B": (0, 0), "1.5GB / 2TB": (1500000000, 2000000000000),
                 "10.2KiB / 3MiB": (int(10.2 * 1024), 3 * 1048576), "648B / 1.1kB": (648, 1100),
                 "garbage": (0, 0), "": (0, 0), "1e3B / 2kB": (1000, 2000)}
        for k, v in cases.items():
            self.assertEqual(srv.parse_docker_netio(k), v, k)


class ProcNetnsTest(unittest.TestCase):
    DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo:  999999     10    0    0    0     0          0         0   999999      10    0    0    0     0       0          0
  eth0: 1234567    100    0    0    0     0          0         0  7654321      90    0    0    0     0       0          0
  eth1:     100      1    0    0    0     0          0         0      200       2    0    0    0     0       0          0
"""
    CID = "a" * 64

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="fnm-proc-")
        os.makedirs(os.path.join(self.root, "self", "ns"))
        os.symlink("net:[4026531840]", os.path.join(self.root, "self", "ns", "net"))
        self._mk(101, "0::/system.slice/docker-%s.scope\n" % self.CID, "net:[4026532999]")
        self._mk(102, "0::/user.slice/other\n", "net:[4026532998]")     # PID reused by another process
        self._mk(103, "0::/docker/%s\n" % self.CID, "net:[4026531840]")  # shares host netns

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _mk(self, pid, cgroup, ns):
        d = os.path.join(self.root, str(pid))
        os.makedirs(os.path.join(d, "ns"))
        os.makedirs(os.path.join(d, "net"))
        with open(os.path.join(d, "cgroup"), "w") as f:
            f.write(cgroup)
        os.symlink(ns, os.path.join(d, "ns", "net"))
        with open(os.path.join(d, "net", "dev"), "w") as f:
            f.write(self.DEV)

    def test_counters(self):
        self.assertEqual(srv._proc_netns_bytes(101, self.CID, self.root), (1234667, 7654521))

    def test_rejections(self):
        self.assertIsNone(srv._proc_netns_bytes(102, self.CID, self.root), "cgroup mismatch")
        self.assertIsNone(srv._proc_netns_bytes(103, self.CID, self.root), "host netns")
        self.assertIsNone(srv._proc_netns_bytes(999, self.CID, self.root), "missing pid")
        self.assertIsNone(srv._proc_netns_bytes(0, self.CID, self.root), "pid 0")


class ContainerRatesTest(unittest.TestCase):
    @staticmethod
    def ctr(i, o, state="running", src="stats", cid="c1"):
        return {"id": cid, "state": state, "net_in_bytes": i, "net_out_bytes": o, "net_src": src}

    def test_sequence(self):
        p = {}
        a = [self.ctr(1000, 2000)]
        p = srv.container_net_rates(p, a, 100.0)
        self.assertIsNone(a[0]["rx_rate"])
        self.assertIsNone(a[0]["tx_rate"])
        b = [self.ctr(31000, 5000)]
        p = srv.container_net_rates(p, b, 130.0)
        self.assertEqual((b[0]["rx_rate"], b[0]["tx_rate"]), (1000.0, 100.0))
        c = [self.ctr(10, 10)]
        p = srv.container_net_rates(p, c, 160.0)
        self.assertIsNone(c[0]["rx_rate"], "counter reset -> null")
        d = [self.ctr(310, 10)]
        p = srv.container_net_rates(p, d, 190.0)
        self.assertEqual(d[0]["rx_rate"], 10.0, "resumes after reset")
        e = [self.ctr(999999, 10, src="proc")]
        p = srv.container_net_rates(p, e, 220.0)
        self.assertIsNone(e[0]["rx_rate"], "source switch -> null")
        f = [self.ctr(0, 0, state="exited", src=None)]
        p = srv.container_net_rates(p, f, 250.0)
        self.assertIsNone(f[0]["rx_rate"])
        self.assertNotIn("c1", p, "stopped container forgotten")
        g = [self.ctr(5, 5, src=None)]
        p = srv.container_net_rates(p, g, 280.0)
        self.assertIsNone(g[0]["rx_rate"], "no source -> null")


class SmallParsersTest(unittest.TestCase):
    def test_pmu_config(self):
        self.assertEqual(srv._parse_pmu_config("config=0x200100"), 0x200100)
        self.assertEqual(srv._parse_pmu_config("config=12"), 12)
        self.assertIsNone(srv._parse_pmu_config("event=0x2"))

    def test_first_cpu(self):
        self.assertEqual(srv._first_cpu("0-3"), 0)
        self.assertEqual(srv._first_cpu("2,4"), 2)
        self.assertEqual(srv._first_cpu(""), 0)

    def test_ver_tuple_ordering(self):
        self.assertLess(srv._ver_tuple("2.9.0"), srv._ver_tuple("2.18.1"))
        self.assertLess(srv._ver_tuple("2.18.0"), srv._ver_tuple("2.18.1"))
        self.assertEqual(srv._ver_tuple("v2.18.1"), srv._ver_tuple("2.18.1"))

    def test_version_matches_manifest(self):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "manifest"), encoding="utf-8") as f:
            m = re.search(r'^\s*version\s*=\s*"?([^"\r\n]+?)"?\s*$', f.read(), re.M)
        self.assertEqual(srv.VERSION, m.group(1))

    def test_estimate_power_formula(self):
        # base = TDP * (cpu%/100 * 0.7 + 0.3) + disks_active * disk_w + nics * nic_w
        self.assertEqual(srv.estimate_power(0, 0, 1, 65, 8, 5), 24.5)
        self.assertEqual(srv.estimate_power(100, 4, 1, 65, 8, 5), 102.0)
        self.assertEqual(srv.estimate_power(None, 0, 0, 65, 8, 5), 19.5)


@unittest.skipUnless(IS_LINUX and os.path.exists("/proc/stat"), "needs Linux /proc")
class LiveProcSmokeTest(unittest.TestCase):
    def test_cpu_mem_net(self):
        a = srv.read_cpu_times()
        self.assertTrue(a)
        mem = srv.read_meminfo()
        self.assertGreater(mem.get("total", 0), 0)
        self.assertTrue(0 <= mem.get("percent", -1) <= 100)
        load = srv.read_loadavg()
        self.assertEqual(len(load), 3)
        self.assertGreater(srv.read_uptime(), 0)
        nets = srv.read_net_dev()
        self.assertTrue(nets)

    def test_own_netns_counters(self):
        # our own process is not in a docker cgroup: must be rejected, never crash
        self.assertIsNone(srv._proc_netns_bytes(os.getpid(), "f" * 64))


def _docker_ok():
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


class CollectDockerAvailabilityTest(unittest.TestCase):
    """docker 已安装但没有任何容器时 `docker ps -a` 输出为空，不能报「不可用」。"""

    def _run(self, ps_out, ps_rc):
        def fake(cmd, timeout=10):
            if cmd[:2] == ["docker", "ps"]:
                return ps_out, ps_rc
            return "", 0
        with mock.patch.object(srv, "run_cmd", side_effect=fake):
            return srv.collect_docker()

    def test_no_containers_is_available(self):
        r = self._run("", 0)
        self.assertTrue(r["available"], r["error"])
        self.assertEqual(r["containers"], [])
        self.assertEqual(r["error"], "")

    def test_command_failure_is_unavailable(self):
        r = self._run("", 1)
        self.assertFalse(r["available"])
        self.assertTrue(r["error"])


@unittest.skipUnless(_docker_ok(), "docker daemon not reachable")
class LiveDockerSmokeTest(unittest.TestCase):
    def test_collect_docker(self):
        r = srv.collect_docker()
        self.assertTrue(r["available"], r.get("error"))
        for c in r["containers"]:
            for k in ("id", "name", "state", "status_zh", "uptime"):
                self.assertIn(k, c)
            self.assertIsNone(re.search(r"[A-Za-z]", c["status_zh"]), c["status_zh"])


if __name__ == "__main__":
    unittest.main()
