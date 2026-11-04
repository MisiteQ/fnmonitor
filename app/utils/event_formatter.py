"""
fnOS 日志事件 ID 白名单 + 事件格式化 + 权限探测。
独立模块，仅依赖 Python 标准库，零第三方。
"""
import json
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

# ============================================================
# 一、可监控事件 ID 白名单
# ============================================================

MONITOR_EVENT_IDS = frozenset({
    # 登录/登出
    "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout",
    # 硬件（磁盘）
    "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED",
    # SSH
    "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS", "SSH_DISCONNECTED",
    "SSH_ENABLED", "SSH_DISABLED", "SSH_OPEN_TIMEOUT",
    # 应用生命周期
    "APP_CRASH", "APP_UPDATE_FAILED", "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
    "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
    "APP_STARTED", "APP_STOPPED", "APP_UPDATED", "APP_INSTALLED",
    "APP_AUTO_STARTED", "APP_UNINSTALLED",
    # 资源告警
    "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED",
    "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED",
    "CPU_TEMPERATURE_ALARM",
    # UPS
    "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE", "UPS_ENABLE", "UPS_DISABLE",
    # 磁盘电源/IO
    "DiskWakeup", "DiskSpindown", "DISK_IO_ERR",
    # 备份
    "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    # 存储/归档
    "ARCHIVING_SUCCESS", "DeleteFile", "MovetoTrashbin",
    "SHARE_EVENTID_DEL", "SHARE_EVENTID_PUT",
    # 网络服务开关
    "WEBDAV_ENABLED", "WEBDAV_DISABLED",
    "SAMBA_ENABLED", "SAMBA_DISABLED",
    "DLNA_ENABLED", "DLNA_DISABLED",
    "FTP_ENABLED", "FTP_DISABLED",
    "NFS_ENABLED", "NFS_DISABLED",
    # 安全
    "FW_ENABLE", "FW_DISABLE", "SECURITY_PORTCHANGED", "WAN_IP_CHANGED",
    # 虚拟机（动态 VM 事件名也允许）
    "SHUTDOWN_VM", "STATUS_RUNNING_VM", "STATUS_REBOOTED_VM", "DESTROY_VM",
    # 影视库
    "MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED",
    "TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS",
    # 相册
    "PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED",
    "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED",
    # 音乐
    "MUSIC_TRACK_ADDED", "MUSIC_TRACK_REMOVED",
    "MUSIC_FAVORITED", "MUSIC_PLAYLIST_TRACK_ADDED",
    # Docker
    "DOCKER_CONTAINER_CREATE", "DOCKER_CONTAINER_START", "DOCKER_CONTAINER_STOP",
    "DOCKER_CONTAINER_DIE", "DOCKER_CONTAINER_OOM", "DOCKER_CONTAINER_KILL",
    "DOCKER_CONTAINER_PAUSE", "DOCKER_CONTAINER_UNPAUSE",
    "DOCKER_CONTAINER_RESTART", "DOCKER_CONTAINER_DESTROY",
})


def is_allowed_event(event_id: Any) -> bool:
    s = str(event_id or "").strip()
    if not s:
        return False
    if s in MONITOR_EVENT_IDS:
        return True
    # 动态发现的 VM 事件（CREATE_VM / START_VM 等），名称含 VM 即允许
    return "VM" in s.upper()


def filter_events(events: List[Any]) -> List[Any]:
    return [e for e in events if is_allowed_event(e)]


