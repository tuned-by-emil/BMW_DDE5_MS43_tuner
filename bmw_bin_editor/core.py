from __future__ import annotations

import ast
import hashlib
import math
import os
import operator
import re
import struct
import tempfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


class EditorError(RuntimeError):
    pass


_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_ALLOWED_UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def parse_int(value: str | None, default: int = 0) -> int:
    if value is None or str(value).strip() == "":
        return default
    return int(str(value).strip(), 0)


@lru_cache(maxsize=256)
def _parsed_equation(equation: str) -> ast.Expression:
    expression = (equation or "X").strip().replace("^", "**")
    return ast.parse(expression, mode="eval")


def safe_math(equation: str, x: float) -> float:
    """Evaluate simple XDF arithmetic safely, caching the parsed expression."""
    tree = _parsed_equation(equation or "X")

    def visit(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return float(node.value)
        if isinstance(node, ast.Name) and node.id == "X":
            return float(x)
        if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
            return float(_ALLOWED_BINOPS[type(node.op)](visit(node.left), visit(node.right)))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            return float(_ALLOWED_UNARY[type(node.op)](visit(node.operand)))
        raise EditorError(f"Unsupported XDF math expression: {equation!r}")

    return visit(tree)


@lru_cache(maxsize=256)
def equation_affine(equation: str) -> tuple[float, float]:
    """Return physical = scale * raw + offset, rejecting non-linear equations."""
    def affine(node: ast.AST) -> tuple[float, float]:
        if isinstance(node, ast.Expression):
            return affine(node.body)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            return 0.0, float(node.value)
        if isinstance(node, ast.Name) and node.id == "X":
            return 1.0, 0.0
        if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARY:
            a, b = affine(node.operand)
            sign = -1.0 if isinstance(node.op, ast.USub) else 1.0
            return sign*a, sign*b
        if isinstance(node, ast.BinOp):
            a, b = affine(node.left)
            c, d = affine(node.right)
            if isinstance(node.op, ast.Add): return a+c, b+d
            if isinstance(node.op, ast.Sub): return a-c, b-d
            if isinstance(node.op, ast.Mult) and (a == 0 or c == 0):
                return a*d+b*c, b*d
            if isinstance(node.op, ast.Div) and c == 0 and d != 0:
                return a/d, b/d
            if a == 0 and c == 0 and type(node.op) in _ALLOWED_BINOPS:
                return 0.0, float(_ALLOWED_BINOPS[type(node.op)](b, d))
            if isinstance(node.op, ast.Pow) and c == 0 and d == 1:
                return a, b
        raise EditorError(f"Non-affine XDF equation is read-only: {equation!r}")

    try:
        scale, offset = affine(_parsed_equation(equation or "X"))
    except (ArithmeticError, ValueError, SyntaxError, TypeError) as exc:
        raise EditorError(f"XDF equation cannot be inverted: {equation!r}") from exc
    if not math.isfinite(scale) or not math.isfinite(offset) or abs(scale) < 1e-18:
        raise EditorError(f"XDF equation cannot be inverted: {equation!r}")
    return scale, offset


def estimate_lambda_from_map_target(
    map_hpa: float,
    iq_mg: float,
    *,
    engine_l: float = 2.0,
    cylinders: int = 4,
    volumetric_efficiency: float = 0.90,
    intake_temp_c: float = 40.0,
    stoich_afr: float = 14.5,
) -> tuple[float, float]:
    """Estimate air mass/cylinder and diesel lambda from absolute MAP and IQ.

    This is deliberately a transparent tuning aid, not an ECU model.  It assumes
    ideal-gas charge density, one cylinder volume per four-stroke intake event,
    fixed VE/IAT, and no EGR.  Returned tuple is (air_mg_per_cyl, lambda).
    """
    p_hpa = float(map_hpa)
    fuel_mg = float(iq_mg)
    if not (math.isfinite(p_hpa) and math.isfinite(fuel_mg)) or p_hpa <= 0 or fuel_mg <= 0:
        return math.nan, math.nan
    if cylinders <= 0 or engine_l <= 0 or volumetric_efficiency <= 0 or stoich_afr <= 0:
        return math.nan, math.nan
    temp_k = float(intake_temp_c) + 273.15
    if temp_k <= 0:
        return math.nan, math.nan
    cylinder_m3 = (float(engine_l) / 1000.0) / float(cylinders)
    air_kg = (p_hpa * 100.0) * cylinder_m3 / (287.05 * temp_k) * float(volumetric_efficiency)
    air_mg = air_kg * 1_000_000.0
    lam = air_mg / (fuel_mg * float(stoich_afr))
    return air_mg, lam


def raw_limits(bits: int, signed: bool) -> tuple[int, int]:
    if bits not in (8, 16, 32):
        raise EditorError(f"Unsupported element size: {bits} bits")
    if signed:
        return -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    return 0, (1 << bits) - 1


@dataclass
class AxisDefinition:
    address: int | None
    count: int
    element_bits: int = 16
    signed: bool = False
    equation: str = "X"
    units: str = ""
    decimal_places: int = 2
    labels: list[str] = field(default_factory=list)
    # Original linked-axis symbol, retained even when the XDF omits its units.
    source_name: str = ""

    @property
    def editable(self) -> bool:
        return self.address is not None and self.count > 0


def fixed_axis_values(axis: AxisDefinition) -> list[float]:
    """Numeric fixed-axis labels are physical breakpoints, not index positions."""
    if len(axis.labels) == axis.count:
        try:
            values = [float(label) for label in axis.labels]
            if all(math.isfinite(value) for value in values):
                return values
        except (ValueError, TypeError):
            pass
    return [float(i) for i in range(axis.count)]


@dataclass
class CalibrationDefinition:
    title: str
    description: str
    category: str
    z_address: int
    rows: int
    cols: int
    z_element_bits: int = 16
    z_signed: bool = False
    z_equation: str = "X"
    z_units: str = ""
    decimal_places: int = 2
    x_axis: AxisDefinition | None = None
    y_axis: AxisDefinition | None = None
    unique_id: str = ""
    kind: str = "table"
    categories: list[str] = field(default_factory=list)
    profile_id: str = ""
    storage_order: str = "row_major"

    @property
    def size(self) -> int:
        return self.rows * self.cols

    @property
    def z_width(self) -> int:
        return self.z_element_bits // 8

    def storage_index(self, row: int, col: int) -> int:
        """Locate a displayed cell in the contiguous serialized value array."""
        if not (0 <= row < self.rows and 0 <= col < self.cols):
            raise EditorError(f"Cell [{row},{col}] is outside {self.rows} x {self.cols} table")
        if self.storage_order == "column_major":
            return col * self.rows + row
        if self.storage_order == "row_major":
            return row * self.cols + col
        raise EditorError(f"Unsupported table storage order: {self.storage_order!r}")

    def logical_rows(self, values: list) -> list[list]:
        """Arrange serialized values without changing the X/Y axis assignments."""
        if self.storage_order == "row_major":
            return [values[r * self.cols:(r + 1) * self.cols] for r in range(self.rows)]
        if self.storage_order == "column_major":
            return [[values[c * self.rows + r] for c in range(self.cols)] for r in range(self.rows)]
        raise EditorError(f"Unsupported table storage order: {self.storage_order!r}")

    @property
    def discovery_unverified(self) -> bool:
        return self.description.startswith(("UNVERIFIED pattern candidate.", "UNVERIFIED semantic hypothesis"))

    @property
    def base_step(self) -> float:
        try:
            scale, _ = equation_affine(self.z_equation)
            return abs(scale)
        except EditorError:
            return 10.0 ** (-max(0, self.decimal_places))


class XDFDatabase:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.tree = ET.parse(self.path, parser=ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True)))
        self._parse_tree()

    @classmethod
    def from_tree(cls, tree: ET.ElementTree, path: Path | str = "Untitled.xdf") -> "XDFDatabase":
        """Decode an existing XML tree without reading or writing a file."""
        database = cls.__new__(cls)
        database.path = Path(path)
        database.tree = tree
        database._parse_tree()
        return database

    def _parse_tree(self) -> None:
        self.root = self.tree.getroot()
        # MS43X supplies separate full/partial XDFs with linked axis tables.
        # Normalize the XML as well as the parsed objects so subsequent XDF edits
        # and saves continue to use consistent file offsets.
        if self.root.findtext("./XDFHEADER/deftitle", "").strip() == "MS43X001":
            from .ms43 import normalize_xdf
            normalize_xdf(self.root)
        defaults = self.root.find("./XDFHEADER/DEFAULTS")
        self.default_lsb_first = bool(parse_int(defaults.get("lsbfirst"), 0)) if defaults is not None else False
        self.title = self.root.findtext("./XDFHEADER/deftitle") or self.path.stem
        self.profile_id = (self.root.findtext('./XDFHEADER/BMWBINEDITORPROFILE') or
                           ('361830' if re.search(r'\b361830\b', self.title) else
                            'xdf:' + hashlib.sha256(self.title.encode('utf-8')).hexdigest()[:16]))
        self.categories: dict[int, str] = {}
        for cat in self.root.findall("./XDFHEADER/CATEGORY"):
            self.categories[parse_int(cat.get("index"), -1)] = cat.get("name") or "Uncategorized"
        self.definitions: list[CalibrationDefinition] = []
        self._parse_tables()
        self._parse_constants()
        for definition in self.definitions:
            definition.profile_id = self.profile_id
        if self.title.strip() == "MS43X001":
            axis_sources = {(d.z_address, d.size, d.z_element_bits, d.z_equation): d.title
                            for d in self.definitions if d.category == "Axis"}
            for definition in self.definitions:
                for axis in (definition.x_axis, definition.y_axis):
                    if axis is not None and axis.address is not None:
                        axis.source_name = axis_sources.get(
                            (axis.address, axis.count, axis.element_bits, axis.equation), "")
        unresolved = {node.get('uniqueid') for node in self.root.findall('./XDFTABLE')
                      if any(axis.get('bmw_unresolved_link') for axis in node.findall('XDFAXIS'))}
        for definition in self.definitions:
            if definition.unique_id in unresolved:
                definition.description = ('UNVERIFIED semantic hypothesis: unresolved linked axis in source XDF; '
                    'index positions shown. Verify before editing.\n' + definition.description)

    @staticmethod
    def _math(node: ET.Element | None) -> str:
        if node is None:
            return "X"
        math_node = node.find("./MATH")
        return math_node.get("equation", "X") if math_node is not None else "X"

    @staticmethod
    def _signed(node: ET.Element, embedded: ET.Element) -> bool:
        flags = parse_int(embedded.get("mmedtypeflags"), 0)
        datatype = parse_int(node.findtext("./datatype"), 0)
        return bool(flags & 0x01) or datatype == 1

    def _categories(self, item: ET.Element) -> list[str]:
        """Decode TunerPro category memberships in their actual numbering scheme.

        XDFHEADER/CATEGORY uses a zero-based ``index`` while CATEGORYMEM's
        ``category`` value is one-based.  CATEGORYMEM ``index`` also expresses
        membership order; later memberships in this BMW XDF are the more
        specific user-facing group (for example Boost -> N75, Fuel -> SOI).
        """
        memberships: list[tuple[int, str]] = []
        for member in item.findall("./CATEGORYMEM"):
            order = parse_int(member.get("index"), 0)
            raw_category = parse_int(member.get("category"), 0)
            header_index = raw_category - 1
            if raw_category <= 0:
                name = "Uncategorized"
            else:
                name = self.categories.get(header_index, f"Category {raw_category}")
            memberships.append((order, name))
        memberships.sort(key=lambda pair: pair[0])
        result: list[str] = []
        for _, name in memberships:
            if name not in result:
                result.append(name)
        return result

    def _category(self, item: ET.Element) -> str:
        categories = self._categories(item)
        return categories[-1] if categories else "Uncategorized"

    def _axis(self, table: ET.Element, axis_id: str) -> AxisDefinition | None:
        axis = table.find(f"./XDFAXIS[@id='{axis_id}']")
        if axis is None:
            return None
        count_node = axis.find("./indexcount")
        labels = [lab.get("value") or str(i) for i, lab in enumerate(axis.findall("./LABEL"))]
        count = parse_int(count_node.text if count_node is not None else None, len(labels) or 1)
        embedded = axis.find("./EMBEDDEDDATA")
        address = None
        bits = 16
        signed = False
        if embedded is not None:
            addr_text = embedded.get("mmedaddress")
            if addr_text is not None:
                address = parse_int(addr_text)
            bits = parse_int(embedded.get("mmedelementsizebits"), 16)
            signed = self._signed(axis, embedded)
        return AxisDefinition(
            address=address,
            count=count,
            element_bits=bits,
            signed=signed,
            equation=self._math(axis),
            units=axis.findtext("./units") or "",
            decimal_places=parse_int(axis.findtext("./decimalpl"), 2),
            labels=labels,
        )

    @staticmethod
    def _storage_order(embedded: ET.Element, rows: int, cols: int, bits: int) -> str:
        try:
            row_stride = parse_int(embedded.get("mmedrowstridebits"), 0)
            col_stride = parse_int(embedded.get("mmedcolstridebits"), 0)
        except ValueError as exc:
            raise EditorError("Invalid XDF table storage strides") from exc
        if row_stride == 0 and col_stride == 0:
            return "row_major"
        if (row_stride or cols * bits, col_stride or bits) == (cols * bits, bits):
            return "row_major"
        if (row_stride, col_stride) == (bits, rows * bits):
            return "column_major"
        raise EditorError(f"Unsupported XDF table storage strides: row={row_stride}, column={col_stride} bits "
                          f"for {rows} x {cols} cells of {bits} bits; only contiguous rows or columns are supported")

    def _parse_tables(self) -> None:
        for table in self.root.findall("./XDFTABLE"):
            z_axis = table.find("./XDFAXIS[@id='z']")
            if z_axis is None:
                continue
            embedded = z_axis.find("./EMBEDDEDDATA")
            if embedded is None or embedded.get("mmedaddress") is None:
                continue
            rows = parse_int(embedded.get("mmedrowcount"), 1)
            cols = parse_int(embedded.get("mmedcolcount"), 1)
            bits = parse_int(embedded.get("mmedelementsizebits"), 16)
            self.definitions.append(
                CalibrationDefinition(
                    title=table.findtext("./title") or "Untitled",
                    description=table.findtext("./description") or "",
                    category=self._category(table),
                    z_address=parse_int(embedded.get("mmedaddress")),
                    rows=rows,
                    cols=cols,
                    z_element_bits=bits,
                    z_signed=self._signed(z_axis, embedded),
                    z_equation=self._math(z_axis),
                    z_units=z_axis.findtext("./units") or "",
                    decimal_places=parse_int(z_axis.findtext("./decimalpl"), 2),
                    x_axis=self._axis(table, "x"),
                    y_axis=self._axis(table, "y"),
                    unique_id=table.get("uniqueid") or "",
                    kind="table",
                    categories=self._categories(table),
                    storage_order=self._storage_order(embedded, rows, cols, bits),
                )
            )

    def _parse_constants(self) -> None:
        for const in self.root.findall("./XDFCONSTANT"):
            embedded = const.find("./EMBEDDEDDATA")
            if embedded is None or embedded.get("mmedaddress") is None:
                continue
            self.definitions.append(
                CalibrationDefinition(
                    title=const.findtext("./title") or "Untitled constant",
                    description=const.findtext("./description") or "",
                    category=self._category(const) if const.find("./CATEGORYMEM") is not None else "Constants",
                    z_address=parse_int(embedded.get("mmedaddress")),
                    rows=1,
                    cols=1,
                    z_element_bits=parse_int(embedded.get("mmedelementsizebits"), 16),
                    z_signed=self._signed(const, embedded),
                    z_equation=self._math(const),
                    z_units=const.findtext("./units") or "",
                    decimal_places=parse_int(const.findtext("./decimalpl"), 2),
                    unique_id=const.get("uniqueid") or "",
                    kind="constant",
                    categories=self._categories(const) if const.find("./CATEGORYMEM") is not None else ["Constants"],
                )
            )

    @property
    def tables(self) -> list[CalibrationDefinition]:
        return [d for d in self.definitions if d.kind == "table"]

    @property
    def constants(self) -> list[CalibrationDefinition]:
        return [d for d in self.definitions if d.kind == "constant"]


