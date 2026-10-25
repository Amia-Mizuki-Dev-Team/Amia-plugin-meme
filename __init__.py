import asyncio
import hashlib
import ipaddress
import json
import math
import os
import random
import re
import socket
import time
import uuid
import urllib.parse
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Set, Optional

import aiofiles
import httpx
try:
    import mysql.connector
except ModuleNotFoundError:
    mysql = None
from nonebot import get_driver, on_message, on_regex
from nonebot.adapters.onebot.v11 import Bot, Event, GroupMessageEvent, Message, MessageSegment
from nonebot.exception import ActionFailed, NetworkError, FinishedException
from nonebot.log import logger
from nonebot.matcher import Matcher
from nonebot.params import RegexGroup
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule
from PIL import Image, ImageDraw, ImageFont

# ================= 1. 插件元数据 =================
__plugin_meta__ = PluginMetadata(
    name="Meme随机 (Final V40.9)",
    description="本地图库：中文帮助、随机发送、上传和分类管理",
    usage=(
        "🤖 Mizuki Bot 表情包\n"
        "\n"
        "【常用功能】\n"
        "• 表情帮助 / 表情列表\n"
        "• 随机表情 / 随一张\n"
        "• 发送 <文件夹名>\n"
        "• 查看 <图片名>\n"
        "• 看所有 <文件夹名>\n"
        "\n"
        "【上传】\n"
        "• <文件夹名> 上传（在同一条消息里配一张图片）\n"
        "\n"
        "投稿只接受同一条消息里自带的图片；回复图片、文件、ZIP、链接都会直接拒绝，"
        "也不会触发任何下载、解压或后台任务。"
    ),
    type="application",
    homepage="",
    supported_adapters={"~onebot.v11"},
    extra={
        "author": "Amia-Mizuki-Dev-Team",
        "version": "010",
        "pmn": {"markdown": True},
        # PicMenu 面向普通成员只展示稳定的中文用户功能；管理命令仍然
        # 保留在代码中，但从默认帮助菜单隐藏，避免把后台操作当成按钮。
        "menu_data": [
            {
                "func": "表情列表",
                "trigger_method": "指令",
                "trigger_condition": "表情列表",
                "brief_des": "查看当前可用的表情分类",
                "detail_des": "只显示当前用户有权限访问且包含图片的分类。",
            },
            {
                "func": "随机表情",
                "trigger_method": "指令",
                "trigger_condition": "随机表情 / 随一张",
                "brief_des": "从可用图库随机发送一张图片",
                "detail_des": "受到分类权限和群屏蔽设置限制。",
            },
            {
                "func": "发送分类表情",
                "trigger_method": "指令",
                "trigger_condition": "发送 <文件夹名>",
                "brief_des": "从指定分类发送下一张图片",
                "detail_des": "同一用户会尽量轮换图片，发送完分类后重新开始。",
            },
            {
                "func": "查看图片",
                "trigger_method": "指令",
                "trigger_condition": "查看 <图片名>",
                "brief_des": "按文件名查看一张图片",
                "detail_des": "图片名支持中文、字母、数字、下划线和常见扩展名。",
            },
            {
                "func": "查看分类预览",
                "trigger_method": "指令",
                "trigger_condition": "看所有 <文件夹名>",
                "brief_des": "生成指定分类的图片预览图",
                "detail_des": "最多生成前 500 张，超出部分会截断。",
            },
            {
                "func": "上传表情",
                "trigger_method": "指令",
                "trigger_condition": "<文件夹名> 上传",
                "brief_des": "向指定分类上传一张图片",
                "detail_des": "请在同一条消息里配一张图片；回复图片、文件、ZIP 与链接均不支持。重复图片会被检测。",
            },
            {
                "func": "最近上传",
                "trigger_method": "管理员指令",
                "trigger_condition": "最近上传",
                "brief_des": "查看最近入库记录",
                "detail_des": "仅管理员可用。",
                "pmn_hidden": True,
            },
            {
                "func": "图库管理",
                "trigger_method": "管理员指令",
                "trigger_condition": "删除 / 溯源 / 锁定 / 屏蔽群",
                "brief_des": "管理图片、分类和群屏蔽",
                "detail_des": "仅管理员可用。",
                "pmn_hidden": True,
            },
        ],
    },
)

# ================= 2. 配置与常量 =================
driver = get_driver()
config = driver.config

# 这里的 getattr 就会自动去读取你的 .env.prod 文件中的对应字段
MEME_DB_HOST = getattr(config, "meme_db_host", "127.0.0.1")
MEME_DB_PORT = getattr(config, "meme_db_port", 3306)
MEME_DB_USER = getattr(config, "meme_db_user", "root")
MEME_DB_PASSWORD = getattr(config, "meme_db_password", "")
MEME_DB_DATABASE = getattr(config, "meme_db_database", "meme_db")

try: SUPERUSERS = config.superusers
except: SUPERUSERS = set()

# 你现在可以在 .env 中添加 MEME_ADMIN_USERS=[3429630094, ...] 来覆盖默认值
ADMIN_USERS = set(getattr(config, "meme_admin_users", [3429630094, 3316413099, 2338680148]))
BANNED_WORDS = {"djb", "sb", "nm", "nmb"}


def _cfg_number(key: str, default, cast):
    """读取可选的预算类配置；非法值回落默认值，不阻断插件加载。"""
    raw = getattr(config, key, None)
    if raw is None or raw == "":
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        logger.warning(f"[Meme] 配置 {key} 非法，已使用默认值")
        return default


# 投稿图片预算：这是项目自定的预算，不是 QQ/OneBot 平台的硬上限，可按需在 .env 调整。
MEME_MAX_IMAGE_BYTES = max(1, _cfg_number("meme_max_image_bytes", 5 * 1024 * 1024, int))
MEME_MAX_IMAGE_PIXELS = max(1, _cfg_number("meme_max_image_pixels", 16_000_000, int))
MEME_MAX_IMAGE_FRAMES = max(1, _cfg_number("meme_max_image_frames", 300, int))
MEME_DOWNLOAD_CONCURRENCY = max(1, _cfg_number("meme_download_concurrency", 2, int))
MEME_DOWNLOAD_TIMEOUT = max(1.0, _cfg_number("meme_download_timeout", 20.0, float))
# 槽位等待与整次下载总时限：防止滴流响应长期占满并发槽位。
MEME_DOWNLOAD_SLOT_WAIT = max(1.0, _cfg_number("meme_download_slot_wait", 30.0, float))
MEME_DOWNLOAD_TOTAL_TIMEOUT = max(1.0, _cfg_number("meme_download_total_timeout", 60.0, float))
MEME_MAX_REDIRECTS = max(0, _cfg_number("meme_max_redirects", 3, int))
MEME_ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "GIF", "WEBP", "BMP")
MEME_DB_RETRY_SECONDS = max(1.0, _cfg_number("meme_db_retry_seconds", 300.0, float))

PLUGIN_DIR = Path(__file__).parent.resolve()
IMAGE_DIR = PLUGIN_DIR / "meme"
RESTRICTED_FILE = PLUGIN_DIR / "restricted.json"
BLACKLIST_FILE = PLUGIN_DIR / "blacklist.json"
IMAGE_INFO_FILE = PLUGIN_DIR / "image_info.json"

