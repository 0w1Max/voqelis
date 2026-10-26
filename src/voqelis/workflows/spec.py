"""Allowlisted, executable-code-free definitions of voice/table workflows."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Literal

FieldKind = Literal["text", "list"]
ExportKind = Literal["docx", "pdf", "pdf_mobile"]
_ALLOWED_EXPORTS = frozenset({"docx", "pdf", "pdf_mobile"})
_KEY = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_MAX_SCHEMA_BYTES = 32_000


def _nonempty(value: Any, *, name: str, max_chars: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_chars:
        raise ValueError(f"{name} must be nonempty text up to {max_chars} chars")
    return value.strip()


def _key(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(f"{name} must be a safe ASCII identifier")
    return value


def _mapping(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != allowed:
        raise ValueError(f"Expected exactly fields: {sorted(allowed)}")
    if not all(isinstance(k, str) for k in value):
        raise ValueError("Object keys must be strings")
    return value


@dataclass(frozen=True, slots=True)
class FieldSpec:
    key: str
    label: str
    prompt: str
    kind: FieldKind

    def __post_init__(self) -> None:
        _key(self.key, name="field.key")
        _nonempty(self.label, name="field.label", max_chars=80)
        _nonempty(self.prompt, name="field.prompt", max_chars=240)
        if not isinstance(self.kind, str) or self.kind not in {"text", "list"}:
            raise ValueError("Only text and list fields are supported in V0")

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> FieldSpec:
        data = _mapping(value, {"key", "label", "prompt", "kind"})
        return cls(
            key=data["key"], label=data["label"],
            prompt=data["prompt"], kind=data["kind"],
        )

    def to_mapping(self) -> dict[str, str]:
        return {
            "key": self.key, "label": self.label,
            "prompt": self.prompt, "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class WorkflowSpec:
    id: str
    version: int
    title: str
    fields: tuple[FieldSpec, ...]
    exports: tuple[ExportKind, ...]

    def __post_init__(self) -> None:
        _key(self.id, name="workflow.id")
        if type(self.version) is not int or not 1 <= self.version <= 1_000_000:
            raise ValueError("Workflow version must be a positive integer")
        _nonempty(self.title, name="workflow.title", max_chars=100)
        if not isinstance(self.fields, tuple) or not 1 <= len(self.fields) <= 12:
            raise ValueError("Workflow must have between 1 and 12 fields")
        if not all(isinstance(f, FieldSpec) for f in self.fields):
            raise ValueError("Invalid field specification")
        ids = [f.key for f in self.fields]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate field keys")
        if (
            not isinstance(self.exports, tuple)
            or len(self.exports) > 3
            or any(
                not isinstance(e, str) or e not in _ALLOWED_EXPORTS
                for e in self.exports
            )
            or len(set(self.exports)) != len(self.exports)
        ):
            raise ValueError("Invalid or duplicate export")
        if len(self.canonical_json().encode("utf-8")) > _MAX_SCHEMA_BYTES:
            raise ValueError("Workflow specification too large")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.id, "version": self.version, "title": self.title,
            "fields": [f.to_mapping() for f in self.fields],
            "exports": list(self.exports),
        }

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_mapping(), ensure_ascii=False,
            separators=(",", ":"), sort_keys=True,
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> WorkflowSpec:
        data = _mapping(value, {"id", "version", "title", "fields", "exports"})
        if (
            not isinstance(data["fields"], list)
            or not 1 <= len(data["fields"]) <= 12
            or not isinstance(data["exports"], list)
        ):
            raise ValueError("Invalid schema collections")
        return cls(
            id=data["id"], version=data["version"], title=data["title"],
            fields=tuple(FieldSpec.from_mapping(f) for f in data["fields"]),
            exports=tuple(data["exports"]),
        )

    @classmethod
    def from_json(cls, text: str) -> WorkflowSpec:
        if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_SCHEMA_BYTES:
            raise ValueError("Workflow specification is too large")
        value = json.loads(text)
        return cls.from_mapping(value)