# 默认选中事件（推荐给首次启用日志事件监控的用户）
# 与 FNMessageBots 的默认口径对齐：不含应用启动/停止等生命周期噪音；
# UPS 启停、SSH 服务开关、WAN IP 变更默认不勾选（按需在界面里自行开启）。
DEFAULT_SELECTED_EVENTS = [
    "LoginSucc", "LoginFail", "Logout", "LoginSucc2FA1",
    "FoundDisk", "InsertDisk", "EjectDisk", "StorageBroken", "STORAGE_DEGRADED",
    "APP_CRASH", "APP_UPDATE_FAILED",
    "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED", "CPU_TEMPERATURE_ALARM",
    "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED",
    "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE",
    "DiskWakeup", "DiskSpindown",
    "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS",
    "SSH_DISCONNECTED", "SSH_OPEN_TIMEOUT",
    "DISK_IO_ERR",
]


# ============================================================
# 二、事件分类（设置页 UI 用）
# ============================================================

EVENT_CATEGORIES = [
    ("登录", [
        "LoginSucc", "LoginSucc2FA1", "LoginFail", "Logout",
    ]),
    ("SSH 登录", [
        "SSH_INVALID_USER", "SSH_AUTH_FAILED", "SSH_LOGIN_SUCCESS",
        "SSH_DISCONNECTED", "SSH_ENABLED", "SSH_DISABLED", "SSH_OPEN_TIMEOUT",
    ]),
    ("应用生命周期", [
        "APP_CRASH", "APP_UPDATE_FAILED",
        "APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION",
        "APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE",
        "APP_STARTED", "APP_STOPPED", "APP_UPDATED", "APP_INSTALLED",
        "APP_AUTO_STARTED", "APP_UNINSTALLED",
    ]),
    ("硬件（磁盘）", [
        "FoundDisk", "InsertDisk", "EjectDisk",
        "DiskWakeup", "DiskSpindown", "DISK_IO_ERR",
        "StorageBroken", "STORAGE_DEGRADED",
    ]),
    ("资源告警", [
        "CPU_USAGE_ALARM", "CPU_USAGE_RESTORED",
        "MEMORY_USAGE_ALARM", "MEMORY_USAGE_RESTORED",
        "CPU_TEMPERATURE_ALARM",
    ]),
    ("UPS", [
        "UPS_ONBATT", "UPS_ONBATT_LOWBATT", "UPS_ONLINE",
        "UPS_ENABLE", "UPS_DISABLE",
    ]),
    ("备份", [
        "BACKUP_TASK_SUCCESS", "BACKUP_TASK_FAILED", "BACKUP_TASK_PARTIAL_SUCCESS",
    ]),
    ("网络服务开关", [
        "WEBDAV_ENABLED", "WEBDAV_DISABLED",
        "SAMBA_ENABLED", "SAMBA_DISABLED",
        "DLNA_ENABLED", "DLNA_DISABLED",
        "FTP_ENABLED", "FTP_DISABLED",
        "NFS_ENABLED", "NFS_DISABLED",
    ]),
    ("安全/防火墙", [
        "FW_ENABLE", "FW_DISABLE", "SECURITY_PORTCHANGED", "WAN_IP_CHANGED",
    ]),
    ("存储/文件", [
        "ARCHIVING_SUCCESS", "DeleteFile", "MovetoTrashbin",
        "SHARE_EVENTID_DEL", "SHARE_EVENTID_PUT",
    ]),
    ("影视库", [
        "MEDIA_LOGIN_SUCC", "MEDIA_LOGOUT", "MEDIA_USER_CREATED",
        "TRIM_RESOURCE_ADDED", "TRIM_SCRAPE_SUCCESS",
    ]),
    ("相册", [
        "PHOTO_SHARE_CREATED", "PHOTO_SHARE_EXPIRED",
        "PHOTO_DEVICE_REGISTERED", "FACE_RECOGNITION_UPDATED",
    ]),
    ("音乐", [
        "MUSIC_TRACK_ADDED", "MUSIC_TRACK_REMOVED",
        "MUSIC_FAVORITED", "MUSIC_PLAYLIST_TRACK_ADDED",
    ]),
    ("Docker 容器", [
        "DOCKER_CONTAINER_CREATE", "DOCKER_CONTAINER_START",
        "DOCKER_CONTAINER_STOP", "DOCKER_CONTAINER_DIE",
        "DOCKER_CONTAINER_OOM", "DOCKER_CONTAINER_KILL",
        "DOCKER_CONTAINER_PAUSE", "DOCKER_CONTAINER_UNPAUSE",
        "DOCKER_CONTAINER_RESTART", "DOCKER_CONTAINER_DESTROY",
    ]),
    ("虚拟机（VM）", [
        "SHUTDOWN_VM", "STATUS_RUNNING_VM", "STATUS_REBOOTED_VM", "DESTROY_VM",
    ]),
]


