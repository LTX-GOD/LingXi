from __future__ import annotations

import re
from typing import Any

_FLAG_PROGRESS_PATTERN = re.compile(r"Flag\s*进度:\s*(\d+)\s*/\s*(\d+)", re.IGNORECASE)


def parse_flag_progress(content: str) -> tuple[int, int] | None:
    text = str(content or "")
    match = _FLAG_PROGRESS_PATTERN.search(text)
    if not match:
        return None
    got = int(match.group(1))
    count = max(1, int(match.group(2)))
    return max(0, got), count


def apply_main_battle_score_progress(
    *,
    content: str,
    submitted_flag: str | None,
    current_flag: str | None,
    scored_flags: list[str],
    flags_scored_count: int,
    expected_flag_count: int,
    observed_flag_got_count: int | None = None,
    observed_flag_count: int | None = None,
) -> dict[str, Any]:
    next_scored_flags = list(scored_flags)
    candidate_flag = (submitted_flag or current_flag or "").strip()
    if candidate_flag and candidate_flag.startswith("flag{") and candidate_flag.endswith("}") and candidate_flag not in next_scored_flags:
        next_scored_flags.append(candidate_flag)

    parsed = parse_flag_progress(content)
    if parsed is not None:
        parsed_got, parsed_count = parsed
    else:
        parsed_got, parsed_count = 0, 0

    next_expected_flag_count = max(1, int(expected_flag_count or 1), int(observed_flag_count or 0), parsed_count)
    next_flags_scored_count = max(
        int(flags_scored_count or 0) + 1,
        len(next_scored_flags),
        int(observed_flag_got_count or 0),
        parsed_got,
    )

    challenge_completed = next_flags_scored_count >= next_expected_flag_count
    continue_message = None
    if not challenge_completed:
        continue_message = (
            f"✅ 当前主战场题目刚刚有新 Flag 得分，已累计 {next_flags_scored_count}/{next_expected_flag_count}。"
            "题目尚未完成，继续寻找剩余 Flag，不要停止。"
        )

    return {
        "flag": candidate_flag or current_flag or content,
        "scored_flags": next_scored_flags,
        "flags_scored_count": next_flags_scored_count,
        "expected_flag_count": next_expected_flag_count,
        "last_submission_scored": True,
        "challenge_completed": challenge_completed,
        "is_finished": challenge_completed,
        "continue_message": continue_message,
    }


def compute_main_battle_solver_outcome(
    *,
    initial_flag_got_count: int,
    final_flags_scored_count: int,
    final_expected_flag_count: int,
    is_finished: bool,
    explicit_challenge_completed: bool,
) -> tuple[bool, bool]:
    challenge_completed = bool(
        explicit_challenge_completed
        or is_finished
        or int(final_flags_scored_count or 0) >= max(1, int(final_expected_flag_count or 1))
    )
    progress_made = int(final_flags_scored_count or 0) > int(initial_flag_got_count or 0)
    success = bool(challenge_completed or progress_made)
    return success, challenge_completed


def should_mark_challenge_solved(*, success: bool, challenge_completed: bool) -> bool:
    return bool(success and challenge_completed)


def should_clear_stale_solved(
    *,
    locally_solved: bool,
    flag_got_count: int,
    flag_count: int,
    instance_status: str,
) -> bool:
    return bool(
        locally_solved
        and int(flag_count or 0) > 0
        and int(flag_got_count or 0) < int(flag_count or 0)
        and str(instance_status or "").strip().lower() == "running"
    )
