"""Read the verdict line in a delegated review; keep qualified opinions distinct."""
import re


VERDICT_LINE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]+)?[*_`]*VERDICT:[ \t]*([^\r\n]*)", re.I | re.M)


def review_verdict(report: str) -> str:
    match = VERDICT_LINE.search(report)
    if match is None or match.start() >= 2000:
        return "unclear"
    value = " ".join(match[1].strip(" \t*_`.。").lower().split())
    if value in ("approved", "changes requested"):
        return value
    if re.match(r"^changes requested\s*[:—–-]", value):
        return "changes requested"
    return "unclear"