@dataclass
class ByteChange:
    address: int
    before: bytes
    after: bytes


@dataclass
class EditCommand:
    label: str
    changes: list[ByteChange]


class CalibrationDocument:
    def __init__(self, xdf: XDFDatabase, bin_path: Path | str):
        self.xdf = xdf
        self.path = Path(bin_path)
        self.original = self.path.read_bytes()
        self.data = bytearray(self.original)
        # Last successfully saved state is tracked separately from the immutable
        # original BIN used for comparison/overlay.
        self.saved = bytes(self.original)
        self.undo_stack: list[EditCommand] = []
        self.redo_stack: list[EditCommand] = []
        self.dirty = False
        # Original calibration never changes, so keep decoded values around. This
        # removes a surprising amount of repeat work during table/3D refreshes.
        self._original_map_cache: dict[tuple, list[list[float]]] = {}
        self._original_axis_cache: dict[tuple, list[float]] = {}

    @property
    def endian(self) -> str:
        return "<" if self.xdf.default_lsb_first else ">"

    def _fmt(self, bits: int, signed: bool) -> str:
        if bits not in (8, 16, 32):
            raise EditorError(f"Unsupported element size: {bits}")
        return {8: "b" if signed else "B", 16: "h" if signed else "H", 32: "i" if signed else "I"}[bits]

    def _unpack(self, source: bytes | bytearray, address: int, count: int, bits: int, signed: bool) -> list[int]:
        width = bits // 8
        end = address + count * width
        if address < 0 or end > len(source):
            raise EditorError(f"Read outside BIN at 0x{address:X}..0x{end:X} (size 0x{len(source):X})")
        return list(struct.unpack(self.endian + self._fmt(bits, signed) * count, source[address:end]))

    def _pack_one(self, raw: int, bits: int, signed: bool) -> bytes:
        lo, hi = raw_limits(bits, signed)
        raw = max(lo, min(hi, int(raw)))
        return struct.pack(self.endian + self._fmt(bits, signed), raw)

    def physical_to_raw(self, value: float, equation: str, bits: int, signed: bool) -> int:
        scale, offset = equation_affine(equation)
        raw = int(round((float(value) - offset) / scale))
        lo, hi = raw_limits(bits, signed)
        return max(lo, min(hi, raw))

    def raw_to_physical(self, raw: int, equation: str) -> float:
        return safe_math(equation, raw)

    def map_raw(self, definition: CalibrationDefinition, original: bool = False) -> list[list[int]]:
        src = self.original if original else self.data
        values = self._unpack(src, definition.z_address, definition.size, definition.z_element_bits, definition.z_signed)
        return definition.logical_rows(values)

    @staticmethod
    def _scale_values(raw: list[int], equation: str) -> list[float]:
        # All editable definitions in the supplied BMW XDF are affine. Use the
        # already-cached scale/offset directly and fall back to safe_math only for
        # unusual read-only equations.
        try:
            scale, offset = equation_affine(equation)
            return [v * scale + offset for v in raw]
        except EditorError:
            return [safe_math(equation, v) for v in raw]

    def map_values_from_source(self, definition: CalibrationDefinition, source: bytes | bytearray) -> list[list[float]]:
        raw = self._unpack(source, definition.z_address, definition.size, definition.z_element_bits, definition.z_signed)
        scaled = self._scale_values(raw, definition.z_equation)
        return definition.logical_rows(scaled)

    def axis_values_from_source(self, axis: AxisDefinition | None, source: bytes | bytearray) -> list[float]:
        if axis is None:
            return []
        if axis.address is None or (axis.labels and not axis.editable):
            return fixed_axis_values(axis)
        raw = self._unpack(source, axis.address, axis.count, axis.element_bits, axis.signed)
        return self._scale_values(raw, axis.equation)

    def map_values(self, definition: CalibrationDefinition, original: bool = False) -> list[list[float]]:
        key = (definition.kind, definition.z_address, definition.rows, definition.cols,
               definition.z_element_bits, definition.z_signed, definition.z_equation, definition.storage_order)
        if original and key in self._original_map_cache:
            return self._original_map_cache[key]
        raw_rows = self.map_raw(definition, original)
        flat = [v for row in raw_rows for v in row]
        scaled = self._scale_values(flat, definition.z_equation)
        values = [scaled[r * definition.cols:(r + 1) * definition.cols] for r in range(definition.rows)]
        if original:
            self._original_map_cache[key] = values
        return values

    def axis_values(self, axis: AxisDefinition | None, original: bool = False) -> list[float]:
        if axis is None:
            return []
        if axis.labels and not axis.editable:
            return fixed_axis_values(axis)
        if axis.address is None:
            return fixed_axis_values(axis)
        key = (axis.address, axis.count, axis.element_bits, axis.signed, axis.equation)
        if original and key in self._original_axis_cache:
            return self._original_axis_cache[key]
        src = self.original if original else self.data
        raw = self._unpack(src, axis.address, axis.count, axis.element_bits, axis.signed)
        values = self._scale_values(raw, axis.equation)
        if original:
            self._original_axis_cache[key] = values
        return values

    def axis_labels(self, axis: AxisDefinition | None) -> list[str]:
        if axis is None:
            return []
        if axis.labels:
            return axis.labels[:]
        vals = self.axis_values(axis)
        return [format_number(v, axis.decimal_places) for v in vals]

    def _apply_command(self, command: EditCommand, forward: bool) -> None:
        for change in command.changes:
            payload = change.after if forward else change.before
            self.data[change.address:change.address + len(payload)] = payload
        self.dirty = bytes(self.data) != self.saved

    def apply_bytes(self, label: str, updates: Iterable[tuple[int, bytes]]) -> bool:
        pending: dict[int, int] = {}
        for address, after in updates:
            after = bytes(after)
            if address < 0 or address + len(after) > len(self.data):
                raise EditorError(f"Write outside BIN at 0x{address:X} (length {len(after)})")
            for pos, value in enumerate(after, address):
                if pos in pending and pending[pos] != value:
                    raise EditorError(f"Conflicting overlapping writes at 0x{pos:X}")
                pending[pos] = value
        changes: list[ByteChange] = []
        for pos in sorted(pending):
            value = pending[pos]
            if self.data[pos] == value:
                continue
            if changes and changes[-1].address + len(changes[-1].after) == pos:
                changes[-1].before += bytes([self.data[pos]])
                changes[-1].after += bytes([value])
            else:
                changes.append(ByteChange(pos, bytes([self.data[pos]]), bytes([value])))
        if not changes:
            return False
        cmd = EditCommand(label, changes)
        self._apply_command(cmd, True)
        self.undo_stack.append(cmd)
        self.redo_stack.clear()
        return True

    def _pack_cell_updates(self, definition: CalibrationDefinition, updates: dict[tuple[int, int], float]) -> list[tuple[int, bytes]]:
        packed: list[tuple[int, bytes]] = []
        for (row, col), value in updates.items():
            if not (0 <= row < definition.rows and 0 <= col < definition.cols):
                continue
            raw = self.physical_to_raw(value, definition.z_equation, definition.z_element_bits, definition.z_signed)
            address = self.cell_address(definition, row, col)
            packed.append((address, self._pack_one(raw, definition.z_element_bits, definition.z_signed)))
        return packed

    def set_cells(self, definition: CalibrationDefinition, updates: dict[tuple[int, int], float], label: str) -> bool:
        return self.apply_bytes(label, self._pack_cell_updates(definition, updates))

    def set_cells_linked(
        self,
        source: CalibrationDefinition,
        updates: dict[tuple[int, int], float],
        targets: list[CalibrationDefinition],
        mode: str,
        label: str,
    ) -> tuple[bool, list[str]]:
        """Write one map edit plus linked-map propagation as one undo command.

        mode='absolute' copies the resulting physical values to all linked maps.
        mode='relative' applies the same percentage change cell-by-cell, preserving
        intentional differences/offsets between linked maps.  Relative propagation
        is skipped whenever either the old or new source value is zero, because a
        percentage change through zero is undefined and unsafe to reconstruct.
        """
        mode = (mode or '').strip().lower()
        if mode not in {'absolute', 'relative'}:
            raise EditorError(f"Unknown linked-map mode: {mode!r}")

        warnings: list[str] = []
        source_before = self.map_values(source)
        packed = self._pack_cell_updates(source, updates)

        for target in targets:
            if target.z_address == source.z_address and target.kind == source.kind:
                continue
            if target.rows != source.rows or target.cols != source.cols:
                warnings.append(
                    f"Skipped {target.title}: {target.rows}x{target.cols} does not match source {source.rows}x{source.cols}"
                )
                continue

            if not self.grids_match(source, target):
                warnings.append(f"Skipped {target.title}: physical axis breakpoints differ; cell positions do not represent the same operating point")
                continue

            target_before = self.map_values(target)
            linked_updates: dict[tuple[int, int], float] = {}
            for (row, col), new_source in updates.items():
                if not (0 <= row < source.rows and 0 <= col < source.cols):
                    continue
                if mode == 'absolute':
                    linked_updates[(row, col)] = new_source
                    continue

                old_source = source_before[row][col]
                old_target = target_before[row][col]
                zero_tol = max(abs(source.base_step) * 0.25, 1e-12)
                if abs(old_source) <= zero_tol or abs(new_source) <= zero_tol:
                    # Safety rule: do not propagate a relative edit through zero in either
                    # direction. This prevents an accidental source 40 -> 0 -> 40 sequence
                    # from zeroing linked maps and then being unable to reconstruct them.
                    linked_updates[(row, col)] = old_target
                    if abs(new_source - old_source) > zero_tol:
                        warnings.append(
                            f"Skipped relative cell [{row},{col}] in {target.title}: relative propagation through zero is disabled"
                        )
                    continue
                factor = new_source / old_source
                linked_updates[(row, col)] = old_target * factor

            packed.extend(self._pack_cell_updates(target, linked_updates))

        linked_label = f"{label} [{mode} linked x{1 + len(targets)}]" if targets else label
        return self.apply_bytes(linked_label, packed), warnings

    def grids_match(self, source: CalibrationDefinition, target: CalibrationDefinition) -> bool:
        if (source.rows, source.cols) != (target.rows, target.cols):
            return False
        for first, second in ((source.x_axis, target.x_axis), (source.y_axis, target.y_axis)):
            a, b = self.axis_values(first), self.axis_values(second)
            if len(a) != len(b) or any(not math.isclose(x, y, rel_tol=1e-10, abs_tol=1e-8) for x, y in zip(a, b)):
                return False
        return True

    def restore_original(self) -> None:
        self.data = bytearray(self.original)
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.dirty = bytes(self.data) != self.saved

    def set_axis_value(self, axis: AxisDefinition, index: int, value: float, label: str) -> bool:
        if axis.address is None or not (0 <= index < axis.count):
            return False
        raw = self.physical_to_raw(value, axis.equation, axis.element_bits, axis.signed)
        width = axis.element_bits // 8
        address = axis.address + index * width
        return self.apply_bytes(label, [(address, self._pack_one(raw, axis.element_bits, axis.signed))])

    def undo(self) -> str | None:
        if not self.undo_stack:
            return None
        cmd = self.undo_stack.pop()
        self._apply_command(cmd, False)
        self.redo_stack.append(cmd)
        return cmd.label

    def redo(self) -> str | None:
        if not self.redo_stack:
            return None
        cmd = self.redo_stack.pop()
        self._apply_command(cmd, True)
        self.undo_stack.append(cmd)
        return cmd.label

    def cell_address(self, definition: CalibrationDefinition, row: int, col: int) -> int:
        return definition.z_address + definition.storage_index(row, col) * definition.z_width

    def cell_raw(self, definition: CalibrationDefinition, row: int, col: int, original: bool = False) -> int:
        address = self.cell_address(definition, row, col)
        src = self.original if original else self.data
        return self._unpack(src, address, 1, definition.z_element_bits, definition.z_signed)[0]

    def cell_value(self, definition: CalibrationDefinition, row: int, col: int, original: bool = False) -> float:
        return self.raw_to_physical(self.cell_raw(definition, row, col, original), definition.z_equation)

    def save_as(self, path: Path | str) -> Path:
        out = Path(path)
        temporary: Path | None = None
        try:
            # Finish the new BIN beside its destination before replacing anything.
            # A failed write or flush leaves the existing file and saved state intact.
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{out.name}.", suffix=".tmp",
                                             dir=out.parent, delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(self.data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, out)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Preserve the original save error if cleanup also fails.
        self.path = out
        self.saved = bytes(self.data)
        self.dirty = False
        return out


