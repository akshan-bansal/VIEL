"""FIX tag=value parser with structural validation (spec §5, Contract A).

The parser never discards information: every field is retained in its original
order, unknown tags are kept, and structural problems (header order, body
length, checksum, duplicate tags) are reported as issues rather than repaired.

Repeating groups are not interpreted; a repeated tag is reported as
``DUPLICATE_TAG`` so that a message needing group support is visible rather
than silently mis-parsed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SOH = "\x01"


@dataclass(frozen=True)
class ParseIssue:
    code: str
    detail: str
    tag: int | None = None


@dataclass(frozen=True)
class FixMessage:
    fields: tuple[tuple[int, str], ...]
    delimiter: str
    issues: tuple[ParseIssue, ...] = ()
    _index: dict[int, str] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        index: dict[int, str] = {}
        for tag, value in self.fields:
            index.setdefault(tag, value)
        object.__setattr__(self, "_index", index)

    def get(self, tag: int) -> str | None:
        return self._index.get(tag)

    def get_all(self, tag: int) -> list[str]:
        return [v for t, v in self.fields if t == tag]

    def has(self, tag: int) -> bool:
        return tag in self._index

    @property
    def begin_string(self) -> str | None:
        return self.get(8)

    @property
    def msg_type(self) -> str | None:
        return self.get(35)

    @property
    def seq_num(self) -> int | None:
        value = self.get(34)
        if value is None or not value.isdigit():
            return None
        return int(value)

    @property
    def is_structurally_valid(self) -> bool:
        return not self.issues

    def tags(self) -> list[int]:
        return [t for t, _ in self.fields]


class FixDictionary:
    """Protocol dictionary (config/fix_dictionary.yaml)."""

    def __init__(self, raw: dict[str, Any]):
        self.raw = raw
        self.version: str = str(raw["dictionary_version"])
        self._tags: dict[int, str] = {int(k): str(v) for k, v in raw["tags"].items()}
        self._enums: dict[int, dict[str, str]] = {
            int(tag): {str(code): str(name) for code, name in values.items()}
            for tag, values in raw.get("enums", {}).items()
        }
        self._protocols: dict[str, dict[str, Any]] = raw.get("protocols", {})
        self._custom: dict[str, dict[int, str]] = {
            broker: {int(k): str(v) for k, v in tags.items()}
            for broker, tags in (raw.get("custom_tags") or {}).items()
        }
        self.session_message_types: frozenset[str] = frozenset(
            str(m) for m in raw.get("session_message_types", [])
        )
        lineage = raw.get("lineage") or {}
        self.parent_order_id_tag: int | None = lineage.get("parent_order_id_tag")
        self.client_order_id_tag: int | None = lineage.get("client_order_id_tag")

    def tag_name(self, tag: int, source_system: str | None = None) -> str | None:
        if tag in self._tags:
            return self._tags[tag]
        if source_system is not None:
            return self._custom.get(source_system, {}).get(tag)
        return None

    def is_known(self, tag: int, source_system: str | None = None) -> bool:
        return self.tag_name(tag, source_system) is not None

    def enum_values(self, tag: int) -> dict[str, str] | None:
        return self._enums.get(tag)

    def enum_name(self, tag: int, code: str) -> str | None:
        values = self._enums.get(tag)
        return None if values is None else values.get(code)

    def supports_protocol(self, protocol: str | None) -> bool:
        return protocol in self._protocols

    def required_tags(self, protocol: str, msg_type: str) -> list[int]:
        proto = self._protocols.get(protocol, {})
        return [int(t) for t in proto.get("required", {}).get(msg_type, [])]

    def correction_semantics(self, protocol: str) -> str:
        return str(self._protocols.get(protocol, {}).get("exec_correction_semantics", "EXEC_TYPE"))


def _detect_delimiter(payload: str) -> str:
    return SOH if SOH in payload else "|"


def compute_checksum(body_through_last_delim: bytes) -> str:
    return f"{sum(body_through_last_delim) % 256:03d}"


def parse_fix(payload: str) -> FixMessage:
    """Parse a FIX message. ``|`` is accepted as a display substitute for SOH."""
    issues: list[ParseIssue] = []
    if not payload:
        return FixMessage(fields=(), delimiter=SOH, issues=(ParseIssue("EMPTY", "empty payload"),))
    delimiter = _detect_delimiter(payload)
    parts = payload.split(delimiter)
    if parts and parts[-1] == "":
        parts = parts[:-1]
    else:
        issues.append(ParseIssue("MISSING_TRAILING_DELIMITER", "message does not end with a delimiter"))

    fields: list[tuple[int, str]] = []
    seen: set[int] = set()
    for position, part in enumerate(parts):
        if "=" not in part:
            issues.append(ParseIssue("MALFORMED_FIELD", f"field {position} has no '=': {part!r}"))
            continue
        tag_text, value = part.split("=", 1)
        if not tag_text.isdigit():
            issues.append(ParseIssue("NON_NUMERIC_TAG", f"field {position} tag {tag_text!r}"))
            continue
        tag = int(tag_text)
        if tag in seen:
            issues.append(ParseIssue("DUPLICATE_TAG", f"tag {tag} repeated (repeating groups unsupported)", tag))
        seen.add(tag)
        fields.append((tag, value))

    tags = [t for t, _ in fields]
    if tags[:3] != [8, 9, 35]:
        issues.append(ParseIssue("HEADER_ORDER", f"first tags must be 8, 9, 35; got {tags[:3]}"))
    if not tags or tags[-1] != 10:
        issues.append(ParseIssue("MISSING_CHECKSUM", "last field must be CheckSum(10)"))
    else:
        issues.extend(_verify_length_and_checksum(payload, delimiter, dict(fields)))

    return FixMessage(fields=tuple(fields), delimiter=delimiter, issues=tuple(issues))


def _verify_length_and_checksum(payload: str, delimiter: str, index: dict[int, str]) -> list[ParseIssue]:
    issues: list[ParseIssue] = []
    wire = payload.replace(delimiter, SOH) if delimiter != SOH else payload
    data = wire.encode("utf-8")
    checksum_pos = data.rfind(f"{SOH}10=".encode()) + 1
    if checksum_pos <= 0:
        return [ParseIssue("MISSING_CHECKSUM", "CheckSum(10) not found")]
    body_length_field = f"9={index.get(9, '')}{SOH}".encode()
    start = data.find(body_length_field)
    if start < 0:
        issues.append(ParseIssue("BODY_LENGTH_MISMATCH", "BodyLength(9) missing"))
    else:
        actual_length = checksum_pos - (start + len(body_length_field))
        declared = index.get(9, "")
        if not declared.isdigit() or int(declared) != actual_length:
            issues.append(
                ParseIssue("BODY_LENGTH_MISMATCH", f"declared {declared!r}, actual {actual_length}", 9)
            )
    expected = compute_checksum(data[:checksum_pos])
    if index.get(10) != expected:
        issues.append(ParseIssue("CHECKSUM_MISMATCH", f"declared {index.get(10)!r}, computed {expected}", 10))
    return issues


def build_fix(
    body_fields: list[tuple[int, str]],
    *,
    begin_string: str = "FIX.4.4",
    delimiter: str = SOH,
) -> str:
    """Build a FIX message with correct BodyLength(9) and CheckSum(10).

    ``body_fields`` must start with MsgType(35). Used by the simulator and tests.
    """
    if not body_fields or body_fields[0][0] != 35:
        raise ValueError("body_fields must start with MsgType (35)")
    body = "".join(f"{tag}={value}{SOH}" for tag, value in body_fields)
    head = f"8={begin_string}{SOH}9={len(body.encode('utf-8'))}{SOH}"
    without_checksum = head + body
    checksum = compute_checksum(without_checksum.encode("utf-8"))
    message = f"{without_checksum}10={checksum}{SOH}"
    return message if delimiter == SOH else message.replace(SOH, delimiter)
