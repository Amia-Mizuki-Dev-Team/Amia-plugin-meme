import asyncio
import hashlib
import json
import math
import random
import re
import uuid
import zipfile
import os
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
        "• <文件夹名> 上传（配图或回复 ZIP）"
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
                "brief_des": "向指定分类上传图片或 ZIP",
                "detail_des": "请配图或回复 ZIP；重复图片会被检测。",
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

PLUGIN_DIR = Path(__file__).parent.resolve()
IMAGE_DIR = PLUGIN_DIR / "meme"
PENDING_DIR = PLUGIN_DIR / "pending"
RESTRICTED_FILE = PLUGIN_DIR / "restricted.json"
BLACKLIST_FILE = PLUGIN_DIR / "blacklist.json"
IMAGE_INFO_FILE = PLUGIN_DIR / "image_info.json"

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
PENDING_DIR.mkdir(parents=True, exist_ok=True)

USER_HISTORY = {}

# ================= 3. 数据库管理器 =================
class MemeDatabase:
    def __init__(self):
        self.conn = None
        self.connect()
        self._init_db()

    def connect(self):
        if mysql is None:
            logger.warning("[Meme DB] mysql.connector is not installed; database disabled.")
            return
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
        except Exception as e:
            logger.error(f"[Meme DB] 连接失败: {e}")

    def _get_cursor(self):
        if not self.conn or not self.conn.is_connected():
            self.connect()
        if self.conn and self.conn.is_connected():
            return self.conn.cursor(dictionary=True)
        return None

    def _init_db(self):
        cursor = self._get_cursor()
        if not cursor: return
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
        except: pass
        finally: cursor.close()

    def migrate_json(self):
        if not IMAGE_INFO_FILE.exists(): return
        cursor = self._get_cursor()
        if not cursor: return
        try:
            with open(IMAGE_INFO_FILE, "r", encoding="utf-8") as f:
                old_data = json.load(f)
            for key, val in old_data.items():
                parts = key.replace("\\", "/").split("/")
                if len(parts) >= 2:
                    try:
                        cursor.execute('''
                            INSERT IGNORE INTO meme_images (folder, filename, user_id, nickname, created_at)
                            VALUES (%s, %s, %s, %s, %s)
                        ''', (parts[-2], parts[-1], str(val.get('uid', '')), val.get('nickname', 'Unknown'), val.get('time', datetime.now())))
                    except: pass
            IMAGE_INFO_FILE.rename(IMAGE_INFO_FILE.with_suffix(".json.bak"))
        except: pass
        finally: cursor.close()

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
        except: pass
        finally: cursor.close()

    def get_record(self, filename: str) -> Optional[dict]:
        cursor = self._get_cursor()
        if not cursor: return None
        try:
            name_stem = Path(filename).stem
            cursor.execute('SELECT * FROM meme_images WHERE filename LIKE %s LIMIT 1', (f"{name_stem}%",))
            res = cursor.fetchone()
            if res:
                return {"folder": res['folder'], "filename": res['filename'], "uid": res['user_id'], "nickname": res['nickname'], "time": str(res['created_at'])}
        finally: cursor.close()
        return None

    def delete_record(self, filename: str):
        cursor = self._get_cursor()
        if not cursor: return
        try:
            name_stem = Path(filename).stem
            cursor.execute('DELETE FROM meme_images WHERE filename LIKE %s', (f"{name_stem}%",))
        finally: cursor.close()
    
    def delete_exact_record(self, folder: str, filename: str):
        cursor = self._get_cursor()
        if not cursor: return
        try:
            cursor.execute('DELETE FROM meme_images WHERE folder = %s AND filename = %s', (folder, filename))
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

    def check_permission(self, user_id: int, folder_name: str) -> bool:
        if self.is_admin(user_id): return True
        if folder_name in self.restricted_folders: return False
        return True

    def is_group_blacklisted(self, group_id: int) -> bool:
        return group_id in self.blacklist

    def toggle_lock(self, folder_name: str) -> bool:
        if folder_name in self.restricted_folders: self.restricted_folders.remove(folder_name)
        else: self.restricted_folders.add(folder_name)
        self._save_config()
        return folder_name in self.restricted_folders

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
        target_files = files[:limit]
        extra_info = f" (前{limit}张)"
    else:
        target_files = files
        extra_info = f" ({len(files)}张)"

    thumb_w, thumb_h = 200, 200
    gap = 10
    cols = 5
    rows = math.ceil(len(target_files) / cols)
    canvas_h = rows * (thumb_h + 40 + gap) + 80
    if canvas_h > 30000:
        canvas_h = 30000
        rows = (canvas_h - 80) // (thumb_h + 40 + gap)
        target_files = target_files[:rows * cols]
        extra_info += " [过长截断]"

    try: canvas = Image.new('RGB', (cols*(thumb_w+gap)+gap, canvas_h), '#f0f0f0')
    except: return b""

    draw = ImageDraw.Draw(canvas)
    font = _get_font(20)
    title_font = _get_font(35)
    # 彻底移除了 Emoji，防止出现方块乱码
    draw.text((20, 20), f"[{folder_name}] 分类预览{extra_info}", fill="black", font=title_font)
    
    for i, file_path in enumerate(target_files):
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

