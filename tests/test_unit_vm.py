"""Unit tests: virsh output parsers (domstats / dumpxml / domifaddr / domblkinfo), VmMonitor rate
diffing against a fake virsh, and vm_action input validation. No libvirt needed."""
import unittest
from unittest import mock

from fnm_support import load_server

srv = load_server()

DOMSTATS_1 = """Domain: 'win11'
  state.state=1
  state.reason=1
  cpu.time=10000000000
  balloon.current=8388608
  balloon.maximum=8388608
  balloon.available=8000000
  balloon.unused=3000000
  balloon.rss=6291456
  vcpu.current=4
  vcpu.maximum=4
  net.count=1
  net.0.name=vnet0
  net.0.rx.bytes=1000
  net.0.tx.bytes=2000
  block.count=2
  block.0.name=sda
  block.0.path=/vol1/vm/win11.qcow2
  block.0.rd.bytes=4096
  block.0.wr.bytes=8192
  block.0.allocation=21474836480
  block.0.capacity=107374182400
  block.1.name=sdb
  block.1.rd.bytes=0
  block.1.wr.bytes=0

Domain: 'debian test'
  state.state=5
  state.reason=1

"""

DOMSTATS_2 = DOMSTATS_1.replace("cpu.time=10000000000", "cpu.time=12000000000") \
    .replace("net.0.rx.bytes=1000", "net.0.rx.bytes=11000") \
    .replace("block.0.wr.bytes=8192", "block.0.wr.bytes=1056768")

XML_WIN = """<domain type='kvm' id='3'>
  <name>win11</name>
  <uuid>4DCB1C3A-0F7B-4B5E-9C1D-2B3A4C5D6E7F</uuid>
  <title>Windows 11 办公</title>
  <description>备注</description>
  <memory unit='KiB'>8388608</memory>
  <vcpu placement='static'>4</vcpu>
  <os firmware='efi'><type arch='x86_64' machine='pc-q35-7.2'>hvm</type></os>
  <devices>
    <disk type='file' device='disk'><source file='/vol1/vm/win11.qcow2'/><target dev='sda' bus='sata'/></disk>
    <disk type='file' device='cdrom'><source file='/vol1/iso/win.iso'/><target dev='sdb' bus='sata'/><readonly/></disk>
    <interface type='bridge'><mac address='52:54:00:AA:BB:CC'/><source bridge='br0'/><target dev='vnet0'/><model type='virtio'/></interface>
    <channel type='unix'><target type='virtio' name='org.qemu.guest_agent.0' state='connected'/></channel>
    <graphics type='vnc' port='5900' autoport='yes' listen='127.0.0.1'><listen type='address' address='127.0.0.1'/></graphics>
  </devices>
</domain>"""

XML_DEB = """<domain type='kvm'>
  <name>debian test</name>
  <uuid>11111111-2222-3333-4444-555555555555</uuid>
  <memory unit='GiB'>2</memory>
  <vcpu current='1'>2</vcpu>
  <os><type arch='aarch64' machine='virt'>hvm</type><loader type='pflash'>/usr/share/AAVMF/AAVMF_CODE.fd</loader></os>
  <devices>
    <disk type='file' device='disk'><source file='/vol1/vm/deb.qcow2'/><target dev='vda' bus='virtio'/></disk>
    <interface type='network'><mac address='52:54:00:11:22:33'/><source network='default'/><model type='virtio'/></interface>
    <graphics type='vnc' port='-1' autoport='yes'/>
  </devices>
</domain>"""

# 飞牛 fnOS 虚拟机应用（trim.vm）实际生成的域 XML（节选）：内部名随机、显示名在 <title>，
# 系统与开机自启写在 <metadata><customMeta>，VNC 走 unix 套接字
XML_FNOS = """<domain type='kvm'>
  <name>q1nrg738</name>
  <uuid>26f470c7-b1f9-460c-a58b-a20a4b2f6746</uuid>
  <title>监控测试 VM</title>
  <metadata>
    <customMeta xmlns="customMeta">
      <osType xmlns="osType">linux</osType>
      <osVersion xmlns="osVersion">6.x - 2.6 Kernel</osVersion>
      <autostart xmlns="autostart">%s</autostart>
      <createdTime xmlns="createdTime">1791895138</createdTime>
    </customMeta>
  </metadata>
  <memory unit='KiB'>1048576</memory>
  <vcpu placement='static'>1</vcpu>
  <os><type arch='x86_64' machine='pc-i440fx-7.2'>hvm</type><bootmenu enable='yes' timeout='5000'/></os>
  <devices>
    <disk type='file' device='disk'><driver name='qemu' type='qcow2'/><source file='/vol1/vm/pool/26f470c7-b1f9-460c-a58b-a20a4b2f6746-hfoo.qcow2'/><target dev='sda' bus='scsi'/></disk>
    <channel type='qemu-vdagent'><target type='virtio' name='com.redhat.spice.0'/></channel>
    <graphics type='vnc' socket='/var/run/vms/q1nrg738.vnc.sock' powerControl='yes'><listen type='socket' socket='/var/run/vms/q1nrg738.vnc.sock'/></graphics>
    <memballoon model='virtio'><stats period='5'/></memballoon>
  </devices>
</domain>"""

