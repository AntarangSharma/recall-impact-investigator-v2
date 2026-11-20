from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass

from pydantic import BaseModel

MAX_BYTES = 256_000
MAX_ROWS = 400
REQUIRED = {"stock_id", "brand", "model", "location"}
OPTIONAL = {"date_code", "serial_number"}
IDENTIFIER = re.compile(r"[A-Za-z0-9]+(?:[./_-][A-Za-z0-9]+)*")
UNRECORDED_MODELS = {
    "N/A",
    "N.A.",
    "NA",
    "NONE",
    "NOT APPLICABLE",
    "NOT AVAILABLE",
    "NOT PROVIDED",
    "NULL",
    "TBD",
    "UNK",
    "UNKNOWN",
}
FORBIDDEN = chr(0x2014)


class Stock(BaseModel):
    stock_id: str
    brand: str
    model: str
    location: str
    date_code: str | None = None
    serial_number: str | None = None


@dataclass(frozen=True)
class Inventory:
    units: list[Stock]
    errors: list[str]


def identifier(value: str | None) -> tuple[str, str]:
    if value is None:
        return "missing", ""
    value = value.strip()
    if not value:
        return "empty", ""
    if len(value) > 64 or not IDENTIFIER.fullmatch(value):
        return "malformed", ""
    return "valid", value.upper()


def model_identifier(value: str | None) -> tuple[str, str]:
    if value is not None and value.strip().upper() in UNRECORDED_MODELS:
        return "missing", ""
    kind, model = identifier(value)
    # Slash-separated values in this notice catalog may combine a model and its alias.
    if kind == "valid" and "/" in model:
        return "ambiguous_model", ""
    return kind, model


def parse_inventory(raw: bytes) -> Inventory:
    if len(raw) > MAX_BYTES:
        return Inventory([], ["The CSV file exceeds 256 KB."])
    try:
        rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
        fields = rows.fieldnames or []
        if len(fields) != len(set(fields)) or not REQUIRED <= set(fields) <= REQUIRED | OPTIONAL:
            return Inventory(
                [],
                [
                    "Use stock_id, brand, model and location, with optional date_code and serial_number."
                ],
            )
        units: list[Stock] = []
        errors: list[str] = []
        seen: dict[str, int] = {}
        duplicates: set[str] = set()
        for count, row in enumerate(rows, start=1):
            line = rows.line_num
            if count > MAX_ROWS:
                return Inventory([], [f"Use at most {MAX_ROWS} inventory rows."])
            if None in row or any(row.get(key) is None for key in REQUIRED):
                errors.append(f"Line {line}: incorrect number of required cells.")
                continue
            values = {
                key: value.strip() if value is not None else None for key, value in row.items()
            }
            if any(
                value is not None
                and (
                    len(value) > 120 or FORBIDDEN in value or any(ord(char) < 32 for char in value)
                )
                for value in values.values()
            ):
                errors.append(
                    f"Line {line}: a cell contains invalid characters or exceeds 120 characters."
                )
                continue
            if not all(values[key] for key in {"stock_id", "brand", "location"}):
                errors.append(f"Line {line}: stock_id, brand and location are required.")
                continue
            unit = Stock.model_validate(values)
            stock_key = unit.stock_id.casefold()
            if stock_key in seen:
                duplicates.add(stock_key)
                errors.append(
                    f"Line {line}: duplicate stock_id from line {seen[stock_key]}. Both records were rejected."
                )
            else:
                seen[stock_key] = line
                units.append(unit)
            if model_identifier(unit.model)[0] != "valid":
                errors.append(f"Line {line}: verify the single model printed on the unit.")
            for key in ("date_code", "serial_number"):
                status, _ = identifier(getattr(unit, key))
                if status == "malformed":
                    errors.append(
                        f"Line {line}: {key} is {status}. Review the identifier on the unit."
                    )
        return Inventory(
            [unit for unit in units if unit.stock_id.casefold() not in duplicates], errors
        )
    except (UnicodeError, csv.Error):
        return Inventory([], ["The inventory must be a well-formed UTF-8 CSV file."])


def export_cell(value: object) -> str:
    text = "" if value is None else str(value)
    if FORBIDDEN in text:
        raise ValueError("Unsupported display punctuation.")
    if text[:1] in {"\t", "\r", "\n"} or text.lstrip()[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text