async def save_image_logic(img_data: bytes, folder_name: str, target_dir: Path, user_id: int, bot: Bot, force: bool = False):
    if not target_dir.exists(): target_dir.mkdir(parents=True, exist_ok=True)
    final_data, suffix = await detect_and_convert(img_data)
    
    if not force:
        h = hashlib.md5(final_data).hexdigest()
        for f in target_dir.iterdir():
            if f.is_file():
                async with aiofiles.open(f, "rb") as fo: 
                    if hashlib.md5(await fo.read()).hexdigest() == h: return f.name, True
                    
    new_name = mgr.get_next_filename(target_dir, folder_name, suffix)
    async with aiofiles.open(target_dir / new_name, "wb") as f: await f.write(final_data)
    
    try: nick = (await bot.get_stranger_info(user_id=user_id))['nickname']
    except: nick = str(user_id)
    db.add_record(folder_name, Path(new_name).name, user_id, nick)
    return Path(new_name).stem, False

# ================= 6. 自检系统 =================
def get_sorted_files(folder: Path):
    valid = {'.webp', '.gif', '.jpg', '.png', '.jpeg'}
    files = [f for f in folder.iterdir() if f.is_file() and f.suffix.lower() in valid and f.stat().st_size > 0]
    files.sort(key=lambda f: [int(c) if c.isdigit() else c for c in re.split(r'(\d+)', f.name)])
    return files

async def clean_ghost_records():
    records = db.get_all_records()
    deleted = 0
    for rec in records:
        f_path = IMAGE_DIR / rec['folder'] / rec['filename']
        if not f_path.exists():
            db.delete_exact_record(rec['folder'], rec['filename'])
            deleted += 1
    if deleted: logger.info(f"[Meme Clean] 清理 {deleted} 条幽灵记录")

async def normalize_folder(folder: Path):
    if not folder.exists(): return
    files = get_sorted_files(folder)
    if not files:
        try: folder.rmdir(); logger.info(f"[Meme Clean] 删除空文件夹: {folder.name}")
        except: pass
        return
    temp_map = []
    folder_name = folder.name
    for f in files:
        if f.suffix.lower() not in ['.webp', '.gif']:
            try:
                async with aiofiles.open(f, "rb") as fo: data = await fo.read()
                if data[:6] not in (b'GIF87a', b'GIF89a'):
                    new_data = await asyncio.to_thread(_convert_to_webp_sync, data)
                    new_path = f.with_suffix(".webp")
                    async with aiofiles.open(new_path, "wb") as fo: await fo.write(new_data)
                    f.unlink(); db.delete_record(f.name); f = new_path
            except: pass
        tmp_name = folder / f"temp_{uuid.uuid4()}{f.suffix}"
        f.rename(tmp_name); temp_map.append(tmp_name)
    for i, tmp in enumerate(temp_map):
        final_name = folder / f"{folder_name}{i+1}{tmp.suffix}"
        tmp.rename(final_name)

