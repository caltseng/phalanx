"""Bounded display text shared by native clients and the operator board."""

import re


SECRET = re.compile(
    r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"
    r"|(?i:\b(?:bearer|basic)\s+)\S+"
    r"""|(?i:\b(?:[a-z][a-z0-9]*[_-])*(?:token|secret|password|lease|(?:api|secret)[_-]?key)\b"""
    r"""["']?\s*[:=]\s*)(?:"[^"]*"|'[^']*'|\S+)"""
)


def reason_text(text, limit):
    text = SECRET.sub("[redacted]", " ".join(text.split()))
    return text if len(text) <= limit else text[:limit - 1] + "\N{HORIZONTAL ELLIPSIS}"


def short_title(goal, limit=80):
    """One display line from the goal: its first sentence, cut at a word boundary."""
    line = next((line for line in goal.splitlines() if line.strip()), "").strip().lstrip("#*-> ").strip()
    sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0].strip() or line or "Untitled"
    if len(sentence) <= limit:
        return sentence
    cut = sentence[:limit - 1]
    return (cut.rsplit(None, 1)[0] if " " in cut.strip() else cut).rstrip(" ,;:-") + "\N{HORIZONTAL ELLIPSIS}"
