"""
权限探测 + 友好修复提示。
用于在保存数据源配置前检测 SQLite 库 / Docker Socket / systemd journal 是否可读。
纯 Python 标准库，零第三方依赖，不依赖 Flask。

返回结构：每个 diagnose_* 函数返回 dict {"ok": bool, "hint": str, "path": str}。
"""
import json
import os
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional


def _in_container() -> bool:
    """判断当前进程是否运行在容器内。"""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _current_user() -> str:
    try:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
    except Exception:
        return str(os.geteuid())


def _db_acl_hint(db_path: str) -> str:
    """生成 SQLite 库 ACL 修复命令（fnOS 非 root 跑时需要）。"""
    user = _current_user()
    path = os.path.abspath(db_path)
    if _in_container():
        return (
            f"在 NAS 核对宿主目录挂载和容器进程的 UID/GID；"
            f"容器 uid={user} 需要对应宿主目录的读权限。"
        )
    # fnOS 上通常 fnmonitor 以 root 跑，不太会遇到。但还是给命令。
    return (
        f"在 NAS SSH 执行以下命令授予只读权限，然后重启 fnmonitor：\n"
        f"  sudo bash -c 'setfacl -m u:{user}:r {path} && "
        f"for d in $(rev <<< \"{path}\" | cut -d/ -f2- | rev | tr / \\n); do "
        f"setfacl -m u:{user}:x \"$d\"; done'\n"
        f"（或者简单粗暴：chmod o+r {path}）"
    )


def _docker_permission_hint(sock_path: str) -> str:
    """Docker Socket 权限修复提示。"""
    user = _current_user()
    try:
        import grp
        group = grp.getgrgid(os.stat(sock_path).st_gid).gr_name
    except Exception:
        group = ""
    if _in_container():
        return (
            f"在 docker-compose.yaml 的 fnmonitor 服务下增加：\n"
            f"  group_add: [\"{group or 'docker'}\"]\n"
            f"或者挂载时把宿主 {sock_path} 以正确 GID 映射进来，再重建容器。"
        )
    if group == "docker":
        return f"在 NAS SSH 执行：sudo usermod -aG docker {user}，然后重启 fnmonitor。"
    return f"在 NAS SSH 执行：sudo setfacl -m u:{user}:rw {sock_path}，然后重启 fnmonitor。"


def _journal_hint() -> str:
    """systemd journal 权限修复提示。"""
    user = _current_user()
    if _in_container():
        return (
            f"在 docker-compose.yaml 挂载宿主目录：\n"
            f"  volumes:\n"
            f"    - /var/log/journal:/var/log/journal:ro\n"
            f"    - /run/log/journal:/run/log/journal:ro\n"
            f"    - /etc/machine-id:/etc/machine-id:ro"
        )
    return f"在 NAS SSH 执行：sudo usermod -aG systemd-journal {user}，然后重启 fnmonitor。"


# ============================================================
# 公开 API：三个 diagnose 函数 + 一个 run_all
# ============================================================

