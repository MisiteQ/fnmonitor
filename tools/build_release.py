#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build_release.py - 发布构建：把「个人发版通道」的差异在构建时注入，不写回 git。

背景
----
fork 维护者通常既想用自己的 GitHub Release 做「应用内一键更新」，又不想让
定制差异污染 git 历史（那样每次合并上游都要解决 manifest 冲突，也没法把改动
提回上游）。这里采用「构建时注入」：

    UPDATE_REPO 常量、manifest 的 version（可选 changelog 前缀）
    只在打包这一刻替换，包里是新值，仓库里保持与上游一致，构建完立即还原。

用法
----
    python3 tools/build_release.py --version 2.19.0 --repo 你的用户名/fnmonitor
    python3 tools/build_release.py --version 2.19.0 --notes "① 修复 xx<br>② 新增 yy"
    python3 tools/build_release.py --version 2.19.0 --check-only   # 只注入+校验，不打包
    python3 tools/build_release.py --version 2.19.0 --dry-run      # 只打印将要改什么

约定
----
* 版本号必须是三段纯数字（2.19.0）。应用的更新比较 `_ver_tuple()` 只取前 3 段数字，
  写成 2.19.0.1 会被截断成 (2,19,0)，与 2.19.0 判等，更新弹不出来。
* 版本号只增不减，且与上游版本号无关（上游发 2.25.0 也不影响本序列；
  想对齐就跳到比上一个 tag 更大的数字）。
