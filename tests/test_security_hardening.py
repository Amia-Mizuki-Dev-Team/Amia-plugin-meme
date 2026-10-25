"""meme 插件独立安全收口用例（复审后修订版）。

覆盖：ZIP/取文件入口行为性拒绝、只接受同条消息图片、路径/命名边界、
启动不维护、维护默认 dry-run 与显式开关、schema/迁移、SSRF 逐跳校验、
字节/格式/超时/并发预算、大小写登记、分类锁、发送失败不删图、显式删除。

全部用例在临时目录里装载插件副本；网络一律走 httpx.MockTransport，
不连接真实 MySQL、真实图库，也不出网。
"""

import asyncio
import hashlib
import importlib.util
import inspect
import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.exception import ActionFailed
from PIL import Image


PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"
PLUGIN_SOURCE = PLUGIN_FILE.read_text(encoding="utf-8")

_CACHE = {}
_HOME = TemporaryDirectory()
_PUBLIC_IP_URL = "http://93.184.216.34/a.png"


def _ensure_nonebot_initialized():
    import nonebot

    try:
        nonebot.get_driver()
    except Exception:  # noqa: BLE001 - 与既有 smoke 用例同一引导方式
        nonebot.init(_env_file=None)


def _load_plugin(root: Path, module_name: str):
    """把 __init__.py 复制进 root 后装载，使 PLUGIN_DIR 指向该临时目录。"""
    _ensure_nonebot_initialized()
    target = root / "__init__.py"
    target.write_text(PLUGIN_SOURCE, encoding="utf-8")
    module = _CACHE.get(module_name)
    if module is not None:
        return module
    with patch.dict(sys.modules, {"mysql": None, "mysql.connector": None}):
        spec = importlib.util.spec_from_file_location(module_name, target)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    _CACHE[module_name] = module
    return module


def _shared_module():
    return _load_plugin(Path(_HOME.name), "meme_hardening_shared")


def _png_bytes(size=(8, 8)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, "red").save(buf, format="PNG")
    return buf.getvalue()