def diagnose_sqlite(label: str, db_path: str, probe_sql: str = "SELECT 1") -> Dict[str, Any]:
    """
    检测 SQLite 数据库是否可读。
    检查点：路径存在 → 父目录可遍历（X 权限）→ WAL/SHM 附属文件 → SQLite 能执行简单查询。
    """
    result: Dict[str, Any] = {
        "label": label,
        "path": db_path,
        "ok": False,
        "error": "",
        "hint": "",
    }
    path = (db_path or "").strip()
    if not path:
        result["error"] = "未配置数据库路径"
        result["hint"] = f"在设置页填写 {label} 的实际 SQLite 数据库路径。"
        return result

    p = Path(os.path.abspath(path))

    # 父目录遍历
    for directory in reversed(p.parents):
        try:
            st = directory.stat()
            if not stat.S_ISDIR(st.st_mode):
                result["error"] = f"父路径 {directory} 不是目录"
                result["hint"] = f"核对 {label} 的数据库路径是否正确。"
                return result
            if not os.access(directory, os.X_OK):
                result["error"] = f"无法进入目录 {directory}"
                result["hint"] = _db_acl_hint(str(p))
                return result
        except PermissionError:
            result["error"] = f"无权访问目录 {directory}"
            result["hint"] = _db_acl_hint(str(p))
            return result
        except FileNotFoundError:
            result["error"] = f"目录不存在：{directory}"
            result["hint"] = f"核对 {label} 对应应用是否已安装，以及数据库路径是否正确。"
            return result
        except OSError as exc:
            result["error"] = f"目录检查失败：{exc}"
            result["hint"] = "检查存储是否已挂载或发生 I/O 错误。"
            return result

    # 主文件
    try:
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            result["error"] = f"{p} 不是普通文件"
            result["hint"] = f"核对 {label} 的数据库文件路径（不能填目录）。"
            return result
        if not os.access(p, os.R_OK):
            result["error"] = f"数据库不可读（权限 {stat.filemode(st.st_mode)}）"
            result["hint"] = _db_acl_hint(str(p))
            return result
    except PermissionError:
        result["error"] = f"无权访问数据库文件"
        result["hint"] = _db_acl_hint(str(p))
        return result
    except FileNotFoundError:
        result["error"] = f"数据库文件不存在：{p}"
        result["hint"] = f"核对 {label} 对应应用是否已安装，以及数据库路径。"
        return result
    except OSError as exc:
        result["error"] = f"数据库检查失败：{exc}"
        result["hint"] = "检查存储状态。"
        return result

    # WAL/SHM 附属文件
    for suffix in ("-wal", "-shm"):
        side = Path(str(p) + suffix)
        try:
            if side.exists() and not os.access(side, os.R_OK):
                result["error"] = f"SQLite 附属文件不可读：{side}"
                result["hint"] = _db_acl_hint(str(p))
                return result
        except PermissionError:
            result["error"] = f"无法检查 SQLite 附属文件：{side}"
            result["hint"] = _db_acl_hint(str(p))
            return result
        except OSError:
            pass

    # 实际执行 SQL 探测
    try:
        import sqlite3
        conn = sqlite3.connect(
            f"file:{path}?mode=ro&immutable=1", uri=True, timeout=2.0
        )
        try:
            conn.execute(probe_sql).fetchone()
        finally:
            conn.close()
        result["ok"] = True
        return result
    except Exception as exc:
        text = str(exc).lower()
        if "no such table" in text or "no such column" in text:
            result["error"] = f"数据库可访问，但表结构不兼容：{exc}"
            result["hint"] = f"核对是否选错了数据库（{label} 需要的表结构与当前库不匹配）。"
        elif "malformed" in text or "not a database" in text:
            result["error"] = f"数据库格式异常：{exc}"
            result["hint"] = "通过对应应用备份/恢复数据库，不要直接删除或重装。"
        elif "locked" in text or "busy" in text:
            result["error"] = f"数据库正在被占用：{exc}"
            result["hint"] = "稍后重试即可，无需提升权限。"
        else:
            result["error"] = f"SQLite 打开失败：{exc}"
            result["hint"] = _db_acl_hint(str(p))
        return result


