"""Unit tests: Intel iGPU PMU sampler (fake perf_event_open), RC6 / gtidle fallback, diag output.
All sysfs trees are temp fixtures; the real perf_event_open wrapper is only exercised on Linux."""
import errno
import json
import os
import shutil
import tempfile
import unittest

from fnm_support import IS_LINUX, load_server

srv = load_server()


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def make_sysfs(pmu_name="i915_0000_00_02.0", driver="i915", rc6_rel="gt/gt0/rc6_residency_ms", events=None):
    root = tempfile.mkdtemp(prefix="fnm-sysfs-")
    pd = os.path.join(root, "bus/event_source/devices", pmu_name)
    w(pd + "/type", "17\n")
    w(pd + "/cpumask", "0-3\n")
    ev = events or {
        "rcs0-busy": "config=0x0", "rcs0-wait": "config=0x1", "rcs0-sema": "config=0x2",
        "bcs0-busy": "config=0x100000", "vcs0-busy": "config=0x200000", "vcs1-busy": "config=0x200100",
        "vecs0-busy": "config=0x300000", "actual-frequency": "config=0x100000",
        "requested-frequency": "config=0x100001", "rc6-residency": "config=0x100002",
        "interrupts": "config=0x100003",
    }
    for k, v in ev.items():
        w(pd + "/events/" + k, v + "\n")
        if k.endswith("-busy") or k == "rc6-residency":
            w(pd + "/events/" + k + ".unit", "ns\n")
        if "frequency" in k:
            w(pd + "/events/" + k + ".unit", "M\n")
    w(os.path.join(root, "bus/event_source/devices/cpu/type"), "4\n")      # unrelated PMU
    dev = os.path.join(root, "devices/pci0000:00/0000:00:02.0")
    os.makedirs(dev)
    os.makedirs(os.path.join(root, "bus/pci/drivers", driver))
    os.symlink(os.path.join(root, "bus/pci/drivers", driver), os.path.join(dev, "driver"))
    card = os.path.join(root, "class/drm/card0")
    os.makedirs(card)
    os.symlink(dev, os.path.join(card, "device"))
    os.makedirs(os.path.join(root, "class/drm/card0-HDMI-A-1"))
    w(os.path.join(card, "gt_act_freq_mhz"), "650\n")
    w(os.path.join(card, "gt_cur_freq_mhz"), "700\n")
    if rc6_rel:
        w(os.path.join(card, rc6_rel), "1000\n")
    return root


class FakePerf:
    def __init__(self, deny=()):
        self.deny = set(deny)
        self.opened, self.vals, self.closed = {}, {}, []
        self.fail_read = False
        self.next = 100
        self.machine, self.nr = "x86_64", 298

    def __call__(self, ptype, cfg, cpu):
        if cfg in self.deny:
            raise OSError(errno.EACCES, "Permission denied")
        fd = self.next
        self.next += 1
        self.opened[fd] = (ptype, cfg, cpu)
        self.vals[fd] = 0
        return fd

    def read(self, fd):
        if self.fail_read:
            raise OSError(errno.ENODEV, "No such device")
        return self.vals[fd]

    def close(self, fd):
        self.closed.append(fd)


class GpuTestBase(unittest.TestCase):
    def setUp(self):
        self.T = [1000.0]
        self.clock = lambda: self.T[0]
        self.roots = []

    def tearDown(self):
        for r in self.roots:
            shutil.rmtree(r, ignore_errors=True)

    def sysfs(self, **kw):
        r = make_sysfs(**kw)
        self.roots.append(r)
        return r


