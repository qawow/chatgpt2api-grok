import os
import tempfile
import unittest
from pathlib import Path


class ConfigLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # test/__init__.py already points CONFIG_FILE at a scratch path, so
        # this no longer has to plant a config.json in the checkout.
        from services import config as config_module

        cls.config_module = config_module

    def test_data_dir_and_config_file_follow_env_overrides(self) -> None:
        """测试包靠这两个变量避开真实 data/；落回仓库目录就说明隔离失效了。"""
        module = self.config_module
        self.assertEqual(module.DATA_DIR, Path(os.environ["CHATGPT2API_DATA_DIR"]))
        self.assertEqual(module.CONFIG_FILE, Path(os.environ["CHATGPT2API_CONFIG_FILE"]))
        self.assertNotEqual(module.DATA_DIR, module.BASE_DIR / "data")

    def test_singletons_write_under_the_overridden_data_dir(self) -> None:
        from services.grok_account_service import GROK_ACCOUNTS_FILE
        from services.image_storage_service import IMAGE_INDEX_FILE
        from services.log_service import log_service

        data_dir = self.config_module.DATA_DIR
        for path in (log_service.path, IMAGE_INDEX_FILE, GROK_ACCOUNTS_FILE, self.config_module.config.images_dir):
            self.assertTrue(Path(path).is_relative_to(data_dir), path)

    def test_load_settings_ignores_directory_config_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            base_dir = Path(tmp_dir)
            data_dir = base_dir / "data"
            config_dir = base_dir / "config.json"
            os_auth_key = "env-auth"

            config_dir.mkdir()

            module = self.config_module
            old_base_dir = module.BASE_DIR
            old_data_dir = module.DATA_DIR
            old_config_file = module.CONFIG_FILE
            old_env_auth_key = module.os.environ.get("CHATGPT2API_AUTH_KEY")
            try:
                module.BASE_DIR = base_dir
                module.DATA_DIR = data_dir
                module.CONFIG_FILE = config_dir
                module.os.environ["CHATGPT2API_AUTH_KEY"] = os_auth_key

                settings = module._load_settings()

                self.assertEqual(settings.auth_key, os_auth_key)
                self.assertEqual(settings.refresh_account_interval_minute, 5)
            finally:
                module.BASE_DIR = old_base_dir
                module.DATA_DIR = old_data_dir
                module.CONFIG_FILE = old_config_file
                if old_env_auth_key is None:
                    module.os.environ.pop("CHATGPT2API_AUTH_KEY", None)
                else:
                    module.os.environ["CHATGPT2API_AUTH_KEY"] = old_env_auth_key

    def test_image_failover_controls_have_safe_bounds(self) -> None:
        config = self.config_module.ConfigStore.__new__(self.config_module.ConfigStore)
        config.data = {
            "image_account_failover_retries": 99,
            "image_poll_failover_retries": -1,
            "image_text_failover_retries": "bad",
            "image_transient_failure_cooldown_secs": 99999,
        }
        self.assertEqual(config.image_account_failover_retries, 20)
        self.assertEqual(config.image_poll_failover_retries, 0)
        self.assertEqual(config.image_text_failover_retries, 3)
        self.assertEqual(config.image_transient_failure_cooldown_secs, 3600)


if __name__ == "__main__":
    unittest.main()