# ============================================================
# 三、事件标题 + body 模板占位符
# ============================================================
# 格式：模板字符串，可用占位符：{...} 会被 event_data 里的同名字段替换；
#      字段缺失时占位符被替换为空字符串。

EVENT_TITLES = {
    'LoginSucc': '登录成功 {user}@{IP}',
    'LoginSucc2FA1': '二次验证登录 {user}@{IP}',
    'LoginFail': '登录失败 {user}@{IP}',
    'Logout': '退出登录 {user}@{IP}',
    'FoundDisk': '发现新硬盘 {name}',
    'InsertDisk': '插入硬盘 {name}',
    'EjectDisk': '弹出硬盘 {name}',
    'StorageBroken': '存储空间损坏',
    'STORAGE_DEGRADED': '存储空间降级',
    'SSH_INVALID_USER': 'SSH 无效用户尝试 {user}@{IP}',
    'SSH_AUTH_FAILED': 'SSH 认证失败 {user}@{IP}',
    'SSH_LOGIN_SUCCESS': 'SSH 登录成功 {user}@{IP}',
    'SSH_DISCONNECTED': 'SSH 断开连接 {user}',
    'SSH_ENABLED': 'SSH 已启用',
    'SSH_DISABLED': 'SSH 已关闭',
    'SSH_OPEN_TIMEOUT': 'SSH 开启超时（已开启 {remind_minutes} 分钟）',
    'APP_CRASH': '应用崩溃 {app_name}',
    'APP_UPDATE_FAILED': '应用更新失败 {app_name}',
    'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': '应用启动失败 {app_name}',
    'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': '应用自启动失败（Docker 不可用）',
    'APP_STARTED': '应用已启动 {app_name}',
    'APP_STOPPED': '应用已停止 {app_name}',
    'APP_UPDATED': '应用已更新 {app_name}',
    'APP_INSTALLED': '应用已安装 {app_name}',
    'APP_AUTO_STARTED': '应用已自启动 {app_name}',
    'APP_UNINSTALLED': '应用已卸载 {app_name}',
    'CPU_USAGE_ALARM': 'CPU 使用率告警：{usage}%',
    'CPU_USAGE_RESTORED': 'CPU 使用率恢复：{usage}%',
    'MEMORY_USAGE_ALARM': '内存使用率告警：{usage}%',
    'MEMORY_USAGE_RESTORED': '内存使用率恢复：{usage}%',
    'CPU_TEMPERATURE_ALARM': 'CPU 温度告警：{temp}°C',
    'UPS_ONBATT': 'UPS 切换到电池供电',
    'UPS_ONBATT_LOWBATT': 'UPS 低电量，即将自动关机',
    'UPS_ONLINE': 'UPS 恢复市电供电',
    'UPS_ENABLE': '已开启 UPS 支持',
    'UPS_DISABLE': '已关闭 UPS 支持',
    'DiskWakeup': '磁盘唤醒 {name}',
    'DiskSpindown': '磁盘休眠 {name}',
    'DISK_IO_ERR': '磁盘 IO 错误 {dev}（{err_cnt} 次）',
    'ARCHIVING_SUCCESS': '归档成功',
    'DeleteFile': '文件删除 {FILE_NAME}',
    'MovetoTrashbin': '文件移到回收站 {FILE_NAME}',
    'SHARE_EVENTID_DEL': '共享删除',
    'SHARE_EVENTID_PUT': '共享更新',
    'WEBDAV_ENABLED': 'WebDAV 已启用',
    'WEBDAV_DISABLED': 'WebDAV 已关闭',
    'SAMBA_ENABLED': 'Samba 已启用',
    'SAMBA_DISABLED': 'Samba 已关闭',
    'DLNA_ENABLED': 'DLNA 已启用',
    'DLNA_DISABLED': 'DLNA 已关闭',
    'FTP_ENABLED': 'FTP 已启用',
    'FTP_DISABLED': 'FTP 已关闭',
    'NFS_ENABLED': 'NFS 已启用',
    'NFS_DISABLED': 'NFS 已关闭',
    'FW_ENABLE': '防火墙已开启',
    'FW_DISABLE': '防火墙已关闭',
    'SECURITY_PORTCHANGED': '安全/端口变更',
    'WAN_IP_CHANGED': '外网 IP 变化 {old_ip} → {new_ip}',
    'MEDIA_LOGIN_SUCC': '影视库登录 {user}@{IP}',
    'MEDIA_LOGOUT': '影视库退出 {user}@{IP}',
    'MEDIA_USER_CREATED': '影视库新建用户 {user}',
    'TRIM_RESOURCE_ADDED': '影视入库 {title}',
    'TRIM_SCRAPE_SUCCESS': '影视刮削完成 {title}',
    'PHOTO_SHARE_CREATED': '相册分享创建 {name}',
    'PHOTO_SHARE_EXPIRED': '相册分享过期 {name}',
    'PHOTO_DEVICE_REGISTERED': '相册同步设备 {device_name}',
    'FACE_RECOGNITION_UPDATED': '相册人脸识别完成',
    'MUSIC_TRACK_ADDED': '音乐入库 {title}',
    'MUSIC_TRACK_REMOVED': '音乐移出库 {title}',
    'MUSIC_FAVORITED': '音乐收藏 {title}',
    'MUSIC_PLAYLIST_TRACK_ADDED': '加入歌单 {playlist_name}',
    'BACKUP_TASK_SUCCESS': '备份任务完成',
    'BACKUP_TASK_FAILED': '备份任务失败',
    'BACKUP_TASK_PARTIAL_SUCCESS': '备份任务部分成功',
    'DOCKER_CONTAINER_CREATE': 'Docker 容器 {container_name} 已创建',
    'DOCKER_CONTAINER_START': 'Docker 容器 {container_name} 已启动',
    'DOCKER_CONTAINER_STOP': 'Docker 容器 {container_name} 已停止',
    'DOCKER_CONTAINER_DIE': 'Docker 容器 {container_name} 已退出',
    'DOCKER_CONTAINER_OOM': 'Docker 容器 {container_name} OOM',
    'DOCKER_CONTAINER_KILL': 'Docker 容器 {container_name} 已 Kill',
    'DOCKER_CONTAINER_PAUSE': 'Docker 容器 {container_name} 已暂停',
    'DOCKER_CONTAINER_UNPAUSE': 'Docker 容器 {container_name} 已恢复运行',
    'DOCKER_CONTAINER_RESTART': 'Docker 容器 {container_name} 已重启',
    'DOCKER_CONTAINER_DESTROY': 'Docker 容器 {container_name} 已删除',
    # 虚拟机（事件名是动态的，静态这几个只是兜底）
    'SHUTDOWN_VM': '虚拟机关机 {vm_name}',
    'STATUS_RUNNING_VM': '虚拟机状态：运行中 {vm_name}',
    'STATUS_REBOOTED_VM': '虚拟机重启 {vm_name}',
    'DESTROY_VM': '虚拟机已删除 {vm_name}',
    # 汇总事件
    'POLL_BATCH_SUMMARY': '多事件合并通知（{count} 条）',
}