# libvirt 9.0 对已关机的虚拟机仍给出上次运行留下的 cpu.time 与 block 统计
DOMSTATS_FNOS_OFF = """Domain: 'q1nrg738'
  state.state=5
  state.reason=0
  cpu.time=1210000000
  balloon.current=1048576
  balloon.maximum=1048576
  vcpu.current=1
  vcpu.maximum=1
  block.count=1
  block.0.name=sda
  block.0.path=/vol1/vm/pool/26f470c7-b1f9-460c-a58b-a20a4b2f6746-hfoo.qcow2
  block.0.allocation=200704
  block.0.capacity=1073741824
  block.0.physical=196624

"""

DOMIFADDR = """ Name       MAC address          Protocol     Address
-------------------------------------------------------------------------------
 lo         00:00:00:00:00:00    ipv4         127.0.0.1/8
 eth0       52:54:00:aa:bb:cc    ipv4         192.168.1.50/24
 -          -                    ipv6         fe80::5054:ff:feaa:bbcc/64
 -          -                    ipv6         2408:8000::50/64
"""


class ParserTest(unittest.TestCase):
    def test_domstats(self):
        doms = srv.parse_domstats(DOMSTATS_1)
        self.assertEqual([n for n, _ in doms], ["win11", "debian test"])
        win, deb = doms[0][1], doms[1][1]
        self.assertEqual(srv.domstats_state(win), "running")
        self.assertEqual(srv.domstats_state(deb), "shutoff")
        self.assertEqual(srv.domstats_counters(win), (10000000000, 4096, 8192, 1000, 2000))
        self.assertIsNone(srv.domstats_counters(deb))
        self.assertEqual(srv.domstats_state({}), "nostate")

    def test_domain_xml(self):
        w = srv.parse_domain_xml(XML_WIN)
        self.assertEqual(w["uuid"], "4dcb1c3a-0f7b-4b5e-9c1d-2b3a4c5d6e7f")
        self.assertEqual((w["title"], w["vcpus"], w["mem_max"]), ("Windows 11 办公", 4, 8 * 1024 ** 3))
        self.assertEqual((w["arch"], w["machine"], w["firmware"]), ("x86_64", "pc-q35-7.2", "UEFI"))
        self.assertEqual([(d["dev"], d["device"], d["readonly"]) for d in w["disks"]],
                         [("sda", "disk", False), ("sdb", "cdrom", True)])
        self.assertEqual(w["nics"][0]["mac"], "52:54:00:aa:bb:cc")
        self.assertEqual((w["nics"][0]["type"], w["nics"][0]["source"], w["nics"][0]["model"]), ("bridge", "br0", "virtio"))
        self.assertEqual(w["graphics"], [{"type": "vnc", "port": 5900, "listen": "127.0.0.1", "socket": ""}])
        self.assertTrue(w["agent"] and w["agent_connected"])

        d = srv.parse_domain_xml(XML_DEB)
        self.assertEqual((d["vcpus"], d["mem_max"], d["firmware"]), (1, 2 * 1024 ** 3, "UEFI"))
        self.assertEqual(d["graphics"][0]["port"], None)
        self.assertFalse(d["agent"])

    def test_fnos_domain_xml(self):
        f = srv.parse_domain_xml(XML_FNOS % "false")
        self.assertEqual((f["title"], f["os"], f["fn_autostart"], f["firmware"]), ("监控测试 VM", "Linux 6.x - 2.6 Kernel", False, "BIOS"))
        self.assertEqual(f["graphics"], [{"type": "vnc", "port": None, "listen": "", "socket": "/var/run/vms/q1nrg738.vnc.sock"}])
        self.assertEqual([(d["dev"], d["bus"]) for d in f["disks"]], [("sda", "scsi")])
        self.assertFalse(f["agent"])
        self.assertTrue(srv.parse_domain_xml(XML_FNOS % "true")["fn_autostart"])

    def test_stopped_vm_has_no_counters(self):
        st = srv.parse_domstats(DOMSTATS_FNOS_OFF)[0][1]
        self.assertEqual(srv.domstats_state(st), "shutoff")
        self.assertIsNone(srv.domstats_counters(st))

    def test_domain_xml_garbage(self):
        info = srv.parse_domain_xml("not xml <")
        self.assertEqual((info["uuid"], info["disks"], info["nics"]), ("", [], []))

    def test_domifaddr(self):
        self.assertEqual(srv.parse_domifaddr(DOMIFADDR), {"52:54:00:aa:bb:cc": ["192.168.1.50", "2408:8000::50"]})
        self.assertEqual(srv.parse_domifaddr(""), {})

    def test_domblkinfo(self):
        self.assertEqual(srv.parse_domblkinfo("Capacity:       10737418240\nAllocation:     2195451904\nPhysical:       2195451900\n"),
                         (10737418240, 2195451900))
        self.assertEqual(srv.parse_domblkinfo("Capacity: 100\nAllocation: 40\n"), (100, 40))

    def test_virsh_error_text(self):
        self.assertEqual(srv.virsh_error_text("error: failed to get domain 'x'\nerror: Domain not found\n"),
                         "failed to get domain 'x'；Domain not found")


