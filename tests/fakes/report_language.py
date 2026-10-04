"""One rule about report language, stated once.

The route evidence report must never assert a success rate, a confidence, or a
recommendation. It *may* name those concepts in order to refuse them -- "不给出
成功率", "not a confidence" -- because a report forbidden from ever saying the
word cannot tell the reader it does not do this.

A substring scan would forbid exactly the refusal sentences. So the check is
per line, and a line counts as a violation only when it mentions the concept
without a negator. That is a heuristic about Chinese and English rather than a
parser of either, which is the honest limit of a check like this: it is here to
catch a future edit that writes "推荐路线 A", not to prove a theorem.

Shared by `tests/unit/test_route_report_evidence.py` and
`tests/integration_offline/test_route_e2e.py`, because two copies of a refusal
rule is one copy too many.
"""

from __future__ import annotations

#: Words and phrases that only ever appear as a verdict in this project's
#: report. English and Chinese both; the report is Chinese and the code
#: comments that leak into it are not.
FORBIDDEN_VERDICTS = (
    "成功率", "把握", "很可能", "推荐", "最佳", "最优", "首选",
    "confidence", "probability", "success rate", "recommended",
    "better", "best route",
)

#: Markers that turn a mention into a refusal. A line carrying one of these is
#: declining the concept rather than asserting it.
_NEGATIONS = ("不", "无", "未", "禁", "拒绝", "never", "no ", "not ", "no_")


def asserts_verdict(text: str, word: str) -> bool:
    """Does any line *assert* `word`, rather than decline to?

    Returns True only when a line mentions the word and carries no negator. The
    unit of the check is the line, because a negation rarely crosses a line
    break in the reports this renderer produces.
    """
    needle = word.lower()
    for line in text.lower().splitlines():
        if needle not in line:
            continue
        if not any(marker in line for marker in _NEGATIONS):
            return True
    return False


def all_lines(text: str) -> list[str]:
    """Split into the lines a reader sees. For callers that want their own scan."""
    return text.splitlines()