# body 模板（{placeholder} 将被替换；缺失时被删掉）
EVENT_BODY_TEMPLATES = {
    'LoginSucc': '用户：{user}\n来源 IP：{IP}\n来源：{via}',
    'LoginFail': '用户：{user}\n来源 IP：{IP}\n来源：{via}',
    'SSH_AUTH_FAILED': '用户：{user}\n来源 IP：{IP}',
    'SSH_INVALID_USER': '用户：{user}\n来源 IP：{IP}',
    'SSH_LOGIN_SUCCESS': '用户：{user}\n来源 IP：{IP}',
    'SSH_DISCONNECTED': '用户：{user}\n来源 IP：{IP}',
    'SSH_OPEN_TIMEOUT': 'SSH 已开启 {remind_minutes} 分钟仍未关闭',
    'CPU_USAGE_ALARM': '当前使用率：{usage}%\n阈值：{threshold}%',
    'CPU_USAGE_RESTORED': '当前使用率：{usage}%\n阈值：{threshold}%',
    'MEMORY_USAGE_ALARM': '当前使用率：{usage}%\n阈值：{threshold}%',
    'MEMORY_USAGE_RESTORED': '当前使用率：{usage}%\n阈值：{threshold}%',
    'DISK_IO_ERR': '磁盘：{dev} ({model} / SN {serial})\nIO 错误次数：{err_cnt}',
    'FoundDisk': '磁盘名：{name}\n型号：{model}\n序列号：{serial}',
    'DiskWakeup': '磁盘名：{name}',
    'DiskSpindown': '磁盘名：{name}',
    'WAN_IP_CHANGED': '旧 IP：{old_ip}\n新 IP：{new_ip}',
    'BACKUP_TASK_SUCCESS': '备份任务已完成，数据安全',
    'BACKUP_TASK_FAILED': '备份任务执行失败，请检查备份配置与存储',
    'DeleteFile': '文件：{FILE}\n用户：{user}',
    'MovetoTrashbin': '文件：{FILE}\n用户：{user}',
    'SHARE_EVENTID_DEL': '共享：{NAME}\n用户：{user}',
    'SHARE_EVENTID_PUT': '共享：{NAME}\n用户：{user}',
    'DOCKER_CONTAINER_START': '容器：{container_name}\n镜像：{image}',
    'DOCKER_CONTAINER_DIE': '容器：{container_name}\n退出码：{exit_code}',
    'DOCKER_CONTAINER_OOM': '容器：{container_name}\n原因：内存超限',
    'TRIM_RESOURCE_ADDED': '名称：{title}\n类型：{type}\n路径：{path}',
    'TRIM_SCRAPE_SUCCESS': '名称：{title}\n类型：{type}',
    'MEDIA_LOGIN_SUCC': '用户：{user}\n来源 IP：{IP}\n设备：{device}',
    'MEDIA_LOGOUT': '用户：{user}\n来源 IP：{IP}',
    'MEDIA_USER_CREATED': '用户：{user}',
    'PHOTO_SHARE_CREATED': '分享：{name}\n分享 ID：{share_id}',
    'PHOTO_SHARE_EXPIRED': '分享：{name}\n分享 ID：{share_id}',
    'PHOTO_DEVICE_REGISTERED': '设备：{device_name}\n设备 ID：{device_id}',
    'FACE_RECOGNITION_UPDATED': '识别照片 ID：{user_photo_id}',
    'MUSIC_TRACK_ADDED': '曲目：{title}',
    'MUSIC_TRACK_REMOVED': '曲目：{title}',
    'MUSIC_FAVORITED': '曲目：{title}\n用户：{user}',
    'MUSIC_PLAYLIST_TRACK_ADDED': '曲目：{title}\n歌单：{playlist_name}',
    # 应用生命周期（fnOS 应用中心事件的 data 字段：APP_ID / APP_NAME / DISPLAY_NAME 等）
    'APP_STARTED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_STOPPED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_UPDATED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_INSTALLED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_AUTO_STARTED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_UNINSTALLED': '应用：{app_name}\n应用 ID：{APP_ID}',
    'APP_CRASH': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_UPDATE_FAILED': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION': '应用：{app_name}\n应用 ID：{APP_ID}\n来源：{from}',
    'APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE': '应用：{app_name}',
}


