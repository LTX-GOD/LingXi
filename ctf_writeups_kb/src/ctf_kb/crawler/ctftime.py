"""
CTF writeup 聚合爬虫：
- 批量遍历 CTFTime writeups 列表
- 解析详情页标签、外链、基础元数据
- 智能爬取任意 seed URL / source manifest
- 自动附加 category / difficulty / year / techniques / tools 元数据
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from crawl4ai import AsyncWebCrawler, CrawlerRunConfig

CTFTIME_BASE = "https://ctftime.org"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

_SKIP_URL_TOKENS = [
    "google.com", "bootstrap", ".css", ".js", "transdata",
    "github.com/search", "twitter.com", "t.co", "youtube.com",
]
MIN_CONTENT_CHARS = 300
_SUPPORTED_CATEGORIES = ("web", "pwn", "crypto", "misc", "reverse", "forensics", "osint", "unknown")

_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "web": (
        "web", "xss", "sqli", "sql injection", "ssti", "ssrf", "csrf", "xxe", "rce",
        "php", "flask", "django", "jwt", "template injection", "deserialization", "upload",
    ),
    "pwn": (
        "pwn", "pwning", "heap", "stack", "rop", "ret2libc", "format string", "uaf",
        "canary", "shellcode", "glibc", "pwntools",
    ),
    "crypto": (
        "crypto", "rsa", "aes", "ecdsa", "lfsr", "lattice", "padding oracle", "hash length extension",
        "cbc", "ecb", "xor", "sage", "number theory",
    ),
    "reverse": (
        "reverse", "re", "rev", "ghidra", "ida", "angr", "decompile", "vm", "bytecode",
        "symbolic execution",
    ),
    "forensics": (
        "forensics", "pcap", "memory dump", "volatility", "wireshark", "disk image", "registry",
        "stego", "autopsy",
    ),
    "osint": (
        "osint", "open source intelligence", "social media", "google dork", "metadata leak",
    ),
    "misc": (
        "misc", "miscellaneous", "jail", "pyjail", "bash jail", "programming", "protocol",
        "qr", "captcha", "stego", "automation",
    ),
}

_TECHNIQUE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "xss": ("xss", "cross-site scripting"),
    "sql injection": ("sql injection", "sqli"),
    "ssti": ("ssti", "template injection"),
    "ssrf": ("ssrf",),
    "xxe": ("xxe", "xml external entity"),
    "rce": ("rce", "remote code execution"),
    "file upload": ("upload", "file upload"),
    "deserialization": ("deserialize", "deserialization", "pickle"),
    "path traversal": ("path traversal", "directory traversal", "../"),
    "race condition": ("race condition",),
    "rop": ("rop", "ret2libc"),
    "heap exploitation": ("heap", "tcache", "fastbin", "unsorted bin"),
    "format string": ("format string", "%n"),
    "rsa": ("rsa",),
    "xor": ("xor",),
    "lattice": ("lattice",),
    "symbolic execution": ("symbolic execution", "angr"),
    "forensics analysis": ("pcap", "memory dump", "volatility", "wireshark"),
}

_TOOL_KEYWORDS: dict[str, tuple[str, ...]] = {
    "burpsuite": ("burp", "burpsuite", "burp suite"),
    "sqlmap": ("sqlmap",),
    "pwntools": ("pwntools",),
    "gdb": ("gdb", "pwndbg", "gef"),
    "ida": ("ida", "ida pro"),
    "ghidra": ("ghidra",),
    "angr": ("angr",),
    "sage": ("sage", "sageMath", "sagemath"),
    "wireshark": ("wireshark",),
    "volatility": ("volatility",),
    "z3": ("z3",),
}


@dataclass(frozen=True)
class SourceSpec:
    url: str
    event: str = ""
    task: str = ""
    title: str = ""
    tags: tuple[str, ...] = ()
    category: str | None = None
    difficulty: str | None = None
    year: int | None = None
    team: str = ""
    points: int | None = None
    solves: int | None = None
    source: str = "seed_manifest"
    ctftime_url: str = ""
    external_url: str = ""
    writeup_id: str = ""


def normalize_category(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in _SUPPORTED_CATEGORIES:
        return text
    aliases = {
        "rev": "reverse",
        "re": "reverse",
        "miscellaneous": "misc",
        "forensic": "forensics",
    }
    if text in aliases:
        return aliases[text]
    return "unknown"


def normalize_difficulty(value: str | None) -> str:
    text = (value or "").strip().lower()
    if text in {"easy", "medium", "hard", "insane", "unknown"}:
        return text
    aliases = {
        "baby": "easy",
        "warmup": "easy",
        "beginner": "easy",
        "normal": "medium",
        "expert": "hard",
        "extreme": "insane",
    }
    return aliases.get(text, "unknown")


def _extract_year(*parts: str) -> int:
    for part in parts:
        for match in re.findall(r"\b(20\d{2})\b", part or ""):
            year = int(match)
            if 2010 <= year <= 2035:
                return year
    return 0


def _extract_team_name(url: str | None) -> str:
    if not url:
        return ""
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or host.endswith("ctftime.org"):
        return ""
    parts = [p for p in host.split(".") if p and p not in {"com", "org", "net", "io", "cn", "me", "blog"}]
    return parts[0] if parts else ""


def _detect_keywords(text: str, mapping: dict[str, tuple[str, ...]]) -> list[str]:
    lowered = (text or "").lower()
    found: list[str] = []
    for label, keywords in mapping.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            found.append(label)
    return found


def _normalize_tags(value: object) -> list[str]:
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple, set)):
        items = [str(part).strip() for part in value]
    else:
        items = []
    return [item.lower() for item in items if item]


def _coerce_int(value: object) -> int | None:
    if value in (None, "", False):
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _merge_unique(items: list[str], extra: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in [*items, *extra]:
        cleaned = str(value or "").strip().lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged


def _source_primary_url(spec: SourceSpec) -> str:
    return spec.external_url or spec.ctftime_url or spec.url


def _source_urls(spec: SourceSpec) -> tuple[str, str]:
    primary = spec.url.strip()
    ctftime_url = spec.ctftime_url.strip()
    external_url = spec.external_url.strip()

    if primary and not ctftime_url and not external_url:
        if "ctftime.org/writeup/" in primary:
            ctftime_url = primary
        else:
            external_url = primary

    return ctftime_url, external_url


def _make_source_writeup_id(spec: SourceSpec) -> str:
    if spec.writeup_id:
        return spec.writeup_id
    token = "|".join(part for part in [_source_primary_url(spec), spec.event, spec.task, spec.title] if part)
    digest = hashlib.sha1(token.encode("utf-8")).hexdigest()[:16]
    return f"seed-{digest}"


def _default_title_for_url(url: str) -> str:
    parsed = urlparse(url)
    stem = Path(parsed.path).stem.strip()
    return stem or parsed.netloc or url


def _default_event_for_url(url: str) -> str:
    parsed = urlparse(url)
    return parsed.netloc or url


def _parse_source_spec(item: SourceSpec | dict | str) -> SourceSpec | None:
    if isinstance(item, SourceSpec):
        return item if item.url.strip() else None

    if isinstance(item, str):
        url = item.strip()
        if not url:
            return None
        return SourceSpec(url=url, source="seed_url")

    if not isinstance(item, dict):
        return None

    primary_url = str(
        item.get("url")
        or item.get("source_url")
        or item.get("external_url")
        or item.get("ctftime_url")
        or ""
    ).strip()
    if not primary_url:
        return None

    raw_category = normalize_category(str(item.get("category", "") or ""))
    category = raw_category if raw_category != "unknown" else None
    raw_difficulty = normalize_difficulty(str(item.get("difficulty", "") or ""))
    difficulty = raw_difficulty if raw_difficulty != "unknown" else None

    return SourceSpec(
        url=primary_url,
        event=str(item.get("event", "") or "").strip(),
        task=str(item.get("task", "") or item.get("name", "") or "").strip(),
        title=str(item.get("title", "") or "").strip(),
        tags=tuple(_normalize_tags(item.get("tags", []))),
        category=category,
        difficulty=difficulty,
        year=_coerce_int(item.get("year")),
        team=str(item.get("team", "") or "").strip(),
        points=_coerce_int(item.get("points")),
        solves=_coerce_int(item.get("solves")),
        source=str(item.get("source", "seed_manifest") or "seed_manifest").strip(),
        ctftime_url=str(item.get("ctftime_url", "") or "").strip(),
        external_url=str(item.get("external_url", "") or "").strip(),
        writeup_id=str(item.get("writeup_id", "") or "").strip(),
    )


def infer_category(
    tags: list[str],
    title: str,
    event: str,
    content: str,
    url: str,
) -> str:
    scores = {category: 0 for category in _CATEGORY_KEYWORDS}

    for tag in tags:
        normalized = normalize_category(tag)
        if normalized != "unknown":
            scores[normalized] += 8
        tag_lower = tag.lower()
        for category, keywords in _CATEGORY_KEYWORDS.items():
            if any(keyword in tag_lower for keyword in keywords):
                scores[category] += 5

    corpus = "\n".join(part for part in [title, event, url, content[:6000]] if part).lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for keyword in keywords:
            if keyword in corpus:
                scores[category] += 1

    best_category, best_score = max(scores.items(), key=lambda item: item[1])
    return best_category if best_score > 0 else "unknown"


def infer_difficulty(points: int | None, solves: int | None, title: str, content: str) -> str:
    if points is not None and points > 0:
        if points >= 500:
            return "hard"
        if points >= 300:
            return "medium"
        return "easy"

    if solves is not None and solves > 0:
        if solves <= 20:
            return "insane"
        if solves <= 80:
            return "hard"
        if solves <= 250:
            return "medium"
        return "easy"

    lowered = f"{title}\n{content[:4000]}".lower()
    if any(token in lowered for token in ("insane", "brutal", "nightmare")):
        return "insane"
    if any(token in lowered for token in ("hard", "challenging", "expert")):
        return "hard"
    if any(token in lowered for token in ("medium", "intermediate")):
        return "medium"
    if any(token in lowered for token in ("easy", "baby", "warmup", "intro")):
        return "easy"
    return "unknown"


def _extract_points_and_solves(html: str) -> tuple[int | None, int | None]:
    points = None
    solves = None

    point_patterns = (
        r"Points\s*:?\s*</?[^>]*>\s*(\d+)",
        r"(\d+)\s*points",
    )
    solve_patterns = (
        r"Solves\s*:?\s*</?[^>]*>\s*(\d+)",
        r"(\d+)\s*solves",
    )

    for pattern in point_patterns:
        match = re.search(pattern, html or "", re.IGNORECASE)
        if match:
            points = int(match.group(1))
            break

    for pattern in solve_patterns:
        match = re.search(pattern, html or "", re.IGNORECASE)
        if match:
            solves = int(match.group(1))
            break

    return points, solves


def _parse_list_page(html: str) -> list[dict]:
    items: list[dict] = []
    for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        row_html = row.group(1)
        wid_m = re.search(r'href="/writeup/(\d+)"', row_html)
        event_m = re.search(r'href="/event/\d+"[^>]*>([^<]+)<', row_html)
        task_m = re.search(r'href="/task/\d+"[^>]*>([^<]+)<', row_html)
        if not wid_m:
            continue
        items.append({
            "writeup_id": wid_m.group(1),
            "writeup_path": f"/writeup/{wid_m.group(1)}",
            "event": event_m.group(1).strip() if event_m else "",
            "task": task_m.group(1).strip() if task_m else "",
        })
    return items


def _parse_detail_page(html: str) -> tuple[list[str], str | None]:
    tags = [t.strip().lower() for t in re.findall(r"label-info[^>]*>([^<]+)<", html)]

    ext_url: str | None = None
    for m in re.finditer(r'href="(https?://(?!ctftime\.org)[^"]+)"', html):
        url = m.group(1)
        if any(tok in url for tok in _SKIP_URL_TOKENS):
            continue
        ext_url = url
        break
    return tags, ext_url


def _clean_content(raw_md: str) -> str:
    lines: list[str] = []
    for line in raw_md.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith(")") and len(stripped) < 80:
            continue
        lines.append(line)

    out: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 2:
                out.append(line)
        else:
            blank_count = 0
            out.append(line)
    return "\n".join(out).strip()


def _merge_contents(
    ctftime_url: str,
    ctftime_content: str,
    ext_url: str | None,
    external_content: str,
) -> str:
    parts: list[str] = []
    if ctftime_content:
        parts.append(f"[CTFTIME]\n{ctftime_url}\n\n{ctftime_content}")
    if ext_url and external_content:
        parts.append(f"[EXTERNAL]\n{ext_url}\n\n{external_content}")
    return "\n\n---\n\n".join(parts).strip()


def _merge_source_contents(ctftime_url: str, ctftime_content: str, external_url: str, external_content: str) -> str:
    if ctftime_url:
        return _merge_contents(ctftime_url, ctftime_content, external_url or None, external_content)
    if external_url and external_content:
        return f"[EXTERNAL]\n{external_url}\n\n{external_content}".strip()
    return external_content or ctftime_content


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _build_record(
    *,
    writeup_id: str,
    event: str,
    task: str,
    title: str,
    tags: list[str],
    url: str,
    content: str,
    ctftime_url: str = "",
    external_url: str = "",
    source_url: str = "",
    source: str = "ctftime",
    points: int | None = None,
    solves: int | None = None,
) -> dict:
    category = infer_category(tags, title or task, event, content, url)
    year = _extract_year(event, title, ctftime_url, external_url, url, source_url, content[:3000])
    difficulty = infer_difficulty(points, solves, title or task, content)
    team = _extract_team_name(external_url or url)
    techniques = _detect_keywords(content, _TECHNIQUE_KEYWORDS)
    tools = _detect_keywords(content, _TOOL_KEYWORDS)

    return {
        "writeup_id": writeup_id,
        "event": event,
        "task": task,
        "tags": tags,
        "url": url,
        "source_url": source_url or url,
        "ctftime_url": ctftime_url,
        "external_url": external_url,
        "content": content,
        "title": title or task or event,
        "category": category,
        "difficulty": difficulty,
        "year": year,
        "team": team,
        "points": points or 0,
        "solves": solves or 0,
        "techniques": techniques,
        "tools": tools,
        "source": source,
    }


def _apply_source_overrides(record: dict, spec: SourceSpec, merged_tags: list[str]) -> dict:
    record["tags"] = merged_tags
    record["source"] = spec.source or record.get("source", "seed_manifest")
    record["source_url"] = spec.url or record.get("source_url") or record.get("url", "")
    if spec.event:
        record["event"] = spec.event
    if spec.task:
        record["task"] = spec.task
    if spec.title:
        record["title"] = spec.title
    if spec.category:
        record["category"] = normalize_category(spec.category)
    if spec.difficulty:
        record["difficulty"] = normalize_difficulty(spec.difficulty)
    if spec.year is not None and spec.year > 0:
        record["year"] = int(spec.year)
    if spec.team:
        record["team"] = spec.team
    if spec.points is not None:
        record["points"] = int(spec.points)
    if spec.solves is not None:
        record["solves"] = int(spec.solves)
    return record


def _load_last_completed_page(state_file: Path) -> int:
    if not state_file.exists():
        return 0

    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        return 0

    page = data.get("last_completed_page", 0)
    return page if isinstance(page, int) and page >= 0 else 0


def _save_last_completed_page(state_file: Path, page: int) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps({"last_completed_page": page}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_seen_state(output_file: Path) -> tuple[set[str], set[str]]:
    seen_ids: set[str] = set()
    seen_urls: set[str] = set()
    if not output_file.exists():
        return seen_ids, seen_urls

    for line in output_file.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except Exception:
            continue
        writeup_id = str(item.get("writeup_id", "") or "")
        if writeup_id:
            seen_ids.add(writeup_id)
        for key in ("url", "source_url", "external_url", "ctftime_url"):
            source_url = str(item.get(key, "") or "")
            if source_url:
                seen_urls.add(source_url)
    return seen_ids, seen_urls


def _mark_seen_urls(seen_urls: set[str], *urls: str) -> None:
    for url in urls:
        cleaned = str(url or "").strip()
        if cleaned:
            seen_urls.add(cleaned)


def _append_record(output_file: Path, record: dict) -> None:
    with output_file.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


async def _fetch_page(crawler: AsyncWebCrawler, url: str, cfg: CrawlerRunConfig) -> str:
    try:
        result = await crawler.arun(url, config=cfg)
        return result.html or ""
    except Exception as e:
        print(f"    [WARN] fetch {url[:80]} failed: {e}")
        return ""


async def _fetch_html_and_markdown(
    crawler: AsyncWebCrawler,
    url: str,
    cfg: CrawlerRunConfig,
) -> tuple[str, str]:
    try:
        result = await crawler.arun(url, config=cfg)
        html = result.html or ""
        markdown = result.markdown or ""
        return html, markdown
    except Exception as e:
        print(f"    [WARN] fetch {url[:80]} failed: {e}")
        return "", ""


async def smart_crawl_url(
    crawler: AsyncWebCrawler,
    url: str,
    cfg: CrawlerRunConfig,
) -> tuple[str, str]:
    html, markdown = await _fetch_html_and_markdown(crawler, url, cfg)
    return html, _clean_content(markdown)


async def _crawl_source_specs(
    crawler: AsyncWebCrawler,
    output_file: Path,
    source_specs: list[SourceSpec | dict | str],
    cfg: CrawlerRunConfig,
    seen_ids: set[str],
    seen_urls: set[str],
    wanted_categories: set[str],
) -> int:
    added = 0
    for raw_spec in source_specs:
        spec = _parse_source_spec(raw_spec)
        if spec is None:
            continue

        ctftime_url, external_url = _source_urls(spec)
        display_url = _source_primary_url(spec)
        writeup_id = _make_source_writeup_id(spec)
        if writeup_id in seen_ids:
            continue
        if any(url and url in seen_urls for url in (display_url, ctftime_url, external_url)):
            continue

        tags = list(spec.tags)
        points = spec.points
        solves = spec.solves
        ctftime_content = ""
        external_content = ""

        if ctftime_url:
            await asyncio.sleep(random.uniform(0.5, 1.1))
            det_html, ctftime_content = await smart_crawl_url(crawler, ctftime_url, cfg)
            parsed_tags, discovered_ext_url = _parse_detail_page(det_html)
            tags = _merge_unique(tags, parsed_tags)
            detail_points, detail_solves = _extract_points_and_solves(det_html)
            if points is None:
                points = detail_points
            if solves is None:
                solves = detail_solves
            if not external_url and discovered_ext_url:
                external_url = discovered_ext_url

        if external_url:
            await asyncio.sleep(random.uniform(0.5, 1.1))
            _, external_content = await smart_crawl_url(crawler, external_url, cfg)

        content = _merge_source_contents(ctftime_url, ctftime_content, external_url, external_content)
        if len(content) < MIN_CONTENT_CHARS:
            print(f"    跳过 seed {display_url[:60]} (内容太短 {len(content)} chars)")
            continue

        inferred_title = _first_heading(external_content or ctftime_content or content) or _default_title_for_url(display_url)
        record = _build_record(
            writeup_id=writeup_id,
            event=spec.event or _default_event_for_url(display_url),
            task=spec.task or inferred_title,
            title=spec.title or inferred_title,
            tags=tags,
            url=external_url or ctftime_url or display_url,
            content=content,
            ctftime_url=ctftime_url,
            external_url=external_url,
            source_url=display_url,
            source=spec.source,
            points=points,
            solves=solves,
        )
        record = _apply_source_overrides(record, spec, tags)
        if wanted_categories and record["category"] not in wanted_categories:
            print(f"    跳过 seed {record['title'][:40]} (category={record['category']})")
            continue

        _append_record(output_file, record)
        seen_ids.add(writeup_id)
        _mark_seen_urls(seen_urls, display_url, ctftime_url, external_url, record.get("url", ""))
        added += 1
        print(
            f"    OK  {record['source']}  {record['category']}  {record['difficulty']}  "
            f"{record['title'][:40]}  {display_url[:60]}"
        )
    return added


async def crawl(
    output_file: Path,
    pages: int = 10,
    state_file: Path | None = None,
    categories: list[str] | None = None,
    seed_urls: list[str] | None = None,
    source_specs: list[SourceSpec | dict | str] | None = None,
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    if state_file is None:
        state_file = output_file.with_suffix(".state.json")

    wanted_categories = {
        normalize_category(category)
        for category in (categories or [])
        if normalize_category(category) != "unknown"
    }
    explicit_sources: list[SourceSpec | dict | str] = [*(source_specs or []), *(seed_urls or [])]

    seen_ids, seen_urls = _load_seen_state(output_file)
    last_completed_page = _load_last_completed_page(state_file)
    start_page = last_completed_page + 1
    if start_page > pages and not explicit_sources:
        print(
            f"[*] state 已记录完成到第 {last_completed_page} 页；"
            f"当前 --pages={pages}，无需继续。"
        )
        return

    cfg_list = CrawlerRunConfig(wait_for="css:table", user_agent=UA)
    cfg_plain = CrawlerRunConfig(user_agent=UA, word_count_threshold=50)

    async with AsyncWebCrawler() as crawler:
        if pages > 0 and start_page <= pages:
            print(f"[*] 从第 {start_page} 页继续，目标页数上限 {pages}")
        for page in range(start_page, pages + 1):
            print(f"\n[*] 列表页 {page}/{pages}")
            html = await _fetch_page(crawler, f"{CTFTIME_BASE}/writeups?page={page}", cfg_list)
            items = _parse_list_page(html)
            print(f"    找到 {len(items)} 条")

            for item in items:
                wid = item["writeup_id"]
                if wid in seen_ids:
                    continue

                await asyncio.sleep(random.uniform(0.8, 1.8))

                detail_url = f"{CTFTIME_BASE}{item['writeup_path']}"
                det_html, ctftime_content = await smart_crawl_url(crawler, detail_url, cfg_plain)
                tags, ext_url = _parse_detail_page(det_html)
                points, solves = _extract_points_and_solves(det_html)

                external_content = ""
                if ext_url:
                    await asyncio.sleep(random.uniform(0.5, 1.2))
                    _, external_content = await smart_crawl_url(crawler, ext_url, cfg_plain)

                content = _merge_contents(detail_url, ctftime_content, ext_url, external_content)
                if len(content) < MIN_CONTENT_CHARS:
                    print(f"    跳过 {wid} (内容太短 {len(content)} chars)")
                    continue

                record = _build_record(
                    writeup_id=wid,
                    event=item.get("event", ""),
                    task=item.get("task", ""),
                    title=item.get("task", "") or item.get("event", ""),
                    tags=tags,
                    url=ext_url or detail_url,
                    content=content,
                    ctftime_url=detail_url,
                    external_url=ext_url or "",
                    source_url=ext_url or detail_url,
                    source="ctftime",
                    points=points,
                    solves=solves,
                )
                if wanted_categories and record["category"] not in wanted_categories:
                    print(f"    跳过 {wid} (category={record['category']})")
                    continue

                _append_record(output_file, record)
                seen_ids.add(wid)
                _mark_seen_urls(seen_urls, detail_url, ext_url or "", record["url"])
                print(
                    f"    OK  {wid}  {record['category']}  {record['difficulty']}  "
                    f"{len(content)} chars  {(ext_url or detail_url)[:60]}"
                )

            _save_last_completed_page(state_file, page)

        if explicit_sources:
            print(f"\n[*] 处理 source manifests / seed URLs: {len(explicit_sources)} 条")
            await _crawl_source_specs(
                crawler,
                output_file,
                explicit_sources,
                cfg_plain,
                seen_ids,
                seen_urls,
                wanted_categories,
            )

    category_note = f" categories={sorted(wanted_categories)}" if wanted_categories else ""
    print(
        f"\n完成，共 {len(seen_ids)} 条 writeup，保存至 {output_file}"
        f"；页码状态保存至 {state_file}{category_note}"
    )