# 画廊根目录只在插件自己的目录下。启动期只做一次兜底 mkdir（不整理、不删除任何内容）；
# 目录不可写时降级告警而不是让整个插件导入失败，写盘失败会在具体操作上报错。
try:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
except OSError as _mkdir_error:
    logger.warning(
        f"[Meme] 图片根目录不可用（将降级运行）: {type(_mkdir_error).__name__}"
    )
GALLERY_ROOT = IMAGE_DIR.resolve()

USER_HISTORY = {}

# ================= 3. 路径与登记边界 =================
# 规则：用户输入只作为「查询键」。分类/图片一律先映射成登记项（登记 ID），
# 再由登记项里的真实名字拼路径，resolve 后断言仍在画廊根内。
# 用户字符串因此从不直接进入文件系统调用。

_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_FORBIDDEN_NAME_CHARS = set('<>:"/\\|?*')
MAX_CATEGORY_NAME_LEN = 32
MAX_IMAGE_NAME_LEN = 100


class MemeError(Exception):
    """面向用户的拒绝原因：message 可直接回群，不含路径与异常正文。"""


class MemePathError(MemeError):
    """名称或路径越界、不合规。"""


class MemeCategoryAmbiguous(MemeError):
    """分类名命中多个仅大小写不同的目录：拒绝操作，绝不猜其中一个。"""


def validate_name(raw, *, max_len: int) -> str:
    """校验单个名字段。拒绝保留设备名、尾点/尾空格、盘符、ADS 与分隔符。"""
    if not isinstance(raw, str):
        raise MemePathError("名称不合法")
    name = raw
    if not name or len(name) > max_len:
        raise MemePathError("名称长度不合法")
    if name != name.strip():
        raise MemePathError("名称前后不能有空格")
    if name in (".", "..") or name.endswith("."):
        raise MemePathError("名称不能以点结尾")
    if name.startswith("."):
        raise MemePathError("名称不能以点开头")
    if any(ch in _FORBIDDEN_NAME_CHARS for ch in name):
        raise MemePathError("名称不能包含路径分隔符、盘符或其它特殊符号")
    if any(ord(ch) < 32 for ch in name):
        raise MemePathError("名称不能包含控制字符")
    if name.split(".")[0].upper() in _WINDOWS_RESERVED_NAMES:
        raise MemePathError("名称与系统保留设备名冲突")
    return name


