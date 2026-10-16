"""Transactional XDF authoring. Definition changes never write calibration bytes."""
from __future__ import annotations

import copy
import math
import os
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from .core import AxisDefinition, CalibrationDefinition, EditorError, XDFDatabase, parse_int, safe_math


def _child(parent: ET.Element, tag: str) -> ET.Element:
    node = parent.find(tag)
    return node if node is not None else ET.SubElement(parent, tag)


def _text(parent: ET.Element, tag: str, value: str) -> None:
    _child(parent, tag).text = value


def _id_key(value: str) -> tuple[str, int | str]:
    try:
        return "number", int(value, 0)
    except ValueError:
        return "text", value.casefold()


class XDFWorkspace:
    def __init__(self, database: XDFDatabase | None = None, bin_size: int | None = None):
        if bin_size is not None and (type(bin_size) is not int or bin_size < 0):
            raise EditorError("BIN size must be a nonnegative integer")
        self.bin_size = bin_size
        if database is None:
            root = ET.Element("XDFFORMAT", version="1.80")
            header = ET.SubElement(root, "XDFHEADER")
            _text(header, "flags", "0x1")
            _text(header, "fileversion", "1.0")
            _text(header, "deftitle", "BMW calibration definitions")
            _text(header, "description", "")
            _text(header, "author", "Emil Larsson")
            ET.SubElement(header, "BASEOFFSET", offset="0", subtract="0")
            ET.SubElement(header, "DEFAULTS", datasizeinbits="16", sigdigits="2", outputtype="1",
                          signed="0", lsbfirst="0", float="0")
            if bin_size is not None:
                ET.SubElement(header, "REGION", type="0xFFFFFFFF", startaddress="0x0",
                              size=f"0x{bin_size:X}", regioncolor="0x0", regionflags="0x0", name="Binary File")
            tree = ET.ElementTree(root)
            path = Path("Untitled.xdf")
        else:
            tree = copy.deepcopy(database.tree)
            path = database.path
        self._saved_xml = ET.tostring(tree.getroot(), encoding="utf-8")
        self._normalize_ids(tree.getroot())
        self.database = XDFDatabase.from_tree(tree, path)

    @property
    def dirty(self) -> bool:
        return ET.tostring(self.database.root, encoding="utf-8") != self._saved_xml

    @staticmethod
    def _fresh_id(root: ET.Element) -> str:
        used = {_id_key(node.get("uniqueid", "")) for node in root.iter() if node.get("uniqueid")}
        numbers = [value for kind, value in used if kind == "number" and isinstance(value, int)]
        candidate = max([0] + numbers) + 1
        while ("number", candidate) in used:
            candidate += 1
        return f"0x{candidate:X}"

    @classmethod
    def _normalize_ids(cls, root: ET.Element) -> None:
        seen = set()
        for node in root:
            if node.tag not in ("XDFTABLE", "XDFCONSTANT"):
                continue
            ident = node.get("uniqueid", "")
            if not ident or _id_key(ident) in seen:
                ident = cls._fresh_id(root)
                node.set("uniqueid", ident)
            seen.add(_id_key(ident))

    def _find(self, unique_id: str) -> tuple[CalibrationDefinition, ET.Element]:
        # The XML is authoritative. A caller may have edited the mutable returned
        # dataclass in place; compare against a freshly decoded snapshot, not it.
        current = XDFDatabase.from_tree(self.database.tree, self.database.path)
        matches = [d for d in current.definitions if d.unique_id == unique_id]
        nodes = [node for node in self.database.root
                 if node.tag in ("XDFTABLE", "XDFCONSTANT") and node.get("uniqueid") == unique_id]
        if len(matches) != 1 or len(nodes) != 1:
            raise EditorError(f"XDF definition was not found: {unique_id}")
        return matches[0], nodes[0]

    def _commit(self, tree: ET.ElementTree, unique_id: str | None = None):
        try:
            database = XDFDatabase.from_tree(tree, self.database.path)
        except (ValueError, TypeError, ArithmeticError, ET.ParseError) as exc:
            raise EditorError(f"Could not decode the edited XDF: {exc}") from exc
        result = None
        if unique_id is not None:
            result = next((d for d in database.definitions if d.unique_id == unique_id), None)
            if result is None:
                raise EditorError("The edited XDF definition could not be decoded")
        self.database = database
        return result

    @staticmethod
    def _changed(before, after, *fields: str) -> bool:
        return before is None or any(getattr(before, name) != getattr(after, name) for name in fields)

    def _range(self, address: int, count: int, bits: int, label: str) -> None:
        if type(address) is not int or address < 0:
            raise EditorError(f"{label} address must be a nonnegative integer")
        if type(count) is not int or count <= 0:
            raise EditorError(f"{label} dimensions must be positive integers")
        if bits not in (8, 16, 32) or type(bits) is not int:
            raise EditorError(f"{label} element size must be 8, 16 or 32 integer bits")
        end = address + count * (bits // 8)
        if self.bin_size is not None and end > self.bin_size:
            raise EditorError(f"{label} extends outside BIN: 0x{address:X}..0x{end:X} (size 0x{self.bin_size:X})")

    @staticmethod
    def _equation(equation: str, label: str) -> None:
        if not isinstance(equation, str) or not equation.strip():
            raise EditorError(f"{label} equation must not be empty")
        # Non-affine expressions supported by the decoder remain read-only in
        # the calibration editor. Probe more than one raw value for expressions
        # with a singularity at zero; unsupported syntax always raises there.
        failure = None
        for raw in (1.0, 0.0, 2.0, 255.0, -1.0):
            try:
                value = safe_math(equation, raw)
                if math.isfinite(value):
                    return
            except (EditorError, ValueError, TypeError, ArithmeticError, SyntaxError, RecursionError) as exc:
                failure = exc
        raise EditorError(f"{label} equation is not supported by this editor: {equation!r}") from failure

    @staticmethod
    def _strings(definition, *fields: str) -> None:
        if any(not isinstance(getattr(definition, field), str) for field in fields):
            raise EditorError("Definition names, descriptions and units must be text")

    @staticmethod
    def _decimals(value: int, label: str) -> None:
        if type(value) is not int or not 0 <= value <= 8:
            raise EditorError(f"{label} decimal places must be between 0 and 8")

    def _validate(self, definition: CalibrationDefinition, before: CalibrationDefinition | None = None) -> None:
        if not isinstance(definition, CalibrationDefinition):
            raise EditorError("Expected a calibration definition")
        if definition.kind not in ("table", "constant"):
            raise EditorError("Only tables and constants can be authored")
        if before is not None and before.kind != definition.kind:
            raise EditorError("Definition kind cannot be changed; create a new table or constant")
        self._strings(definition, "title", "description", "category", "z_units")
        if not definition.title.strip():
            raise EditorError("Definition title must not be empty")
        storage_changed = self._changed(before, definition, "rows", "cols", "z_address", "z_element_bits", "storage_order")
        if storage_changed:
            if definition.storage_order not in ("row_major", "column_major"):
                raise EditorError("Table storage order must be row_major or column_major")
            if type(definition.rows) is not int or type(definition.cols) is not int or min(definition.rows, definition.cols) <= 0:
                raise EditorError("Table rows and columns must be positive integers")
            if definition.kind == "constant" and (definition.rows, definition.cols) != (1, 1):
                raise EditorError("A constant must have one row and one column")
            if definition.kind == "constant" and definition.storage_order != "row_major":
                raise EditorError("Constants do not have a table storage order")
            self._range(definition.z_address, definition.size, definition.z_element_bits, "Z values")
        if self._changed(before, definition, "z_signed") and type(definition.z_signed) is not bool:
            raise EditorError("Z signedness must be true or false")
        if self._changed(before, definition, "z_equation"):
            self._equation(definition.z_equation, "Z")
        if self._changed(before, definition, "decimal_places"):
            self._decimals(definition.decimal_places, "Z")
        if definition.kind == "constant":
            if definition.x_axis is not None or definition.y_axis is not None:
                raise EditorError("Constants do not have X/Y axes")
            return
        for name, count in (("x_axis", definition.cols), ("y_axis", definition.rows)):
            axis = getattr(definition, name)
            old_axis = getattr(before, name) if before else None
            if axis is None:
                continue
            if not isinstance(axis, AxisDefinition):
                raise EditorError("Expected an axis definition")
            structural = old_axis is None or axis != old_axis or storage_changed
            if not structural:
                continue
            label = name[0].upper() + " axis"
            if type(axis.count) is not int or axis.count <= 0 or axis.count != count:
                raise EditorError(f"{label} count must match its table dimension ({count})")
            if self._changed(old_axis, axis, "address", "count", "element_bits"):
                if axis.address is not None:
                    self._range(axis.address, axis.count, axis.element_bits, label)
                elif axis.element_bits not in (8, 16, 32) or type(axis.element_bits) is not int:
                    raise EditorError(f"{label} element size must be 8, 16 or 32 integer bits")
            if self._changed(old_axis, axis, "signed") and type(axis.signed) is not bool:
                raise EditorError(f"{label} signedness must be true or false")
            self._strings(axis, "units")
            if self._changed(old_axis, axis, "equation"):
                self._equation(axis.equation, label)
            if self._changed(old_axis, axis, "decimal_places"):
                self._decimals(axis.decimal_places, label)
            if self._changed(old_axis, axis, "labels", "count"):
                if not isinstance(axis.labels, list) or any(not isinstance(value, str) or not value.strip() for value in axis.labels):
                    raise EditorError(f"{label} labels must be nonempty text")
                if axis.labels and len(axis.labels) != axis.count:
                    raise EditorError(f"{label} label count must match its axis count")
                if len({value.strip() for value in axis.labels}) != len(axis.labels):
                    raise EditorError(f"{label} labels must be unique")

    @staticmethod
    def _category(root: ET.Element, item: ET.Element, category: str) -> None:
        category = category or ("Constants" if item.tag == "XDFCONSTANT" else "Uncategorized")
        header = _child(root, "XDFHEADER")
        if category == "Uncategorized":
            membership = 0
        else:
            categories = header.findall("CATEGORY")
            existing = next((node for node in categories if node.get("name") == category), None)
            if existing is None:
                index = max([-1] + [parse_int(node.get("index"), -1) for node in categories]) + 1
                existing = ET.SubElement(header, "CATEGORY", index=f"0x{index:X}", name=category)
            membership = parse_int(existing.get("index")) + 1
        members = item.findall("CATEGORYMEM")
        member = max(enumerate(members), key=lambda pair: (parse_int(pair[1].get("index"), 0), pair[0]))[1] if members else ET.SubElement(item, "CATEGORYMEM", index="0")
        member.set("category", str(membership))
        # The parser deduplicates category names in membership order. Move earlier
        # occurrences of the new primary category after the other memberships so
        # switching to an existing parent category still gives the requested primary.
        high = max([0] + [parse_int(node.get("index"), 0) for node in members])
        for other in members:
            if other is not member and parse_int(other.get("category"), 0) == membership:
                high += 1
                other.set("index", str(high))

    @staticmethod
    def _set_math(node: ET.Element, equation: str) -> None:
        math_node = node.find("MATH")
        if math_node is None:
            math_node = ET.SubElement(node, "MATH")
            ET.SubElement(math_node, "VAR", id="X")
        math_node.set("equation", equation)

    @staticmethod
    def _set_signed(node: ET.Element, embedded: ET.Element, signed: bool) -> None:
        flags = parse_int(embedded.get("mmedtypeflags"), 0)
        embedded.set("mmedtypeflags", f"0x{((flags | 1) if signed else (flags & ~1)):X}")
        _text(node, "datatype", "1" if signed else "0")

    def _write_axis(self, item: ET.Element, axis_id: str, axis: AxisDefinition | None,
                    before: AxisDefinition | None) -> None:
        node = item.find(f"./XDFAXIS[@id='{axis_id}']")
        if axis is None:
            if node is not None and before is not None:
                item.remove(node)
            return
        if before == axis:
            return
        if node is None:
            node = ET.SubElement(item, "XDFAXIS", id=axis_id)
        embedded = node.find("EMBEDDEDDATA")
        if self._changed(before, axis, "address", "element_bits", "signed"):
            embedded = _child(node, "EMBEDDEDDATA")
        if self._changed(before, axis, "address"):
            if axis.address is None:
                embedded.attrib.pop("mmedaddress", None)
            else:
                embedded.set("mmedaddress", f"0x{axis.address:X}")
        if self._changed(before, axis, "element_bits"):
            embedded.set("mmedelementsizebits", str(axis.element_bits))
        if self._changed(before, axis, "signed"):
            self._set_signed(node, embedded, axis.signed)
        if self._changed(before, axis, "count"):
            _text(node, "indexcount", str(axis.count))
        if self._changed(before, axis, "equation"):
            self._set_math(node, axis.equation)
        for field, tag in (("units", "units"), ("decimal_places", "decimalpl")):
            if self._changed(before, axis, field):
                _text(node, tag, str(getattr(axis, field)))
        if self._changed(before, axis, "labels"):
            old_labels = node.findall("LABEL")
            for i, value in enumerate(axis.labels):
                label = old_labels[i] if i < len(old_labels) else ET.SubElement(node, "LABEL", index=str(i))
                label.set("value", value)
            for label in old_labels[len(axis.labels):]:
                node.remove(label)

    def _write_definition(self, root: ET.Element, item: ET.Element, definition: CalibrationDefinition,
                          before: CalibrationDefinition | None = None) -> None:
        for field in ("title", "description"):
            if self._changed(before, definition, field):
                _text(item, field, getattr(definition, field))
        if self._changed(before, definition, "category"):
            self._category(root, item, definition.category)
        if definition.kind == "table":
            node = item.find("./XDFAXIS[@id='z']")
            if node is None:
                node = ET.SubElement(item, "XDFAXIS", id="z")
        else:
            node = item
        embedded = _child(node, "EMBEDDEDDATA")
        for field, attribute in (("z_address", "mmedaddress"), ("z_element_bits", "mmedelementsizebits")):
            if self._changed(before, definition, field):
                value = getattr(definition, field)
                embedded.set(attribute, f"0x{value:X}" if field == "z_address" else str(value))
        if definition.kind == "table":
            for field, attribute in (("rows", "mmedrowcount"), ("cols", "mmedcolcount")):
                if self._changed(before, definition, field):
                    embedded.set(attribute, str(getattr(definition, field)))
            if self._changed(before, definition, "rows", "cols", "z_element_bits", "storage_order"):
                if definition.storage_order == "column_major":
                    embedded.set("mmedrowstridebits", str(definition.z_element_bits))
                    embedded.set("mmedcolstridebits", str(definition.rows * definition.z_element_bits))
                elif any(parse_int(embedded.get(attribute), 0) for attribute in ("mmedrowstridebits", "mmedcolstridebits")):
                    embedded.set("mmedrowstridebits", str(definition.cols * definition.z_element_bits))
                    embedded.set("mmedcolstridebits", str(definition.z_element_bits))
        if self._changed(before, definition, "z_signed"):
            self._set_signed(node, embedded, definition.z_signed)
        if self._changed(before, definition, "z_equation"):
            self._set_math(node, definition.z_equation)
        for field, tag in (("z_units", "units"), ("decimal_places", "decimalpl")):
            if self._changed(before, definition, field):
                _text(node, tag, str(getattr(definition, field)))
        if definition.kind == "table":
            for field, axis_id in (("x_axis", "x"), ("y_axis", "y")):
                self._write_axis(item, axis_id, getattr(definition, field), getattr(before, field) if before else None)

    def set_metadata(self, title: str, author: str, description: str, default_lsb_first: bool) -> None:
        if not all(isinstance(value, str) for value in (title, author, description)) or not title.strip():
            raise EditorError("XDF title must not be empty, and header fields must be text")
        if type(default_lsb_first) is not bool:
            raise EditorError("XDF byte order must be true (little endian) or false (big endian)")
        tree = copy.deepcopy(self.database.tree)
        header = _child(tree.getroot(), "XDFHEADER")
        for tag, value in (("deftitle", title), ("author", author), ("description", description)):
            if (header.findtext(tag) or "") != value:
                _text(header, tag, value)
        if self.database.default_lsb_first != default_lsb_first:
            _child(header, "DEFAULTS").set("lsbfirst", "1" if default_lsb_first else "0")
        self._commit(tree)

    def add(self, definition: CalibrationDefinition) -> CalibrationDefinition:
        self._validate(definition)
        tree = copy.deepcopy(self.database.tree)
        root = tree.getroot()
        ident = self._fresh_id(root)
        item = ET.SubElement(root, "XDFTABLE" if definition.kind == "table" else "XDFCONSTANT", uniqueid=ident, flags="0x0")
        self._write_definition(root, item, definition)
        return self._commit(tree, ident)

    def update(self, unique_id: str, definition: CalibrationDefinition) -> CalibrationDefinition:
        before, _ = self._find(unique_id)
        self._validate(definition, before)
        tree = copy.deepcopy(self.database.tree)
        root = tree.getroot()
        item = next(node for node in root if node.tag in ("XDFTABLE", "XDFCONSTANT") and node.get("uniqueid") == unique_id)
        self._write_definition(root, item, definition, before)
        return self._commit(tree, unique_id)

    def remove(self, unique_id: str) -> None:
        self._find(unique_id)
        tree = copy.deepcopy(self.database.tree)
        root = tree.getroot()
        item = next(node for node in root if node.tag in ("XDFTABLE", "XDFCONSTANT") and node.get("uniqueid") == unique_id)
        root.remove(item)
        self._commit(tree)

    def duplicate(self, unique_id: str) -> CalibrationDefinition:
        before, source = self._find(unique_id)
        tree = copy.deepcopy(self.database.tree)
        root = tree.getroot()
        item = copy.deepcopy(source)
        ident = self._fresh_id(root)
        item.set("uniqueid", ident)
        _text(item, "title", before.title + " (copy)")
        root.append(item)
        return self._commit(tree, ident)

    def save_as(self, path: Path | str) -> XDFDatabase:
        out = Path(path)
        if out.suffix.casefold() != ".xdf":
            raise EditorError("Save definitions as an .xdf file. BIN files must be saved from the calibration editor.")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="wb", prefix=f".{out.name}.", suffix=".tmp", dir=out.parent, delete=False) as handle:
                temporary = Path(handle.name)
                self.database.tree.write(handle, encoding="utf-8", xml_declaration=True)
                handle.flush()
                os.fsync(handle.fileno())
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True, insert_pis=True))
            database = XDFDatabase.from_tree(ET.parse(temporary, parser=parser), out)
            if database.root.tag != "XDFFORMAT":
                raise EditorError("Expected an XDFFORMAT document")
            os.replace(temporary, out)
        except (ET.ParseError, ValueError, TypeError, ArithmeticError) as exc:
            raise EditorError(f"Could not validate the saved XDF: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        self.database = database
        self._saved_xml = ET.tostring(database.root, encoding="utf-8")
        return database