def _snapshot(root: Path) -> dict:
    """相对路径 → sha256；只看文件内容，不看 atime 等读取副作用。"""
    snap = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if "__pycache__" in rel or rel == "__init__.py":
            continue
        snap[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snap


def _build_legacy_fixture(root: Path) -> Path:
    """旧图库 + 待转换后缀图片 + 空分类 + 旧 pending + 旧 JSON 迁移源。"""
    gallery = root / "meme"
    demo = gallery / "demo"
    demo.mkdir(parents=True)
    Image.new("RGB", (8, 8), "red").save(demo / "demo1.png", format="PNG")
    Image.new("RGB", (8, 8), "blue").save(demo / "Untitled.jpg", format="JPEG")
    (gallery / "empty").mkdir()
    pending = root / "pending"
    pending.mkdir()
    (pending / "legacy.zip").write_bytes(b"PK\x03\x04old-archive")
    (root / "image_info.json").write_text(
        json.dumps({"demo/demo1.png": {"uid": 1, "nickname": "old"}}),
        encoding="utf-8",
    )
    return gallery


def _event(message=(), reply=None, text="", user_id=10001):
    return SimpleNamespace(
        message=list(message),
        reply=reply,
        user_id=user_id,
        get_plaintext=lambda: text,
    )


def _reply(message):
    return SimpleNamespace(message=list(message))


class _FakeBot:
    """替身 Bot：只回答昵称；任何 OneBot 取文件类接口被调用即算用例失败。"""

    def __init__(self):
        self.api_calls = []

    async def get_stranger_info(self, user_id):
        return {"nickname": "tester"}

    async def _forbidden(self, name, **_kwargs):
        self.api_calls.append(name)
        raise AssertionError(f"拒绝路径不得调用 OneBot 接口 {name}")

    async def get_file(self, **kwargs):
        await self._forbidden("get_file", **kwargs)

    async def get_image(self, **kwargs):
        await self._forbidden("get_image", **kwargs)

    async def call_api(self, api, **kwargs):
        await self._forbidden(f"call_api:{api}", **kwargs)


class _FakeDb:
    """替身数据库：只记录调用，绝不连接真实 MySQL。"""

    def __init__(self, records=None, schema_ready=False, migrate_ok=False):
        self.records = list(records or [])
        self.calls = []
        self._schema_ready = schema_ready
        self._migrate_ok = migrate_ok

    def get_all_records(self):
        self.calls.append("get_all")
        return list(self.records)

    def delete_exact_record(self, folder, filename):
        self.calls.append(("exact", folder, filename))

    def delete_record(self, filename):
        self.calls.append(("delete", filename))

    def rename_exact_record(self, folder, filename, new_filename):
        self.calls.append(("rename", folder, filename, new_filename))
        for rec in self.records:
            if rec.get("folder") == folder and rec.get("filename") == filename:
                rec["filename"] = new_filename
                return True
        return False

    def is_available(self):
        self.calls.append("is_available")
        return True

    def add_record(self, folder, filename, user_id, nickname):
        self.calls.append(("add", folder, filename))

    def schema_ready(self):
        self.calls.append("schema_ready")
        return self._schema_ready

    def ensure_schema(self):
        self.calls.append("ensure_schema")
        self._schema_ready = True
        return True

    def migrate_json(self):
        self.calls.append("migrate")
        return self._migrate_ok


class _FakeCursor:
    def __init__(self, rowcount=0, one=None):
        self.rowcount = rowcount
        self.one = one
        self.executed = []
        self.closed = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return []

    def close(self):
        self.closed = True


def _mock_factory(handler):
    """返回可注入的 httpx client 工厂（不触碰真实网络）。"""
    transport = httpx.MockTransport(handler)

    def factory():
        return httpx.AsyncClient(transport=transport, follow_redirects=False)

    return factory


class ZipEntryRemovedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_zip_helpers_and_matchers_are_gone(self):
        for attr in ("process_zip", "download_file", "PENDING_DIR"):
            self.assertFalse(hasattr(self.module, attr), f"{attr} 应已删除")

    def test_reply_zip_is_rejected_without_any_network_call(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(200, content=b"")

        event = _event(
            message=[],
            reply=_reply([MessageSegment("file", {"name": "a.zip", "file_id": "9"})]),
            text="demo 上传",
        )
        bot = _FakeBot()
        with patch.object(self.module, "_new_http_client", _mock_factory(handler)):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.submit_upload(bot, event, "demo"))
        self.assertIn("不支持回复文件", str(ctx.exception))
        self.assertEqual(calls, [], "拒绝路径不得发起任何网络请求")
        self.assertEqual(bot.api_calls, [], "拒绝路径不得调用 OneBot 取文件接口")


class UploadSourceGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def _reject(self, event):
        with self.assertRaises(self.module.MemeError) as ctx:
            self.module.resolve_upload_image(event)
        return str(ctx.exception)

    def test_same_message_image_is_accepted(self):
        url = "https://example.invalid/a.png"
        event = _event(message=[MessageSegment.image(url)], text="demo 上传")
        self.assertEqual(self.module.resolve_upload_image(event), url)

    def test_reply_image_is_rejected_with_instructions(self):
        event = _event(
            message=[], reply=_reply([MessageSegment.image("https://example.invalid/b.png")])
        )
        msg = self._reject(event)
        self.assertIn("不支持回复图片", msg)
        self.assertIn("同一条消息", msg)

    def test_reply_file_zip_is_rejected(self):
        event = _event(
            message=[],
            reply=_reply([MessageSegment("file", {"name": "a.zip", "file_id": "9"})]),
        )
        msg = self._reject(event)
        self.assertIn("不支持回复文件", msg)
        self.assertIn("不会下载或解压", msg)

    def test_file_segment_in_same_message_is_rejected(self):
        event = _event(
            message=[MessageSegment("file", {"name": "a.zip"})], text="demo 上传"
        )
        self.assertIn("不支持文件", self._reject(event))

    def test_link_submission_is_rejected(self):
        event = _event(text="demo 上传 https://example.invalid/a.png")
        self.assertIn("不支持链接", self._reject(event))

    def test_every_legacy_entry_is_refused_without_network_or_onebot_api(self):
        """行为断言：三种历史入口都在本地拒绝，零网络、零 OneBot 取文件调用。"""
        http_calls = []

        def handler(request):
            http_calls.append(str(request.url))
            return httpx.Response(200, content=b"")

        cases = {
            "回复图片": _event(
                message=[],
                reply=_reply([MessageSegment.image(_PUBLIC_IP_URL)]),
            ),
            "回复文件": _event(
                message=[],
                reply=_reply(
                    [MessageSegment("file", {"name": "a.zip", "file_id": "9"})]
                ),
            ),
            "同条文件": _event(
                message=[MessageSegment("file", {"name": "a.zip"})], text="demo 上传"
            ),
            "链接正文": _event(text="demo 上传 https://example.invalid/a.png"),
        }
        for label, event in cases.items():
            bot = _FakeBot()
            with patch.object(self.module, "_new_http_client", _mock_factory(handler)):
                with self.assertRaises(self.module.MemeError, msg=label):
                    asyncio.run(self.module.submit_upload(bot, event, "demo"))
            self.assertEqual(bot.api_calls, [], f"{label} 不得调用 OneBot 接口")
        self.assertEqual(http_calls, [], "拒绝路径一律不得发起网络请求")

    def test_reply_to_plain_text_still_needs_same_message_image(self):
        event = _event(
            message=[], reply=_reply([MessageSegment.text("hi")]), text="demo 上传"
        )
        self.assertIn("同一条消息", self._reject(event))

    def test_same_message_image_is_stored_end_to_end(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_submit_ok")
            png = _png_bytes()
            handler = lambda request: httpx.Response(  # noqa: E731
                200, headers={"content-type": "image/png"}, content=png
            )
            db = _FakeDb()
            event = _event(
                message=[MessageSegment.image(_PUBLIC_IP_URL)], text="demo 上传"
            )
            with patch.object(
                module, "_new_http_client", _mock_factory(handler)
            ), patch.object(module, "db", db):
                msg = asyncio.run(module.submit_upload(_FakeBot(), event, "demo"))
            self.assertIn("存入", msg)
            stored = list((module.IMAGE_DIR / "demo").iterdir())
            self.assertEqual(len(stored), 1)
            self.assertEqual(db.calls[0][0], "add")


class UploadCommandParseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_force_upload_without_space_is_parsed(self):
        parse = self.module.parse_upload_command
        self.assertEqual(parse("猫猫强制上传"), ("猫猫", "强制上传"))
        self.assertEqual(parse("猫猫 强制上传"), ("猫猫", "强制上传"))
        self.assertEqual(parse("猫猫上传"), ("猫猫", "上传"))
        self.assertEqual(parse("cat upload"), ("cat", "upload"))
        self.assertIsNone(parse("猫猫"))
        self.assertIsNone(parse("上传"))


class PathBoundaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_reserved_names_trailing_dot_and_separators_are_refused(self):
        module = self.module
        for bad in (
            "..", ".", "demo.", "demo ", " demo", "CON", "con.txt", "LPT9",
            "C:/windows", "C:\\windows", "a:b", "a/b", "a\\b", "x\x00y", "", "a" * 40,
        ):
            with self.assertRaises(module.MemePathError, msg=f"{bad!r} 应被拒绝"):
                module.validate_name(bad, max_len=32)
        for good in ("demo", "猫猫", "a b", "demo_v2", "a-1"):
            self.assertEqual(module.validate_name(good, max_len=32), good)

    def test_escape_probes_are_refused_and_nothing_is_written(self):
        module = self.module
        base = Path(_HOME.name)
        gallery = module.IMAGE_DIR.resolve()
        demo = gallery / "demo"
        demo.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), "red").save(demo / "demo1.png")
        outside = base / "escape_case_outside"
        outside.mkdir(exist_ok=True)
        (outside / "secret.png").write_bytes(b"secret")
        before = _snapshot(base)

        index = module.GalleryIndex(gallery).refresh()
        for probe in (
            "..", "../outside", "..\\outside", "demo/../../outside",
            "C:\\Windows", "demo.", "CON",
        ):
            self.assertIsNone(index.find(probe))

        entry = index.find("demo")
        self.assertIsNotNone(entry)
        self.assertEqual(module.ensure_within(gallery, entry["path"]), demo)
        with self.assertRaises(module.MemePathError):
            module.ensure_within(gallery, gallery / ".." / "escape_case_outside")

        for probe in (
            "../outside/secret.png", "..\\outside\\secret.png", "..",
            "C:/Windows/win.ini",
        ):
            self.assertIsNone(module.find_image(demo, probe))
        self.assertEqual(module.find_image(demo, "demo1").name, "demo1.png")
        self.assertEqual(_snapshot(base), before, "拒绝路径不应产生任何写盘")


