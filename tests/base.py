"""Shared test setup: every test gets its own temporary SQLite database."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import config  # noqa: E402
import db  # noqa: E402
import pipeline  # noqa: E402

SAMPLES = config.SAMPLES_DIR


class TempDBTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        self._old_path = db.DB_PATH
        db.DB_PATH = self.db_path
        self._grn = config.GRN_REQUIRED
        db.init()

    def tearDown(self):
        db.DB_PATH = self._old_path
        config.GRN_REQUIRED = self._grn
        self.tmp.cleanup()

    def run_file(self, name_or_path):
        p = Path(name_or_path)
        if not p.is_absolute():
            p = SAMPLES / name_or_path
        return pipeline.run(p.read_bytes(), p.name)

    @staticmethod
    def codes(res, outcome=None):
        return [c.code for c in res["checks"] if outcome is None or c.outcome == outcome]