# ============================================================
# 四、工具函数：data 字段扁平化 + 占位符替换
# ============================================================

# 兜底正文用的英文字段名 → 中文标签（没有专用 body 模板的事件走这里）；
# 命中不了的键保持原文（动态字段无法穷举）
_FIELD_LABELS = {
    'APP_ID': '应用 ID', 'APP_NAME': '应用名', 'APP_TYPE': '应用类型', 'APP_VERSION': '应用版本',
    'DISPLAY_NAME': '显示名', 'INSTALL_VOLUME_ID': '安装卷', 'META_VOLUME_ID': '元数据卷',
    'PORT_USAGE': '端口占用数', 'VOLUME': '存储卷', 'VOLUME_ID': '存储卷 ID',
    'DATETIME': '时间', 'FROM': '来源', 'LEVEL': '日志级别', 'SERVICE': '服务',
    'USER': '用户', 'USERNAME': '用户', 'NAME': '名称', 'TITLE': '标题',
    'MODEL': '型号', 'SERIAL': '序列号', 'SN': '序列号', 'DEV': '设备', 'DEVICE': '设备',
    'DEVICE_NAME': '设备名', 'DEVICE_ID': '设备 ID', 'SHARE_ID': '分享 ID', 'SHARE_NAME': '分享名',
    'TYPE': '类型', 'PATH': '路径', 'FILE': '文件', 'FILE_NAME': '文件名', 'SIZE': '大小',
    'STATUS': '状态', 'OLD_IP': '旧 IP', 'NEW_IP': '新 IP', 'EXIT_CODE': '退出码', 'IMAGE': '镜像',
    'CONTAINER_NAME': '容器名', 'CONTAINER_ID': '容器 ID', 'VM_NAME': '虚拟机名',
    'THRESHOLD': '阈值', 'USAGE': '使用率', 'TEMP': '温度', 'ERR_CNT': '错误次数',
    'REASON': '原因', 'MESSAGE': '消息', 'ERROR': '错误', 'HOST': '主机', 'PORT': '端口',
    'BATT_CHARGE': '电池电量', 'BATT_STATUS': '电池状态', 'RUNTIME': '剩余续航', 'CHARGE': '电量',
    'PLAYLIST_NAME': '歌单', 'MAC': 'MAC 地址', 'IPADDR': 'IP',
}
# fnOS 日志级别数字 → 中文（取不到映射时保留原值）
_LEVEL_TEXT = {0: '信息', 1: '警告', 2: '错误', 3: '严重'}


