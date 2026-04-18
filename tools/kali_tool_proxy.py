#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from kali_container import get_kali_container_name, list_running_docker_container_names


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: kali_tool_proxy.py <tool> [args...]", file=sys.stderr)
        return 2

    tool = sys.argv[1]
    args = sys.argv[2:]
    container = get_kali_container_name(log=None)
    running = set(list_running_docker_container_names())
    if not container or container not in running:
        print(
            f"[kali-proxy] Kali container unavailable: {container or '<unset>'}",
            file=sys.stderr,
        )
        return 127

    proc = subprocess.run(["docker", "exec", "-i", container, tool, *args])
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
