"""Bounded validation of the bundled, supported AvailKit schema subset."""

import json
import re
import unicodedata
from datetime import datetime
from importlib.resources import files
from typing import Any, NoReturn

from .errors import AvailKitError

SCHEMAS: dict[str, dict[str, Any]] = json.loads(files("availkit").joinpath("schemas.json").read_text())
OPERATIONS: dict[str, dict[str, Any]] = json.loads(files("availkit").joinpath("operations.json").read_text())


def fail(code: str = "invalid_input") -> NoReturn:
    raise AvailKitError("input", code)


def instant(value: Any) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(
        r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,9})?Z", value
    ):
        raise ValueError
    return datetime.fromisoformat(value[:-1] + "+00:00")


def identifier(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{32}", value):
        fail("invalid_id")
    return value


def credential(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{42}[AEIMQUYcgkosw048]", value):
        fail("invalid_credential")
    return value


def intent_key(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[\x20-\x7e]{16,128}", value) or value.strip() != value:
        fail("invalid_idempotency_key")
    return value


def text(value: Any, maximum: int = 160) -> bool:
    return (isinstance(value, str) and bool(value.strip()) and len(value) <= maximum
            and not any(unicodedata.category(c) == "Cc" for c in value))


def validate(value: Any, schema: dict[str, Any] | str, depth: int = 0) -> bool:
    if depth > 20:
        return False
    if isinstance(schema, str):
        schema = SCHEMAS[schema]
    if "$ref" in schema:
        return validate(value, schema["$ref"].rsplit("/", 1)[-1], depth + 1)
    if "allOf" in schema and not all(validate(value, s, depth + 1) for s in schema["allOf"]):
        return False
    if "anyOf" in schema and not any(validate(value, s, depth + 1) for s in schema["anyOf"]):
        return False
    if "oneOf" in schema and sum(validate(value, s, depth + 1) for s in schema["oneOf"]) != 1:
        return False
    if "const" in schema and (type(value) != type(schema["const"]) or value != schema["const"]):
        return False
    if "enum" in schema and not any(type(value) == type(x) and value == x for x in schema["enum"]):
        return False
    kinds = schema.get("type", [])
    kinds = [kinds] if isinstance(kinds, str) else kinds
    checks = {"null": lambda: value is None, "object": lambda: type(value) is dict,
              "array": lambda: type(value) is list, "string": lambda: type(value) is str,
              "integer": lambda: type(value) is int, "boolean": lambda: type(value) is bool,
              "number": lambda: type(value) in (int, float)}
    if kinds and not any(checks[k]() for k in kinds):
        return False
    if type(value) is dict:
        props = schema.get("properties", {})
        if not all(k in value for k in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and value.keys() - props.keys():
            return False
        return all(k not in props or validate(v, props[k], depth + 1) for k, v in value.items())
    if type(value) is list:
        return (schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 10000)
                and all(validate(v, schema.get("items", {}), depth + 1) for v in value))
    if type(value) is str:
        if not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 8192):
            return False
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            return False
        if schema.get("format") == "date-time":
            try:
                instant(value)
            except (ValueError, OverflowError):
                return False
    if type(value) in (int, float):
        return schema.get("minimum", -9007199254740991) <= value <= schema.get("maximum", 9007199254740991)
    return True


def input_body(value: Any, schema: dict[str, Any]) -> bytes:
    if not validate(value, schema):
        fail()
    try:
        # Snapshot to immutable bytes before touching the network or caller data.
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        payload = None
    if payload is None:
        fail("invalid_json")
    if len(payload) > 16384:
        fail("body_too_large")
    name = schema.get("$ref", "").rsplit("/", 1)[-1]
    data = json.loads(payload)
    if name in ("ResourceInput", "OfferingInput"):
        if not text(data["name"]):
            fail()
        if name == "ResourceInput":
            # The server owns IANA timezone validation; no platform tzdata
            # dependency or false rejection on systems lacking a local database.
            if not text(data["timeZone"], 100):
                fail("invalid_time_zone")
        else:
            identifier(data["resourceId"])
    if name in ("BookingInput", "PublicBookingInput"):
        identifier(data["offeringId"])
    if name == "PublicBookingInput":
        input_body(data["contact"], {"$ref": "#/components/schemas/CustomerInput"})
    if name == "CustomerInput":
        if not text(data["displayName"]):
            fail("invalid_customer")
        if data.get("email") is not None and (not text(data["email"], 254) or not re.fullmatch(r"[^@\s]+@[^@\s]+", data["email"])):
            fail("invalid_customer")
        if data.get("phone") is not None and not text(data["phone"], 32):
            fail("invalid_customer")
    if name in ("BookingPageInput",) and not text(data["title"]):
        fail()
    if name == "Hours":
        windows = data["windows"]
        for i, w in enumerate(windows):
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", w["start"]) or not re.fullmatch(r"(?:(?:[01]\d|2[0-3]):[0-5]\d|24:00)", w["end"]) or w["end"] <= w["start"]:
                fail("invalid_hours")
            if any(v["weekday"] == w["weekday"] and v["start"] < w["end"] and w["start"] < v["end"] for v in windows[:i]):
                fail("invalid_hours")
    return payload