def _dump_line(k: Any, v: Any) -> str:
    """兜底正文的一行：字段名转中文、时间戳转可读时间、日志级别转中文。"""
    if v is None:
        return ''
    sv = str(v).strip()
    if not sv:
        return ''
    ku = str(k).strip().upper()
    label = _FIELD_LABELS.get(ku) or _FIELD_LABELS.get(ku.lstrip('_')) or str(k).strip()
    if ku == 'DATETIME':
        digits = re.sub(r'\D', '', sv)
        if len(digits) in (10, 13):
            try:
                ts = int(digits) / (1000.0 if len(digits) == 13 else 1.0)
                sv = datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                pass
    elif ku == 'LEVEL':
        try:
            sv = _LEVEL_TEXT.get(int(float(sv)), sv)
        except Exception:
            pass
    return f'{label}：{sv}'


# 动态 VM 事件（START_VM / CREATE_VM 等，事件名不在白名单字典里）的标题前缀
_VM_TITLE_RULES = [
    (re.compile(r'CREATE_VM'), '虚拟机已创建'),
    (re.compile(r'^(START|BOOT)_VM'), '虚拟机已启动'),
    (re.compile(r'^(REBOOT|RESET)_VM'), '虚拟机已重启'),
    (re.compile(r'^(RESUME|UNPAUSE)_VM'), '虚拟机已恢复运行'),
    (re.compile(r'^(PAUSE|SUSPEND)_VM'), '虚拟机已暂停'),
    (re.compile(r'^(STOP|SHUTDOWN|HALT|POWEROFF)_VM'), '虚拟机已停止'),
    (re.compile(r'^(DESTROY|DELETE)_VM'), '虚拟机已删除'),
    (re.compile(r'VM'), '虚拟机事件'),
]