@get_driver().on_startup
async def startup_task():
    db.migrate_json()
    tasks = [normalize_folder(f) for f in IMAGE_DIR.iterdir() if f.is_dir()]
    await asyncio.gather(*tasks)
    await clean_ghost_records()
    logger.info("[Meme] 系统就绪")

# ================= 7. 业务指令 =================

# 加入了 meme帮助 的正则匹配
help_cmd = on_regex(r"^(\u8868\u60c5\u5e2e\u52a9|\u8868\u60c5\u5305\u5e2e\u52a9|memehelp|meme\u5e2e\u52a9)$", priority=5, block=True)
@help_cmd.handle()
async def _(event: Event):
    await help_cmd.finish(__plugin_meta__.usage)

list_meme = on_regex(r"^(meme\s*list|\u8868\u60c5\u5217\u8868|\u8868\u60c5\u5305\u5217\u8868|\u56fe\u5e93\u5217\u8868)$", priority=5, block=True)
@list_meme.handle()
async def _(event: Event):
    folders = [d.name for d in IMAGE_DIR.iterdir() if d.is_dir()]
    valid = []
    is_admin = mgr.is_admin(event.user_id)
    for f in folders:
        if not any((IMAGE_DIR/f).iterdir()): 
            try: (IMAGE_DIR/f).rmdir()
            except: pass
            continue
        if f in mgr.restricted_folders:
            if is_admin: valid.append(f"{f}(🔒)")
        else: valid.append(f)
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
    for folder in IMAGE_DIR.iterdir():
        if folder.is_dir() and mgr.check_permission(event.user_id, folder.name):
            allowed_files.extend(get_sorted_files(folder))
            
    if not allowed_files:
        await random_cmd.finish("⚠️ 当前图库为空或没有权限访问任何分类。")
        
    fail_count = 0; max_fails = 5
    while fail_count < max_fails and allowed_files:
        choice = random.choice(allowed_files)
        try:
            async with aiofiles.open(choice, "rb") as f:
                img_bytes = await f.read()
            await random_cmd.send(MessageSegment.image(file=img_bytes))
            return
        except ActionFailed as e:
            if e.retcode in [34002, 1200, 89000, 10000]:
                try: 
                    choice.unlink()
                    db.delete_record(choice.name)
                    allowed_files.remove(choice)
                except: pass
            fail_count += 1
        except Exception: 
            fail_count += 1
            
    await random_cmd.finish("🚫 连续发送失败，请稍后再试。")

async def is_meme_folder(event: Event) -> bool:
    text = event.get_plaintext().strip()
    if not text: return False
    if any(x in text for x in ["upload", "上传", "删除", "查看", "list", "帮助", "看所有", "锁定", "屏蔽"]): return False
    return (IMAGE_DIR / text).is_dir()

