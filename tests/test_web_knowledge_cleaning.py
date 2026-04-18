from __future__ import annotations

import unittest

from web import server


class WebKnowledgeCleaningTests(unittest.TestCase):
    def test_external_record_uses_clean_title_category_and_summary(self) -> None:
        item = {
            "writeup_id": "40381",
            "task": "web/group chat",
            "title": "web/group chat",
            "event": "Lexington Informatics Tournament CTF 2025",
            "category": "unknown",
            "difficulty": "unknown",
            "year": 2025,
            "url": "https://example.com/group-chat",
            "ctftime_url": "https://ctftime.org/writeup/40381",
            "content": (
                "[CTFTIME]\n"
                "https://ctftime.org/writeup/40381\n\n"
                "This website uses cookies to manage authentication, for analytics, and other functions.\n"
                "[Privacy policy](https://ctftime.org/privacy)\n"
                "* CTFs\n"
                "* Upcoming\n"
                "* Archive\n\n"
                "[EXTERNAL]\n"
                "https://example.com/group-chat\n\n"
                "# Group Chat\n"
                "We exploited a stored XSS in the group invite flow to steal the admin session.\n"
                "Then we used the session to access /admin/export and recover the flag.\n"
            ),
        }

        normalized = server._normalize_external_record(item)

        self.assertEqual("group chat", normalized["title"])
        self.assertEqual("web", normalized["category"])
        self.assertIn("stored xss", normalized["summary"].lower())
        self.assertNotIn("this website uses cookies", normalized["summary"].lower())
        self.assertNotIn("privacy policy", normalized["summary"].lower())

    def test_external_category_stats_uses_cleaned_categories(self) -> None:
        records = (
            {
                "writeup_id": "1",
                "task": "web/group chat",
                "title": "web/group chat",
                "event": "Example CTF 2025",
                "category": "unknown",
                "content": "[EXTERNAL]\nhttps://example.com\n\nStored XSS in invite flow.",
            },
        )

        stats = server._external_category_stats(records)

        self.assertEqual({"web": 1}, stats)


if __name__ == "__main__":
    unittest.main()
