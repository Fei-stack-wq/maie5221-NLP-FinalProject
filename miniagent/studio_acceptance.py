"""User-supplied, declarative checks on the delivered workspace."""
import json
import re

from .acceptance import _read_regular, _relative_name, accept_files


def build_checks(paths, criteria):
    if not isinstance(paths, list) or not isinstance(criteria, list) or len(criteria) > 32:
        raise ValueError("验收规则最多 32 项。")
    if not paths and not criteria:
        return None
    # Studio's path-only field promises the user a nonempty file. Length,
    # encoding and substance are separate from existence.
    delivered = accept_files(*paths, min_bytes=1, min_lines=0) if paths else None
    if not criteria:
        return delivered
    checks = []
    for row in criteria:
        if not isinstance(row, dict):
            raise ValueError("验收项须为对象。")
        kind = row.get("kind")
        spec = {"contains": {"text"}, "sha256": {"value"}, "json_equals": {"pointer", "expected"}}
        if not isinstance(kind, str) or kind not in spec or set(row) != {"kind", "path"} | spec[kind]:
            raise ValueError("支持 contains、sha256 和 json_equals 验收项。")
        if not isinstance(row["path"], str):
            raise ValueError("验收项需要相对文件名。")
        path = _relative_name(row["path"])
        if any(part.startswith(".") for part in path.split("/")):
            raise ValueError("验收项不能引用隐藏文件。")
        if kind == "contains":
            if type(row["text"]) is not str or not 0 < len(row["text"]) <= 8192:
                raise ValueError("请填写要匹配的文本。")
            checks.append((path, accept_files(path, min_bytes=0, min_lines=0, contains={path:row["text"]})))
        elif kind == "sha256":
            checks.append((path, accept_files(path, min_bytes=0, min_lines=0, sha256={path:row["value"]})))
        else:
            pointer = row["pointer"]
            if type(pointer) is not str or (pointer and not pointer.startswith("/")) or re.search(r"~(?![01])", pointer):
                raise ValueError("JSON 字段请使用 JSON Pointer，如 /rooms/A/hours。")
            parts = [part.replace("~1", "/").replace("~0", "~") for part in pointer.split("/")[1:]]
            expected = json.dumps(row["expected"], sort_keys=True, allow_nan=False, ensure_ascii=False)

            def matches(root, path=path, parts=parts, expected=expected):
                raw = _read_regular(root, path)
                if raw is None or len(raw) > 8 * 1024 * 1024:
                    return False, "缺少 JSON 文件或文件过大"
                try:
                    data = json.loads(raw)
                    for key in parts:
                        if isinstance(data, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
                            data = data[int(key)]
                        else:
                            data = data[key]
                    equal = json.dumps(data, sort_keys=True, allow_nan=False, ensure_ascii=False) == expected
                except (KeyError, IndexError, ValueError, TypeError):
                    return False, "JSON 或字段无效"
                return equal, "字段符合期望" if equal else "字段与期望不符"
            checks.append((path, matches))
    if delivered:
        checks.insert(0, ("交付文件", delivered))

    def verify(root):
        failures = []
        for label, check in checks:
            passed, reason = check(root)
            if not passed:
                failures.append(f"{label} · {reason}")
        return not failures, "；".join(failures) if failures else f"{len(checks)} 项交付检查通过"
    return verify