send_meme = on_message(rule=Rule(is_meme_folder), priority=1, block=True)
@send_meme.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    text = event.get_plaintext().strip()
    if isinstance(event, GroupMessageEvent):
        if mgr.is_group_blacklisted(event.group_id) and not mgr.is_admin(event.user_id): return
    if not mgr.check_permission(event.user_id, text): return
    folder = IMAGE_DIR / text
    files = get_sorted_files(folder)
    if not files:
        try: folder.rmdir()
        except: pass
        await send_meme.finish(f"⚠️ 分类【{text}】为空，已清理。")
    if event.user_id not in USER_HISTORY: USER_HISTORY[event.user_id] = {}
    if text not in USER_HISTORY[event.user_id]: USER_HISTORY[event.user_id][text] = []
    seen = USER_HISTORY[event.user_id][text]
    available = [f for f in files if f.name not in seen]
    if not available: available = files; USER_HISTORY[event.user_id][text] = []
    fail_count = 0; max_fails = 5
    while fail_count < max_fails and available:
        choice = random.choice(available)
        try:
            async with aiofiles.open(choice, "rb") as f:
                img_bytes = await f.read()
            await send_meme.send(MessageSegment.image(file=img_bytes))
            USER_HISTORY[event.user_id][text].append(choice.name)
            return
        except ActionFailed as e:
            if e.retcode in [34002, 1200, 89000, 10000]:
                try: choice.unlink(); db.delete_record(choice.name); available.remove(choice)
                except: pass
            fail_count += 1
        except Exception: fail_count += 1
    await send_meme.finish("🚫 连续失败，停止发送。")

view_all = on_regex(r"^\u770b\u6240\u6709\s*(\w+)$", priority=5, block=True)
@view_all.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    name = regex_group[0].strip()
    folder = IMAGE_DIR / name
    if not folder.exists(): await view_all.finish("分类不存在")
    if not mgr.check_permission(event.user_id, name): await view_all.finish("权限不足或分类被锁定")
    files = get_sorted_files(folder)
    if not files: await view_all.finish("分类为空")
    await view_all.send(f"⏳ 正在生成【{name}】预览图 ({len(files)}张)...")
    img_bytes, error_msg = None, None
    try:
        img_bytes = await asyncio.to_thread(_sync_create_huge_grid, files, name)
        if not img_bytes: error_msg = "生成失败：内存不足"
    except Exception as e: error_msg = f"生成异常: {e}"
    if error_msg: await view_all.finish(error_msg)
    if img_bytes: await view_all.finish(MessageSegment.image(file=img_bytes))

async def download_file(url: str) -> bytes:
    async with httpx.AsyncClient() as c:
        resp = await c.get(url, timeout=60.0)
        return resp.content

upload_cmd = on_message(priority=5, block=False)
@upload_cmd.handle()
async def _(bot: Bot, event: Event, matcher: Matcher):
    text = event.get_plaintext().strip()
    match = re.match(r"^(\w+[\u4e00-\u9fa5]*)\s*(upload|\u4e0a\u4f20|\u5f3a\u5236\u4e0a\u4f20)$", text)
    if not match: return
    matcher.stop_propagation()
    folder_name, mode = match.groups()
    is_force = "强制" in mode
    if folder_name.lower() in BANNED_WORDS: await upload_cmd.finish("违禁词")
    if not mgr.check_permission(event.user_id, folder_name): await upload_cmd.finish("权限不足")

    if event.reply and event.reply.message:
        for seg in event.reply.message:
            if seg.type == "file":
                fname = str(seg.data.get("name", "")).lower()
                if fname.endswith(".zip") and (fid := seg.data.get("file_id") or seg.data.get("id")):
                    await upload_cmd.send("📦 ZIP后台导入中...")
                    f_info = await bot.get_file(file_id=fid)
                    url = f_info.get("url") or f_info.get("download_url")
                    asyncio.create_task(process_zip(bot, url, folder_name, event.user_id, event))
                    return
    url = None
    if event.reply:
        for s in event.reply.message: 
            if s.type == "image": url = s.data.get("url"); break
    else:
        for s in event.message:
            if s.type == "image": url = s.data.get("url"); break
    if not url: await upload_cmd.finish("请配图或回复ZIP")

    msg_to_send = ""
    try:
        data = await download_file(url)
        path = IMAGE_DIR / folder_name
        name, is_dup = await save_image_logic(data, folder_name, path, event.user_id, bot, is_force)
        msg_to_send = "⚠️ 已存在" if is_dup else f"✅ 存入: {name}"
    except Exception as e: msg_to_send = f"失败: {e}"
    await upload_cmd.finish(msg_to_send)

