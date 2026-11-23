"""Local Studio settings; provider credentials never appear in API responses."""
import os
import re
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from dataclasses import replace
from .config import SUPPORTED_MODELS
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


def _write_env(path: Path, values: dict[str, str]) -> None:
    """Replace ``values`` in the .env atomically (owner-only file); an empty value removes the line."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    for name in values:
        lines = [line for line in lines if not re.match(rf"\s*(?:export\s+)?{name}\s*=", line)]
    lines += [f"{name}={value}" for name, value in values.items() if value]
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write("\n".join(lines) + "\n")
            f.close()
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)


def save_key(studio, value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._~-]{8,300}", value):
        raise StudioError("密钥格式无效。")
    verified = verify(studio.settings.base_url, value)
    with studio._lock:
        _write_env(studio.env_file, {"DEEPSEEK_API_KEY": value})
        studio.settings = replace(studio.settings, api_key=value)
    return {"has_key": True, "verified": verified, "environment_override": bool(os.getenv("DEEPSEEK_API_KEY"))}


MODEL_SETTINGS = {"model": "DEEPSEEK_MODEL", "reviewer_model": "MA_REVIEWER_MODEL",
                  "reviewer_thinking": "MA_REVIEWER_THINKING"}


def model_settings(settings) -> dict:
    return {"model": settings.model, "thinking": settings.thinking, "reviewer_model": settings.reviewer_model,
            "reviewer_thinking": settings.reviewer_thinking, "models": sorted(SUPPORTED_MODELS)}


def save_models(studio, body):
    """Studio-wide model choice for new tasks; review work may use another model or thinking mode."""
    if not isinstance(body, dict) or not body or body.keys() - MODEL_SETTINGS.keys():
        raise StudioError("Expected model, reviewer_model and/or reviewer_thinking")
    if any(not isinstance(value, str) for value in body.values()):
        raise StudioError("Model settings must be text")
    try:
        updated = replace(studio.settings, **body)
    except (TypeError, ValueError) as exc:
        raise StudioError(str(exc)) from None
    with studio._lock:
        _write_env(studio.env_file, {MODEL_SETTINGS[name]: value for name, value in body.items()})
        studio.settings = replace(studio.settings, **body)
    overridden = [MODEL_SETTINGS[name] for name in body if os.getenv(MODEL_SETTINGS[name])]
    return {**model_settings(updated), "environment_override": overridden}