def format_number(value: float, decimals: int = 2) -> str:
    if not math.isfinite(value):
        return "nan"
    decimals = max(0, min(8, int(decimals)))
    text = f"{value:.{decimals}f}"
    if decimals > 0:
        text = text.rstrip("0").rstrip(".")
    return text


@dataclass
class Selection:
    anchor_row: int = 0
    anchor_col: int = 0
    active_row: int = 0
    active_col: int = 0

    @property
    def r0(self) -> int:
        return min(self.anchor_row, self.active_row)

    @property
    def r1(self) -> int:
        return max(self.anchor_row, self.active_row)

    @property
    def c0(self) -> int:
        return min(self.anchor_col, self.active_col)

    @property
    def c1(self) -> int:
        return max(self.anchor_col, self.active_col)

    def cells(self) -> list[tuple[int, int]]:
        return [(r, c) for r in range(self.r0, self.r1 + 1) for c in range(self.c0, self.c1 + 1)]

    def set_single(self, row: int, col: int) -> None:
        self.anchor_row = self.active_row = row
        self.anchor_col = self.active_col = col

    def set_rect(self, r0: int, c0: int, r1: int, c1: int) -> None:
        self.anchor_row, self.anchor_col = r0, c0
        self.active_row, self.active_col = r1, c1

    def clamp(self, rows: int, cols: int) -> None:
        self.anchor_row = max(0, min(rows - 1, self.anchor_row))
        self.active_row = max(0, min(rows - 1, self.active_row))
        self.anchor_col = max(0, min(cols - 1, self.anchor_col))
        self.active_col = max(0, min(cols - 1, self.active_col))


