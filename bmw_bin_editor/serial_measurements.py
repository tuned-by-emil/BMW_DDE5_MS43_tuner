"""Strict DDE361830 selective measurement decoding, independent of EDIABAS.

The ECU has already converted its source RAM values into unsigned big-endian
16-bit words. These formulas reproduce the D50M47A PRG's physical scaling;
the datatype of the underlying firmware getter must not be applied twice.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from types import MappingProxyType


DEFINITIONS_SHA256 = "15d3108e9357c21ae64caa002a1eefb2ad1968e699ca6facd8937a2404d186cf"


@dataclass(frozen=True)
class _Parameter:
    arg: str
    ident: int
    result: str
    unit: str
    mul: float
    div: float
    add: float

    def decode(self, raw: int) -> float:
        # Match BEST operations in order, using its double-precision F registers.
        value = ((float(raw) * self.mul) / self.div) + self.add
        if not math.isfinite(value):
            raise ValueError("Invalid physical value for " + self.arg)
        return value


def _coefficient(value, default: float) -> float:
    if value == "-":
        return default
    if not isinstance(value, str) or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value):
        raise ValueError("Invalid direct serial scaling coefficient.")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Invalid direct serial scaling coefficient.")
    return result


def _identifier(value) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"0[xX][0-9a-fA-F]{1,4}", value):
        raise ValueError("Invalid direct serial channel identifier.")
    return int(value, 16)


class SerialMeasurements:
    """Snapshot catalog metadata and validate every group before opening COM.

    ``decode_measurements`` receives only the diagnostic payload (starting
    6C 10), after the transport has validated and removed its frame/checksum.
    Groups must describe the selection most recently acknowledged by the ECU.
    """

    def __init__(self, catalog: Mapping, definitions_path=None):
        if not isinstance(catalog, Mapping):
            raise ValueError("Direct serial logging requires a parameter catalog.")
        base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
        path = Path(definitions_path) if definitions_path is not None else base / "definitions/direct_serial_parameters.json"
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise ValueError("Verified direct serial parameter definitions are missing.") from exc
        if hashlib.sha256(encoded).hexdigest() != DEFINITIONS_SHA256:
            raise ValueError("Direct serial parameter definitions have changed; refusing unverified scaling.")
        document = json.loads(encoded.decode("utf-8"))
        if document.get("schema") != 1 or document.get("ecu") != "DDE5/EDC16 361830":
            raise ValueError("Unsupported direct serial parameter definitions.")
        parameters = {}
        for row in document["parameters"]:
            if row["wire_type"] != "uint16_be" or row["arg"] in parameters:
                raise ValueError("Unsupported direct serial wire datatype.")
            div = _coefficient(row["div"], 1.0)
            if div == 0:
                raise ValueError("Direct serial scaling divisor cannot be zero.")
            parameters[row["arg"]] = _Parameter(row["arg"], _identifier(row["id"]),
                row["result"], row["unit"], _coefficient(row["mul"], 1.0), div,
                _coefficient(row["add"], 0.0))
        self._parameters = MappingProxyType(parameters)
        # Do not allow later GUI/catalog mutation to relabel an active stream.
        self._catalog = MappingProxyType({key: dict(value) if isinstance(value, Mapping) else value
                                          for key, value in catalog.items()})

    @property
    def supported_args(self) -> tuple[str, ...]:
        return tuple(self._parameters)

    def validate_group(self, group) -> tuple[str, ...]:
        if not isinstance(group, Sequence) or isinstance(group, (str, bytes, bytearray)):
            raise ValueError("Direct serial measurement group must contain 1–10 parameters.")
        group = tuple(group)
        if not 1 <= len(group) <= 10 or any(not isinstance(arg, str) for arg in group):
            raise ValueError("Direct serial measurement group must contain 1–10 parameters.")
        if len(set(group)) != len(group):
            raise ValueError("Direct serial measurement groups cannot repeat a parameter.")
        results = set()
        for arg in group:
            if arg not in self._parameters or arg not in self._catalog:
                raise ValueError("No verified direct serial scaling for parameter " + arg)
            expected = self._parameters[arg]
            row = self._catalog[arg]
            if not isinstance(row, Mapping) or row.get("arg") != arg:
                raise ValueError("Direct serial catalog metadata changed for " + arg)
            if (_identifier(row.get("id")) != expected.ident or
                    row.get("result") != expected.result or row.get("unit") != expected.unit):
                raise ValueError("Direct serial catalog metadata changed for " + arg)
            if expected.result in results:
                raise ValueError("Direct serial group contains conflicting result names: " + expected.result)
            results.add(expected.result)
        return group

    def validate_groups(self, groups) -> tuple[tuple[str, ...], ...]:
        validated = tuple(self.validate_group(group) for group in groups)
        if not validated:
            raise ValueError("Choose at least one direct serial measurement group.")
        result_owners = {}
        for group in validated:
            for arg in group:
                result = self._parameters[arg].result
                if result in result_owners and result_owners[result] != arg:
                    raise ValueError("Direct serial profile contains conflicting result names: " + result)
                result_owners[result] = arg
        return validated

    def selection_payload(self, group) -> bytes:
        validated = self.validate_group(group)
        return b"\x2C\x10" + b"".join(self._parameters[arg].ident.to_bytes(2, "big") for arg in validated)

    def decode_measurements(self, payload, group) -> dict[str, float]:
        validated = self.validate_group(group)
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError("Direct serial measurement response must be bytes.")
        payload = bytes(payload)
        if payload[:1] == b"\x7F":
            raise ValueError("ECU rejected direct serial measurements: " + payload.hex(" ").upper())
        if payload[:2] != b"\x6C\x10":
            raise ValueError("Unexpected direct serial measurement response service.")
        expected_length = 2 + 2 * len(validated)
        if len(payload) != expected_length:
            raise ValueError(f"Direct serial measurement response length is {len(payload)}; expected {expected_length}.")
        values = {}
        for index, arg in enumerate(validated):
            offset = 2 + 2 * index
            raw = int.from_bytes(payload[offset:offset + 2], "big", signed=False)
            parameter = self._parameters[arg]
            values[parameter.result] = parameter.decode(raw)
        return values


def validate_group(group, catalog) -> tuple[str, ...]:
    return SerialMeasurements(catalog).validate_group(group)


def decode_measurements(payload, group, catalog) -> dict[str, float]:
    return SerialMeasurements(catalog).decode_measurements(payload, group)