* 产物：fnmonitor-<版本>-x86.fpk / -arm.fpk + SHA256 清单。
"""

import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SERVER_PY = os.path.join(ROOT, "app", "server.py")
MANIFEST = os.path.join(ROOT, "manifest")

VER_RE = re.compile(r"^\d+\.\d+\.\d+$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- 版本 / 仓库
def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))[:3])


def read_manifest_version():
    with open(MANIFEST, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("version="):
                return line[len("version="):].strip()
    return ""


def read_update_repo():
    with open(SERVER_PY, "r", encoding="utf-8", errors="replace") as f:
        m = re.search(r'(?m)^UPDATE_REPO\s*=\s*"([^"]*)"', f.read())
    return m.group(1) if m else ""


def git_tags():
    """本地已有的 tag（拿不到 git 就返回空，不阻塞构建）。"""
    try:
        out = subprocess.run(["git", "tag", "--list"], cwd=ROOT, capture_output=True,
                             text=True, timeout=15)
        if out.returncode != 0:
            return []
        return [t.strip() for t in out.stdout.splitlines() if t.strip()]
    except Exception:
        return []


def check_version(version, force):
    """版本号必须是三段纯数字，且比已有 tag / manifest 版本都大。"""
    if not VER_RE.match(version):
        raise BuildError("版本号必须形如 2.19.0（三段纯数字）：%r\n"
                         "  应用更新比较只取前 3 段数字，多一段会被截断当作同版本。" % version)
    cur = _ver_tuple(read_manifest_version())
    if _ver_tuple(version) <= cur and not force:
        raise BuildError("版本号 %s 必须大于当前 manifest 的 %s（确实要重发同版本加 --force）"
                         % (version, read_manifest_version()))
    highest = None
    for t in git_tags():
        tt = t.lstrip("vV")
        if VER_RE.match(tt) and (highest is None or _ver_tuple(tt) > _ver_tuple(highest)):
            highest = tt
    if highest and _ver_tuple(version) <= _ver_tuple(highest) and not force:
        raise BuildError("版本号 %s 必须大于已有 tag %s（否则 NAS 上不会提示更新，加 --force 强制）"
                         % (version, highest))


# ---------------------------------------------------------------- 注入
def _read_raw(path):
    """按原始换行读取（newline="" 不做转换）。

    必须这样读：仓库里的 manifest 被 Git 以 CRLF 检出，若用默认的通用换行模式读，
    CRLF 会被折成 LF，写回后整个文件 15 行全部变成「已修改」，源码树就被污染了。
    """
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def patched_texts(version, repo, notes):
    """返回 {路径: 新内容}，仅内存里替换，不落盘。"""
    srv = _read_raw(SERVER_PY)
    new_srv, n = re.subn(r'(?m)^UPDATE_REPO\s*=\s*"[^"]*"',
                         'UPDATE_REPO = "%s"' % repo, srv, count=1)
    if n != 1:
        raise BuildError('app/server.py 里没找到 UPDATE_REPO = "..." 这一行，'
                         '无法注入发版仓库地址（上游改过常量名？）')
    # VERSION 的硬编码回退值（manifest 读不到时用）也一起对齐，避免包内版本号打架
    new_srv, n = re.subn(r'(?m)^(VERSION\s*=\s*_read_manifest_version\(\)\s+or\s+")[^"]*(")',
                         r"\g<1>%s\g<2>" % version, new_srv, count=1)
    if n != 1:
        raise BuildError("app/server.py 里没找到 VERSION = _read_manifest_version() or \"...\" 回退值")

    man = _read_raw(MANIFEST)
    # 只替换行内值，不碰行尾：用 (?m)$ 会把 \r 一起吃掉，导致这一行变成 LF、其余仍是 CRLF
    new_man, n = re.subn(r"(?m)^version=[^\r\n]*", "version=%s" % version, man, count=1)
    if n != 1:
        raise BuildError("manifest 里没找到 version= 行")
    if notes:
        # changelog 是一整行超长文本：把本次说明插到最前面，应用中心能看到
        one = " ".join(str(notes).split())          # 压成单行，避免破坏 manifest 格式
        new_man, n = re.subn(r"(?m)^changelog=", "changelog=v%s<br>%s<br><br>" % (version, one),
                             new_man, count=1)
        if n != 1:
            raise BuildError("manifest 里没找到 changelog= 行")

    return {SERVER_PY: new_srv, MANIFEST: new_man}


def show_diff(plan):
    for path, new in plan.items():
        old = _read_raw(path)
        print("  %s" % os.path.relpath(path, ROOT))
        if path == MANIFEST:
            for line in new.splitlines():
                if line.startswith(("version=", "changelog=")):
                    print("    + %s" % (line[:110] + ("…" if len(line) > 110 else "")))
            continue
        for old_l, new_l in zip(old.splitlines(), new.splitlines()):
            if old_l != new_l:
                print("    - %s" % old_l.strip()[:100])
                print("    + %s" % new_l.strip()[:100])


def write_with_restore(plan):
    """写入注入内容，返回 restore() 用于还原（保证异常也还原）。"""
    backups = {}
    try:
        for path, new in plan.items():
            backups[path] = _read_raw(path)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(new)
    except Exception:
        _restore(backups)
        raise
    return lambda: _restore(backups)


def _restore(backups):
    for path, text in (backups or {}).items():
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        except Exception as e:
            print("[警告] 还原 %s 失败：%s" % (path, e), file=sys.stderr)


# ---------------------------------------------------------------- 构建
def find_builder():
    """返回 ('sh'|'ps1', 说明)。优先用能跑通的那个。"""
    fnpack_exe = os.path.join(ROOT, "fnpack.exe")
    fnpack_bin = os.path.join(ROOT, "fnpack")
    if os.name == "nt":
        if os.path.isfile(fnpack_exe):
            return "ps1", "./fnpack.exe"
        raise BuildError("Windows 下需要 fnpack.exe 放在工程根目录：\n"
                         "  https://static2.fnnas.com/fnpack/fnpack-1.2.3-windows-amd64\n"
                         "  下载后改名为 fnpack.exe")
    if os.path.isfile(fnpack_bin) and os.access(fnpack_bin, os.X_OK):
        return "sh", "./fnpack"
    which = subprocess.run(["sh", "-c", "command -v fnpack"], capture_output=True, text=True)
    if which.returncode == 0 and which.stdout.strip():
        return "sh", which.stdout.strip()
    raise BuildError("未找到 fnpack。Linux/macOS 安装：\n"
                     "  curl -L -o /usr/local/bin/fnpack "
                     "https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64\n"
                     "  chmod +x /usr/local/bin/fnpack")


def build():
    kind, how = find_builder()
    print("[构建] 使用 %s（fnpack: %s）" % (kind, how))
    env = dict(os.environ)
    if os.name == "nt":
        # build.ps1 内部调 `python make_icon.py`；确保解释器在 PATH 里
        env["PATH"] = os.path.dirname(sys.executable) + os.pathsep + env.get("PATH", "")
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", os.path.join(ROOT, "build.ps1")]
    else:
        cmd = ["bash", os.path.join(ROOT, "build.sh")]
    r = subprocess.run(cmd, cwd=ROOT, env=env)
    if r.returncode != 0:
        raise BuildError("打包失败（退出码 %s）" % r.returncode)


def collect(version):
    out = []
    for plat in ("x86", "arm"):
        p = os.path.join(ROOT, "fnmonitor-%s-%s.fpk" % (version, plat))
        if not os.path.isfile(p):
            raise BuildError("没有生成 %s" % os.path.basename(p))
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        out.append((os.path.basename(p), os.path.getsize(p), h.hexdigest()))
    return out


# ---------------------------------------------------------------- 入口
def main():
    ap = argparse.ArgumentParser(description="注入发版差异并构建 fpk（不写回 git）")
    ap.add_argument("--version", required=True, help="三段纯数字版本号，如 2.19.0")
    ap.add_argument("--repo", required=True, help="发版仓库 owner/name（你的 fork，如 user/fnmonitor）")
    ap.add_argument("--notes", default="", help="本次更新说明（会插到 manifest changelog 最前）")
    ap.add_argument("--check-only", action="store_true", help="只注入并做语法自检，不打包")
    ap.add_argument("--dry-run", action="store_true", help="只打印将要改什么，不落盘")
    ap.add_argument("--force", action="store_true", help="允许版本号不递增")
    args = ap.parse_args()

    try:
        check_version(args.version, args.force)
        if not REPO_RE.match(args.repo):
            raise BuildError("--repo 必须形如 owner/name：%r" % args.repo)
        print("[1/4] 版本 %s → 发版通道 %s" % (args.version, args.repo))
        print("      仓库当前：manifest %s / UPDATE_REPO %s"
              % (read_manifest_version(), read_update_repo()))

        plan = patched_texts(args.version, args.repo, args.notes)
        print("[2/4] 将注入的差异：")
        show_diff(plan)
        if args.dry_run:
            print("[dry-run] 未写入、未打包")
            return 0

        restore = write_with_restore(plan)
        try:
            r = subprocess.run([sys.executable, "-m", "py_compile", SERVER_PY],
                               capture_output=True, text=True)
            if r.returncode != 0:
                raise BuildError("注入后 server.py 语法检查失败：\n%s" % (r.stderr or "")[:800])
            print("[3/4] 注入后语法检查通过")
            if args.check_only:
                print("[check-only] 未打包，已还原源文件")
                return 0
            build()
        finally:
            restore()
            print("[4/4] 源文件已还原（git 工作区应保持干净）")
    except BuildError as e:
        print("[失败] %s" % e, file=sys.stderr)
        return 1

    rows = collect(args.version)
    print("\n产物：")
    for name, size, sha in rows:
        print("  %-30s %10s 字节  sha256=%s" % (name, "{:,}".format(size), sha))
    work = os.path.join(tempfile.gettempdir(), "fnm-release-%s.txt" % args.version)
    with open(work, "w", encoding="utf-8") as f:
        f.write(" %s  %s\n" % (" " * 32, "  sha256"))
        for name, size, sha in rows:
            f.write("%s  %s  %s\n" % (sha, "{:,}".format(size).rjust(10), name))
    print("\n校验值清单：%s" % work)
    print("发 Release：gh release create %s fnmonitor-%s-x86.fpk fnmonitor-%s-arm.fpk "
          "--title \"v%s\" --notes \"...\"" % (args.version, args.version, args.version, args.version))
    return 0


if __name__ == "__main__":
    sys.exit(main())
