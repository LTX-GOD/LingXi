"""
scraper.py — 爬取入口，等价于 uv run main.py crawl [args]
用法: uv run scraper.py [--pages N] [--output PATH]
"""
import sys
from ctf_kb.cli import main

if __name__ == "__main__":
    main(["crawl"] + sys.argv[1:])