class FakeVirsh:
    """按参数返回预置输出；domstats 依次返回 samples 中的文本（最后一份重复）。"""

    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = []

    def __call__(self, args, timeout=15):
        self.calls.append(list(args))
        cmd = args[0]
        if cmd == "domstats":
            return (self.samples.pop(0) if len(self.samples) > 1 else self.samples[0]), "", 0
        if cmd == "list":
            return "win11\n\n", "", 0
        if cmd == "dumpxml":
            return (XML_WIN if args[-1] == "win11" else XML_DEB), "", 0
        if cmd == "domblkinfo":
            return "Capacity: 21474836480\nAllocation: 1073741824\n", "", 0
        if cmd == "domifaddr":
            return DOMIFADDR, "", 0
        return "", "error: unexpected", 1


class VmMonitorTest(unittest.TestCase):
    def _run(self, samples, clock):
        fake = FakeVirsh(samples)
        with mock.patch.object(srv, "run_virsh", fake), \
                mock.patch.object(srv, "virsh_path", return_value="/usr/bin/virsh"), \
                mock.patch.object(srv, "arp_table", return_value={}), \
                mock.patch.object(srv, "qemu_proc_info", return_value=(3600, None)), \
                mock.patch.object(srv.time, "time", side_effect=clock), \
                mock.patch.object(srv.time, "sleep"):
            return srv.VmMonitor().collect(), fake

    def test_first_collect_double_samples_and_diffs(self):
        # time.time() 依次为：结果时间戳、第一次采样（100s）、第二次采样（102s，sleep 已 mock）、其余
        ticks = [100.0, 100.0, 102.0] + [102.0] * 50
        res, fake = self._run([DOMSTATS_1, DOMSTATS_2], iter(ticks).__next__)
        self.assertTrue(res["available"])
        self.assertEqual([c[0] for c in fake.calls].count("domstats"), 2)
        win, deb = res["vms"]
        self.assertEqual((win["name"], win["state"], win["running"], win["autostart"]), ("win11", "running", True, True))
        self.assertEqual(win["uuid"], "4dcb1c3a-0f7b-4b5e-9c1d-2b3a4c5d6e7f")
        # cpu.time +2s 纳秒 / dt 2s = 1 核；4 vCPU → 25%
        self.assertEqual((win["cpu_cores"], win["cpu"]), (1.0, 25.0))
        self.assertEqual((win["rx_rate"], win["tx_rate"], win["rd_rate"], win["wr_rate"]), (5000.0, 0.0, 0.0, 524288.0))
        self.assertEqual(win["mem"], 8 * 1024 ** 3)
        self.assertEqual(win["mem_rss"], 6 * 1024 ** 3)
        self.assertEqual(win["mem_guest_used"], (8000000 - 3000000) * 1024)
        self.assertEqual(win["disks"][0]["capacity"], 107374182400)
        self.assertEqual(win["ips"], ["192.168.1.50", "2408:8000::50"])   # ARP 为空 → guest agent
        self.assertEqual(win["uptime"], 3600)
        self.assertEqual((deb["state"], deb["running"], deb["cpu"], deb["graphics"]), ("shutoff", False, None, []))
        self.assertEqual(deb["disks"][0]["capacity"], 21474836480)          # 未运行 → domblkinfo
        self.assertFalse(deb["autostart"])

    def test_counter_reset_gives_no_rate(self):
        ticks = [100.0, 100.0, 102.0] + [102.0] * 50
        reset = DOMSTATS_1.replace("cpu.time=10000000000", "cpu.time=5")
        res, _ = self._run([DOMSTATS_1, reset], iter(ticks).__next__)
        self.assertIsNone(res["vms"][0]["cpu"])
        self.assertIsNone(res["vms"][0]["rx_rate"])

    def test_fnos_stopped_vm_single_sample(self):
        """只有已关机的虚拟机时：只采一次（不 sleep 连采）、容量取自 domstats（不跑 domblkinfo）、开机自启读 customMeta。"""
        class Fnos(FakeVirsh):
            def __call__(self, args, timeout=15):
                if args[0] == "dumpxml":
                    self.calls.append(list(args))
                    return XML_FNOS % "true", "", 0
                if args[0] == "list":
                    self.calls.append(list(args))
                    return "\n", "", 0
                return FakeVirsh.__call__(self, args, timeout)
        fake = Fnos([DOMSTATS_FNOS_OFF])
        with mock.patch.object(srv, "run_virsh", fake), \
                mock.patch.object(srv, "virsh_path", return_value="/usr/bin/virsh"), \
                mock.patch.object(srv.time, "sleep") as sleep:
            res = srv.VmMonitor().collect()
        sleep.assert_not_called()
        cmds = [c[0] for c in fake.calls]
        self.assertEqual((cmds.count("domstats"), cmds.count("domblkinfo")), (1, 0))
        vm = res["vms"][0]
        self.assertEqual((vm["title"], vm["os"], vm["state"], vm["running"], vm["autostart"]),
                         ("监控测试 VM", "Linux 6.x - 2.6 Kernel", "shutoff", False, True))
        # 实际占用取 physical（镜像文件大小 196624），不是 allocation
        self.assertEqual((vm["disks"][0]["capacity"], vm["disks"][0]["allocation"]), (1073741824, 196624))
        self.assertIsNone(vm["cpu"])

    def test_virsh_missing(self):
        with mock.patch.object(srv, "virsh_path", return_value=""):
            res = srv.VmMonitor().collect()
        self.assertEqual((res["installed"], res["available"], res["vms"]), (False, False, []))
        self.assertTrue(res["error"])

    def test_libvirt_down(self):
        with mock.patch.object(srv, "virsh_path", return_value="/usr/bin/virsh"), \
                mock.patch.object(srv, "run_virsh", return_value=("", "error: failed to connect to the hypervisor\n", 1)):
            res = srv.VmMonitor().collect()
        self.assertEqual((res["installed"], res["available"]), (True, False))
        self.assertEqual(res["error"], "failed to connect to the hypervisor")


