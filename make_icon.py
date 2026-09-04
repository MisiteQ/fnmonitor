#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_icon.py - 生成 fnMonitor 应用图标（PNG）
================================================
仅使用 Python 标准库（zlib / struct / math / os / shutil），无第三方依赖。
按 fnOS FPK 官方规范生成 4 个文件：
  ICON.PNG                      (64x64,   包根小图标)
  ICON_256.PNG                  (256x256, 包根大图标)
  app/ui/images/icon-64.png     (64x64,   桌面图标，fnOS 要求连字符命名)
  app/ui/images/icon-256.png    (256x256, 桌面图标，fnOS 要求连字符命名)

图标样式：深蓝渐变圆角底 + 白色监控波形（含半透明面积填充）
用法：python3 make_icon.py    （在工程根目录执行）
"""
import zlib
import struct
import math
import os
import shutil

ROOT = os.path.dirname(os.path.abspath(__file__))


def _chunk(typ, data):
    c = struct.pack(">I", len(data)) + typ + data
    return c + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)


def _encode_png(size, get_rgba):
    """get_rgba(x, y) -> (r, g, b, a)，坐标范围 0..size-1"""
    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter: None
        for x in range(size):
            r, g, b, a = get_rgba(x, y)
            raw += bytes((r & 0xFF, g & 0xFF, b & 0xFF, a & 0xFF))
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8bit RGBA
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def _pt_seg_dist(px, py, ax, ay, bx, by):
    vx, vy = bx - ax, by - ay
    wx, wy = px - ax, py - ay
    l2 = vx * vx + vy * vy
    if l2 == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / l2))
    projx = ax + t * vx
    projy = ay + t * vy
    return math.hypot(px - projx, py - projy)


def build_icon(size):
    radius = size * 0.20
    # 波形控制点（相对坐标 0..1，从左下到右上）
    pts = [(0.06, 0.80), (0.22, 0.48), (0.38, 0.62), (0.55, 0.30),
           (0.72, 0.52), (0.94, 0.18)]

    def in_round(px, py):
        r = radius
        cx = min(max(px, r), size - r)
        cy = min(max(py, r), size - r)
        dx = px - cx
        dy = py - cy
        return dx * dx + dy * dy <= r * r

    def get_rgba(x, y):
        if not in_round(x + 0.5, y + 0.5):
            return (0, 0, 0, 0)
        # 背景：深蓝渐变（上深下亮）
        t = (y + 0.5) / size
        bg = (int(11 + 43 * t), int(16 + 173 * t), int(32 + 216 * t))
        # 波形折线距离（相对坐标）
        nx = (x + 0.5) / size
        ny = (y + 0.5) / size
        dmin = 1e9
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            dmin = min(dmin, _pt_seg_dist(nx, ny, ax, ay, bx, by))
        # 面积填充（折线下方淡白）
        below = False
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            if min(ax, bx) <= nx <= max(ax, bx):
                y_line = ay + (by - ay) * (nx - ax) / (bx - ax) if bx != ax else ay
                if ny > y_line:
                    below = True
                    break
        if below and ny > 0.78:
            return (255, 255, 255, 0)  # 避免最底部面积遮挡
        # 线宽（随尺寸缩放）
        lw = 0.030 if size < 128 else 0.022
        if dmin < lw:
            return (255, 255, 255, 255)  # 白线
        if below:
            alpha = max(0, int(90 * (1.0 - dmin / 0.10)))
            return (255, 255, 255, alpha)  # 半透明面积
        return (bg[0], bg[1], bg[2], 255)

    return _encode_png(size, get_rgba)


def main():
    targets = [
        ("ICON.PNG", 64),
        ("ICON_256.PNG", 256),
        (os.path.join("app", "ui", "images", "icon-64.png"), 64),
        (os.path.join("app", "ui", "images", "icon-256.png"), 256),
    ]
    for rel, size in targets:
        path = os.path.join(ROOT, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(build_icon(size))
        print("生成图标: %s (%dx%d, %d bytes)" % (rel, size, size, os.path.getsize(path)))
    # 兼容旧路径：把规范图标也复制到 ui 根目录（部分 fnOS 版本读取 ui/ICON.PNG）
    compat = [
        (os.path.join(ROOT, "ICON.PNG"),
         os.path.join(ROOT, "app", "ui", "ICON.PNG")),
        (os.path.join(ROOT, "ICON_256.PNG"),
         os.path.join(ROOT, "app", "ui", "ICON_256.PNG")),
    ]
    for src, dst in compat:
        try:
            shutil.copyfile(src, dst)
            print("兼容图标: %s" % os.path.relpath(dst, ROOT))
        except Exception as e:
            print("兼容图标跳过 %s: %s" % (os.path.relpath(dst, ROOT), e))
    print("图标生成完成。")


if __name__ == "__main__":
    main()
