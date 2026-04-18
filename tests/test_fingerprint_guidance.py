from __future__ import annotations

import unittest

from agent.skills import LEVEL2_POC_ROOT, select_skill_contexts
from challenge_fingerprints import detect_product_fingerprints


class ChallengeFingerprintDetectionTests(unittest.TestCase):
    def test_detects_known_level2_products_from_runtime_text(self) -> None:
        text = """
        HTTP/1.1 200 OK
        Server: OFBiz
        X-App: Nacos
        <title>1Panel</title>
        <script src="/extensions/core/ComfyUI-Manager/main.js"></script>
        """

        labels = detect_product_fingerprints(text)

        for expected in ("1Panel", "ComfyUI Manager", "Nacos", "OFBiz"):
            self.assertIn(expected, labels)

    def test_detects_1panel_from_api_markers(self) -> None:
        text = """
        HTTP/1.1 200 OK
        Server: nginx
        Set-Cookie: panel_client=abc123
        X-App: fit2cloud
        POST /api/v1/hosts/command/search HTTP/1.1
        """

        labels = detect_product_fingerprints(text)

        self.assertIn("1Panel", labels)


class SkillFingerprintGuidanceTests(unittest.TestCase):
    def test_generic_title_uses_runtime_fingerprint_guidance(self) -> None:
        challenge = {
            "title": "xx系统 xx引擎",
            "description": "",
            "entrypoint": ["forum-target.example:8443"],
        }
        recon_info = """
        **响应头:**
        Server: nginx
        **产品指纹:** 1Panel, GeoServer, Gradio
        **Signals:** login-form, json-content-type
        """

        skill_context, _, _ = select_skill_contexts(challenge, recon_info=recon_info)

        self.assertIn("组件/产品指纹", skill_context)
        self.assertIn("1Panel", skill_context)
        self.assertIn("GeoServer", skill_context)
        self.assertIn("Gradio", skill_context)
        self.assertIn("不要依赖题目标题", skill_context)

    def test_level2_skill_loads_real_specialized_skill_from_runtime_fingerprint(self) -> None:
        challenge = {
            "title": "xx系统 xx引擎",
            "description": "",
            "entrypoint": ["http://app-target.example:3000"],
        }
        recon_info = """
        HTTP/1.1 200 OK
        Server: nginx
        **产品指纹:** Dify
        Next-Action: abcdef123456
        """

        skill_context, _, enabled_skills = select_skill_contexts(challenge, recon_info=recon_info)

        self.assertTrue(LEVEL2_POC_ROOT.exists())
        self.assertIn("### cve-exploit-kb", skill_context)
        self.assertIn("run_level2_cve_poc", skill_context)
        self.assertIn("check -> hunt_flag -> exec", skill_context)
        self.assertIn("当前没有命中可直接调用的 `poc_name`", skill_context)
        self.assertIn("不要为了凑工具而硬选", skill_context)
        self.assertTrue(any(label.startswith("cve-exploit-kb") for label in enabled_skills))

    def test_generic_web_without_cve_signals_does_not_load_level2_skill(self) -> None:
        challenge = {
            "title": "plain login",
            "description": "",
            "entrypoint": ["http://service-target.example:8081"],
        }
        recon_info = """
        HTTP/1.1 200 OK
        Server: nginx
        **Signals:** login-form, json-content-type
        """

        skill_context, _, enabled_skills = select_skill_contexts(challenge, recon_info=recon_info)

        self.assertNotIn("### cve-exploit-kb", skill_context)
        self.assertFalse(any(label.startswith("cve-exploit-kb") for label in enabled_skills))

    def test_manual_level2_task_id_surfaces_known_cve_and_poc(self) -> None:
        challenge = {
            "title": "manual task",
            "description": "",
            "task_id": "p71MyGzdIAR13xvgr8SePV4UZwa6p",
            "manual_task": True,
            "level": 2,
            "entrypoint": ["panel-target.example:10086"],
        }

        skill_context, _, enabled_skills = select_skill_contexts(challenge, recon_info="")

        self.assertIn("### cve-exploit-kb", skill_context)
        self.assertIn("cve-2024-39907", skill_context)
        self.assertIn("1panel", skill_context.lower())
        self.assertIn("target` 必须取当前题目的最新 `entrypoint`", skill_context)
        self.assertTrue(any(label.startswith("cve-exploit-kb") for label in enabled_skills))

    def test_1panel_guidance_prefers_poc_when_psession_exists(self) -> None:
        challenge = {
            "title": "xx系统 xx引擎",
            "description": "",
            "level": 2,
            "entrypoint": ["http://panel-target.example:10086"],
        }
        recon_info = """
        HTTP/1.1 200 OK
        Set-Cookie: panel_client=abcdef
        X-App: fit2cloud
        POST /api/v1/hosts/command/search HTTP/1.1
        Cookie: psession=demo-token
        """

        skill_context, _, enabled_skills = select_skill_contexts(challenge, recon_info=recon_info)

        self.assertIn("当前最像且可直接调用的 `poc_name`: 1panel", skill_context)
        self.assertIn("先跑 `run_level2_cve_poc(1panel, target, check)`", skill_context)
        self.assertIn("如果已经拿到 `psession`/Cookie，不要先用 `execute_python(requests.Session())`", skill_context)
        self.assertTrue(any(label.startswith("cve-exploit-kb") for label in enabled_skills))


if __name__ == "__main__":
    unittest.main()