class VmActionTest(unittest.TestCase):
    UUID = "4dcb1c3a-0f7b-4b5e-9c1d-2b3a4c5d6e7f"

    def test_rejects_bad_input(self):
        with mock.patch.object(srv, "run_virsh") as rv:
            self.assertFalse(srv.vm_action(self.UUID, "undefine")["ok"])
            for bad in ("win11", "--help", "4dcb1c3a-0f7b-4b5e-9c1d-2b3a4c5d6e7f; rm -rf /", ""):
                self.assertEqual(srv.vm_action(bad, "start")["error"], "非法的虚拟机标识")
            rv.assert_not_called()

    def test_runs_virsh(self):
        with mock.patch.object(srv, "run_virsh", return_value=("Domain started\n", "", 0)) as rv:
            self.assertTrue(srv.vm_action(self.UUID, "start")["ok"])
        rv.assert_called_once_with(["start", "--domain", self.UUID], timeout=60)
        with mock.patch.object(srv, "run_virsh", return_value=("", "error: Requested operation is not valid: domain is not running\n", 1)):
            r = srv.vm_action(self.UUID, "shutdown")
        self.assertEqual((r["ok"], r["error"]), (False, "Requested operation is not valid: domain is not running"))


if __name__ == "__main__":
    unittest.main()
