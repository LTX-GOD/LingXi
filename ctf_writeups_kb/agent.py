"""
agent.py — Agent 聊天入口，等价于 uv run main.py chat
用法: uv run agent.py
"""
import sys
from ctf_kb.cli import main

if __name__ == "__main__":
    main(["chat"] + sys.argv[1:])
