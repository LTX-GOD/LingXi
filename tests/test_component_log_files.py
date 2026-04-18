from __future__ import annotations

import logging
import os
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

import log_utils


class ComponentLogFileTests(unittest.TestCase):
    def _close_root_handlers(self) -> None:
        root = logging.getLogger()
        for handler in list(root.handlers):
            try:
                handler.close()
            except Exception:
                pass
        root.handlers.clear()

    def tearDown(self) -> None:
        self._close_root_handlers()

    def test_resolve_log_file_prefers_explicit_argument(self) -> None:
        with patch.dict(os.environ, {"LINGXI_LOG_FILE": "/tmp/from-env.log"}, clear=False):
            self.assertEqual("/tmp/from-arg.log", log_utils.resolve_log_file("/tmp/from-arg.log"))

    def test_resolve_log_file_uses_environment_variable(self) -> None:
        with patch.dict(os.environ, {"LINGXI_LOG_FILE": "/tmp/from-env.log"}, clear=False):
            self.assertEqual("/tmp/from-env.log", log_utils.resolve_log_file())

    def test_resolve_log_file_falls_back_to_default(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual("lingxi.log", log_utils.resolve_log_file())

    def test_setup_logging_binds_file_handler_to_component_log_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "web.log"
            try:
                with patch.dict(os.environ, {"LINGXI_LOG_FILE": str(log_path)}, clear=False):
                    log_utils.setup_logging()

                file_handlers = [
                    handler for handler in logging.getLogger().handlers
                    if isinstance(handler, logging.FileHandler)
                ]
                self.assertEqual(1, len(file_handlers))
                self.assertEqual(str(log_path), file_handlers[0].baseFilename)
            finally:
                self._close_root_handlers()

    def test_setup_logging_uses_rotating_file_handler_and_env_levels(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "web.log"
            try:
                with patch.dict(
                    os.environ,
                    {
                        "LINGXI_LOG_FILE": str(log_path),
                        "LINGXI_CONSOLE_LOG_LEVEL": "WARNING",
                        "LINGXI_FILE_LOG_LEVEL": "ERROR",
                        "LINGXI_LOG_FILE_MAX_BYTES": "256",
                        "LINGXI_LOG_FILE_BACKUP_COUNT": "2",
                    },
                    clear=False,
                ):
                    log_utils.setup_logging()

                root = logging.getLogger()
                file_handlers = [
                    handler for handler in root.handlers
                    if isinstance(handler, RotatingFileHandler)
                ]
                console_handlers = [
                    handler for handler in root.handlers
                    if isinstance(handler, logging.StreamHandler)
                    and not isinstance(handler, logging.FileHandler)
                ]

                self.assertEqual(1, len(file_handlers))
                self.assertEqual(1, len(console_handlers))
                self.assertEqual(logging.ERROR, file_handlers[0].level)
                self.assertEqual(logging.WARNING, console_handlers[0].level)
                self.assertEqual(256, file_handlers[0].maxBytes)
                self.assertEqual(2, file_handlers[0].backupCount)
            finally:
                self._close_root_handlers()

    def test_setup_logging_rotates_file_when_size_limit_reached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "web.log"
            try:
                with patch.dict(
                    os.environ,
                    {
                        "LINGXI_LOG_FILE": str(log_path),
                        "LINGXI_LOG_FILE_MAX_BYTES": "300",
                        "LINGXI_LOG_FILE_BACKUP_COUNT": "2",
                    },
                    clear=False,
                ):
                    log_utils.setup_logging()

                logger = logging.getLogger("tests.log_rotation")
                for _ in range(20):
                    logger.info("rotation-check %s", "x" * 80)

                for handler in logging.getLogger().handlers:
                    handler.flush()

                rotated_files = sorted(path.name for path in Path(temp_dir).glob("web.log*"))
                self.assertGreater(len(rotated_files), 1)
                self.assertIn("web.log", rotated_files)
            finally:
                self._close_root_handlers()

    def test_default_file_level_keeps_info_and_filters_debug(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "web.log"
            try:
                with patch.dict(
                    os.environ,
                    {
                        "LINGXI_LOG_FILE": str(log_path),
                    },
                    clear=False,
                ):
                    log_utils.setup_logging()

                logger = logging.getLogger("tests.default_file_level")
                logger.debug("debug-noise")
                logger.info("important-info")

                for handler in logging.getLogger().handlers:
                    handler.flush()

                content = log_path.read_text(encoding="utf-8")
                self.assertNotIn("debug-noise", content)
                self.assertIn("important-info", content)
            finally:
                self._close_root_handlers()


if __name__ == "__main__":
    unittest.main()
