from __future__ import annotations

import os
import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest.mock import patch


if "pydantic" not in sys.modules:
    pydantic_stub = ModuleType("pydantic")
    pydantic_stub.BaseModel = object
    pydantic_stub.Field = lambda *args, **kwargs: None
    pydantic_stub.create_model = lambda name, **kwargs: type(name, (), {})
    sys.modules["pydantic"] = pydantic_stub

import tools.kali_mcp as kali_mcp


class KaliMCPContainerResolutionTests(unittest.TestCase):
    def test_runner_uses_env_container_name_at_runtime(self) -> None:
        with patch.object(kali_mcp, "ClientSession", object()), patch.dict(
            os.environ,
            {"DOCKER_CONTAINER_NAME": "kali-research"},
            clear=False,
        ):
            runner = kali_mcp.KaliMCPRunner()

        self.assertEqual("kali-research", runner.container)

    def test_runner_auto_discovers_single_running_kali_container(self) -> None:
        with patch.object(kali_mcp, "ClientSession", object()), patch.dict(
            os.environ,
            {},
            clear=True,
        ), patch.object(
            kali_mcp,
            "get_kali_container_name",
            return_value="kali-research",
        ):
            runner = kali_mcp.KaliMCPRunner()

        self.assertEqual("kali-research", runner.container)

    def test_runner_falls_back_from_stale_env_container_to_unique_running_kali_container(self) -> None:
        with patch.object(kali_mcp, "ClientSession", object()), patch.dict(
            os.environ,
            {"DOCKER_CONTAINER_NAME": "kali-pentest"},
            clear=True,
        ), patch.object(
            kali_mcp,
            "get_kali_container_name",
            return_value="kali-research",
        ):
            runner = kali_mcp.KaliMCPRunner()

        self.assertEqual("kali-research", runner.container)

    def test_runner_rejects_ambiguous_discovered_kali_containers(self) -> None:
        with patch.object(kali_mcp, "ClientSession", object()), patch.dict(
            os.environ,
            {},
            clear=True,
        ), patch.object(
            kali_mcp,
            "get_kali_container_name",
            side_effect=kali_mcp.KaliMCPError("多个运行中的 Kali 容器"),
        ):
            with self.assertRaises(kali_mcp.KaliMCPError) as ctx:
                kali_mcp.KaliMCPRunner()

        self.assertIn("多个运行中的 Kali 容器", str(ctx.exception))

    def test_tool_description_mentions_active_container(self) -> None:
        spec = SimpleNamespace(name="nmap_scan", description="desc")
        description = kali_mcp._tool_description_from_spec(spec, "kali-research")
        self.assertIn("`kali-research`", description)


if __name__ == "__main__":
    unittest.main()