class EditEngine:
    """Map editing operations shared by the 2D table and the 3D surface."""

    def __init__(self, document: CalibrationDocument, definition: CalibrationDefinition):
        self.document = document
        self.definition = definition
        self.selection = Selection()
        self.link_targets: list[CalibrationDefinition] = []
        self.link_mode: str | None = None
        self.links_enabled = True
        self.last_warnings: list[str] = []

    def values(self) -> list[list[float]]:
        return self.document.map_values(self.definition)

    def configure_links(self, targets: list[CalibrationDefinition] | None, mode: str | None, enabled: bool = True) -> None:
        self.link_targets = list(targets or [])
        self.link_mode = mode if mode in {'absolute', 'relative'} else None
        self.links_enabled = bool(enabled)

    def _write(self, updates: dict[tuple[int, int], float], label: str) -> bool:
        self.last_warnings = []
        if self.links_enabled and self.link_mode and self.link_targets:
            changed, warnings = self.document.set_cells_linked(
                self.definition, updates, self.link_targets, self.link_mode, label
            )
            self.last_warnings = warnings
            return changed
        return self.document.set_cells(self.definition, updates, label)

    def nudge(self, delta: float) -> bool:
        z = self.values()
        updates = {(r, c): z[r][c] + delta for r, c in self.selection.cells()}
        return self._write(updates, f"Nudge {self.definition.title}: {delta:+g}")

    def multiply(self, factor: float) -> bool:
        z = self.values()
        updates = {(r, c): z[r][c] * factor for r, c in self.selection.cells()}
        return self._write(updates, f"Multiply {self.definition.title} x{factor:g}")

    def set_value(self, value: float) -> bool:
        updates = {(r, c): value for r, c in self.selection.cells()}
        return self._write(updates, f"Set {self.definition.title} = {value:g}")

    def interpolate_horizontal(self) -> bool:
        s = self.selection
        if s.c1 <= s.c0:
            return False
        z = self.values()
        updates: dict[tuple[int, int], float] = {}
        span = s.c1 - s.c0
        for r in range(s.r0, s.r1 + 1):
            left, right = z[r][s.c0], z[r][s.c1]
            for c in range(s.c0, s.c1 + 1):
                t = (c - s.c0) / span
                updates[(r, c)] = left * (1 - t) + right * t
        return self._write(updates, f"Horizontal interpolate {self.definition.title}")

    def interpolate_vertical(self) -> bool:
        s = self.selection
        if s.r1 <= s.r0:
            return False
        z = self.values()
        updates: dict[tuple[int, int], float] = {}
        span = s.r1 - s.r0
        for c in range(s.c0, s.c1 + 1):
            top, bottom = z[s.r0][c], z[s.r1][c]
            for r in range(s.r0, s.r1 + 1):
                t = (r - s.r0) / span
                updates[(r, c)] = top * (1 - t) + bottom * t
        return self._write(updates, f"Vertical interpolate {self.definition.title}")

    def interpolate_area(self) -> bool:
        """Bilinear interpolation from the four selection corners."""
        s = self.selection
        if s.r1 <= s.r0 or s.c1 <= s.c0:
            return self.interpolate_horizontal() or self.interpolate_vertical()
        z = self.values()
        q00 = z[s.r0][s.c0]
        q10 = z[s.r0][s.c1]
        q01 = z[s.r1][s.c0]
        q11 = z[s.r1][s.c1]
        updates: dict[tuple[int, int], float] = {}
        for r in range(s.r0, s.r1 + 1):
            ty = (r - s.r0) / (s.r1 - s.r0)
            for c in range(s.c0, s.c1 + 1):
                tx = (c - s.c0) / (s.c1 - s.c0)
                top = q00 * (1 - tx) + q10 * tx
                bottom = q01 * (1 - tx) + q11 * tx
                updates[(r, c)] = top * (1 - ty) + bottom * ty
        return self._write(updates, f"Area interpolate {self.definition.title}")

    def smooth(self) -> bool:
        """Conservative 3x3 weighted smoothing of selected cells.

        This intentionally behaves like an MTune-style smoothing operation, but V1
        does not claim MaxxECU's proprietary weighting is reproduced bit-for-bit.
        """
        z = self.values()
        s = self.selection
        updates: dict[tuple[int, int], float] = {}
        for r, c in s.cells():
            weighted = 0.0
            total = 0.0
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < self.definition.rows and 0 <= cc < self.definition.cols:
                        weight = 4.0 if (dr == 0 and dc == 0) else (2.0 if dr == 0 or dc == 0 else 1.0)
                        weighted += z[rr][cc] * weight
                        total += weight
            updates[(r, c)] = weighted / total if total else z[r][c]
        return self._write(updates, f"Smooth {self.definition.title}")

    def copy_direction(self, dr: int, dc: int) -> bool:
        z = self.values()
        src = self.selection.cells()
        dest = [(r + dr, c + dc) for r, c in src]
        if any(not (0 <= r < self.definition.rows and 0 <= c < self.definition.cols) for r, c in dest):
            return False
        updates = {(r + dr, c + dc): z[r][c] for r, c in src}
        changed = self._write(updates, f"Copy {self.definition.title} direction ({dr},{dc})")
        if changed:
            self.selection.anchor_row += dr
            self.selection.active_row += dr
            self.selection.anchor_col += dc
            self.selection.active_col += dc
        return changed

    def paste_grid(self, grid: list[list[float]]) -> bool:
        if not grid:
            return False
        sr, sc = self.selection.r0, self.selection.c0
        updates: dict[tuple[int, int], float] = {}
        for r_off, row in enumerate(grid):
            for c_off, value in enumerate(row):
                r, c = sr + r_off, sc + c_off
                if 0 <= r < self.definition.rows and 0 <= c < self.definition.cols:
                    updates[(r, c)] = value
        return self._write(updates, f"Paste {self.definition.title}")

    def move_selection(self, dr: int, dc: int, extend: bool = False) -> None:
        s = self.selection
        if extend:
            s.active_row += dr
            s.active_col += dc
        else:
            height = s.r1 - s.r0
            width = s.c1 - s.c0
            nr = max(0, min(self.definition.rows - 1 - height, s.r0 + dr))
            nc = max(0, min(self.definition.cols - 1 - width, s.c0 + dc))
            s.set_rect(nr, nc, nr + height, nc + width)
        s.clamp(self.definition.rows, self.definition.cols)

    def grow(self) -> None:
        s = self.selection
        s.set_rect(max(0, s.r0 - 1), max(0, s.c0 - 1), min(self.definition.rows - 1, s.r1 + 1), min(self.definition.cols - 1, s.c1 + 1))

    def reduce(self) -> None:
        s = self.selection
        r0, r1, c0, c1 = s.r0, s.r1, s.c0, s.c1
        if r1 > r0 + 1:
            r0 += 1; r1 -= 1
        elif r1 > r0:
            r1 -= 1
        if c1 > c0 + 1:
            c0 += 1; c1 -= 1
        elif c1 > c0:
            c1 -= 1
        s.set_rect(r0, c0, r1, c1)
