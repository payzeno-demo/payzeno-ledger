"""Acquirer settlement file formats.

Two parsers, one interface, and no plan to converge them. Worldflow moved to CSV in
2023; Nordpay still files fixed-width and has said "next year" three times. The CSV
parser is the "new" one, which is why the fixed-width one is called legacy even though
it processes a third of the volume.
"""

from __future__ import annotations

import abc
import csv
import io
from dataclasses import dataclass
from typing import ClassVar

from app.domain.money import to_minor
from app.errors import ValidationError
from app.logging import get_logger

logger = get_logger(__name__)

#: Acquirer line-type codes → `reconciliation_item.line_type`.
LINE_TYPE_BY_CODE: dict[str, str] = {
    "SL": "sale",
    "RF": "refund",
    "CB": "chargeback",
    "CR": "chargeback_reversal",
    "SF": "scheme_fee",
    "AD": "adjustment",
    "RH": "reserve_hold",
    "RL": "reserve_release",
}


@dataclass(frozen=True, slots=True)
class ParsedSettlementLine:
    """One line of an acquirer settlement file, currency-normalised to minor units."""

    acquirer_reference: str
    network_reference: str | None
    line_type: str
    gross_minor: int
    fee_minor: int
    interchange_minor: int
    scheme_fee_minor: int
    net_minor: int
    currency: str


class SettlementFileParser(abc.ABC):
    """Turns acquirer bytes into :class:`ParsedSettlementLine` values."""

    acquirer: ClassVar[str]

    @abc.abstractmethod
    def parse(self, raw: bytes) -> list[ParsedSettlementLine]:
        """Parse a whole file. Raises ``ValidationError`` on a malformed file."""

    @staticmethod
    def _line_type(code: str) -> str:
        normalised = code.strip().upper()
        if normalised in LINE_TYPE_BY_CODE:
            return LINE_TYPE_BY_CODE[normalised]
        lowered = code.strip().lower()
        if lowered in set(LINE_TYPE_BY_CODE.values()):
            return lowered
        raise ValidationError(f"unknown settlement line type {code!r}", line_type=code)


class WorldflowCsvParser(SettlementFileParser):
    """Worldflow's CSV format.

    Header row is mandatory and column order is not guaranteed — Worldflow reorders
    columns between file versions without telling anyone, so everything is read by name.
    """

    acquirer = "worldflow"

    REQUIRED_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {"acquirer_reference", "gross_minor", "net_minor", "currency", "line_type"}
    )

    def parse(self, raw: bytes) -> list[ParsedSettlementLine]:
        text = raw.decode("utf-8-sig", errors="strict")
        reader = csv.DictReader(io.StringIO(text))
        if reader.fieldnames is None:
            raise ValidationError("settlement file has no header row", acquirer=self.acquirer)

        missing = self.REQUIRED_COLUMNS - {name.strip() for name in reader.fieldnames}
        if missing:
            raise ValidationError(
                "settlement file is missing required columns",
                acquirer=self.acquirer,
                missing=sorted(missing),
            )

        lines: list[ParsedSettlementLine] = []
        for row_number, row in enumerate(reader, start=2):
            if not row.get("acquirer_reference"):
                logger.warning("settlement_line_skipped", row=row_number, reason="no_reference")
                continue
            currency = (row.get("currency") or "USD").strip().upper()
            lines.append(
                ParsedSettlementLine(
                    acquirer_reference=row["acquirer_reference"].strip(),
                    network_reference=(row.get("network_reference") or "").strip() or None,
                    line_type=self._line_type(row["line_type"]),
                    gross_minor=_int(row.get("gross_minor")),
                    fee_minor=_int(row.get("fee_minor")),
                    interchange_minor=_int(row.get("interchange_minor")),
                    scheme_fee_minor=_int(row.get("scheme_fee_minor")),
                    net_minor=_int(row.get("net_minor")),
                    currency=currency,
                )
            )

        logger.info("settlement_file_parsed", acquirer=self.acquirer, lines=len(lines))
        return lines


class LegacyFixedWidthParser(SettlementFileParser):
    """Nordpay's fixed-width format.

    Field offsets come straight out of the 2019 spec PDF. Amounts arrive as decimal
    strings with an implied two decimal places for every currency Nordpay supports,
    which is not true in general — hence the trip through ``to_minor`` rather than a
    naive multiply by 100.

    Record layout::

        0-19   acquirer reference        (left-justified, space padded)
        20-39  network reference
        40-41  line type code
        42-53  gross amount              (signed, decimal string)
        54-65  fee amount
        66-77  interchange amount
        78-89  scheme fee amount
        90-101 net amount
        102-104 currency
    """

    acquirer = "nordpay"

    RECORD_LENGTH: ClassVar[int] = 105

    def parse(self, raw: bytes) -> list[ParsedSettlementLine]:
        text = raw.decode("latin-1")
        lines: list[ParsedSettlementLine] = []

        for row_number, record in enumerate(text.splitlines(), start=1):
            if not record.strip():
                continue
            if record.startswith(("HDR", "TRL")):
                # Header and trailer carry counts and a checksum we verify elsewhere.
                continue
            if len(record) < self.RECORD_LENGTH:
                raise ValidationError(
                    "fixed-width settlement record is short",
                    acquirer=self.acquirer,
                    row=row_number,
                    length=len(record),
                    expected=self.RECORD_LENGTH,
                )

            currency = record[102:105].strip().upper() or "EUR"
            lines.append(
                ParsedSettlementLine(
                    line_type=self._line_type(record[40:42]),
                    gross_minor=to_minor(record[42:54].strip(), currency),
                    fee_minor=to_minor(record[54:66].strip() or "0", currency),
                    interchange_minor=to_minor(record[66:78].strip() or "0", currency),
                    scheme_fee_minor=to_minor(record[78:90].strip() or "0", currency),
                    net_minor=to_minor(record[90:102].strip(), currency),
                )
            )

        logger.info("settlement_file_parsed", acquirer=self.acquirer, lines=len(lines))
        return lines


PARSER_BY_ACQUIRER: dict[str, SettlementFileParser] = {
    WorldflowCsvParser.acquirer: WorldflowCsvParser(),
    LegacyFixedWidthParser.acquirer: LegacyFixedWidthParser(),
}


def parser_for(acquirer: str) -> SettlementFileParser:
    parser = PARSER_BY_ACQUIRER.get(acquirer)
    if parser is None:
        raise ValidationError(f"no settlement parser for acquirer {acquirer!r}", acquirer=acquirer)
    return parser


def _int(raw: object) -> int:
    if raw in (None, ""):
        return 0
    try:
        return int(str(raw).strip())
    except ValueError as exc:
        raise ValidationError(f"non-integer minor amount {raw!r}", value=str(raw)) from exc
