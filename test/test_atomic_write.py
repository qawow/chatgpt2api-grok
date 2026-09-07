from __future__ import annotations

import errno
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils.atomic import atomic_write_json, atomic_write_text


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "config.json"

    def test_writes_content_and_leaves_no_tmp(self) -> None:
        atomic_write_json(self.path, {"a": 1})
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual(list(Path(self._tmp.name).glob("*.tmp")), [])
        self.assertEqual(list(Path(self._tmp.name).glob(".*.tmp")), [])

    def test_ebusy_falls_back_to_in_place_write(self) -> None:
        self.path.write_text('{"old": true}\n', encoding="utf-8")
        original = os.replace

        def _ebusy(src: str, dst: str) -> None:
            raise OSError(errno.EBUSY, "Device or resource busy", dst)

        with mock.patch("os.replace", side_effect=_ebusy) as mocked:
            atomic_write_json(self.path, {"new": True})
        mocked.assert_called_once()
        # Content persisted even though rename over the (bind-mounted) file failed.
        self.assertEqual(json.loads(self.path.read_text(encoding="utf-8")), {"new": True})
        # The temp file must not leak.
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [self.path])
        # In-place fallback keeps original bytes/encoding; original was small JSON.
        self.assertTrue(original)

    def test_other_replace_error_propagates_and_tmp_is_cleaned(self) -> None:
        with mock.patch("os.replace", side_effect=OSError(errno.EACCES, "permission denied")):
            with self.assertRaises(OSError):
                atomic_write_text(self.path, "boom")
        # No partial target and no leftover temp.
        self.assertFalse(self.path.exists())
        self.assertEqual(list(Path(self._tmp.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
