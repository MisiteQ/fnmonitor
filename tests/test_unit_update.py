"""Unit tests: online-update supply-chain safety. Tar extraction hardening (native data_filter and
the manual fallback), SHA256 required (fail closed), tampered mirrors skipped, destination and
asset-name checks, downgrade / same-version / concurrent install refusal, fnpack.json fallback.

A private copy of server.py is loaded from a temp dir so that install_fpk() (which writes next
to its own __file__) can never touch the repository, and os.execv is blocked. No network: every
URL opener is mocked."""
import hashlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest import mock

from fnm_support import SERVER_PY


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def mktar(entries, path=None):
    buf = io.BytesIO() if path is None else open(path, "wb")
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for kind, name, extra in entries:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                data = extra or b"x"
                ti.size = len(data)
                ti.mode = 0o4755 if name.endswith(".suid") else 0o644
                t.addfile(ti, io.BytesIO(data))
            elif kind == "sym":
                ti.type = tarfile.SYMTYPE
                ti.linkname = extra
                t.addfile(ti)
            elif kind == "lnk":
                ti.type = tarfile.LNKTYPE
                ti.linkname = extra
                t.addfile(ti)
            elif kind == "dev":
                ti.type = tarfile.CHRTYPE
                t.addfile(ti)
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
                t.addfile(ti)
    if path is None:
        buf.seek(0)
        return buf
    buf.close()
    return path


class UpdateSafetyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fnm-upd-")
        app = os.path.join(cls.tmp, "pkg", "app")
        os.makedirs(app)
        shutil.copy2(SERVER_PY, os.path.join(app, "server.py"))
        shutil.copy2(os.path.join(os.path.dirname(os.path.dirname(SERVER_PY)), "manifest"),
                     os.path.join(cls.tmp, "pkg", "manifest"))
        spec = importlib.util.spec_from_file_location("fnm_server_update", os.path.join(app, "server.py"))
        cls.S = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.S)
        cls.execv = mock.patch("os.execv", side_effect=AssertionError("os.execv must not run in tests"))
        cls.execv.start()

    @classmethod
    def tearDownClass(cls):
        cls.execv.stop()
        sys.modules.pop("fnm_server_update", None)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.d = tempfile.mkdtemp(dir=self.tmp)
        self.payload = b"FPKDATA" * 1000
        self.good = hashlib.sha256(self.payload).hexdigest()
        self.asset = {"name": "fnmonitor-9.9.9-x86.fpk",
                      "download_url": "https://github.com/x/y/releases/download/9.9.9/fnmonitor-9.9.9-x86.fpk",
                      "digest": ""}
        self.um = self.S.UpdateManager(self.d)
        self.no_net = mock.patch("urllib.request.urlopen", side_effect=OSError("network disabled in tests"))
        self.no_net.start()

    def tearDown(self):
        self.no_net.stop()

    # ---- tar extraction ----
    BAD = {
        "dotdot": [("file", "../evil", b"x")],
        "nested dotdot": [("file", "a/../../evil", b"x")],
        "absolute": [("file", "/tmp/fnm_evil_abs", b"x")],
        "windows drive": [("file", "C:/evil", b"x")],
        "symlink outside": [("sym", "link", "/etc"), ("file", "link/passwd", b"x")],
        "symlink dotdot": [("sym", "up", "../../")],
        "hardlink outside": [("lnk", "hl", "/etc/passwd")],
        "device": [("dev", "dev0", None)],
        "fifo": [("fifo", "f0", None)],
    }

    def _extract_cases(self):
        for name, ents in self.BAD.items():
            d = tempfile.mkdtemp(dir=self.tmp)
            with self.subTest(case=name):
                with tarfile.open(fileobj=mktar(ents), mode="r:*") as t:
                    with self.assertRaises(Exception):
                        self.S._safe_extract(t, d)
                self.assertEqual(os.listdir(d), [], "nothing written for rejected archive")
        d = tempfile.mkdtemp(dir=self.tmp)
        ok = [("file", "server.py", b"print(1)"), ("file", "ui/config", b"{}"), ("sym", "ui/alias", "config"),
              ("file", "bin/x.suid", b"#!/bin/sh")]
        with tarfile.open(fileobj=mktar(ok), mode="r:*") as t:
            self.S._safe_extract(t, d)
        self.assertTrue(os.path.isfile(os.path.join(d, "server.py")))
        self.assertTrue(os.path.islink(os.path.join(d, "ui", "alias")))
        self.assertFalse(os.stat(os.path.join(d, "bin", "x.suid")).st_mode & 0o4000, "setuid stripped")
        self.assertFalse(os.path.exists("/tmp/fnm_evil_abs"))

    def test_extract_native_filter(self):
        self._extract_cases()

    def test_extract_manual_fallback(self):
        saved = getattr(tarfile, "data_filter", None)
        if saved is not None:
            del tarfile.data_filter
        try:
            self._extract_cases()
        finally:
            if saved is not None:
                tarfile.data_filter = saved

    # ---- digest / mirrors ----
    def test_fail_closed_without_digest(self):
        data, err = self.um.fetch_verified(self.asset)
        self.assertIsNone(data)
        self.assertIn("SHA256", err)
        data, err = self.um.fetch_verified(dict(self.asset, digest="abc"))
        self.assertIsNone(data)

    def test_non_github_url_refused(self):
        data, err = self.um.fetch_verified(dict(self.asset, digest=self.good, download_url="https://evil.example/x.fpk"))
        self.assertIsNone(data)

    def test_tampered_source_skipped(self):
        served = []

        def fake_open(url, timeout=30, mirrors=None):
            m = (mirrors or [""])[0]
            served.append(m)
            return FakeResp(b"TAMPERED" if m == "" else self.payload)
        with mock.patch.object(self.S, "_gh_open", fake_open):
            data, err = self.um.fetch_verified(dict(self.asset, digest=self.good))
        self.assertEqual(data, self.payload)
        self.assertEqual(served[:2], ["", self.S.GH_MIRRORS[1]])

    def test_all_sources_tampered(self):
        with mock.patch.object(self.S, "_gh_open", lambda url, timeout=30, mirrors=None: FakeResp(b"BAD")):
            data, err = self.um.fetch_verified(dict(self.asset, digest=self.good))
        self.assertIsNone(data)
        self.assertIn("校验失败", err)

    def test_download_destination_and_name(self):
        with mock.patch.object(self.S, "_gh_open", lambda url, timeout=30, mirrors=None: FakeResp(self.payload)):
            ok, msg = self.um.download_to_nas(dict(self.asset, digest=self.good), dest_dir="/etc/cron.d")
            self.assertFalse(ok, msg)
            ok, msg = self.um.download_to_nas(dict(self.asset, digest=self.good), dest_dir="relative/dir")
            self.assertFalse(ok, msg)
            for bad in ("../../evil.fpk", "fnmonitor-9.9.9-x86.fpk/../../x", "evil.sh", "fnmonitor-9.9.9-mips.fpk"):
                ok, msg = self.um.download_to_nas(dict(self.asset, name=bad, digest=self.good))
                self.assertFalse(ok, bad)
            ok, path = self.um.download_to_nas(dict(self.asset, digest=self.good))
            self.assertTrue(ok, path)
            self.assertEqual(os.path.dirname(path), self.um.update_dir())
            with open(path, "rb") as f:
                self.assertEqual(f.read(), self.payload)

    # ---- install guards ----
    def test_same_version_and_downgrade_refused(self):
        a = dict(self.asset, digest=self.good)
        with mock.patch.object(self.um, "check", lambda force=False: {"ok": True, "latest": self.S.VERSION, "asset": a, "arch": "x86"}):
            r = self.um.install_latest()
        self.assertFalse(r["ok"])
        self.assertIn("最新", r["error"])
        with mock.patch.object(self.um, "check", lambda force=False: {"ok": True, "latest": "1.0.0", "asset": a, "arch": "x86"}):
            self.assertFalse(self.um.install_latest()["ok"])

    def test_concurrent_install_refused(self):
        self.um._install_lock.acquire()
        try:
            r = self.um.install_latest()
        finally:
            self.um._install_lock.release()
        self.assertFalse(r["ok"])
        self.assertIn("正在安装", r["error"])

    def test_install_fpk_reverifies_sha(self):
        p = os.path.join(self.d, "x.fpk")
        with open(p, "wb") as f:
            f.write(b"zz")
        ok, msg = self.um.install_fpk(p, expect_sha256=self.good)
        self.assertFalse(ok)
        self.assertIn("SHA256", msg)

    def test_install_fpk_rejects_malicious_or_incomplete(self):
        evil = mktar([("file", "../escape", b"x")], os.path.join(self.d, "evil.fpk"))
        ok, msg = self.um.install_fpk(evil)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(self.d, "update", "escape")))
        empty = mktar([("file", "readme.txt", b"x")], os.path.join(self.d, "empty.fpk"))
        ok, msg = self.um.install_fpk(empty)
        self.assertFalse(ok, msg)
        nomani = mktar([("file", "server.py", b"x")], os.path.join(self.d, "nomani.fpk"))
        ok, msg = self.um.install_fpk(nomani)
        self.assertFalse(ok, msg)
        self.assertIn("manifest", msg)

    def test_fnpack_sha256_fallback(self):
        body = json.dumps({"apps": {"fnmonitor": {"releases": {"9.9.9": {"packages": {"x86": {"sha256": self.good},
                                                                                       "arm": {"sha256": "nothex"}}}}}}}).encode()
        with mock.patch("urllib.request.urlopen", lambda req, timeout=15: FakeResp(body)):
            self.assertEqual(self.um._fnpack_sha256("9.9.9", "x86"), self.good)
            self.assertEqual(self.um._fnpack_sha256("9.9.9", "arm"), "")
            self.assertEqual(self.um._fnpack_sha256("1.2.3", "x86"), "")

    def test_check_offline_is_clean(self):
        info = self.um.check(force=True)
        self.assertFalse(info["ok"])
        self.assertTrue(info["error"])
        json.dumps(info)


if __name__ == "__main__":
    unittest.main()
