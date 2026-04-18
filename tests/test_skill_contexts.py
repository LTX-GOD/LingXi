from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.skills import load_about_security_skills, load_local_skills, select_skill_contexts


class SkillContextTests(unittest.TestCase):
    def test_missing_optional_skill_directories_return_empty_maps(self) -> None:
        load_local_skills.cache_clear()
        load_about_security_skills.cache_clear()
        try:
            self.assertEqual({}, load_local_skills())
            self.assertEqual({}, load_about_security_skills())
        finally:
            load_local_skills.cache_clear()
            load_about_security_skills.cache_clear()

    def test_select_skill_contexts_gracefully_handles_public_export_without_private_skills(self) -> None:
        challenge = {
            "title": "Login Portal",
            "description": "A web login page for employees",
            "entrypoint": ["https://target.example:8080"],
            "forum_task": False,
        }

        with patch("agent.skills.load_local_skills", return_value={}), patch(
            "agent.skills.load_about_security_skills",
            return_value={},
        ), patch("agent.skills.load_level2_poc_skill", return_value=None):
            main_context, advisor_context, labels = select_skill_contexts(
                challenge,
                recon_info="title: login-form",
            )

        self.assertIsInstance(main_context, str)
        self.assertIsInstance(advisor_context, str)
        self.assertIsInstance(labels, list)


if __name__ == "__main__":
    unittest.main()
