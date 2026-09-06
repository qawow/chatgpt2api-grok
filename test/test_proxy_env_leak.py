from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class ProxyEnvLeakTests(unittest.TestCase):
    def test_load_dotenv_skips_process_wide_proxy_keys(self) -> None:
        from gpt_free_register.engines.core.proxy_env import load_dotenv

        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join([
                    "HTTP_PROXY=socks5h://127.0.0.1:1",
                    "HTTPS_PROXY=socks5h://127.0.0.1:1",
                    "ALL_PROXY=socks5h://127.0.0.1:1",
                    "REGISTER_PROXY_DEFAULT=socks5h://127.0.0.1:1080",
                    "CFD1_DOMAIN=mail.example.com",
                ])
                + "\n",
                encoding="utf-8",
            )
            saved = {
                key: os.environ.pop(key, None)
                for key in (
                    "HTTP_PROXY",
                    "HTTPS_PROXY",
                    "ALL_PROXY",
                    "REGISTER_PROXY_DEFAULT",
                    "CFD1_DOMAIN",
                )
            }
            try:
                loaded = load_dotenv(env_path)
                self.assertEqual(loaded, env_path)
                self.assertIsNone(os.environ.get("HTTP_PROXY"))
                self.assertIsNone(os.environ.get("HTTPS_PROXY"))
                self.assertIsNone(os.environ.get("ALL_PROXY"))
                self.assertEqual(os.environ.get("REGISTER_PROXY_DEFAULT"), "socks5h://127.0.0.1:1080")
                self.assertEqual(os.environ.get("CFD1_DOMAIN"), "mail.example.com")
            finally:
                for key, value in saved.items():
                    os.environ.pop(key, None)
                    if value is not None:
                        os.environ[key] = value

    def test_grok_session_ignores_env_proxy(self) -> None:
        import services.grok_backend_api as grok

        grok._tls.session = None
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "socks5h://127.0.0.1:1"}):
            session = grok._http()
        self.assertFalse(session.trust_env)


if __name__ == "__main__":
    unittest.main()
