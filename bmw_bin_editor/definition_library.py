"""Validated definition catalog and read-only binary matching.

The catalog contains definitions and small verification hashes, never original
firmware images. A match proposes a definition; it does not select or load it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

from .core import EditorError


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def library_root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])) / "definitions" / "library"


@dataclass
class LibraryEntry:
    key: str
    title: str
    xdf_path: Path
    size: int
    metadata: dict = field(default_factory=dict)

    @property
    def path(self) -> Path:
        return self.xdf_path


@dataclass
class DefinitionMatch:
    entry: LibraryEntry
    path: Path
    score: int
    confidence: str
    reasons: list[str]
    usable: bool = True


def discover_entries(root: Path | str | None = None) -> list[LibraryEntry]:
    root = Path(root) if root is not None else library_root()
    manifest = root / "manifest.json"
    if not manifest.is_file():
        return []
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict) or payload.get("format") != 1:
        return []
    records = payload.get("entries", [])
    if not isinstance(records, list):
        return []
    entries = []
    for record in records:
        if not isinstance(record, dict):
            continue
        try:
            path = (root / record["xdf"]).resolve()
            # The retained 361830 definition lives one directory above library.
            path.relative_to(root.resolve().parent)
            if not path.is_file() or path.suffix.lower() != ".xdf":
                continue
            if record.get("xdf_sha256") != sha256(path.read_bytes()):
                continue
            size = int(record["byte_length"])
            if size <= 0 or record.get("usable") is not True:
                continue
            entries.append(LibraryEntry(record["key"], record["title"], path, size, record))
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return entries


def software_identifiers(data: bytes) -> list[str]:
    # Bosch identifiers are sometimes adjacent to another numeric identifier.
    return sorted({m.group().decode("ascii") for m in re.finditer(rb"103[0-9]{7}", data)})


def _fingerprints(data: bytes, samples: list[dict]) -> tuple[int, int]:
    matched = 0
    valid = 0
    for sample in samples:
        try:
            offset, length = int(sample["offset"]), int(sample["length"])
            if offset < 0 or length <= 0 or offset + length > len(data):
                continue
            valid += 1
            matched += sha256(data[offset:offset + length]) == sample["sha256"]
        except (KeyError, ValueError, TypeError):
            continue
    return matched, valid


def _legacy_structure(data: bytes, record: dict) -> tuple[int, int]:
    matched = 0
    samples = record.get("structural_axes", [])
    for axis in samples:
        try:
            offset, count, bits = axis["offset"], axis["count"], axis["bits"]
            width = bits // 8
            code = {(8, False): "B", (8, True): "b", (16, False): "H", (16, True): "h",
                    (32, False): "I", (32, True): "i"}[(bits, axis["signed"])]
            if not 0 <= offset <= len(data) - count * width:
                continue
            values = struct.unpack_from(("<" if record.get("byte_order") == "little" else ">") + code * count,
                                        data, offset)
            matched += (count >= 4 and (all(a < b for a, b in zip(values, values[1:])) or
                                       all(a > b for a, b in zip(values, values[1:]))))
        except (KeyError, TypeError, ValueError, struct.error):
            continue
    return matched, len(samples)


def propose_matches(data: bytes, root: Path | str | None = None,
                    entries: list[LibraryEntry] | None = None) -> list[DefinitionMatch]:
    """Return only compatible proposals, ranked by independently checked evidence."""
    entries = discover_entries(root) if entries is None else entries
    digest = sha256(data)
    actual_ids = set(software_identifiers(data))
    matches = []
    for entry in entries:
        if len(data) != entry.size:
            continue
        record = entry.metadata
        expected_ids = set(record.get("software_identifiers", []))
        # A different readable Bosch software ID is negative evidence even if
        # this software happens to share code or an axis layout.
        if actual_ids and expected_ids and not actual_ids.intersection(expected_ids):
            continue
        identity_ok = True
        for anchor in record.get("identity_anchors", []):
            try:
                offset, value = int(anchor["offset"]), bytes.fromhex(anchor["bytes"])
                identity_ok &= bool(value) and data[offset:offset + len(value)] == value
            except (KeyError, TypeError, ValueError):
                identity_ok = False
        if not identity_ok:
            continue
        exact = digest in record.get("original_sha256", [])
        axes_ok, axes_total = _fingerprints(data, record.get("axis_fingerprints", []))
        code_ok, code_total = _fingerprints(data, record.get("code_fingerprints", []))
        id_ok = bool(actual_ids.intersection(expected_ids))
        if exact:
            matches.append(DefinitionMatch(entry, entry.path, 100, "Exact reference image", [
                "File size and SHA-256 match the validated reference-image layout.",
                f"Validated {record.get('map_count', 0):,} definitions for this address layout."]))
            continue
        if record.get("retained"):
            shape_ok, shape_total = _legacy_structure(data, record)
            if id_ok and shape_total >= 8 and shape_ok >= max(8, (shape_total * 3 + 3) // 4):
                matches.append(DefinitionMatch(entry, entry.path, 86, "Software and layout match", [
                    "Bosch software identification matches the retained 361830 definition.",
                    f"File size matches; {shape_ok}/{shape_total} known axes retain a monotonic layout.",
                    "Retained 361830 XDF includes reviewed N75 scaling corrections; no original image fingerprint was available."]))
            continue
        # Calibration changes must leave enough independent layout anchors and
        # executable code unchanged. Matching a filename or ECU family is never
        # evidence. Full-image hashes are only the strongest optional shortcut.
        code_match = code_total >= 8 and code_ok >= (code_total * 3 + 3) // 4
        axes_match = axes_total >= 8 and axes_ok >= (axes_total * 2 + 2) // 3
        if record.get('family') == 'ms43x001':
            # Calibration-only reads have no executable code. Require both
            # version markers and independent layout evidence; size alone is
            # never sufficient, and edited table cells need not match a hash.
            if record.get('identity_anchors') and axes_match and (entry.size == 0x10000 or code_match):
                reasons = ['MS43X001 calibration version markers match.',
                           f'Unchanged axis-layout fingerprints: {axes_ok}/{axes_total}.']
                if entry.size != 0x10000:
                    reasons.append(f'Unchanged executable-code fingerprints: {code_ok}/{code_total}.')
                matches.append(DefinitionMatch(entry, entry.path, 93, 'MS43X001 version and layout match', reasons))
            continue
        if not (code_match and axes_match):
            continue
        if expected_ids and not id_ok:
            # Do not propose software with expected readable ID after that
            # identity was removed or replaced; code/layout alone can collide.
            continue
        reasons = [f"File size and address layout match the {record.get('layout', 'validated')} image.",
                   f"Unchanged executable-code fingerprints: {code_ok}/{code_total}.",
                   f"Unchanged axis-layout fingerprints: {axes_ok}/{axes_total}.",
                   "Calibration values may differ from the original image."]
        if id_ok:
            reasons.insert(1, "Embedded Bosch software identification matches: " + ", ".join(sorted(actual_ids.intersection(expected_ids))) + ".")
        if record.get("identity_anchors"):
            reasons.insert(1, "Embedded project/revision marker matches the image-validated A2L source.")
        matches.append(DefinitionMatch(entry, entry.path, 95 if id_ok else 90,
                                       "Software, code and layout match" if id_ok else "Code and layout match", reasons))
    return sorted(matches, key=lambda item: (-item.score, item.entry.title, item.entry.key))


@dataclass
class MemoryImage:
    """Source image plus explicit sparse-address coverage (no assumed zero fill)."""
    data: bytes
    start: int
    ranges: list[tuple[int, int]]
    source_format: str
    source_sha256: str

    @property
    def end(self) -> int:
        return self.start + len(self.data)

    def covered(self, address: int, length: int) -> bool:
        return length >= 0 and any(begin <= address and address + length <= end for begin, end in self.ranges)

    def read(self, address: int, length: int) -> bytes:
        if not self.covered(address, length):
            raise EditorError(f"Image has no source bytes for 0x{address:X}..0x{address + length:X}")
        return self.data[address - self.start:address - self.start + length]


def parse_intel_hex(raw: bytes, *, maximum_span: int = 0x1000000) -> MemoryImage:
    """Parse Intel HEX, checking every checksum and conflicting overlaps."""
    try:
        # Some original Bosch exports have one trailing DOS text EOF marker.
        # It is not a firmware byte or a second HEX stream.
        lines = raw.rstrip(b" \t\r\n\x1a").decode("ascii").splitlines()
    except UnicodeError as exc:
        raise EditorError("Intel HEX is not ASCII text") from exc
    chunks = []
    upper = 0
    ended = False
    for line_number, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        if ended:
            raise EditorError(f"Intel HEX has data after end-of-file at line {line_number}")
        if not line.startswith(":"):
            raise EditorError(f"Invalid Intel HEX record at line {line_number}")
        try:
            record = bytes.fromhex(line[1:])
        except ValueError as exc:
            raise EditorError(f"Invalid Intel HEX digits at line {line_number}") from exc
        if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 255:
            raise EditorError(f"Invalid Intel HEX length/checksum at line {line_number}")
        count, kind = record[0], record[3]
        offset = int.from_bytes(record[1:3], "big")
        value = record[4:-1]
        if kind == 0:
            if offset + count > 0x10000:
                raise EditorError(f"Intel HEX record crosses an address page at line {line_number}")
            chunks.append((upper + offset, value))
        elif kind == 1 and count == 0 and offset == 0:
            ended = True
        elif kind in (2, 4) and count == 2 and offset == 0:
            upper = int.from_bytes(value, "big") << (4 if kind == 2 else 16)
        elif kind in (3, 5) and count == 4 and offset == 0:
            pass  # Entry point is not an address mapping or image byte.
        else:
            raise EditorError(f"Unsupported/invalid Intel HEX record type {kind} at line {line_number}")
    chunks = [(address, value) for address, value in chunks if value]
    if not ended or not chunks:
        raise EditorError("Intel HEX requires data and an end-of-file record")
    start = min(address for address, value in chunks if value)
    end = max(address + len(value) for address, value in chunks)
    if end - start > maximum_span:
        raise EditorError("Intel HEX address span is too large")
    data = bytearray(b"\xFF" * (end - start))
    present = bytearray(end - start)
    for address, value in chunks:
        offset = address - start
        if any(present[offset:offset + len(value)]):
            for index, byte in enumerate(value):
                if present[offset + index] and data[offset + index] != byte:
                    raise EditorError(f"Conflicting Intel HEX bytes at 0x{address + index:X}")
        data[offset:offset + len(value)] = value
        present[offset:offset + len(value)] = b"\1" * len(value)
    intervals = sorted((address, address + len(value)) for address, value in chunks if value)
    ranges = []
    for begin, finish in intervals:
        if ranges and begin <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(finish, ranges[-1][1]))
        else:
            ranges.append((begin, finish))
    return MemoryImage(bytes(data), start, ranges, "Intel HEX", sha256(raw))


def load_memory_image(path: Path | str) -> MemoryImage:
    raw = Path(path).read_bytes()
    if raw.lstrip().startswith(b":"):
        return parse_intel_hex(raw)
    if len(raw) not in (0x40000, 0x80000, 0x100000, 0x200000, 0x400000):
        raise EditorError("Unrecognized binary image size (not Intel HEX)")
    return MemoryImage(raw, 0, [(0, len(raw))], "Binary", sha256(raw))