async def process_zip(bot, url, folder, uid, event):
    try:
        data = await download_file(url)
        temp = PENDING_DIR / f"{uuid.uuid4()}.zip"
        async with aiofiles.open(temp, "wb") as f: await f.write(data)
        count = 0
        path = IMAGE_DIR / folder
        with zipfile.ZipFile(temp, 'r') as z:
            for info in z.infolist():
                if info.is_dir() or info.filename.startswith('.'): continue
                if Path(info.filename).suffix.lower() not in ['.jpg','.png','.gif','.webp']: continue
                await save_image_logic(z.read(info), folder, path, uid, bot)
                count += 1
        msg = f"✅ ZIP完成: {folder} (+{count}张)"
        if isinstance(event, GroupMessageEvent): await bot.send_group_msg(group_id=event.group_id, message=msg)
        else: await bot.send_private_msg(user_id=event.user_id, message=msg)
        temp.unlink()
    except Exception as e: logger.error(f"ZIP: {e}")

lock_cmd = on_regex(r"^(\u9501\u5b9a|\u89e3\u9501)\s+(.+)$", priority=1, block=True)
@lock_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    name = regex_group[1].strip()
    is_locked = mgr.toggle_lock(name)
    await lock_cmd.finish(f"{'🔒 已锁定' if is_locked else '🔓 已解锁'}: {name}")

ban_cmd = on_regex(r"^(\u5c4f\u853d\u7fa4|\u89e3\u9664\u5c4f\u853d)\s+(\d+)$", priority=1, block=True)
@ban_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    gid = int(regex_group[1])
    is_banned = mgr.toggle_blacklist(gid)
    await ban_cmd.finish(f"{'🚫 已屏蔽群' if is_banned else '✅ 已恢复群'}: {gid}")

del_cmd = on_regex(r"^\u5220\u9664\s+(.+)$", priority=5, block=True)
@del_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    fname = regex_group[0].strip()
    target = None
    for d in IMAGE_DIR.iterdir():
        for f in d.iterdir():
            if f.stem == fname or f.name == fname: target = f; break
        if target: break
    if target:
        target.unlink()
        db.delete_record(target.name)
        asyncio.create_task(normalize_folder(target.parent))
        await del_cmd.finish(f"🗑️ 已删除 {target.name}")
    else: await del_cmd.finish("文件不存在")

info_cmd = on_regex(r"^\u6eaf\u6e90\s+(.+)$", priority=5, block=True)
@info_cmd.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    if not mgr.is_admin(event.user_id): return
    fname = regex_group[0].strip()
    info = db.get_record(fname)
    if not info:
         for ext in ['.webp', '.gif']:
             if (i := db.get_record(f"{fname}{ext}")): info = i; break
    if info: await info_cmd.finish(f"📄 {info['filename']}\n👤 {info['nickname']} ({info['uid']})\n⏰ {info['time']}")
    else: await info_cmd.finish("无记录")

view_single = on_regex(r"^\u67e5\u770b\s+([a-zA-Z0-9_\u4e00-\u9fa5\.]+)$", priority=5, block=True)
@view_single.handle()
async def _(event: Event, regex_group: tuple = RegexGroup()):
    fname = regex_group[0].strip()
    target = None
    for d in IMAGE_DIR.iterdir():
        if (d/fname).exists(): target = d/fname; break
        for ext in ['.webp', '.gif', '.jpg']:
             if (d/f"{fname}{ext}").exists(): target = d/f"{fname}{ext}"; break
        if target: break
    if target:
        if not mgr.check_permission(event.user_id, target.parent.name): return
        async with aiofiles.open(target, "rb") as f: await view_single.finish(MessageSegment.image(file=await f.read()))
    else: await view_single.finish("未找到")