class IntelPmuTest(GpuTestBase):
    def test_discovery_engines_and_failure_recovery(self):
        root = self.sysfs()
        fp = FakePerf()
        pmu = srv.IntelGpuPmu(sysfs=root, opener=fp, reader=fp.read, closer=fp.close, clock=self.clock,
                              paranoid_path=os.path.join(root, "nope"))
        self.assertEqual(pmu.sample("00:02.0"), {"warming": True})
        cfgs = sorted(v[1] for v in fp.opened.values())
        self.assertTrue(all(v[0] == 17 and v[2] == 0 for v in fp.opened.values()), "type 17, first cpu of 0-3")
        self.assertNotIn(0x1, cfgs)
        self.assertNotIn(0x100002, cfgs)
        self.assertEqual(len(fp.opened), 7, "rcs,bcs,vcs0,vcs1,vecs,act,req")
        names = dict(pmu._fds)
        self.T[0] += 10
        fp.vals[names["rcs0-busy"]] = int(2.5e9)
        fp.vals[names["bcs0-busy"]] = int(0.1e9)
        fp.vals[names["vcs0-busy"]] = int(4e9)
        fp.vals[names["vcs1-busy"]] = int(6e9)       # video = max(40, 60)
        fp.vals[names["vecs0-busy"]] = int(20e9)     # 200 % -> clamped 100
        fp.vals[names["actual-frequency"]] = 6500    # MHz*s over 10 s
        fp.vals[names["requested-frequency"]] = 9000
        r = pmu.sample("00:02.0")
        self.assertEqual(r["engines"], {"render": 25.0, "blitter": 1.0, "video": 60.0, "video_enhance": 100.0})
        self.assertEqual(r["util"], 100.0)
        self.assertEqual((r["freq_act"], r["freq_req"]), (650, 900))
        # counter going backwards -> re-baseline
        self.T[0] += 10
        fp.vals[names["rcs0-busy"]] = 5
        self.assertEqual(pmu.sample(), {"warming": True})
        self.T[0] += 10
        fp.vals[names["rcs0-busy"]] = 5 + int(1e9)
        r = pmu.sample()
        self.assertAlmostEqual(r["engines"]["render"], 10.0)
        self.T[0] += 0.05
        self.assertEqual(pmu.sample(), r, "sub-200ms resample returns last result")
        # read failure -> close fds, retry after 30 s
        fp.fail_read = True
        self.T[0] += 10
        self.assertIsNone(pmu.sample())
        self.assertEqual(len(fp.closed), 7)
        fp.fail_read = False
        self.T[0] += 5
        self.assertIsNone(pmu.sample(), "no re-init within 30 s")
        self.T[0] += 30
        self.assertEqual(pmu.sample(), {"warming": True})
        self.assertEqual(len(pmu._fds), 7)
        d = pmu.diag()
        json.dumps(d)
        self.assertTrue(d["pmu_dir"].endswith("i915_0000_00_02.0"))
        self.assertEqual((d["type"], d["cpumask"]), (17, "0-3"))
        self.assertEqual(d["events"]["rcs0-busy"], dict(d["events"]["rcs0-busy"], unit="ns", config="0x0"))
        self.assertIn("values", d["last_raw"])
        pmu.close()
        self.assertEqual(len(fp.closed), 14)

    def test_permission_denied_backoff_and_rc6_fallback(self):
        root = self.sysfs(pmu_name="i915")
        fp = FakePerf(deny={0x0, 0x100000, 0x200000, 0x200100, 0x300000})
        pmu = srv.IntelGpuPmu(sysfs=root, opener=fp, reader=fp.read, closer=fp.close, clock=self.clock)
        self.assertIsNone(pmu.sample("00:02.0"))
        d = pmu.diag()
        self.assertEqual(d["open"]["rcs0-busy"]["errno"], errno.EACCES)
        self.assertIn("Permission", d["open"]["rcs0-busy"]["strerror"])
        self.assertTrue(d["pmu_dir"].endswith("/i915"))
        self.assertTrue(d["error"])
        n = len(fp.opened)
        self.T[0] += 100
        pmu.sample()
        self.assertEqual(len(fp.opened), n, "no re-open within the 300 s backoff")
        self.T[0] += 300
        pmu.sample()
        self.assertGreater(len(fp.opened), n)

        idle = srv.GpuIdleResidency(sysfs=root, clock=self.clock)
        u = srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)
        self.assertEqual(u["util_source"], "rc6")
        self.assertTrue(u["warming"])
        self.assertIsNone(u["util"])
        self.assertEqual(u["freq_mhz"], 650, "sysfs act freq preferred")
        rc6 = os.path.join(root, "class/drm/card0/gt/gt0/rc6_residency_ms")
        self.T[0] += 10
        w(rc6, str(1000 + 7000))          # 7 s idle in 10 s -> 30 % busy
        u = srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)
        self.assertEqual((u["util_source"], u["util"], u["engines"]), ("rc6", 30.0, None))
        self.T[0] += 10
        w(rc6, str(8000 + 12000))         # > 100 % idle (clock skew) -> clamp 0
        self.assertEqual(srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)["util"], 0.0)
        self.T[0] += 10
        w(rc6, "5")                       # wrap -> warming
        u = srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)
        self.assertTrue(u["warming"])
        self.assertIsNone(u["util"])
        dg = srv.intel_gpu_diag("00:02.0", sysfs=root, pmu=pmu, idle=idle)
        json.dumps(dg)
        self.assertEqual(dg["drm_cards"], [{"card": "card0", "pci": "0000:00:02.0", "driver": "i915"}])
        self.assertTrue(dg["rc6_path"].endswith("gt/gt0/rc6_residency_ms"))

    def test_xe_driver_gtidle_fallback(self):
        root = self.sysfs(pmu_name="xe_0000_00_02.0", driver="xe",
                          rc6_rel="device/tile0/gt0/gtidle/idle_residency_ms",
                          events={"engine-active-ticks": "event=0x2"})
        fp = FakePerf()
        pmu = srv.IntelGpuPmu(sysfs=root, opener=fp, reader=fp.read, closer=fp.close, clock=self.clock)
        idle = srv.GpuIdleResidency(sysfs=root, clock=self.clock)
        u = srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)
        self.assertEqual(u["util_source"], "rc6")
        self.assertIsNone(u["engines"])
        self.assertFalse(fp.opened, "no i915 events opened for xe")
        dg = srv.intel_gpu_diag("00:02.0", sysfs=root, pmu=pmu, idle=idle)
        self.assertEqual(dg["drm_cards"][0]["driver"], "xe")
        self.assertEqual(dg["pmu"]["pmu_candidates"], [])

    def test_nothing_available(self):
        root = tempfile.mkdtemp(prefix="fnm-sysfs-")
        self.roots.append(root)
        pmu = srv.IntelGpuPmu(sysfs=root, clock=self.clock)
        idle = srv.GpuIdleResidency(sysfs=root, clock=self.clock)
        u = srv.intel_gpu_usage("00:02.0", pmu=pmu, idle=idle, sysfs=root)
        self.assertEqual(u, {"util": None, "util_source": None, "engines": None, "freq_mhz": None,
                             "freq_req_mhz": None, "warming": False})
        d = srv.intel_gpu_diag(None, sysfs=root, pmu=pmu, idle=idle)
        json.dumps(d)
        for k in ("drm_cards", "pmu", "rc6_path"):
            self.assertIn(k, d)


@unittest.skipUnless(IS_LINUX, "perf_event_open is Linux-only")
class RealPerfOpenerTest(unittest.TestCase):
    def test_degrades_cleanly(self):
        op = srv._PerfOpener()
        try:
            fd = op(1, 0, 0)     # PERF_TYPE_SOFTWARE / CPU_CLOCK
        except OSError as e:     # paranoid / seccomp in containers: must be a clean OSError
            self.assertIsInstance(e.errno, int)
            return
        try:
            self.assertIsInstance(srv._perf_read(fd), int)
        finally:
            os.close(fd)


if __name__ == "__main__":
    unittest.main()