def _dynamic_vm_title(event_type: str) -> str:
    s = str(event_type or '').upper()
    for pat, t in _VM_TITLE_RULES:
        if pat.search(s):
            return t
    return ''


def _flatten_data(data: Any, prefix: str = "") -> Dict[str, Any]:
    """把 event_data 扁平化成 {key: value} 字典，嵌套 dict 用 path 扁平化（也保留顶层 key）。"""
    out: Dict[str, Any] = {}
    if not isinstance(data, dict):
        return out
    for k, v in data.items():
        top_key = str(k).strip()
        if top_key:
            out[top_key] = v
            out[top_key.lower()] = v
            out[top_key.upper()] = v
        if isinstance(v, dict):
            sub = _flatten_data(v, prefix=top_key + "_")
            out.update(sub)
    return out


def _render_template(tpl: str, flat: Dict[str, Any]) -> str:
    """把模板里的 {placeholder} 替换成 flat 里的值。缺失的占位符直接删掉。"""
    def _sub(m):
        key = m.group(1).strip()
        if not key:
            return ""
        if key in flat:
            return str(flat[key])
        # 大小写宽松
        lk = key.lower()
        if lk in flat:
            return str(flat[lk])
        uk = key.upper()
        if uk in flat:
            return str(flat[uk])
        return ""
    return re.sub(r"\{([^}]+)\}", _sub, tpl)


def _normalize_app_name(event_data: Dict[str, Any]) -> str:
    """从 event_data 里提取应用显示名（兼容 data.DISPLAY_NAME / data.APP_NAME / 顶层 name）。"""
    data = event_data.get("data") if isinstance(event_data.get("data"), dict) else {}
    return (
        data.get("DISPLAY_NAME")
        or data.get("APP_NAME")
        or event_data.get("name")
        or event_data.get("app_name")
        or ""
    )


