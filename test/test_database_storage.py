from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from sqlalchemy import inspect

os.environ.setdefault("CHATGPT2API_AUTH_KEY", "chatgpt2api")

from services.storage.database_storage import AccountModel, DatabaseStorageBackend


def _sqlite_url() -> str:
    tmpdir = Path(os.environ.get("TMPDIR") or "/tmp")
    tmpdir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{tmpdir / 'dsh_storage_test.db'}"


class DatabaseStorageTests(unittest.TestCase):
    """The account token column must accept long JWTs.

    Real ChatGPT access tokens are ~1.9k-char JWTs. The column was previously
    varchar(2048) (almost no headroom), and PostgreSQL/MySQL reject over-long
    values, failing the whole save. SQLite ignores lengths, so these tests
    assert the schema definition and the round-trip rather than the DB error.
    """

    def test_access_token_column_is_unbounded(self) -> None:
        columns = {column.name: column for column in inspect(AccountModel).columns}
        self.assertIn("access_token", columns)
        token_type = columns["access_token"].type
        # String(N) with a length would cap at N; Text is unbounded.
        length = getattr(token_type, "length", None)
        self.assertIsNone(length, f"access_token must be unbounded, got length={length}")

    def test_long_token_round_trips(self) -> None:
        backend = DatabaseStorageBackend(_sqlite_url())
        try:
            long_token = "t" * 4096  # well over the old varchar(2048) ceiling
            accounts = [
                {"access_token": long_token, "email": "long@example.com", "status": "正常"},
                {"access_token": "short", "email": "short@example.com", "status": "正常"},
            ]
            backend.save_accounts(accounts)
            loaded = backend.load_accounts()
            by_token = {item["access_token"]: item for item in loaded}
            self.assertIn(long_token, by_token)
            self.assertEqual(by_token[long_token]["email"], "long@example.com")
            self.assertEqual(by_token["short"]["email"], "short@example.com")
        finally:
            backend.engine.dispose()

    def test_promote_is_skipped_on_sqlite_without_error(self) -> None:
        # _promote_token_column_to_text must be a no-op on SQLite (its ALTER
        # syntax differs and lengths are not enforced anyway).
        backend = DatabaseStorageBackend(_sqlite_url())
        try:
            self.assertEqual(backend._promote_token_column_to_text(), None)
        finally:
            backend.engine.dispose()


if __name__ == "__main__":
    unittest.main()
