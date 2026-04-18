from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import log_utils
from web import server


class LogRedactionTests(unittest.TestCase):
    def test_redacts_flags_tokens_and_gateway_paths(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            redacted = log_utils.redact_sensitive_text(
                "flag=flag{demo_secret} "
                "Authorization: Bearer super-secret-token "
                "Agent-Token: team-secret "
                "url=https://fallback.example:8000/demo/v1/chat/completions"
            )

        self.assertNotIn("flag{demo_secret}", redacted)
        self.assertNotIn("super-secret-token", redacted)
        self.assertNotIn("team-secret", redacted)
        self.assertNotIn("/85_demo/v1/chat/completions", redacted)
        self.assertIn("<flag:", redacted)
        self.assertIn("<redacted-token>", redacted)
        self.assertIn("fallback.example", redacted)

    def test_shell_and_python_summaries_do_not_leak_raw_payloads(self) -> None:
        shell_summary = log_utils.describe_shell_command(
            "curl -H 'Authorization: Bearer abc123' https://panel.example/api/admin/export?token=secret"
        )
        python_summary = log_utils.describe_python_script(
            "import requests\nrequests.get('https://login.example/login', headers={'Authorization': 'Bearer xyz'})\n",
            purpose="http_probe",
        )

        self.assertIn("tool=curl", shell_summary)
        self.assertIn("target=panel.example", shell_summary)
        self.assertNotIn("abc123", shell_summary)
        self.assertNotIn("/api/admin/export", shell_summary)

        self.assertIn("purpose=http_probe", python_summary)
        self.assertIn("target=login.example", python_summary)
        self.assertNotIn("Bearer xyz", python_summary)

    def test_push_log_redacts_before_buffering(self) -> None:
        server._log_buffer.clear()
        try:
            server.push_log(
                "info",
                "submitted flag{buffer_secret} with Cookie=session=abcd to https://internal-admin.example/admin",
                "test",
            )
            entry = server._log_buffer[-1]
        finally:
            server._log_buffer.clear()

        self.assertNotIn("flag{buffer_secret}", entry["message"])
        self.assertNotIn("session=abcd", entry["message"])
        self.assertNotIn("/internal/admin", entry["message"])
        self.assertIn("internal-admin.example", entry["message"])


if __name__ == "__main__":
    unittest.main()
