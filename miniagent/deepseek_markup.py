"""Keep DeepSeek control markers out of chat. Only the native tool channel may execute."""
import re

from .provider_http import ProviderError

_PREFIXES = tuple("<" + slash + bar + "DSML" for slash in ("", "/") for bar in ("｜", "｜｜", "|", "||"))
_MARK = re.compile(r"</?[｜|]{1,2}DSML")


def public_prefix(text):
    """Drop control text; withhold ambiguous suffixes until the next stream chunk."""
    mark = _MARK.search(text)
    if mark:
        return text[:mark.start()], "", True
    for length in range(min(8, len(text)), 0, -1):
        suffix = text[-length:]
        if any(p.startswith(suffix) for p in _PREFIXES):
            return text[:-length], suffix, False
    return text, "", False


def historic_text(text):
    mark = _MARK.search(text)
    if not mark:
        return text
    return (text[:mark.start()].rstrip() + "\n\n旧版回复包含工具协议文本。操作结果请以任务回执为准。").strip()


def public_content(content, native):
    marker = _MARK.search(content or "")
    if not marker:
        return content
    if native:
        return content[:marker.start()].rstrip() or None
    # An HTTP API client has no token IDs to distinguish special tokens from a quotation.
    raise ProviderError("模型把工具标记放进了回复正文，没有执行这次调用。", code="invalid_tool_markup")
