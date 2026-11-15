"""The checked-in design example is the cross-module contract fixture."""
from __future__ import annotations

import json
import re
from pathlib import Path


def example_data() -> dict:
    path = Path(__file__).resolve().parents[1] / "docs" / "m0-01-computer-maintenance-example.md"
    data: dict = {}
    for block in re.findall(r"```json\n(.*?)\n```", path.read_text(encoding="utf-8"), re.S):
        part = json.loads(block)
        if data.keys() & part.keys():
            raise AssertionError("duplicate example sections")
        data.update(part)
    return data


def example_record(collection: str, identifier: str | None = None) -> dict:
    records = example_data()[collection]
    if identifier is None:
        return records[0]
    for record in records:
        if identifier in record.values():
            return record
    raise KeyError(identifier)