def _stub_index(module, names):
    """构造只含给定目录名的登记表：Windows 文件系统无法真实并存 A/a。"""
    children = [SimpleNamespace(name=n, is_dir=lambda: True) for n in names]
    index = module.GalleryIndex.__new__(module.GalleryIndex)
    index.root = SimpleNamespace(iterdir=lambda: list(children))
    index._by_name = {}
    index._by_folded = {}
    return index.refresh()


class CategoryCaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_case_only_duplicates_are_both_registered(self):
        index = _stub_index(self.module, ["A", "B", "a"])
        names = sorted(entry["name"] for entry in index.entries())
        self.assertEqual(names, ["A", "B", "a"], "仅大小写不同的两个目录必须都登记")
        self.assertEqual(index.find("A")["name"], "A")
        self.assertEqual(index.find("a")["name"], "a")
        self.assertEqual(index.find("b")["name"], "B")
        self.assertEqual(len(index.ambiguous("aa")), 0)

    def test_ambiguous_case_lookup_is_refused_and_new_folder_rejected(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_case_fixture")
            index = _stub_index(module, ["Ab", "aB"])
            self.assertIsNone(index.find("ab"), "命中多个必须视为歧义，不静默合并")
            self.assertEqual(len(index.ambiguous("ab")), 2)
            with patch.object(module, "GALLERY", index):
                with self.assertRaises(module.MemePathError):
                    module.ensure_category("ab")
            self.assertFalse((module.IMAGE_DIR / "ab").exists(), "歧义被拒后不得新建目录")


class LockCaseTests(unittest.TestCase):
    def test_lock_uses_registered_real_name_across_case(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_lock_fixture")
            (module.IMAGE_DIR / "Test").mkdir()
            manager = module.MemeManager()
            self.assertTrue(manager.toggle_lock("test"))
            self.assertTrue(manager.is_locked("Test"))
            self.assertTrue(manager.is_locked("TEST"))
            self.assertFalse(manager.check_permission(10001, "test"))
            self.assertFalse(manager.check_permission(10001, "Test"))
            self.assertFalse(manager.toggle_lock("TEST"))
            self.assertTrue(manager.check_permission(10001, "Test"))


class StartupNoMaintenanceTests(unittest.TestCase):
    def test_import_and_startup_leave_legacy_data_untouched(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            gallery = _build_legacy_fixture(root)
            before = _snapshot(root)

            module = _load_plugin(root, "meme_startup_fixture")
            self.assertEqual(_snapshot(root), before, "普通 import 不应改动历史文件")
            self.assertIsNone(module.db.conn, "import 阶段不应连接数据库")

            def tripwire(*_a, **_k):
                raise AssertionError("普通启动不应调用维护/DDL 入口")

            connect_calls = []
            fake_mysql = SimpleNamespace(
                connector=SimpleNamespace(connect=lambda **kw: connect_calls.append(kw))
            )
            with patch.object(module, "mysql", fake_mysql), patch.multiple(
                module,
                normalize_folder=Mock(side_effect=tripwire),
                clean_ghost_records=Mock(side_effect=tripwire),
                run_maintenance=Mock(side_effect=tripwire),
            ), patch.object(
                module.MemeDatabase, "migrate_json", tripwire
            ), patch.object(
                module.MemeDatabase, "ensure_schema", tripwire
            ):
                asyncio.run(module.startup_task())

            self.assertEqual(connect_calls, [], "普通启动不应连接数据库或建表")
            self.assertEqual(_snapshot(root), before, "普通启动不应改动历史文件")
            self.assertTrue((root / "pending" / "legacy.zip").exists())
            self.assertTrue((root / "image_info.json").exists())
            self.assertFalse((root / "image_info.json.bak").exists())
            self.assertEqual(
                sorted(p.name for p in (gallery / "demo").iterdir()),
                ["Untitled.jpg", "demo1.png"],
            )
            self.assertTrue((gallery / "empty").is_dir())


class MaintenanceDefaultsTests(unittest.TestCase):
    """Major-1：无参调用必须是 dry-run，零写盘零删除。"""

    def _fixture(self, name):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        gallery = _build_legacy_fixture(root)
        return _load_plugin(root, name), root, gallery

    def test_no_arg_call_is_dry_run_and_touches_nothing(self):
        module, root, gallery = self._fixture("meme_maint_dry")
        ghost = {"folder": "demo", "filename": "missing.webp"}
        db = _FakeDb(records=[ghost])
        before = _snapshot(root)
        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance())

        self.assertTrue(report["dry_run"])
        for key in ("normalized_files", "ghost_deleted"):
            self.assertEqual(report["applied"][key], 0)
        self.assertFalse(report["applied"]["migrated_json"])
        self.assertGreater(report["plan"]["normalize_rename"], 0)
        self.assertEqual(report["plan"]["ghost_records"], 1)
        self.assertEqual(report["plan"]["remove_empty_folders"], 1)
        self.assertEqual(_snapshot(root), before, "dry-run 必须零写盘")
        self.assertNotIn(("exact", "demo", "missing.webp"), db.calls)
        self.assertNotIn("migrate", db.calls)
        self.assertNotIn("ensure_schema", db.calls, "dry-run 不建表")
        self.assertEqual(db.records, [ghost], "dry-run 不得改动任何数据库记录")
        self.assertEqual(
            [c for c in db.calls if isinstance(c, tuple)], [], "dry-run 不得有写库动作"
        )

    def test_signature_defaults_are_keyword_only_and_non_destructive(self):
        """Major-1 关闭用例：默认值本身就是「不动任何历史数据」。"""
        module = _shared_module()
        params = inspect.signature(module.run_maintenance).parameters
        for name, expected in (
            ("dry_run", True),
            ("normalize", False),
            ("clean_ghosts", False),
            ("allow_json_migration", False),
        ):
            self.assertIn(name, params, name)
            self.assertEqual(params[name].kind, inspect.Parameter.KEYWORD_ONLY, name)
            self.assertIs(params[name].default, expected, f"{name} 的默认值必须非破坏性")

    def test_normalize_renames_only_with_explicit_flag(self):
        module, _root, gallery = self._fixture("meme_maint_normalize")
        db = _FakeDb()
        with patch.object(module, "db", db):
            asyncio.run(module.run_maintenance(dry_run=False))
        self.assertEqual(
            sorted(p.name for p in (gallery / "demo").iterdir()),
            ["Untitled.jpg", "demo1.png"],
            "未给 normalize=True 不得改名",
        )

        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance(dry_run=False, normalize=True))
        names = sorted(p.name for p in (gallery / "demo").iterdir())
        self.assertNotIn("Untitled.jpg", names)
        self.assertTrue(any(n.lower().endswith(".webp") for n in names))
        self.assertGreater(report["applied"]["normalized_files"], 0)

    def test_ghosts_deleted_only_with_explicit_flag(self):
        module, _root, _gallery = self._fixture("meme_maint_ghost")
        ghost = {"folder": "demo", "filename": "missing.webp"}
        db = _FakeDb(records=[ghost])
        with patch.object(module, "db", db):
            asyncio.run(module.run_maintenance(dry_run=False))
        self.assertNotIn(("exact", "demo", "missing.webp"), db.calls)

        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance(dry_run=False, clean_ghosts=True))
        self.assertIn(("exact", "demo", "missing.webp"), db.calls)
        self.assertEqual(report["applied"]["ghost_deleted"], 1)

    def test_schema_created_first_and_json_migration_is_opt_in(self):
        module, _root, _gallery = self._fixture("meme_maint_schema")
        db = _FakeDb()
        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance(dry_run=False))
        self.assertEqual(
            db.calls[:2],
            ["schema_ready", "ensure_schema"],
            "schema 检查/建表必须是维护的第一步，早于任何迁移与删除",
        )
        self.assertTrue(report["applied"]["schema_created"])
        self.assertNotIn("migrate", db.calls)

    def test_schema_create_precedes_record_scans_at_cursor_level(self):
        """Major-2：新部署必须先 CREATE TABLE，早于任何记录扫描。"""
        module, _root, _gallery = self._fixture("meme_maint_cursor")
        db = module.MemeDatabase()
        cursor = _FakeCursor(one=None)  # SHOW TABLES 查不到表 → 需要建表
        with patch.object(module, "db", db), patch.object(
            db, "_get_cursor", return_value=cursor
        ):
            report = asyncio.run(module.run_maintenance(dry_run=False))

        sqls = [sql for sql, _params in cursor.executed]
        create_at = next(i for i, s in enumerate(sqls) if "CREATE TABLE" in s)
        self.assertEqual(create_at, 1, f"CREATE 必须在 SHOW TABLES 之后、其它语句之前: {sqls}")
        self.assertTrue(any("SHOW TABLES" in s for s in sqls[:create_at]))
        selects = [
            i for i, s in enumerate(sqls) if s.strip().upper().startswith("SELECT")
        ]
        self.assertTrue(selects, "维护流程应当扫描过记录表")
        self.assertTrue(
            all(create_at < i for i in selects), f"CREATE 必须早于任何 SELECT: {sqls}"
        )
        self.assertTrue(report["applied"]["schema_created"])

    def test_existing_schema_is_not_recreated(self):
        module, _root, _gallery = self._fixture("meme_maint_schema_ok")
        db = _FakeDb(schema_ready=True)
        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance(dry_run=False))
        self.assertNotIn("ensure_schema", db.calls, "表已存在时不得重复 DDL")
        self.assertFalse(report["applied"]["schema_created"])

    def test_dry_run_never_migrates_even_when_allowed(self):
        module, _root, _gallery = self._fixture("meme_maint_dry_migrate")
        db = _FakeDb()
        info = module.IMAGE_INFO_FILE
        before = info.read_bytes()
        with patch.object(module, "db", db):
            report = asyncio.run(
                module.run_maintenance(dry_run=True, allow_json_migration=True)
            )
        self.assertFalse(report["applied"]["migrated_json"])
        self.assertNotIn("migrate", db.calls)
        self.assertEqual(info.read_bytes(), before, "dry-run 不得改动历史 JSON")
        self.assertFalse(info.with_suffix(".json.bak").exists())