def ensure_within(root: Path, candidate: Path) -> Path:
    """resolve 之后断言仍在 root 内，否则拒绝。"""
    root = Path(root).resolve()
    resolved = Path(candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise MemePathError("路径越界")
    return resolved


class GalleryIndex:
    """分类登记表：登记 ID → 分类名 → 根内路径。只存在内存里，不写盘。

    登记 ID 由**真实名**（区分大小写）派生；查询支持大小写不敏感匹配，
    但只在唯一命中时接受，命中多个（如 ``A`` 与 ``a`` 并存）即视为歧义。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self._by_name: Dict[str, dict] = {}
        self._by_folded: Dict[str, List[dict]] = {}

    def refresh(self) -> "GalleryIndex":
        self._by_name.clear()
        self._by_folded.clear()
        try:
            children = sorted(self.root.iterdir(), key=lambda p: p.name)
        except OSError:
            return self
        for child in children:
            try:
                if not child.is_dir():
                    continue
                name = validate_name(child.name, max_len=MAX_CATEGORY_NAME_LEN)
            except (MemePathError, OSError):
                # 不合规的历史目录不登记：只是不可见，不做任何改名或删除。
                continue
            entry = {
                "id": hashlib.sha256(name.encode("utf-8")).hexdigest()[:12],
                "name": name,
                "path": child,
            }
            self._by_name[name] = entry
            self._by_folded.setdefault(name.casefold(), []).append(entry)
        return self

    def ambiguous(self, query) -> List[dict]:
        if not isinstance(query, str):
            return []
        if query in self._by_name:
            return []
        return list(self._by_folded.get(query.casefold(), []))

    def find(self, query) -> Optional[dict]:
        if not isinstance(query, str):
            return None
        exact = self._by_name.get(query)
        if exact is not None:
            return exact
        matches = self._by_folded.get(query.casefold()) or []
        # 仅大小写不同的多个目录互不合并：只有唯一命中时才认作该分类。
        return matches[0] if len(matches) == 1 else None

    def resolve(self, entry: dict) -> Path:
        return ensure_within(self.root, entry["path"])

    def entries(self) -> List[dict]:
        return list(self._by_name.values())


GALLERY = GalleryIndex(IMAGE_DIR)


def lookup_category(query) -> Optional[dict]:
    """查询键 → 登记项；每次刷新都是只读目录扫描，新增分类立即可见。"""
    return GALLERY.refresh().find(query)


def category_path(entry: dict) -> Path:
    return GALLERY.resolve(entry)


def ensure_category(raw_name: str) -> tuple[dict, Path, bool]:
    """校验并登记一个分类，返回 (登记项, 根内路径, 是否新建)。"""
    name = validate_name(raw_name, max_len=MAX_CATEGORY_NAME_LEN)
    index = GALLERY.refresh()
    exact = index.find(name)
    if exact is not None:
        return exact, category_path(exact), False
    rivals = index.ambiguous(name)
    if rivals:
        # 已有仅大小写不同的分类：绝不静默合并到其中一个。
        raise MemePathError(
            "分类名与已有分类仅大小写不同：" + "、".join(sorted(e["name"] for e in rivals))
        )
    candidate = ensure_within(GALLERY_ROOT, IMAGE_DIR / name)
    candidate.mkdir(parents=True, exist_ok=True)
    entry = GALLERY.refresh().find(name)
    if entry is None:
        raise MemePathError("分类登记失败")
    return entry, category_path(entry), True


def find_image(folder: Path, query: str) -> Optional[Path]:
    """按登记到的真实文件名/主名精确查找；不接受任何路径成分。"""
    if not isinstance(query, str) or not query:
        return None
    try:
        wanted = validate_name(query, max_len=MAX_IMAGE_NAME_LEN)
    except MemePathError:
        return None
    folder = ensure_within(GALLERY_ROOT, folder)
    for path in get_sorted_files(folder):
        if (
            path.name.casefold() == wanted.casefold()
            or path.stem.casefold() == wanted.casefold()
        ):
            return ensure_within(folder, path)
    return None

# ================= 4. 数据库管理器 =================
class MemeDatabase:
    """MySQL 访问层。

    普通 import / startup 不连接、不建表：连接按需建立，建表只在显式
    ensure_schema() 里执行。连接失败后进入冷却期，避免每条消息都重试。
    """

    def __init__(self):
        self.conn = None
        self._next_retry_at = 0.0
        self._warned_missing_driver = False

    def connect(self) -> bool:
        if mysql is None:
            if not self._warned_missing_driver:
                logger.warning("[Meme DB] mysql.connector 未安装；数据库功能不可用")
                self._warned_missing_driver = True
            return False
        now = time.monotonic()
        if now < self._next_retry_at:
            return False
        try:
            self.conn = mysql.connector.connect(
                host=MEME_DB_HOST,
                port=MEME_DB_PORT,
                user=MEME_DB_USER,
                password=MEME_DB_PASSWORD,
                database=MEME_DB_DATABASE,
                charset='utf8mb4',
                autocommit=True
            )
            logger.info("[Meme DB] 已连接")
            return True
        except Exception as e:
            self.conn = None
            self._next_retry_at = now + MEME_DB_RETRY_SECONDS
            # 数据库不可用时插件整体降级运行，属可恢复状态，因此只记 warning。
            logger.warning(
                f"[Meme DB] 连接失败，{MEME_DB_RETRY_SECONDS:.0f}s 内不再重试: {type(e).__name__}"
            )
            return False

    def _get_cursor(self):
        if not self.conn or not getattr(self.conn, "is_connected", lambda: False)():
            self.conn = None
            if not self.connect():
                return None
        try:
            return self.conn.cursor(dictionary=True)
        except Exception as e:
            logger.warning(f"[Meme DB] 获取游标失败: {type(e).__name__}")
            self.conn = None
            return None

    def ensure_schema(self) -> bool:
        """显式建表入口：只有维护流程会调用，import/startup 不调用。"""
        cursor = self._get_cursor()
        if not cursor:
            return False
        try:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS meme_images (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    folder VARCHAR(50) NOT NULL,
                    filename VARCHAR(100) NOT NULL,
                    user_id VARCHAR(20),
                    nickname VARCHAR(100),
                    created_at DATETIME,
                    UNIQUE KEY unique_img (folder, filename)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
            ''')
            return True
        except Exception as e:
            logger.warning(f"[Meme DB] 建表失败: {type(e).__name__}")
            return False
        finally: cursor.close()

    def schema_ready(self) -> bool:
        """只读检查表是否存在：dry-run 用它，不执行任何 DDL。"""
        cursor = self._get_cursor()
        if not cursor:
            return False
        try:
            cursor.execute("SHOW TABLES LIKE %s", ("meme_images",))
            return cursor.fetchone() is not None
        except Exception as e:
            logger.warning(f"[Meme DB] 表检查失败: {type(e).__name__}")
            return False
        finally: cursor.close()

    def migrate_json(self) -> bool:
        """显式维护专用：历史 JSON 真正迁入后才把原文件改名备份。

        插入 0 行（例如数据库不可写或 JSON 全是坏键）时保留原文件并返回 False，
        避免出现「文件改名了、一条记录都没进库」的不可逆结果。
        """
        if not IMAGE_INFO_FILE.exists(): return False
        cursor = self._get_cursor()
        if not cursor: return False
        inserted = 0
        try:
            with open(IMAGE_INFO_FILE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            for key, val in old_data.items():
                parts = key.replace("\\", "/").split("/")
                if len(parts) < 2: continue
                try:
                    cursor.execute('''
                        INSERT IGNORE INTO meme_images (folder, filename, user_id, nickname, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (parts[-2], parts[-1], str(val.get('uid', '')), val.get('nickname', 'Unknown'), val.get('time', datetime.now())))
                    inserted += max(int(getattr(cursor, "rowcount", 0) or 0), 0)
                except Exception as e:
                    logger.warning(f"[Meme DB] 迁移单条记录失败: {type(e).__name__}")
        except Exception as e:
            logger.warning(f"[Meme DB] 历史 JSON 迁移失败: {type(e).__name__}")
            return False
        finally: cursor.close()
        if inserted <= 0:
            logger.warning("[Meme DB] 历史 JSON 零行迁入，保留原文件不改名")
            return False
        IMAGE_INFO_FILE.rename(IMAGE_INFO_FILE.with_suffix(".json.bak"))
        return True


    def add_record(self, folder: str, filename: str, user_id: int, nickname: str):
        cursor = self._get_cursor()
        if not cursor: return
        try:
            now = datetime.now()
            cursor.execute('''
                INSERT INTO meme_images (folder, filename, user_id, nickname, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE user_id=%s, nickname=%s, created_at=%s
            ''', (folder, filename, str(user_id), nickname, now, str(user_id), nickname, now))
        except Exception as e:
            # 记录写失败不阻断发送，但必须可见：否则溯源与「最近上传」会永远为空。
            logger.warning(f"[Meme DB] 写入记录失败: {type(e).__name__}")
        finally: cursor.close()

    def get_record(self, filename: str) -> Optional[dict]:
        cursor = self._get_cursor()
        if not cursor: return None
        try:
            # 按完整文件名精确匹配，不用主名前缀，避免命中同前缀的其它记录。
            cursor.execute('SELECT * FROM meme_images WHERE filename = %s LIMIT 1', (filename,))
            res = cursor.fetchone()
            if res:
                return {"folder": res['folder'], "filename": res['filename'], "uid": res['user_id'], "nickname": res['nickname'], "time": str(res['created_at'])}
        finally: cursor.close()
        return None

    def delete_record(self, filename: str):
        cursor = self._get_cursor()
        if not cursor: return
        try:
            cursor.execute('DELETE FROM meme_images WHERE filename = %s', (filename,))
        finally: cursor.close()
    
    def delete_exact_record(self, folder: str, filename: str):
        cursor = self._get_cursor()
        if not cursor: return
        try:
            cursor.execute('DELETE FROM meme_images WHERE folder = %s AND filename = %s', (folder, filename))
        finally: cursor.close()

    def rename_exact_record(self, folder: str, old_filename: str, new_filename: str) -> bool:
        """把一条记录的文件名改到新名字，保留上传人与时间（供改名流程回写）。

        返回 False 表示「没有这条记录」或「库不可用/写失败」；_get_cursor 自身失败
        已记过 warning，这里再失败也记一条，避免改名与记录静默脱节。
        """
        cursor = self._get_cursor()
        if not cursor: return False
        try:
            cursor.execute(
                'UPDATE meme_images SET filename = %s WHERE folder = %s AND filename = %s',
                (new_filename, folder, old_filename),
            )
            return bool(getattr(cursor, "rowcount", 0))
        except Exception as e:
            logger.warning(f"[Meme DB] 回写记录文件名失败: {type(e).__name__}")
            return False
        finally: cursor.close()

    def is_available(self) -> bool:
        """只读探测数据库是否可用（能拿到游标即视为可用），不建表、不写任何数据。"""
        cursor = self._get_cursor()
        if not cursor: return False
        try: return True
        finally: cursor.close()

    def get_all_records(self) -> List[dict]:
        cursor = self._get_cursor()
        if not cursor: return []
        try:
            cursor.execute('SELECT folder, filename FROM meme_images')
            return cursor.fetchall()
        finally: cursor.close()

    def get_recent(self, limit=10) -> List[dict]:
        cursor = self._get_cursor()
        if not cursor: return []
        try:
            cursor.execute('SELECT folder, filename, user_id, nickname, created_at FROM meme_images ORDER BY created_at DESC LIMIT %s', (limit,))
            rows = cursor.fetchall()
            return [{"key": f"{r['folder']}/{r['filename']}", "info": {"uid": r['user_id'], "nickname": r['nickname'], "time": str(r['created_at'])}} for r in rows]
        finally: cursor.close()

db = MemeDatabase()

# ================= 4. 管理器 =================
class MemeManager:
    def __init__(self):
        self.restricted_folders: Set[str] = set()
        self.blacklist: Set[int] = set()
        self._load_config()

    def _load_config(self):
        if RESTRICTED_FILE.exists():
            try:
                with open(RESTRICTED_FILE, "r", encoding="utf-8") as f:
                    self.restricted_folders = set(json.load(f))
            except: pass
        if BLACKLIST_FILE.exists():
            try:
                with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                    self.blacklist = set(json.load(f))
            except: pass

    def _save_config(self):
        try:
            with open(RESTRICTED_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.restricted_folders), f)
            with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
                json.dump(list(self.blacklist), f)
        except: pass

    def is_admin(self, user_id: int) -> bool:
        return str(user_id) in SUPERUSERS or user_id in ADMIN_USERS

    def canonical_name(self, folder_name: str) -> str:
        """把用户输入收敛成登记到的真实分类名（大小写正确）。

        命中多个仅大小写不同的目录时直接拒绝：退回原样比较会让 ``A`` 上的锁
        对 ``a`` 不生效，即大小写绕过。
        """
        index = GALLERY.refresh()
        entry = index.find(folder_name)
        if entry is not None:
            return entry["name"]
        rivals = index.ambiguous(folder_name)
        if rivals:
            raise MemeCategoryAmbiguous(
                "分类名有歧义（存在仅大小写不同的多个分类）："
                + "、".join(sorted(e["name"] for e in rivals))
            )
        try:
            return validate_name(folder_name, max_len=MAX_CATEGORY_NAME_LEN)
        except MemePathError:
            return folder_name

    def is_locked(self, folder_name: str) -> bool:
        name = self.canonical_name(folder_name)
        return any(self.canonical_name(item) == name for item in self.restricted_folders)

    def check_permission(self, user_id: int, folder_name: str) -> bool:
        if self.is_admin(user_id): return True
        return not self.is_locked(folder_name)

    def is_group_blacklisted(self, group_id: int) -> bool:
        return group_id in self.blacklist

    def toggle_lock(self, folder_name: str) -> bool:
        # 锁与校验都基于登记真名，避免 `Test` 锁不住 `test 上传` 这类大小写绕过。
        name = self.canonical_name(folder_name)
        if any(self.canonical_name(item) == name for item in self.restricted_folders):
            self.restricted_folders = {
                item for item in self.restricted_folders
                if self.canonical_name(item) != name
            }
        else:
            self.restricted_folders.add(name)
        self._save_config()
        return self.is_locked(name)

    def toggle_blacklist(self, group_id: int) -> bool:
        if group_id in self.blacklist: self.blacklist.remove(group_id)
        else: self.blacklist.add(group_id)
        self._save_config()
        return group_id in self.blacklist

    def get_next_filename(self, folder: Path, folder_name: str, suffix: str) -> str:
        if not folder.exists(): return f"{folder_name}1{suffix}"
        max_num = 0
        pattern = re.compile(f"^{re.escape(folder_name)}(\\d+)")
        for f in folder.iterdir():
            if f.is_file():
                match = pattern.search(f.stem)
                if match:
                    if int(match.group(1)) > max_num: max_num = int(match.group(1))
        return f"{folder_name}{max_num + 1}{suffix}"

mgr = MemeManager()

# ================= 5. 图片处理 =================
def _get_font(size=20):
    for path in ["C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"]:
        if Path(path).exists(): return ImageFont.truetype(path, size)
    return ImageFont.load_default()

def _convert_to_webp_sync(img_data: bytes) -> bytes:
    try:
        img = Image.open(BytesIO(img_data))
        if getattr(img, 'is_animated', False): return img_data
        if img.mode != 'RGBA': img = img.convert('RGBA')
        out = BytesIO()
        img.save(out, format='WEBP', quality=85)
        return out.getvalue()
    except: return img_data

async def detect_and_convert(img_data: bytes) -> tuple[bytes, str]:
    if img_data[:6] in (b'GIF87a', b'GIF89a'): return img_data, ".gif"
    try:
        webp_data = await asyncio.to_thread(_convert_to_webp_sync, img_data)
        return webp_data, ".webp"
    except: return img_data, ".jpg"

def _sync_create_recent_list(recent_data: list) -> bytes:
    row_height = 120
    width = 800
    height = len(recent_data) * row_height + 80
    canvas = Image.new('RGB', (width, height), '#f9f9f9')
    draw = ImageDraw.Draw(canvas)
    title_font = _get_font(30)
    text_font = _get_font(20)
    small_font = _get_font(16)
    
    # 彻底移除了 Emoji，防止出现方块乱码
    draw.text((20, 20), "最近上传记录 (Top 10)", fill="black", font=title_font)
    
    y = 70
    for item in recent_data:
        draw.rectangle([(10, y), (width-10, y+row_height-10)], fill="white", outline="#dddddd")
        file_path = IMAGE_DIR / item['key']
        image_loaded = False
        if file_path.exists():
            try:
                img = Image.open(file_path)
                img.thumbnail((100, 100))
                canvas.paste(img, (20, y + 5))
                image_loaded = True
            except: pass
        
        if not image_loaded:
             draw.text((30, y + 40), "[图片缺失]", fill="#ff5555", font=text_font)

        draw.text((140, y + 15), f"[文件] {item['key']}", fill="black", font=text_font)
        draw.text((140, y + 45), f"[用户] {item['info']['nickname']} ({item['info']['uid']})", fill="#555555", font=text_font)
        draw.text((140, y + 75), f"[时间] {item['info']['time']}", fill="#999999", font=small_font)
        y += row_height
    buf = BytesIO()
    canvas.save(buf, format='JPEG', quality=85)
    return buf.getvalue()

def _sync_create_huge_grid(files: List[Path], folder_name: str) -> bytes:
    limit = 500
    if len(files) > limit:
        preview_files = files[:limit]
        extra_info = f" (前{limit}张)"
    else:
        preview_files = files
        extra_info = f" ({len(files)}张)"

    thumb_w, thumb_h = 200, 200
    gap = 10
    cols = 5
    rows = math.ceil(len(preview_files) / cols)
    canvas_h = rows * (thumb_h + 40 + gap) + 80
    if canvas_h > 30000:
        canvas_h = 30000
        rows = (canvas_h - 80) // (thumb_h + 40 + gap)
        preview_files = preview_files[:rows * cols]
        extra_info += " [过长截断]"

    try: canvas = Image.new('RGB', (cols*(thumb_w+gap)+gap, canvas_h), '#f0f0f0')
    except: return b""

    draw = ImageDraw.Draw(canvas)
    font = _get_font(20)
    title_font = _get_font(35)
    # 彻底移除了 Emoji，防止出现方块乱码
    draw.text((20, 20), f"[{folder_name}] 分类预览{extra_info}", fill="black", font=title_font)
    
    for i, file_path in enumerate(preview_files):
        x = (i % cols) * (thumb_w + gap) + gap
        y = (i // cols) * (thumb_h + 40 + gap) + 80
        draw.rectangle([x, y, x+thumb_w, y+thumb_h+40], fill="white", outline="#cccccc")
        try:
            img = Image.open(file_path)
            img.thumbnail((thumb_w, thumb_h))
            off_x, off_y = (thumb_w - img.width) // 2, (thumb_h - img.height) // 2
            canvas.paste(img, (x + off_x, y + 5 + off_y))
            text = file_path.stem
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            draw.text((x + (thumb_w - text_w)//2, y + thumb_h + 10), text, fill="black", font=font)
        except: 
            draw.text((x+10, y+50), "损坏", fill="red", font=font)

    buf = BytesIO()
    canvas.save(buf, format='JPEG', quality=80)
    return buf.getvalue()

# 同名抢名重试上限：并发上传时用 O_EXCL 抢号，冲突即换下一个号重试。
MEME_NAME_ATTEMPTS = 8


def _create_exclusive_file(path: Path, data: bytes) -> None:
    """以 O_EXCL 新建文件再写入；目标已存在时抛 FileExistsError，绝不覆盖。"""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except BaseException:
        # fdopen 成功后 fd 已由 handle 接管；这里只兜住 fdopen 自身失败，避免句柄泄漏。
        try: os.close(fd)
        except OSError: pass
        raise


async def write_new_image(target_dir: Path, folder_name: str, suffix: str, data: bytes) -> str:
    """取号并以 O_EXCL 原子新建：并发同名时换号重试，不会覆盖已入库图片。"""
    last_error = None
    for _ in range(MEME_NAME_ATTEMPTS):
        name = mgr.get_next_filename(target_dir, folder_name, suffix)
        try:
            await asyncio.to_thread(_create_exclusive_file, target_dir / name, data)
        except FileExistsError as e:
            last_error = e
            continue
        return name
    raise MemeError("❌ 保存失败：同名图片并发过多，请稍后再试") from last_error


async def save_image_logic(img_data: bytes, folder_name: str, target_dir: Path, user_id: int, bot: Bot, force: bool = False):
    if not target_dir.exists(): target_dir.mkdir(parents=True, exist_ok=True)
    final_data, suffix = await detect_and_convert(img_data)
    
    if not force:
        h = hashlib.md5(final_data).hexdigest()
        for f in target_dir.iterdir():
            if f.is_file():
                async with aiofiles.open(f, "rb") as fo: 
                    if hashlib.md5(await fo.read()).hexdigest() == h: return f.name, True
                    
    new_name = await write_new_image(target_dir, folder_name, suffix, final_data)
    
    try: nick = (await bot.get_stranger_info(user_id=user_id))['nickname']
    except: nick = str(user_id)
    db.add_record(folder_name, Path(new_name).name, user_id, nick)
    return Path(new_name).stem, False

# ================= 6. 自检系统 =================
def get_sorted_files(folder: Path):
    valid = {'.webp', '.gif', '.jpg', '.png', '.jpeg'}
    files = []
    try:
        children = list(folder.iterdir())
    except OSError as e:
        logger.warning(f"[Meme] 无法读取分类目录: {type(e).__name__}")
        return []
    for f in children:
        try:
            if f.is_file() and f.suffix.lower() in valid and f.stat().st_size > 0:
                files.append(f)
        except OSError as e:
            # 并发删除或坏 junction：跳过该条，不中断整个分类。
            logger.warning(f"[Meme] 跳过不可读文件 {f.name}: {type(e).__name__}")
    files.sort(key=lambda f: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', f.name)])
    return files

def ghost_records() -> List[dict]:
    """只读列出「记录存在但文件不在」的幽灵记录；不做任何删除。"""
    ghosts = []
    for rec in db.get_all_records():
        try:
            path = ensure_within(GALLERY_ROOT, IMAGE_DIR / rec['folder'] / rec['filename'])
        except (MemeError, OSError, KeyError, TypeError):
            continue
        if not path.exists():
            ghosts.append(rec)
    return ghosts

async def clean_ghost_records() -> int:
    deleted = 0
    for rec in ghost_records():
        db.delete_exact_record(rec['folder'], rec['filename'])
        deleted += 1
    if deleted: logger.info(f"[Meme Clean] 清理 {deleted} 条幽灵记录")
    return deleted

def plan_normalize(folder: Path) -> dict:
    """只读统计 normalize_folder 会做什么，不写盘、不改名、不删目录。"""
    plan = {"folder": folder.name, "convert": 0, "rename": 0, "remove_empty": False}
    try:
        if not folder.exists():
            return plan
        files = get_sorted_files(folder)
    except OSError:
        return plan
    if not files:
        plan["remove_empty"] = True
        return plan
    for f in files:
        if f.suffix.lower() not in ['.webp', '.gif']:
            plan["convert"] += 1
    plan["rename"] = len(files)
    return plan

async def normalize_folder(folder: Path, known_records: Optional[Set[tuple]] = None) -> int:
    """格式化一个分类，并把改过的文件名回写到记录里。

    返回「确认存在但无法回写」的记录数（``records_invalidated``）：改名后记录若不
    跟着改，/溯源 与 /最近上传 会指向不存在的文件，随后的 clean_ghosts 还会把它们
    当幽灵记录删掉。
    """
    if not folder.exists(): return 0
    files = get_sorted_files(folder)
    if not files:
        try: folder.rmdir(); logger.info(f"[Meme Clean] 删除空文件夹: {folder.name}")
        except: pass
        return 0
    temp_map = []
    folder_name = folder.name
    for f in files:
        origin = f.name
        if f.suffix.lower() not in ['.webp', '.gif']:
            try:
                async with aiofiles.open(f, "rb") as fo: data = await fo.read()
                if data[:6] not in (b'GIF87a', b'GIF89a'):
                    new_data = await asyncio.to_thread(_convert_to_webp_sync, data)
                    new_path = f.with_suffix(".webp")
                    async with aiofiles.open(new_path, "wb") as fo: await fo.write(new_data)
                    # 只处理这一条记录：按文件名跨分类删除会误伤其它分类的同名文件。
                    f.unlink(); f = new_path
            except: pass
        tmp_name = folder / f"temp_{uuid.uuid4()}{f.suffix}"
        f.rename(tmp_name); temp_map.append((tmp_name, origin))
    invalidated = 0
    for i, (tmp, origin) in enumerate(temp_map):
        final_name = f"{folder_name}{i+1}{tmp.suffix}"
        tmp.rename(folder / final_name)
        if origin == final_name:
            continue
        if not db.rename_exact_record(folder_name, origin, final_name):
            # 没有这条记录属正常（例如上传时库不可用）；确认存在却回写失败才算失效。
            if known_records is not None and (folder_name, origin) in known_records:
                invalidated += 1
    return invalidated

@get_driver().on_startup
async def startup_task():
    # 普通启动只做只读准备：不迁移历史 JSON、不批量转换/改名、不清理幽灵记录、
    # 不扫描 pending、不建表、不连接数据库。维护只能走 run_maintenance 显式调用。
    logger.info("[Meme] 系统就绪（未执行任何历史维护）")


async def run_maintenance(
    *,
    dry_run: bool = True,
    normalize: bool = False,
    clean_ghosts: bool = False,
    allow_json_migration: bool = False,
) -> dict:
    """显式维护入口，默认 dry_run 且不做任何破坏性操作。

    - ``dry_run=True``（默认）：只返回计划（将改名/转码的文件数、幽灵记录数、
      JSON 是否可迁入），零写盘、零删除、零改名；schema 只做只读检查。
    - ``normalize=True``：才执行图片格式转换 + 全库改名 + 删空目录。
    - ``clean_ghosts=True``：才删除幽灵记录（先由 ``ghost_records()`` 只读判定）。
    - ``allow_json_migration=True``：才迁移历史 JSON（真正插入 > 0 行才改名备份）。

    普通 import/startup 永远不调用本函数；调用方需自行取得授权并先做备份。
    """
    # 建表/表检查永远是第一步，早于任何迁移、改名与删除。
    schema_ready = bool(db.schema_ready())
    schema_created = False
    if not dry_run and not schema_ready:
        schema_created = bool(db.ensure_schema())

    index = GALLERY.refresh()
    entries = index.entries()
    plans = [plan_normalize(category_path(entry)) for entry in entries]
    # 库不可用时 get_all_records() 只会返回 []，没法和「确实没有幽灵记录」区分：
    # 因此显式给出 db_available，并把 ghost_records 置 None 而不是 0。
    db_available = bool(db.is_available())
    ghosts = ghost_records() if db_available else None
    report = {
        "dry_run": dry_run,
        "db_available": db_available,
        "schema_ready": schema_ready or schema_created,
        "plan": {
            "categories": [p["folder"] for p in plans],
            "normalize_convert": sum(p["convert"] for p in plans),
            "normalize_rename": sum(p["rename"] for p in plans),
            "remove_empty_folders": sum(1 for p in plans if p["remove_empty"]),
            "ghost_records": None if ghosts is None else len(ghosts),
            "json_migratable": IMAGE_INFO_FILE.exists(),
        },
        "applied": {
            "schema_created": schema_created,
            "migrated_json": False,
            "normalized_files": 0,
            "records_invalidated": 0,
            "ghost_deleted": 0,
        },
    }
    if not db_available:
        logger.warning("[Meme Maint] 数据库不可用：幽灵记录数与记录回写均无法判定")
    if dry_run:
        if not schema_ready:
            logger.warning("[Meme Maint] dry-run：meme_images 表尚不存在（未建表）")
        return report

    if allow_json_migration:
        report["applied"]["migrated_json"] = bool(db.migrate_json())
    if normalize:
        known_records = None
        if db_available:
            known_records = {
                (rec.get("folder"), rec.get("filename"))
                for rec in db.get_all_records()
                if isinstance(rec, dict)
            }
        for entry in entries:
            report["applied"]["records_invalidated"] += await normalize_folder(
                category_path(entry), known_records
            )
            report["applied"]["normalized_files"] += plan_and_count(entry)
        if report["applied"]["records_invalidated"]:
            logger.warning(
                f"[Meme Clean] {report['applied']['records_invalidated']} 条记录未能回写新文件名，"
                "/溯源 与 /最近上传 可能失准"
            )
    if clean_ghosts:
        report["applied"]["ghost_deleted"] = await clean_ghost_records()
    return report


def plan_and_count(entry: dict) -> int:
    """在 normalize 之后统计该分类下的文件数，供报告使用（只读）。"""
    try:
        return len(get_sorted_files(category_path(entry)))
    except (MemeError, OSError):
        return 0

# ================= 7. 业务指令 =================

# 加入了 meme帮助 的正则匹配
help_cmd = on_regex(r"^(\u8868\u60c5\u5e2e\u52a9|\u8868\u60c5\u5305\u5e2e\u52a9|memehelp|meme\u5e2e\u52a9)$", priority=5, block=True)
@help_cmd.handle()
async def _(event: Event):
    await help_cmd.finish(__plugin_meta__.usage)

list_meme = on_regex(r"^(meme\s*list|\u8868\u60c5\u5217\u8868|\u8868\u60c5\u5305\u5217\u8868|\u56fe\u5e93\u5217\u8868)$", priority=5, block=True)
@list_meme.handle()
async def _(event: Event):
    valid = []
    is_admin = mgr.is_admin(event.user_id)
    for entry in GALLERY.refresh().entries():
        try:
            if not any(category_path(entry).iterdir()):
                # 空分类只是不列出；不在读取路径上 rmdir 任何目录。
                continue
        except (MemePathError, OSError):
            continue
        name = entry["name"]
        if name in mgr.restricted_folders:
            if is_admin: valid.append(f"{name}(🔒)")
        else: valid.append(name)
    if not valid: await list_meme.finish("当前无可用表情包")
    valid.sort()
    msg = "✨ 表情列表 ✨\n"
    chunk = []
    for f in valid:
        chunk.append(f)
        if len(chunk) == 3: msg += " | ".join(chunk) + "\n"; chunk = []
    if chunk: msg += " | ".join(chunk)
    await list_meme.finish(msg.strip())

recent_cmd = on_regex(r"^\u6700\u8fd1\u4e0a\u4f20$", priority=5, block=True)
@recent_cmd.handle()
async def _(bot: Bot, event: Event):
    if not mgr.is_admin(event.user_id): return
    data = db.get_recent(10)
    if not data: await recent_cmd.finish("暂无记录")
    try:
        img = await asyncio.to_thread(_sync_create_recent_list, data)
        await recent_cmd.send(MessageSegment.image(file=img))
    except Exception as e:
        await recent_cmd.finish(f"生成报表失败: {e}")

random_cmd = on_regex(r"^(随机meme|随机表情|随机图片|随一张)$", priority=4, block=True)
@random_cmd.handle()
async def _(bot: Bot, event: Event):
    if isinstance(event, GroupMessageEvent):
        if mgr.is_group_blacklisted(event.group_id) and not mgr.is_admin(event.user_id): return
    
    allowed_files = []
    for entry in GALLERY.refresh().entries():
        if not mgr.check_permission(event.user_id, entry["name"]):
            continue
        try:
            allowed_files.extend(get_sorted_files(category_path(entry)))
        except (MemePathError, OSError):
            continue
            
    if not allowed_files:
        await random_cmd.finish("⚠️ 当前图库为空或没有权限访问任何分类。")

    if await send_candidate(random_cmd, allowed_files) is None:
        await random_cmd.finish("🚫 连续发送失败，请稍后再试。")


async def is_meme_folder(event: Event) -> bool:
    text = event.get_plaintext().strip()
    if not text: return False
    if any(x in text for x in ["upload", "上传", "删除", "查看", "list", "帮助", "看所有", "锁定", "屏蔽"]): return False
    # 只判断「登记表里是否存在这个分类」，不把用户文本拼成路径。
    return lookup_category(text) is not None

async def send_candidate(matcher, candidates: List[Path]) -> Optional[Path]:
    """发送一张候选图；任何失败都只记日志，绝不删除原图或数据库记录。"""
    fail_count = 0; max_fails = 5
    while fail_count < max_fails and candidates:
        choice = random.choice(candidates)
        try:
            async with aiofiles.open(choice, "rb") as f:
                img_bytes = await f.read()
            await matcher.send(MessageSegment.image(file=img_bytes))
            return choice
        except ActionFailed as e:
            logger.warning(f"[Meme] 发送失败 retcode={getattr(e, 'retcode', None)} file={choice.name}")
            candidates.remove(choice)
            fail_count += 1
        except Exception as e:
            logger.warning(f"[Meme] 发送异常 file={choice.name}: {type(e).__name__}")
            fail_count += 1
    return None

send_meme = on_message(rule=Rule(is_meme_folder), priority=1, block=True)
@send_meme.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    entry = lookup_category(event.get_plaintext().strip())
    if entry is None: return
    text = entry["name"]
    if isinstance(event, GroupMessageEvent):
        if mgr.is_group_blacklisted(event.group_id) and not mgr.is_admin(event.user_id): return
    if not mgr.check_permission(event.user_id, text): return
    try:
        folder = category_path(entry)
    except (MemePathError, OSError) as e:
        logger.warning(f"[Meme] 分类路径不可用 {text}: {type(e).__name__}")
        await send_meme.finish(f"⚠️ 分类【{text}】暂时不可用，请联系管理员。")
    files = get_sorted_files(folder)
    if not files:
        await send_meme.finish(f"⚠️ 分类【{text}】暂时没有可用图片。")
    if event.user_id not in USER_HISTORY: USER_HISTORY[event.user_id] = {}
    if text not in USER_HISTORY[event.user_id]: USER_HISTORY[event.user_id][text] = []
    seen = USER_HISTORY[event.user_id][text]
    available = [f for f in files if f.name not in seen]
    if not available: available = list(files); USER_HISTORY[event.user_id][text] = []
    sent = await send_candidate(send_meme, available)
    if sent is None:
        await send_meme.finish("🚫 连续失败，停止发送。")
    USER_HISTORY[event.user_id][text].append(sent.name)

view_all = on_regex(r"^\u770b\u6240\u6709\s*(\w+)$", priority=5, block=True)
@view_all.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    name = regex_group[0].strip()
    entry = lookup_category(name)
    if entry is None: await view_all.finish("分类不存在")
    if not mgr.check_permission(event.user_id, entry["name"]): await view_all.finish("权限不足或分类被锁定")
    name = entry["name"]
    try:
        folder = category_path(entry)
        files = get_sorted_files(folder)
    except (MemePathError, OSError) as e:
        logger.warning(f"[Meme] 分类路径不可用 {name}: {type(e).__name__}")
        await view_all.finish("分类暂时不可用")
    if not files: await view_all.finish("分类为空")
    await view_all.send(f"⏳ 正在生成【{name}】预览图 ({len(files)}张)...")
    img_bytes, error_msg = None, None
    try:
        img_bytes = await asyncio.to_thread(_sync_create_huge_grid, files, name)
        if not img_bytes: error_msg = "生成失败：内存不足"
    except Exception as e: error_msg = f"生成异常: {e}"
    if error_msg: await view_all.finish(error_msg)
    if img_bytes: await view_all.finish(MessageSegment.image(file=img_bytes))

# ================= 8. 投稿：只接受同条消息自带的图片 =================
# 历史入口（回复图片 / 文件或 ZIP / 链接）在这里全部收起：不调用 OneBot 取文件接口、
# 不下载文件、不解压、不起后台任务，只回一条说明「该怎么做」的拒绝文案。

_UPLOAD_HINT = "请把【<分类> 上传】和一张图片放在同一条消息里发送。"
REJECT_REPLY_IMAGE = "❌ 不支持回复图片投稿：" + _UPLOAD_HINT
REJECT_REPLY_FILE = "❌ 不支持回复文件/ZIP 投稿：" + _UPLOAD_HINT + "（不会下载或解压文件）"
REJECT_FILE_SEGMENT = "❌ 不支持文件/ZIP 投稿：" + _UPLOAD_HINT + "（不会下载或解压文件）"
REJECT_LINK = "❌ 不支持链接投稿：" + _UPLOAD_HINT
REJECT_NO_IMAGE = "❌ 没有找到图片。" + _UPLOAD_HINT


def _segments(message) -> List[MessageSegment]:
    return [seg for seg in (message or []) if getattr(seg, "type", None)]


def resolve_upload_image(event) -> str:
    """返回当前消息自带图片的 URL；其它来源一律抛 MemeError（带用户文案）。"""
    reply = getattr(event, "reply", None)
    if reply is not None:
        reply_segments = _segments(getattr(reply, "message", None))
        if any(seg.type == "file" for seg in reply_segments):
            raise MemeError(REJECT_REPLY_FILE)
        if any(seg.type == "image" for seg in reply_segments):
            raise MemeError(REJECT_REPLY_IMAGE)

    segments = _segments(getattr(event, "message", None))
    if any(seg.type == "file" for seg in segments):
        raise MemeError(REJECT_FILE_SEGMENT)
    for seg in segments:
        if seg.type != "image":
            continue
        url = str(seg.data.get("url") or seg.data.get("file") or "")
        if not url.lower().startswith(("http://", "https://")):
            raise MemeError(REJECT_LINK)
        return url
    text = getattr(event, "get_plaintext", lambda: "")() or ""
    if re.search(r"https?://", text):
        raise MemeError(REJECT_LINK)
    raise MemeError(REJECT_NO_IMAGE)


_DOWNLOAD_SLOTS = asyncio.Semaphore(MEME_DOWNLOAD_CONCURRENCY)

_BLOCKED_IP_ATTRS = ("is_private", "is_loopback", "is_link_local", "is_reserved",
                     "is_multicast", "is_unspecified")


def _assert_public_url(url: str) -> None:
    """校验单跳目标：scheme 只能是 http(s)，且解析到的每个地址都必须是公网。"""
    if not str(url).lower().startswith(("http://", "https://")):
        raise MemeError(REJECT_LINK)
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        # 非法端口（如 :99999）或控制字符会让 urlsplit/port 抛 ValueError，
        # 这里统一收敛成用户可见的拒绝，而不是落到通用失败文案。
        raise MemeError(REJECT_LINK) from None
    if not host:
        raise MemeError(REJECT_LINK)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        raise MemeError("❌ 图片地址无法解析") from None
    for info in infos:
        try:
            ip = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        except ValueError:
            raise MemeError("❌ 图片地址无法识别") from None
        if any(getattr(ip, attr) for attr in _BLOCKED_IP_ATTRS):
            raise MemeError("❌ 图片地址指向内网或保留地址，已拒绝")


def _new_http_client() -> httpx.AsyncClient:
    """工厂：测试可替换为 MockTransport，避免真实出网。"""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(MEME_DOWNLOAD_TIMEOUT),
        follow_redirects=False,
    )


async def _download_hop(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url)


async def _download_image(url: str) -> bytes:
    limit = MEME_MAX_IMAGE_BYTES
    size_hint = f"{limit // (1024 * 1024)} MiB"
    current = str(url)
    buffer = bytearray()
    async with _new_http_client() as client:
        for _hop in range(MEME_MAX_REDIRECTS + 1):
            # 每一跳都重新校验：302 指向内网时在发出第二跳之前就拒绝。
            await asyncio.to_thread(_assert_public_url, current)
            async with client.stream("GET", current) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise MemeError("❌ 图片地址重定向缺少目标")
                    current = urllib.parse.urljoin(current, location)
                    continue
                if resp.status_code != 200:
                    raise MemeError(f"❌ 图片下载失败（HTTP {resp.status_code}）")
                ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
                if ctype and not ctype.startswith("image/"):
                    raise MemeError("❌ 该链接返回的不是图片")
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > limit:
                    raise MemeError(f"❌ 图片超过 {size_hint} 预算")
                async for chunk in resp.aiter_bytes(64 * 1024):
                    buffer.extend(chunk)
                    if len(buffer) > limit:
                        raise MemeError(f"❌ 图片超过 {size_hint} 预算")
                return bytes(buffer)
    raise MemeError("❌ 图片地址重定向次数过多")


async def fetch_image_within_budget(url: str) -> bytes:
    """流式下载并施加字节/超时/并发预算；超限立即中止，不落盘。"""
    # DNS 解析放线程里，避免阻塞事件循环。
    await asyncio.to_thread(_assert_public_url, url)
    try:
        # 槽位等待与整次下载各自设总时限，滴流响应无法长期占住并发槽。
        await asyncio.wait_for(_DOWNLOAD_SLOTS.acquire(), timeout=MEME_DOWNLOAD_SLOT_WAIT)
    except asyncio.TimeoutError:
        raise MemeError("❌ 下载排队超时，请稍后再试") from None
    try:
        try:
            return await asyncio.wait_for(_download_image(url), timeout=MEME_DOWNLOAD_TOTAL_TIMEOUT)
        except asyncio.TimeoutError:
            raise MemeError("❌ 图片下载超时") from None
        except MemeError:
            raise
        except Exception as e:
            logger.warning(f"[Meme] 图片下载失败: {type(e).__name__}")
            raise MemeError("❌ 图片下载失败，请稍后再试") from None
    finally:
        _DOWNLOAD_SLOTS.release()


def _inspect_image(data: bytes) -> tuple[str, int, int]:
    """格式/像素/帧数预算校验；只读文件头，不解码整图。"""
    try:
        with Image.open(BytesIO(data)) as img:
            fmt = (img.format or "").upper()
            width, height = img.size
            frames = int(getattr(img, "n_frames", 1) or 1)
    except Exception:
        raise MemeError("❌ 无法识别的图片格式") from None
    if fmt not in MEME_ALLOWED_IMAGE_FORMATS:
        raise MemeError(f"❌ 不支持的图片格式: {fmt or '未知'}")
    if width * height > MEME_MAX_IMAGE_PIXELS:
        raise MemeError(f"❌ 图片像素数超过 {MEME_MAX_IMAGE_PIXELS} 预算")
    if frames > MEME_MAX_IMAGE_FRAMES:
        raise MemeError(f"❌ 动图帧数超过 {MEME_MAX_IMAGE_FRAMES} 预算")
    return fmt, width, height


def parse_upload_command(text: str) -> Optional[tuple[str, str]]:
    """解析 `<分类> 上传|强制上传|upload`；无空格写法同样识别为强制上传。"""
    match = re.match(r"^(.+?)\s*(强制上传|上传|upload)$", text)
    if not match:
        return None
    folder_name = match.group(1).strip()
    if not folder_name:
        return None
    return folder_name, match.group(2)


async def submit_upload(bot, event, folder_name: str, *, is_force: bool = False) -> str:
    """投稿主路径：只接受同条消息图片 → 预算下载 → 校验 → 入库。"""
    url = resolve_upload_image(event)
    data = await fetch_image_within_budget(url)
    await asyncio.to_thread(_inspect_image, data)
    entry, target_dir, _created = ensure_category(folder_name)
    name, is_dup = await save_image_logic(
        data, entry["name"], target_dir, event.user_id, bot, is_force
    )
    return "⚠️ 已存在" if is_dup else f"✅ 存入: {name}"


upload_cmd = on_message(priority=5, block=False)
@upload_cmd.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    text = event.get_plaintext().strip()
    parsed = parse_upload_command(text)
    if parsed is None: return
    matcher.stop_propagation()
    folder_name, mode = parsed
    is_force = "强制" in mode
    if folder_name.lower() in BANNED_WORDS: await upload_cmd.finish("违禁词")
    try:
        allowed = mgr.check_permission(event.user_id, folder_name)
    except MemeError as e:
        await upload_cmd.finish(str(e))
    if not allowed: await upload_cmd.finish("权限不足")

    try:
        msg = await submit_upload(bot, event, folder_name, is_force=is_force)
    except MemeError as e:
        await upload_cmd.finish(str(e))
    except Exception as e:
        logger.warning(f"[Meme] 投稿失败: {type(e).__name__}")
        await upload_cmd.finish("❌ 保存失败，请稍后再试")
    await upload_cmd.finish(msg)

lock_cmd = on_regex(r"^(\u9501\u5b9a|\u89e3\u9501)\s+(.+)$", priority=1, block=True)
@lock_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    name = regex_group[1].strip()
    try:
        is_locked = mgr.toggle_lock(name)
        shown = mgr.canonical_name(name)
    except MemeError as e:
        # 仅大小写不同的多个分类并存时不给猜测结果，直接回明确的歧义原因。
        await lock_cmd.finish(str(e))
    await lock_cmd.finish(f"{'🔒 已锁定' if is_locked else '🔓 已解锁'}: {shown}")

ban_cmd = on_regex(r"^(\u5c4f\u853d\u7fa4|\u89e3\u9664\u5c4f\u853d)\s+(\d+)$", priority=1, block=True)
@ban_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    gid = int(regex_group[1])
    is_banned = mgr.toggle_blacklist(gid)
    await ban_cmd.finish(f"{'🚫 已屏蔽群' if is_banned else '✅ 已恢复群'}: {gid}")

async def delete_image(query: str) -> str:
    """管理员显式删除：只删登记到的那一个文件与它的精确记录，不做任何批量改名。"""
    target = None
    for entry in GALLERY.refresh().entries():
        try: target = find_image(category_path(entry), query)
        except (MemePathError, OSError): target = None
        if target: break
    if not target: return "文件不存在"
    try:
        target.unlink()
    except OSError as e:
        logger.warning(f"[Meme] 删除失败 {target.name}: {type(e).__name__}")
        return f"删除失败: {type(e).__name__}"
    # 精确删除这一条记录；不按主名前缀批量删，也不顺带重命名同分类的其它图片。
    db.delete_exact_record(target.parent.name, target.name)
    return f"🗑️ 已删除 {target.name}"

del_cmd = on_regex(r"^\u5220\u9664\s+(.+)$", priority=5, block=True)
@del_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    await del_cmd.finish(await delete_image(regex_group[0].strip()))

info_cmd = on_regex(r"^\u6eaf\u6e90\s+(.+)$", priority=5, block=True)
@info_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    fname = regex_group[0].strip()
    target = None
    for entry in GALLERY.refresh().entries():
        try: target = find_image(category_path(entry), fname)
        except (MemePathError, OSError): target = None
        if target: break
    if not target: await info_cmd.finish("无记录")
    info = db.get_record(target.name)
    if info: await info_cmd.finish(f"📄 {info['filename']}\n👤 {info['nickname']} ({info['uid']})\n⏰ {info['time']}")
    else: await info_cmd.finish("无记录")

view_single = on_regex(r"^\u67e5\u770b\s+([a-zA-Z0-9_\u4e00-\u9fa5\.]+)$", priority=5, block=True)
@view_single.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    fname = regex_group[0].strip()
    target = None
    for entry in GALLERY.refresh().entries():
        try: target = find_image(category_path(entry), fname)
        except (MemePathError, OSError): target = None
        if target: break
    if target:
        if not mgr.check_permission(event.user_id, target.parent.name): return
        async with aiofiles.open(target, "rb") as f: await view_single.finish(MessageSegment.image(file=await f.read()))
    else: await view_single.finish("未找到")
