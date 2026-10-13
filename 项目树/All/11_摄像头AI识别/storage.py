# -*- coding: utf-8 -*-
r"""
摄像头台账存储（SQLite）

只有一张表 `cameras`，做增删改查。**没有账户表、没有权限、没有审计链**——
本子方案刻意不带这些（需求就是"无需加密码什么的"）。

url 支持三种写法，取流层会各自处理：
  · RTSP/HTTP 地址：rtsp://... / http://.../video_feed
  · 本机摄像头序号：0、1、2（整数）
  · 本地视频文件：D:\videos\room1.mp4
"""
import re
import sqlite3
import time
from dataclasses import dataclass, field, asdict

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cameras (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    url         TEXT    NOT NULL,
    room        TEXT    DEFAULT '',
    enabled     INTEGER DEFAULT 1,
    note        TEXT    DEFAULT '',
    created_at  TEXT    DEFAULT '',
    updated_at  TEXT    DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cameras_url ON cameras(url);
"""


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Camera:
    """一路摄像头。id 为 None 表示尚未入库。"""
    name: str
    url: str
    room: str = ""
    enabled: bool = True
    note: str = ""
    id: int = None
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self):
        return asdict(self)

    @property
    def kind(self):
        """给界面显示用的类型标签。"""
        u = (self.url or "").strip()
        if not u:
            return "空"
        if re.fullmatch(r"\d+", u):
            return "本机摄像头 %s" % u
        low = u.lower()
        if low.startswith("rtsp://"):
            return "RTSP"
        if low.startswith(("http://", "https://")):
            return "HTTP/推流"
        if low.endswith((".mp4", ".avi", ".mkv", ".mov", ".flv", ".mjpg")):
            return "本地视频"
        return "未知"


def validate_url(url):
    """校验地址写法，返回 (是否合法, 原因)。入库前调用，挡住明显的错。"""
    u = (url or "").strip()
    if not u:
        return False, "地址不能为空"
    if re.fullmatch(r"\d+", u):
        return True, "本机摄像头序号"
    low = u.lower()
    if low.startswith(("rtsp://", "http://", "https://", "rtmp://")):
        # 至少要有个主机名/端口，不能只有 scheme
        rest = u.split("://", 1)[1]
        if not rest or rest.startswith("/"):
            return False, "地址缺少主机名（例如 rtsp://192.168.1.64:554/...）"
        return True, "网络地址"
    if low.endswith((".mp4", ".avi", ".mkv", ".mov", ".flv", ".mjpg")):
        import os
        if not os.path.exists(u):
            return False, "本地视频文件不存在"
        return True, "本地视频"
    return False, "无法识别的地址（应为序号 / rtsp:// / http:// / 本地视频路径）"


class CameraStore:
    """摄像头台账（SQLite）。所有方法都不抛业务异常，失败返回 False/None。"""

    def __init__(self, db_path=None):
        self.db_path = str(db_path or config.DB_PATH)
        self._init_db()

    # ---------- 底层 ----------
    def _conn(self):
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def _init_db(self):
        with self._conn() as con:
            con.executescript(_SCHEMA)

    @staticmethod
    def _row_to_cam(row):
        return Camera(
            id=row["id"], name=row["name"], url=row["url"], room=row["room"] or "",
            enabled=bool(row["enabled"]), note=row["note"] or "",
            created_at=row["created_at"] or "", updated_at=row["updated_at"] or "")

    # ---------- 读 ----------
    def all(self):
        """全部摄像头，按 id 升序。"""
        with self._conn() as con:
            rows = con.execute("SELECT * FROM cameras ORDER BY id").fetchall()
        return [self._row_to_cam(r) for r in rows]

    def enabled(self):
        """启用中的摄像头（AI 页只跑这些）。"""
        with self._conn() as con:
            rows = con.execute(
                "SELECT * FROM cameras WHERE enabled=1 ORDER BY id").fetchall()
        return [self._row_to_cam(r) for r in rows]

    def get(self, cam_id):
        with self._conn() as con:
            row = con.execute("SELECT * FROM cameras WHERE id=?",
                              (int(cam_id),)).fetchone()
        return self._row_to_cam(row) if row else None

    def count(self):
        with self._conn() as con:
            return int(con.execute("SELECT COUNT(*) FROM cameras").fetchone()[0])

    def url_exists(self, url, exclude_id=None):
        """同一地址是否已登记（避免同一路加两遍）。"""
        sql = "SELECT id FROM cameras WHERE url=?"
        args = [(url or "").strip()]
        if exclude_id is not None:
            sql += " AND id<>?"
            args.append(int(exclude_id))
        with self._conn() as con:
            return con.execute(sql, args).fetchone() is not None

    # ---------- 写 ----------
    def add(self, cam):
        """新增；返回 (是否成功, 说明)。重复地址会被拒绝。"""
        ok, why = validate_url(cam.url)
        if not ok:
            return False, why
        url = cam.url.strip()
        if self.url_exists(url):
            return False, "该地址已登记过，请勿重复添加"
        ts = _now()
        with self._conn() as con:
            cur = con.execute(
                "INSERT INTO cameras(name,url,room,enabled,note,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (cam.name.strip() or "未命名摄像头", url, cam.room or "",
                 1 if cam.enabled else 0, cam.note or "", ts, ts))
            cam.id = int(cur.lastrowid)
        cam.created_at = cam.updated_at = ts
        return True, "已添加 #%d" % cam.id

    def update(self, cam):
        """按 id 更新；返回 (是否成功, 说明)。"""
        if not cam.id:
            return False, "缺少 id，无法更新"
        ok, why = validate_url(cam.url)
        if not ok:
            return False, why
        url = cam.url.strip()
        if self.url_exists(url, exclude_id=cam.id):
            return False, "该地址已被其它摄像头占用"
        ts = _now()
        with self._conn() as con:
            cur = con.execute(
                "UPDATE cameras SET name=?,url=?,room=?,enabled=?,note=?,updated_at=?"
                " WHERE id=?",
                (cam.name.strip() or "未命名摄像头", url, cam.room or "",
                 1 if cam.enabled else 0, cam.note or "", ts, int(cam.id)))
            if cur.rowcount == 0:
                return False, "未找到 #%s" % cam.id
        cam.updated_at = ts
        return True, "已保存 #%d" % cam.id

    def set_enabled(self, cam_id, enabled):
        with self._conn() as con:
            cur = con.execute("UPDATE cameras SET enabled=?, updated_at=? WHERE id=?",
                              (1 if enabled else 0, _now(), int(cam_id)))
            return cur.rowcount > 0

    def delete(self, cam_id):
        with self._conn() as con:
            cur = con.execute("DELETE FROM cameras WHERE id=?", (int(cam_id),))
            return cur.rowcount > 0

    def clear(self):
        with self._conn() as con:
            con.execute("DELETE FROM cameras")

    # ---------- 导入导出（方便批量登记/备份） ----------
    def export_json(self):
        import json
        return json.dumps([c.to_dict() for c in self.all()],
                          ensure_ascii=False, indent=2)

    def import_json(self, text, replace=False):
        """从 JSON 批量导入；返回 (新增数, 跳过数, 说明)。"""
        import json
        try:
            items = json.loads(text)
        except Exception as e:
            return 0, 0, "JSON 解析失败：%s" % str(e)[:80]
        if not isinstance(items, list):
            return 0, 0, "JSON 顶层应为数组"
        if replace:
            self.clear()
        added = skipped = 0
        for it in items:
            if not isinstance(it, dict) or not it.get("url"):
                skipped += 1
                continue
            cam = Camera(name=str(it.get("name") or "未命名摄像头"),
                         url=str(it.get("url")),
                         room=str(it.get("room") or ""),
                         enabled=bool(it.get("enabled", True)),
                         note=str(it.get("note") or ""))
            ok, _ = self.add(cam)
            if ok:
                added += 1
            else:
                skipped += 1
        return added, skipped, "新增 %d 路，跳过 %d 路" % (added, skipped)