class MigrateJsonTests(unittest.TestCase):
    """Major-2：0 行迁入必须保留原文件并返回 False。"""

    def _module(self, name):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _build_legacy_fixture(root)
        return _load_plugin(root, name)

    def test_zero_rows_keeps_file_and_returns_false(self):
        module = self._module("meme_migrate_zero")
        info = module.IMAGE_INFO_FILE
        before = info.read_bytes()
        db = module.MemeDatabase()
        cursor = _FakeCursor(rowcount=0)
        with patch.object(db, "_get_cursor", return_value=cursor):
            self.assertFalse(db.migrate_json())
        self.assertTrue(info.exists(), "零行迁入不得改名原文件")
        self.assertEqual(info.read_bytes(), before, "零行迁入不得改动原文件内容")
        self.assertFalse(info.with_suffix(".json.bak").exists())
        self.assertTrue(cursor.closed)

    def test_missing_json_is_a_no_op(self):
        module = self._module("meme_migrate_missing")
        module.IMAGE_INFO_FILE.unlink()
        db = module.MemeDatabase()
        cursor = _FakeCursor(rowcount=1)
        with patch.object(db, "_get_cursor", return_value=cursor):
            self.assertFalse(db.migrate_json())
        self.assertEqual(cursor.executed, [], "没有历史 JSON 时不应执行任何语句")

    def test_positive_rows_renames_file_and_returns_true(self):
        module = self._module("meme_migrate_ok")
        info = module.IMAGE_INFO_FILE
        db = module.MemeDatabase()
        cursor = _FakeCursor(rowcount=1)
        with patch.object(db, "_get_cursor", return_value=cursor):
            self.assertTrue(db.migrate_json())
        self.assertFalse(info.exists())
        self.assertTrue(info.with_suffix(".json.bak").exists())

    def test_ensure_schema_executes_create_table(self):
        module = self._module("meme_schema")
        db = module.MemeDatabase()
        cursor = _FakeCursor()
        with patch.object(db, "_get_cursor", return_value=cursor):
            self.assertTrue(db.ensure_schema())
        self.assertTrue(cursor.executed)
        self.assertIn("CREATE TABLE", cursor.executed[0][0])


class SsrfTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_private_loopback_and_link_local_targets_are_refused(self):
        for url in (
            "http://127.0.0.1/x.png",
            "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.5/a.png",
            "http://192.168.1.1/a.png",
            "http://[::1]/a.png",
            "http://0.0.0.0/a.png",
        ):
            with self.assertRaises(self.module.MemeError, msg=url):
                self.module._assert_public_url(url)

    def test_redirect_to_private_is_refused_without_second_request(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "http://10.0.0.7/secret.png"})

        with patch.object(self.module, "_new_http_client", _mock_factory(handler)):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertIn("内网", str(ctx.exception))
        self.assertEqual(len(calls), 1, "拒绝后不得发出第二跳")

    def test_redirect_to_non_http_scheme_is_refused(self):
        calls = []

        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"location": "file:///C:/x.png"})

        with patch.object(self.module, "_new_http_client", _mock_factory(handler)):
            with self.assertRaises(self.module.MemeError):
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertEqual(len(calls), 1)


class ImageBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_non_http_url_is_refused_without_network(self):
        with self.assertRaises(self.module.MemeError):
            asyncio.run(self.module.fetch_image_within_budget("file:///C:/x.png"))

    def test_body_over_byte_budget_is_rejected(self):
        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "image/png"}, content=b"x" * 200
            )

        with patch.object(
            self.module, "_new_http_client", _mock_factory(handler)
        ), patch.object(self.module, "MEME_MAX_IMAGE_BYTES", 10):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertIn("预算", str(ctx.exception))

    def test_non_image_content_type_is_rejected(self):
        def handler(request):
            return httpx.Response(
                200, headers={"content-type": "text/html"}, content=b"<html/>"
            )

        with patch.object(self.module, "_new_http_client", _mock_factory(handler)):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertIn("不是图片", str(ctx.exception))

    def test_slot_wait_has_upper_bound(self):
        with patch.object(
            self.module, "_DOWNLOAD_SLOTS", asyncio.Semaphore(0)
        ), patch.object(self.module, "MEME_DOWNLOAD_SLOT_WAIT", 0.05):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertIn("排队超时", str(ctx.exception))

    def test_total_download_has_upper_bound(self):
        async def slow(_url):
            await asyncio.sleep(0.4)
            return b"x"

        with patch.object(self.module, "_download_image", slow), patch.object(
            self.module, "MEME_DOWNLOAD_TOTAL_TIMEOUT", 0.05
        ):
            with self.assertRaises(self.module.MemeError) as ctx:
                asyncio.run(self.module.fetch_image_within_budget(_PUBLIC_IP_URL))
        self.assertIn("超时", str(ctx.exception))

    def test_pixel_budget_rejects_large_image(self):
        buf = BytesIO()
        Image.new("RGB", (8, 8), "green").save(buf, format="PNG")
        with patch.object(self.module, "MEME_MAX_IMAGE_PIXELS", 4), self.assertRaises(
            self.module.MemeError
        ) as ctx:
            self.module._inspect_image(buf.getvalue())
        self.assertIn("像素", str(ctx.exception))

    def test_unknown_format_is_refused(self):
        with self.assertRaises(self.module.MemeError):
            self.module._inspect_image(b"definitely not an image")

    def test_normal_image_passes_budget(self):
        self.assertEqual(self.module._inspect_image(_png_bytes()), ("PNG", 8, 8))


class SendFailureKeepsFilesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _shared_module()

    def test_action_failed_does_not_delete_image_or_record(self):
        folder = Path(_HOME.name) / "send_fail"
        folder.mkdir(exist_ok=True)
        image = folder / "keep1.png"
        image.write_bytes(_png_bytes())
        before = _snapshot(folder)
        db = _FakeDb()

        class _FailingMatcher:
            async def send(self, _message):
                raise ActionFailed("blocked")

        with patch.object(self.module, "db", db):
            sent = asyncio.run(self.module.send_candidate(_FailingMatcher(), [image]))

        self.assertIsNone(sent)
        self.assertTrue(image.exists(), "发送失败不得删除原图")
        self.assertEqual(_snapshot(folder), before)
        self.assertEqual(db.calls, [], "发送失败不得删除数据库记录")

    def test_missing_folder_listing_returns_empty(self):
        self.assertEqual(
            self.module.get_sorted_files(Path(_HOME.name) / "not_a_folder"), []
        )


class DeleteCommandTests(unittest.TestCase):
    def test_explicit_delete_removes_only_resolved_file(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_delete_fixture")
            demo = module.IMAGE_DIR / "demo"
            demo.mkdir()
            (demo / "demo1.png").write_bytes(_png_bytes())
            (demo / "demo10.png").write_bytes(_png_bytes())
            db = _FakeDb()
            with patch.object(module, "db", db):
                msg = asyncio.run(module.delete_image("demo1"))
            self.assertIn("已删除", msg)
            self.assertFalse((demo / "demo1.png").exists())
            self.assertTrue((demo / "demo10.png").exists(), "不得按主名前缀连带删除")
            self.assertEqual(
                [c for c in db.calls if c[0] == "exact"], [("exact", "demo", "demo1.png")]
            )

    def test_escape_attempt_deletes_nothing(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_delete_escape")
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "secret.png").write_bytes(b"secret")
            db = _FakeDb()
            before = _snapshot(Path(tmp))
            with patch.object(module, "db", db):
                msg = asyncio.run(module.delete_image("../outside/secret.png"))
            self.assertIn("文件不存在", msg)
            self.assertEqual(_snapshot(Path(tmp)), before)
            self.assertEqual(db.calls, [])


class NormalizeRecordIntegrityTests(unittest.TestCase):
    """Minor-1：normalize 只处理本条记录，并把新文件名回写到记录里。"""

    def _two_categories_with_same_name(self, module_name):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        gallery = root / "meme"
        (gallery / "demo").mkdir(parents=True)
        (gallery / "other").mkdir(parents=True)
        (gallery / "demo" / "dup.png").write_bytes(_png_bytes())
        (gallery / "other" / "dup.png").write_bytes(_png_bytes())
        return _load_plugin(root, module_name), gallery

    def test_normalize_does_not_delete_same_name_record_in_other_category(self):
        module, gallery = self._two_categories_with_same_name("meme_norm_crosscat")
        db = _FakeDb(
            records=[
                {"folder": "demo", "filename": "dup.png"},
                {"folder": "other", "filename": "dup.png"},
            ]
        )
        with patch.object(module, "db", db):
            invalidated = asyncio.run(
                module.normalize_folder(
                    gallery / "demo", {("demo", "dup.png"), ("other", "dup.png")}
                )
            )

        self.assertEqual(invalidated, 0)
        self.assertEqual(
            [c for c in db.calls if c[0] == "delete"], [], "不得按文件名跨分类删除记录"
        )
        self.assertEqual(
            [r for r in db.records if r["folder"] == "other"],
            [{"folder": "other", "filename": "dup.png"}],
            "另一个分类里的同名记录必须仍在",
        )
        self.assertEqual(
            sorted(p.name for p in (gallery / "demo").iterdir()), ["demo1.webp"]
        )
        self.assertEqual(
            [r["filename"] for r in db.records if r["folder"] == "demo"],
            ["demo1.webp"],
            "改名后记录必须跟着改名，否则 /溯源 与 /最近上传 会指向不存在的文件",
        )
        with patch.object(module, "db", db):
            self.assertEqual(module.ghost_records(), [], "回写正确后不应产生幽灵记录")

    def test_failed_record_rewrite_is_counted_not_silent(self):
        module, gallery = self._two_categories_with_same_name("meme_norm_invalidated")
        db = _FakeDb(records=[{"folder": "demo", "filename": "dup.png"}])
        with patch.object(module, "db", db), patch.object(
            db, "rename_exact_record", return_value=False
        ):
            invalidated = asyncio.run(
                module.normalize_folder(gallery / "demo", {("demo", "dup.png")})
            )
        self.assertEqual(invalidated, 1, "确认存在却回写失败的记录必须计数")

    def test_maintenance_report_exposes_records_invalidated(self):
        module, gallery = self._two_categories_with_same_name("meme_norm_report")
        db = _FakeDb(records=[{"folder": "demo", "filename": "dup.png"}])
        with patch.object(module, "db", db), patch.object(
            db, "rename_exact_record", return_value=False
        ):
            report = asyncio.run(module.run_maintenance(dry_run=False, normalize=True))
        self.assertEqual(report["applied"]["records_invalidated"], 1)
        self.assertIn("records_invalidated", report["applied"])


class ExclusiveWriteTests(unittest.TestCase):
    """Minor-2：取号与写盘之间用 O_EXCL 抢名，并发同名不覆盖已入库图片。"""

    def test_exclusive_create_refuses_existing_target(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_excl_unit")
            target = Path(tmp) / "demo1.webp"
            target.write_bytes(b"ORIGINAL")
            with self.assertRaises(FileExistsError):
                module._create_exclusive_file(target, b"NEW")
            self.assertEqual(target.read_bytes(), b"ORIGINAL", "O_EXCL 必须拒绝覆盖")

    def test_concurrent_same_name_uploads_never_overwrite(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_excl_concurrent")
            folder = module.IMAGE_DIR / "demo"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "demo1.webp").write_bytes(b"ORIGINAL")

            async def race():
                return await asyncio.gather(
                    module.write_new_image(folder, "demo", ".webp", _png_bytes((8, 8))),
                    module.write_new_image(folder, "demo", ".webp", _png_bytes((12, 12))),
                )

            names = asyncio.run(race())
            self.assertEqual(len(set(names)), 2, "并发同名必须拿到两个不同文件名")
            for name in names:
                self.assertTrue((folder / name).is_file(), name)
            self.assertEqual(
                (folder / "demo1.webp").read_bytes(), b"ORIGINAL", "已有图片不得被覆盖"
            )


class UnwritableImageDirTests(unittest.TestCase):
    """Minor-3：IMAGE_DIR 不可建时导入仍成功（只告警，不阻断加载）。"""

    def test_import_survives_when_image_dir_cannot_be_created(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "meme").write_text("not a directory", encoding="utf-8")
            module = _load_plugin(root, "meme_bad_image_dir")
            self.assertTrue((root / "meme").is_file(), "导入不得改动占用该名字的文件")
            self.assertEqual(module.IMAGE_DIR, root / "meme")
            self.assertIsInstance(module.GALLERY_ROOT, Path)


class AmbiguousCategoryLockTests(unittest.TestCase):
    """Minor-4：仅大小写不同的多个分类并存时直接拒绝，不退回原样比较。"""

    def test_ambiguous_category_is_refused_instead_of_unlocked(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_ambig_lock")
            index = _stub_index(module, ["Ab", "aB"])
            with patch.object(module, "GALLERY", index):
                manager = module.MemeManager()
                manager.restricted_folders = {"Ab"}
                self.assertTrue(manager.is_locked("Ab"), "登记真名仍按原样生效")
                with self.assertRaises(module.MemeCategoryAmbiguous):
                    manager.is_locked("ab")
                with self.assertRaises(module.MemeCategoryAmbiguous):
                    manager.check_permission(10001, "ab")
                with self.assertRaises(module.MemeCategoryAmbiguous):
                    manager.toggle_lock("ab")


class MaintenanceDbStatusTests(unittest.TestCase):
    """Minor-5：库不可用时 ghost_records 必须是 None，不能是 0。"""

    def test_unavailable_db_reports_none_ghosts(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        gallery = _build_legacy_fixture(root)
        module = _load_plugin(root, "meme_maint_db_down")

        class _DownDb(_FakeDb):
            def is_available(self):
                self.calls.append("is_available")
                return False

        db = _DownDb(records=[{"folder": "demo", "filename": "missing.webp"}])
        before = _snapshot(gallery)
        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance())
        self.assertIs(report["db_available"], False)
        self.assertIsNone(report["plan"]["ghost_records"], "库不可用时不得报 0 条幽灵记录")
        self.assertEqual(db.calls.count("get_all"), 0, "库不可用时不做无意义的记录扫描")
        self.assertEqual(_snapshot(gallery), before)

    def test_available_db_reports_count(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        _build_legacy_fixture(root)
        module = _load_plugin(root, "meme_maint_db_up")
        db = _FakeDb(records=[{"folder": "demo", "filename": "missing.webp"}])
        with patch.object(module, "db", db):
            report = asyncio.run(module.run_maintenance())
        self.assertIs(report["db_available"], True)
        self.assertEqual(report["plan"]["ghost_records"], 1)


class UrlParsingRejectTests(unittest.TestCase):
    """Minor-6：非法端口 / 括号错误必须收敛成 MemeError，不落通用失败文案。"""

    def test_malformed_port_and_url_raise_meme_error(self):
        with TemporaryDirectory() as tmp:
            module = _load_plugin(Path(tmp), "meme_url_reject")
        for bad in ("http://example.com:99999/a.png", "http://[::1/a.png"):
            with self.assertRaises(module.MemeError) as ctx:
                module._assert_public_url(bad)
            self.assertEqual(str(ctx.exception), module.REJECT_LINK)


if __name__ == "__main__":
    unittest.main()
