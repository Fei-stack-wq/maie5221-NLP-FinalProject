"""Local Studio settings; provider credentials never appear in API responses."""
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import replace
from .studio import StudioError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def verify(base_url, key):
    request = urllib.request.Request(base_url.rstrip("/") + "/models",
                                     headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=12):
            return True
    except urllib.error.HTTPError as exc:
        exc.close()
        if exc.code in (401, 403):
            raise StudioError("DeepSeek 拒绝了此密钥。") from None
    except OSError:
        pass
    return None


def save_key(studio, value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,300}", value):
        raise StudioError("密钥格式无效。")
    verified = verify(studio.settings.base_url, value)
    with studio._lock:
        path = studio.env_file
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines = [line for line in lines if not re.match(r"\s*(?:export\s+)?DEEPSEEK_API_KEY\s*=", line)]
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
            tmp = Path(f.name)
            try:
                f.write("\n".join([*lines, "DEEPSEEK_API_KEY=" + value]) + "\n")
                f.close()
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
        studio.settings = replace(studio.settings, api_key=value)
    return {"has_key": True, "verified": verified, "environment_override": bool(os.getenv("DEEPSEEK_API_KEY"))}
