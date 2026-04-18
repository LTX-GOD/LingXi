"""
store.py — 向量入库入口，等价于 uv run main.py ingest [args]
用法: uv run store.py [--file PATH]
"""
import sys
from ctf_kb.cli import main

if __name__ == "__main__":
    main(["ingest"] + sys.argv[1:])
