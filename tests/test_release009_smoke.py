import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image


PLUGIN_FILE = Path(__file__).resolve().parents[1] / "__init__.py"


def _ensure_nonebot_initialized():
    import nonebot

    try:
        nonebot.get_driver()
    except Exception:
        nonebot.init(_env_file=None)
    return nonebot


def _load_plugin_in_temp_dir():
    nonebot = _ensure_nonebot_initialized()
    with TemporaryDirectory() as temp_dir:
        temp_file = Path(temp_dir) / "meme_smoke.py"
        temp_file.write_bytes(PLUGIN_FILE.read_bytes())
        module_name = "release009_meme_smoke"
        # Force the optional connector branch without contacting any database.
        with patch.dict(
            sys.modules,
            {"mysql": None, "mysql.connector": None},
        ):
            spec = importlib.util.spec_from_file_location(module_name, temp_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        return module


class Release009MemeSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = _load_plugin_in_temp_dir()

    def test_plugin_import_matchers_and_dependency_degradation(self):
        self.assertIsNotNone(self.module.__plugin_meta__)
        for matcher_name in ("help_cmd", "list_meme", "upload_cmd"):
            self.assertTrue(callable(getattr(self.module, matcher_name).handle))
        self.assertIsNone(self.module.db.conn)

    def test_key_image_processing_and_safe_manager_state(self):
        manager = self.module.MemeManager()
        with TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir) / "demo"
            folder.mkdir()
            (folder / "demo1.webp").write_bytes(b"x")
            self.assertEqual(manager.get_next_filename(folder, "demo", ".webp"), "demo2.webp")
            self.assertTrue(manager.toggle_blacklist(123456))
            self.assertTrue(manager.is_group_blacklisted(123456))

        with TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "source.png"
            Image.new("RGB", (8, 8), "red").save(source, format="PNG")
            converted = self.module._convert_to_webp_sync(source.read_bytes())
            self.assertTrue(converted.startswith(b"RIFF"))


if __name__ == "__main__":
    unittest.main()