def build_message(event_type: str, event_data: Optional[Dict[str, Any]] = None) -> Optional[Tuple[str, str]]:
    """
    事件 → (title, body)。title 空模板返回 None 表示这个事件不应该推送（理论上不会发生）。

    event_data 允许包含 data 嵌套字段（来自 logger_data.db3 parameter）。
    """
    ed = event_data or {}
    flat = _flatten_data(ed)
    # 应用名特殊字段兜底（DISPLAY_NAME 优先，覆盖扁平化带进来的 APP_NAME）
    app_name = _normalize_app_name(ed)
    if app_name:
        flat["app_name"] = app_name
    # FoundDisk / DISK_IO_ERR：data 里是 SN / MODEL，模板占位符是 {serial} / {model}，补别名
    if "SN" in flat and "serial" not in flat:
        flat["serial"] = flat["SN"]
    if "MODEL" in flat and "model" not in flat:
        flat["model"] = flat["MODEL"]

    # DISK_IO_ERR：把规范化后的字段也塞到 flat 里，方便模板用 {model} / {serial}
    if event_type == "DISK_IO_ERR":
        dev, model, sn, err_cnt = normalize_disk_io_payload(ed)
        if dev and "dev" not in flat:
            flat["dev"] = dev
        if model and "model" not in flat:
            flat["model"] = model
        if sn and "serial" not in flat:
            flat["serial"] = sn
            flat["sn"] = sn
        if err_cnt is not None and "err_cnt" not in flat:
            flat["err_cnt"] = err_cnt

    # 文件类事件：标题只显示文件名，正文里再给完整路径
    if event_type in ("DeleteFile", "MovetoTrashbin"):
        fp = str(flat.get("FILE") or flat.get("file") or "").strip()
        if fp:
            flat["FILE_NAME"] = os.path.basename(fp.rstrip("/")) or fp

    title_tpl = EVENT_TITLES.get(event_type)
    if not title_tpl:
        # 未知事件：动态 VM 事件用中文标题（如 START_VM → 虚拟机已启动），其余用 event_type 做前缀
        vm_t = _dynamic_vm_title(event_type)
        if vm_t:
            vd = str(flat.get('vm_name') or flat.get('VM_NAME') or flat.get('name') or flat.get('NAME') or '').strip()
            if vd:
                flat['vm_disp'] = ' ' + vd
            title_tpl = vm_t + '{vm_disp}'
        else:
            title_tpl = event_type + " {user}@{IP}"
            # 把 event_type 也塞进 flat，方便后续扩展
            if "event_type" not in flat:
                flat["event_type"] = event_type

    title = _render_template(title_tpl, flat) or event_type
    # {user}@{IP} 双双缺失时标题尾部会残留「 @」，清掉
    title = re.sub(r"\s+@\s*$", "", title.strip())

    body_tpl = EVENT_BODY_TEMPLATES.get(event_type)
    if body_tpl:
        body = _render_template(body_tpl, flat)
    else:
        # 没有专用模板：把 event_data 里的顶层非空字段拼成几行
        # 跳过轮询器内部字段（cat / template / _source / _logtime 等），
        # 否则推送正文会出现「cat: 2 / template: DeleteFile」这类调试噪音；
        # 字段名转中文、时间戳转可读时间（见 _dump_line）
        _INTERNAL_KEYS = frozenset({
            "data", "raw_data", "entry", "cat", "template", "_source", "_logtime",
            "event_type", "eventId", "serviceId", "loglevel", "id", "uid",
        })
        body_lines = []
        for k, v in ed.items():
            if k in _INTERNAL_KEYS:
                continue
            line = _dump_line(k, v)
            if line:
                body_lines.append(line)
        data_block = ed.get("data")
        if isinstance(data_block, dict) and data_block:
            for k, v in data_block.items():
                line = _dump_line(k, v)
                if line:
                    body_lines.append(line)
        body = "\n".join(body_lines)

    # 占位符缺失或字段为空时会出现「标签：」空值行，统一清掉
    if body:
        body = "\n".join(l for l in body.split("\n") if l.strip() and not l.strip().endswith(("：", ":")))

    # 全部被删掉 = 空 title 且空 body
    if not (title or body):
        return None
    return title, body


# ============================================================
# 五、DISK_IO_ERR 特殊解析（来自 FnMessageBots 的 normalize_disk_io_payload）
# ============================================================

def normalize_disk_io_payload(ed: Dict[str, Any]) -> Tuple[str, str, str, Any]:
    nested = ed.get("data")
    data = nested if isinstance(nested, dict) else {}
    dev = str(data.get("DEV") or data.get("dev") or ed.get("disk") or ed.get("dev") or "").strip()
    model = str(data.get("MODEL") or data.get("model") or ed.get("model") or "").strip()
    sn = str(data.get("SN") or data.get("sn") or ed.get("serial") or "").strip()
    err_cnt = data.get("ERR_CNT", data.get("err_cnt"))
    if err_cnt is None:
        err_cnt = ed.get("ERR_CNT", ed.get("err_cnt", 0))
    return dev, model, sn, err_cnt