def diagnose_docker(sock_path: str = "/var/run/docker.sock") -> Dict[str, Any]:
    """检测 Docker Engine Unix Socket 是否可连接。"""
    result: Dict[str, Any] = {
        "label": "Docker 容器事件",
        "path": sock_path,
        "ok": False,
        "error": "",
        "hint": "",
    }
    sp = (sock_path or "").strip() or "/var/run/docker.sock"

    if not os.path.exists(sp):
        result["error"] = f"Socket 文件不存在：{sp}"
        result["hint"] = "Docker 未安装或 socket 路径不同。检查 Docker 服务是否在运行。"
        return result

    try:
        st = os.stat(sp)
        if not stat.S_ISSOCK(st.st_mode):
            result["error"] = f"{sp} 不是 Unix socket（权限 {stat.filemode(st.st_mode)}）"
            result["hint"] = "核对 Docker socket 路径。"
            return result
    except Exception as exc:
        result["error"] = f"stat 失败：{exc}"
        result["hint"] = _docker_permission_hint(sp)
        return result

    if not os.access(sp, os.R_OK | os.W_OK):
        result["error"] = f"无权访问 socket（权限 {stat.filemode(os.stat(sp).st_mode)}）"
        result["hint"] = _docker_permission_hint(sp)
        return result

    # 真正 ping Docker API（HTTP GET /version over Unix socket）
    try:
        import urllib.request
        # 通过 Unix socket 的简单 ping：先建立 socket 再 HTTP GET
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2.0)
        sock.connect(sp)
        try:
            # 发送最小 HTTP 请求
            sock.sendall(b"GET /_ping HTTP/1.0\r\nHost: localhost\r\n\r\n")
            resp = sock.recv(256)
            if b"200 OK" in resp or b"OK" in resp:
                result["ok"] = True
                return result
            result["error"] = f"Docker API 返回非 OK：{resp[:80]!r}"
            result["hint"] = "Docker 服务状态异常，重启 Docker 后再试。"
            return result
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            sock.close()
    except Exception as exc:
        result["error"] = f"Docker socket 连接失败：{exc}"
        result["hint"] = _docker_permission_hint(sp)
        return result


def diagnose_ssh_journal() -> Dict[str, Any]:
    """检测 systemd journal 是否可读（用于 SSH 登录事件监听）。"""
    result: Dict[str, Any] = {
        "label": "SSH 日志（systemd journal）",
        "path": "/var/log/journal 或 /run/log/journal",
        "ok": False,
        "error": "",
        "hint": "",
    }
    try:
        proc = subprocess.run(
            ["journalctl", "--no-pager", "-n", "1"],
            capture_output=True, text=True, timeout=3.0,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            result["ok"] = True
            return result
        err = (proc.stderr or "").strip()
        if proc.returncode == 1 and "Permission denied" in err:
            result["error"] = "无权读取 systemd journal（Permission denied）"
        elif proc.returncode != 0:
            result["error"] = f"journalctl 返回码 {proc.returncode}：{err[:120]}"
        else:
            result["error"] = "journalctl 可执行但没有返回任何日志"
        result["hint"] = _journal_hint()
        return result
    except FileNotFoundError:
        result["error"] = "journalctl 命令不存在"
        result["hint"] = "当前系统不是 systemd 管理，无法用 journal 监控 SSH 事件。"
        return result
    except subprocess.TimeoutExpired:
        result["error"] = "journalctl 执行超时"
        result["hint"] = "journalctl 响应过慢，暂时跳过 SSH 事件监控。"
        return result
    except Exception as exc:
        result["error"] = f"检测失败：{exc}"
        result["hint"] = _journal_hint()
        return result


def run_all(
    logger_db_path: str = "",
    backup_db_path: str = "",
    trim_media_db_path: str = "",
    trim_activity_db_path: str = "",
    photo_db_path: str = "",
    music_db_path: str = "",
    docker_socket_path: str = "/var/run/docker.sock",
) -> List[Dict[str, Any]]:
    """
    一次性跑所有数据源的权限探测。返回 List[dict]，每项包含 label / path / ok / error / hint。
    设置页保存配置时调用，把结果展示给用户（ok=False 的项显示 hint）。
    """
    probes: List[tuple] = [
        ("fnOS 主日志库", "logger", logger_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
        ("备份任务库", "backup", backup_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
        ("影视库（资源）", "trimmedia", trim_media_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
        ("影视库（活动）", "trimactivity", trim_activity_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
        ("相册库", "photo", photo_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
        ("音乐库", "music", music_db_path, "SELECT 1 FROM sqlite_master LIMIT 1"),
    ]
    results: List[Dict[str, Any]] = []
    for label, _, path, probe in probes:
        results.append(diagnose_sqlite(label, path, probe_sql=probe))
    results.append(diagnose_docker(docker_socket_path))
    results.append(diagnose_ssh_journal())
    return results


def is_fully_ok(results: List[Dict[str, Any]]) -> bool:
    """所有探测项都是 ok=True 时返回 True。"""
    return all(r.get("ok") for r in results)


def get_failed(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [r for r in results if not r.get("ok")]
