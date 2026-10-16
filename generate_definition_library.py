"""Generate validated, image-paired XDFs without changing source packs or BINs."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import struct
import xml.etree.ElementTree as ET

from bmw_bin_editor.a2l_import import convert_a2l
from bmw_bin_editor.core import EditorError, XDFDatabase
from bmw_bin_editor.definition_library import (MemoryImage, load_memory_image, sha256,
                                              software_identifiers)


def inventory(directory: Path) -> list[dict]:
    sources = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.name.startswith("._"):
            continue
        raw = path.read_bytes()
        sources.append({"path": path, "relative_path": path.relative_to(directory).as_posix(),
                        "sha256": sha256(raw), "length": len(raw), "suffix": path.suffix.lower()})
    return sources


def external_flash(image: MemoryImage) -> tuple[bytes, int]:
    """Use original low-address flash space, explicitly excluding internal ROM.

    These Bosch exports use low-address 1/2 MiB external flash and sometimes
    include a separate 0x400000 internal-code segment. No concatenation or
    speculative offset is applied to that segment.
    """
    external_end = max((min(end, 0x400000) for begin, end in image.ranges if begin < 0x400000), default=0)
    if image.start >= 0x400000 or external_end <= 0:
        raise EditorError("No supported zero-based external flash segment")
    size = next((value for value in (0x40000, 0x80000, 0x100000, 0x200000, 0x400000)
                 if external_end <= value), None)
    if size is None:
        raise EditorError("External flash exceeds supported image layout")
    data = bytearray(b"\xFF" * size)
    for begin, end in image.ranges:
        if begin >= size:
            continue
        end = min(end, size)
        data[begin:end] = image.read(begin, end - begin)
    return bytes(data), 0


def _span(definition):
    spans = [(definition.z_address, definition.size * definition.z_width)]
    for axis in (definition.x_axis, definition.y_axis):
        if axis is not None and axis.address is not None:
            spans.append((axis.address, axis.count * axis.element_bits // 8))
    return spans


def validate_source_coverage(result, image: MemoryImage, address_offset: int = 0) -> None:
    """Drop objects that touch synthesized holes, independently of range checks."""
    records = result.report["records"]
    if len(records) != len(result.definitions):
        raise EditorError("Import records do not correspond one-to-one to definitions")
    retained = []
    remove = set()
    for definition, record in zip(result.definitions, records):
        spans = list(_span(definition))
        for field in record.get("dimension_fields", []):
            spans.append((field["offset"], field["bits"] // 8))
        spans.extend((item["offset"], item["length"]) for item in record.get("spans", []))
        if any(not image.covered(offset + address_offset, length) for offset, length in spans):
            remove.add(definition.unique_id)
            result.report["skipped"].append({"name": definition.title,
                                              "reason": "A2L storage crosses a source image hole/unmapped segment"})
            continue
        retained.append(record)
    if remove:
        root = result.database.root
        for node in list(root):
            if node.get("uniqueid") in remove:
                root.remove(node)
        result.workspace.database = XDFDatabase.from_tree(result.database.tree, result.database.path)
    result.report["records"] = retained
    result.report["converted"] = len(result.definitions)
    result.report["skipped_count"] = len(result.report["skipped"])
    result.report["skip_reasons"] = dict(Counter(item["reason"] for item in result.report["skipped"]))
    result.report["source_coverage_validated"] = True


def validate_json_structure(result, data: bytes) -> None:
    """Corroborate exported two-axis tables against stored Bosch headers.

    Names/IdNames may repeat; each definition remains identified by its storage
    and both explicit axis addresses, never by the display name alone.
    """
    endian = result.report["byte_order"]
    def count_matches(address, count):
        return 0 <= address <= len(data) - 2 and int.from_bytes(data[address:address + 2], endian) == count

    def stored_header(definition, record):
        x, y = definition.x_axis, definition.y_axis
        if not x or not y or x.address is None or y.address is None:
            return False
        xe = x.address + x.count * x.element_bits // 8
        ye = y.address + y.count * y.element_bits // 8
        if ye == x.address and xe == definition.z_address:
            return count_matches(y.address - 4, definition.rows) and count_matches(y.address - 2, definition.cols)
        if xe == y.address and ye == definition.z_address:
            return count_matches(x.address - 4, definition.cols) and count_matches(x.address - 2, definition.rows)
        if ye == x.address - 2 and xe == definition.z_address:
            return count_matches(y.address - 2, definition.rows) and count_matches(x.address - 2, definition.cols)
        if xe == y.address - 2 and ye == definition.z_address:
            return count_matches(x.address - 2, definition.cols) and count_matches(y.address - 2, definition.rows)
        # WinOLS may explicitly reuse axes from an earlier map. Verify the
        # local stored copies and their dimension counts against those declared
        # shared axis bytes, while retaining the exported shared addresses.
        x_length, y_length = xe - x.address, ye - y.address
        raw_x, raw_y = data[x.address:xe], data[y.address:ye]
        def axis_equal(address, raw):
            return 0 <= address <= len(data) - len(raw) and data[address:address + len(raw)] == raw
        def shared_copy(local_x, local_y):
            record["shared_axes_validated"] = {
                "declared_x": x.address, "declared_y": y.address,
                "local_x": local_x, "local_y": local_y,
                "method": "Declared shared axis bytes equal the local stored axis copies; dimension headers match."}
            return True
        z = definition.z_address
        local_y, local_x = z - x_length - y_length, z - x_length
        if (count_matches(local_y - 4, definition.rows) and count_matches(local_y - 2, definition.cols)
                and axis_equal(local_y, raw_y) and axis_equal(local_x, raw_x)):
            return shared_copy(local_x, local_y)
        local_x, local_y = z - x_length - y_length, z - y_length
        if (count_matches(local_x - 4, definition.cols) and count_matches(local_x - 2, definition.rows)
                and axis_equal(local_x, raw_x) and axis_equal(local_y, raw_y)):
            return shared_copy(local_x, local_y)
        local_x, local_y = z - x_length, z - x_length - 2 - y_length
        if (count_matches(local_y - 2, definition.rows) and count_matches(local_x - 2, definition.cols)
                and axis_equal(local_y, raw_y) and axis_equal(local_x, raw_x)):
            return shared_copy(local_x, local_y)
        local_y, local_x = z - y_length, z - y_length - 2 - x_length
        if (count_matches(local_x - 2, definition.cols) and count_matches(local_y - 2, definition.rows)
                and axis_equal(local_x, raw_x) and axis_equal(local_y, raw_y)):
            return shared_copy(local_x, local_y)
        return False
    retained, remove, checked = [], set(), set()
    for definition, record in zip(result.definitions, result.report["records"]):
        if definition.kind == "table" and min(definition.rows, definition.cols) > 1:
            x, y = definition.x_axis, definition.y_axis
            layout = (definition.z_address, definition.rows, definition.cols,
                      x.address if x else None, y.address if y else None)
            if not stored_header(definition, record):
                remove.add(definition.unique_id)
                result.report["skipped"].append({"name": definition.title, "z_offset": definition.z_address,
                    "reason": "Exported two-axis map is not corroborated by stored dimension header and adjacent ROM axes"})
                continue
            checked.add(layout)
            record["structural_header_validated"] = True
            if record.get("shared_axes_validated"):
                note = (f"Shared ROM axes: exported X 0x{x.address:X} / Y 0x{y.address:X} retained. "
                        "Their bytes equal this table's local stored axis copies and its dimension headers match. "
                        "Editing an axis changes the referenced shared storage; local copies are separate bytes.")
                result.report.setdefault("warnings", []).append(f"{definition.title}: {note}")
                node = next(item for item in result.database.root if item.get("uniqueid") == definition.unique_id)
                description = node.find("description")
                if description is None:
                    description = ET.SubElement(node, "description")
                description.text = (description.text or "") + "\n" + note
        retained.append(record)
    if len(checked) < 8:
        raise EditorError("JSON/image pairing has fewer than eight distinct corroborated two-axis headers")
    if remove or any(item.get("shared_axes_validated") for item in retained):
        root = result.database.root
        for node in list(root):
            if node.get("uniqueid") in remove:
                root.remove(node)
        result.workspace.database = XDFDatabase.from_tree(result.database.tree, result.database.path)
    result.report.update(records=retained, converted=len(result.definitions), skipped_count=len(result.report["skipped"]),
                         skip_reasons=dict(Counter(item["reason"] for item in result.report["skipped"])),
                         validated_two_axis_headers=len(checked), json_structure_validated=True,
                         validated_shared_axis_tables=sum(bool(item.get("shared_axes_validated")) for item in retained))


def fingerprints(data: bytes, result, image: MemoryImage) -> tuple[list[dict], list[dict]]:
    axes = {}
    storage = []
    for definition in result.definitions:
        storage.extend(_span(definition))
        for axis in (definition.x_axis, definition.y_axis):
            if axis is None or axis.address is None or axis.count < 4:
                continue
            width = axis.element_bits // 8
            length = axis.count * width
            raw = data[axis.address:axis.address + length]
            if len(set(raw)) < 3:
                continue
            axes[(axis.address, length)] = {"offset": axis.address, "length": length, "sha256": sha256(raw)}
    available_axes = sorted(axes.values(), key=lambda item: item["offset"])
    if len(available_axes) > 32:
        available_axes = [available_axes[index * (len(available_axes) - 1) // 31] for index in range(32)]
    # Executable-code anchors must be before calibration storage, not arbitrary
    # unidentified gaps among tables/constants that a tune might legitimately edit.
    meaningful = [offset for offset, length in storage if offset >= 0x10000]
    ceiling = min(meaningful, default=0)
    candidates = []
    length = 256
    for offset in range(0x10000, max(0x10000, ceiling - length), 0x1000):
        if not image.covered(offset, length):
            continue
        raw = data[offset:offset + length]
        if len(set(raw)) >= 32 and raw.count(255) < length // 2 and raw.count(0) < length // 2:
            candidates.append({"offset": offset, "length": length, "sha256": sha256(raw)})
    if len(candidates) > 16:
        candidates = [candidates[index * (len(candidates) - 1) // 15] for index in range(16)]
    return available_axes, candidates


def verify_pair_identity(report: dict, image: MemoryImage, data: bytes) -> list[dict]:
    """Require a source-declared version to agree with bytes in the image."""
    declared_epk = report.get("epk")
    addresses = report.get("epk_addresses", [])
    if declared_epk:
        if not addresses:
            raise EditorError("A2L declares EPK but no verifiable EPK address")
        value = declared_epk.encode("ascii")
        anchors = []
        for address in addresses:
            if image.read(address, len(value)) != value:
                raise EditorError("A2L EPK identifier does not match the paired image")
            if 0 <= address <= len(data) - len(value):
                anchors.append({"offset": address, "bytes": value.hex()})
        if not anchors:
            raise EditorError("A2L EPK lies outside the supported BIN layout")
        report["pair_identity"] = {"method": "A2L EPK", "value": declared_epk}
        return anchors
    # The provided BMW Bosch exports declare Pxxx and Vxx.x in PROJECT;
    # firmware embeds those values as P_xxx_WI.x.x.x. This connects definitions
    # to image identity without trusting a vehicle folder or source filename.
    project = re.fullmatch(r"P([0-9]{3})", report.get("project", ""), re.I)
    revision = re.search(r"\bV([A-Z0-9]+\.[A-Z0-9]+)\b", report.get("project_description", ""), re.I)
    markers = list(re.finditer(rb"P_([0-9]{3})_WI\.([A-Z0-9]+(?:\.[A-Z0-9]+)*)", data))
    if not project or not revision or not markers:
        raise EditorError("No verifiable A2L-to-image project/revision identity")
    normalize = lambda value: re.sub(r"[^A-Z0-9]", "", value.upper())
    matches = [marker for marker in markers if marker[1].decode() == project[1] and
               normalize(marker[2].decode()) == normalize(revision[1])]
    if not matches:
        raise EditorError("A2L project/revision does not match the paired firmware identity")
    # A pack that points at the wrong version can accidentally decode plausible
    # scalars. Widespread invalid stored axis counts are independent rejection.
    successful = sum(bool(item.get("dimension_fields")) for item in report["records"])
    failures = sum("dimension count" in item["reason"] for item in report["skipped"])
    if successful < 8 or failures > max(2, successful // 20):
        raise EditorError("A2L/image dynamic dimension checks do not support a common address layout")
    report["pair_identity"] = {"method": "A2L PROJECT and firmware project/revision marker",
                                "project": report["project"], "revision": revision[1],
                                "validated_dimension_records": successful, "invalid_dimension_records": failures}
    return [{"offset": marker.start(), "bytes": marker.group().hex()} for marker in matches]


def _retained_entry(output: Path) -> dict | None:
    path = output.parent / "EDC16C31 361830.xdf"
    if not path.is_file():
        return None
    database = XDFDatabase(path)
    axes = {}
    for definition in database.definitions:
        for axis in (definition.x_axis, definition.y_axis):
            if axis is not None and axis.address is not None and axis.count >= 4:
                axes[(axis.address, axis.count, axis.element_bits, axis.signed)] = {
                    "offset": axis.address, "count": axis.count, "bits": axis.element_bits, "signed": axis.signed}
    values = sorted(axes.values(), key=lambda item: item["offset"])
    if len(values) > 32:
        values = [values[index * (len(values) - 1) // 31] for index in range(32)]
    nomenclature = next((item["nomenclature"] for item in _previous_entries(output)
                         if item.get("key") == "retained-361830" and isinstance(item.get("nomenclature"), dict)),
                        {"ecu_type": "EDC16C31", "software_label": "361830",
                         "evidence": "User's reviewed EDC16C31 361830 definition."})
    return {"key": "retained-361830", "title": "EDC16C31 361830",
            "xdf": "../" + path.name, "xdf_sha256": sha256(path.read_bytes()), "byte_length": 0x100000,
            "nomenclature": nomenclature,
            "byte_order": "big", "software_identifiers": ["1037361830"], "usable": True, "retained": True,
            "map_count": len(database.definitions), "table_count": sum(d.kind == "table" for d in database.definitions),
            "layout": "1 MiB full flash, original address layout", "address_offset": 0,
            "structural_axes": values, "original_sha256": [], "axis_fingerprints": [], "code_fingerprints": [],
            "notes": ["User's 361830 definition retained with the reviewed N75 native-command scaling correction (100/8192) in v1.32.0.",
                      "No paired original image is available in this source folder; software and structural layout checks required."]}


def _verify_xdf_roundtrip(path: Path, result) -> XDFDatabase:
    decoded = XDFDatabase(path)
    if len(decoded.definitions) != len(result.definitions):
        raise EditorError("Generated XDF round-trip lost definitions")
    for before, after in zip(result.definitions, decoded.definitions):
        for attr in ("z_address", "rows", "cols", "z_signed", "z_element_bits", "z_equation", "x_axis", "y_axis", "storage_order"):
            if getattr(before, attr) != getattr(after, attr):
                raise EditorError(f"Generated XDF round-trip changed {before.title}/{attr}")
    return decoded


def _previous_entries(output: Path) -> list[dict]:
    try:
        payload = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        entries = payload.get("entries", [])
        return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def _safe_xdf_path(output: Path, filename: str) -> Path:
    """Accept a local XDF basename on both Windows and POSIX hosts."""
    if (not isinstance(filename, str) or not filename or
            re.search(r'[<>:"/\\|?*\x00-\x1f]', filename) or
            filename.endswith((" ", ".")) or Path(filename).suffix.lower() != ".xdf" or
            re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", filename, re.I)):
        raise EditorError(f"Unsafe XDF filename: {filename!r}")
    output = output.resolve()
    path = output / filename
    if path.resolve().parent != output:
        raise EditorError(f"XDF output escapes the library directory: {filename!r}")
    return path


def _profile_naming(output: Path, key: str, profile: str, source_stem: str, identifiers: list[str],
                    previous_entries: list[dict] | None = None,
                    claimed_paths: dict[str, str] | None = None) -> tuple[str, Path, dict]:
    """Keep reviewed names and never replace a different or edited profile."""
    previous_entries = _previous_entries(output) if previous_entries is None else previous_entries
    old = next((item for item in previous_entries if item.get("key") == key), {})
    reviewed = old.get("nomenclature")
    if isinstance(reviewed, dict) and reviewed.get("ecu_type") and reviewed.get("software_label"):
        nomenclature = dict(reviewed)
        title = old.get("title") or f"{reviewed['ecu_type']} {reviewed['software_label']}"
        filename = old.get("xdf") or f"{reviewed['ecu_type']} {reviewed['software_label']}.xdf"
    else:
        bosch_ids = sorted(identifier for identifier in identifiers if re.fullmatch(r"1037[0-9]{6}", identifier))
        label = bosch_ids[0][-6:] if bosch_ids else re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", source_stem).strip(" .")[:120]
        label = label or "source"
        nomenclature = {"ecu_type": "EDC16", "software_label": label,
                        "evidence": f"Embedded Bosch software identifier {bosch_ids[0]}; ECU subtype unverified."
                                    if bosch_ids else "Source stem; ECU subtype and numeric software identifier unverified."}
        title = f"EDC16 {label}"
        filename = title + ".xdf"
    path = _safe_xdf_path(output, filename)
    claims = claimed_paths if claimed_paths is not None else {}

    def available(candidate: Path) -> bool:
        name = candidate.name.casefold()
        if name in claims and claims[name] != profile:
            return False
        owners = [item for item in previous_entries
                  if isinstance(item.get("xdf"), str) and item["xdf"].casefold() == name]
        if any(item.get("key") != key or item.get("profile_id", profile) != profile for item in owners):
            return False
        if any(item.name.casefold() == name and item.name != candidate.name for item in output.iterdir()):
            return False
        if candidate.is_symlink():
            return False
        if not candidate.exists():
            return True
        if not candidate.is_file():
            return False
        try:
            if ET.parse(candidate).findtext("./XDFHEADER/BMWBINEDITORPROFILE") != profile:
                return False
            return all(not item.get("xdf_sha256") or sha256(candidate.read_bytes()) == item["xdf_sha256"]
                       for item in owners)
        except (OSError, ET.ParseError):
            return False

    if not available(path):
        digest = sha256(key.encode("utf-8"))
        for length in (8, 12, 16, 64):
            candidate = _safe_xdf_path(output, f"{path.stem} {digest[:length]}.xdf")
            if available(candidate):
                path = candidate
                break
        else:
            raise EditorError(f"No safe XDF filename is available for profile {key}")
    claims[path.name.casefold()] = profile
    return title, path, nomenclature


def generate_json_profile(json_path: Path, image_path: Path, output: Path,
                          previous_entries: list[dict] | None = None,
                          claimed_paths: dict[str, str] | None = None) -> tuple[dict, dict]:
    from bmw_bin_editor.ols_json_import import convert_ols_json

    json_path, image_path, output = json_path.resolve(), image_path.resolve(), output.resolve()
    image = load_memory_image(image_path)
    data, base = external_flash(image)
    result = convert_ols_json(json_path, image=data, address_offset=base)
    digest = sha256(json_path.read_bytes())
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", json_path.stem)
    key = f"WinOLS-{stem}-{digest[:8]}-{image.source_sha256[:8]}-{len(data):X}"
    if result.report.get("recognition_eligible") is not True:
        rejected_name = key + ".rejected.import.json"
        result.report.update(reference_image_name=image_path.name, source_image_sha256=image.source_sha256,
                             xdf=None, usable=False)
        (output / rejected_name).write_text(json.dumps(result.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        raise EditorError(f"JSON byte order/axes/headers are not eligible for automatic recognition; see {rejected_name}")
    validate_source_coverage(result, image, base)
    validate_json_structure(result, data)
    axes, code = fingerprints(data, result, image)
    if len(axes) < 8 or len(code) < 8:
        raise EditorError("JSON/image pairing lacks independent axis/code recognition anchors")
    ids = software_identifiers(data)
    if not ids:
        raise EditorError("JSON/image pairing has no readable Bosch software identifier")
    identity_anchors = [{"offset": match.start(), "bytes": match.group().hex()}
                        for identifier in ids for match in re.finditer(re.escape(identifier.encode("ascii")), data)]
    profile = f"ols-json:{digest}:{image.source_sha256}:{len(data):X}"
    ET.SubElement(result.database.root.find("XDFHEADER"), "BMWBINEDITORPROFILE").text = profile
    result.workspace.database = XDFDatabase.from_tree(result.database.tree, result.database.path)
    title, xdf_path, nomenclature = _profile_naming(output, key, profile, json_path.stem, ids,
                                                  previous_entries, claimed_paths)
    result.workspace.set_metadata(title, "Emil Larsson",
        f'Validated WinOLS JSON import: {json_path.name}.\nSource JSON SHA-256: {digest}\n'
        f'Paired reference image SHA-256: {image.source_sha256}\n'
        f'File offset = ECU address - 0x{base:X}. Explicit source byte order, signedness and conversion factors retained.\n'
        'Broken, unsupported or structurally inconsistent entries are omitted; see the accompanying import report.\n'
        'Display names may repeat; definitions are kept separate by their original storage addresses.',
        result.report["byte_order"] == "little")
    result.workspace.save_as(xdf_path)
    decoded = _verify_xdf_roundtrip(xdf_path, result)
    report_name = key + ".import.json"
    result.report.update(source_image_sha256=image.source_sha256, source_name=json_path.name,
                        source_sha256=digest, reference_image_name=image_path.name,
                        reference_image_sha256=sha256(data), bin_size=len(data), address_offset=base,
                        source_address_ranges=image.ranges, source_type="WinOLS JSON",
                        xdf=xdf_path.name, nomenclature=nomenclature, xdf_roundtrip_validated=True,
                        pair_identity={"method": "Explicit paired reference image, Bosch software identifiers and stored map layout",
                                       "software_identifiers": ids,
                                       "validated_two_axis_headers": result.report["validated_two_axis_headers"]})
    (output / report_name).write_text(json.dumps(result.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"key": key, "title": title, "xdf": xdf_path.name, "xdf_sha256": sha256(xdf_path.read_bytes()),
            "profile_id": profile, "nomenclature": nomenclature,
            "usable": True, "source_type": "WinOLS JSON", "byte_length": len(data),
            "byte_order": result.report["byte_order"], "address_offset": base,
            "layout": f"{len(data)//0x100000} MiB zero-based external flash", "software_identifiers": ids,
            "original_sha256": [sha256(data)], "source_json_sha256": digest,
            "source_image_sha256": image.source_sha256, "source_paths": [json_path.name], "image_paths": [image_path.name],
            "map_count": len(decoded.definitions), "table_count": sum(d.kind == "table" for d in decoded.definitions),
            "axis_fingerprints": axes, "code_fingerprints": code, "identity_anchors": identity_anchors,
            "pair_identity": result.report["pair_identity"], "import_report": report_name,
            "skipped_objects": result.report["skipped_count"]}, result.report


def generate_native_profile(ols_path: Path, output: Path, version_index: int = 0,
                            previous_entries: list[dict] | None = None,
                            claimed_paths: dict[str, str] | None = None) -> tuple[dict, dict]:
    """Create an image-verified catalog entry from a supported native OLS."""
    from bmw_bin_editor.ols_import import load_ols
    from bmw_bin_editor.ols_to_xdf import convert_ols

    ols_path, output = ols_path.resolve(), output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    project = load_ols(ols_path)
    result = convert_ols(project, version_index)
    data = next(item.data for item in project.versions if item.index == version_index)
    digest, image_digest = project.source_sha256, sha256(data)
    image = MemoryImage(data, 0, [(0, len(data))], "Embedded native OLS BIN", image_digest)
    if not result.report["recognition_eligible"]:
        raise EditorError("Native OLS conversion lacks independently corroborated two-axis headers")
    validate_source_coverage(result, image)
    axes, code = fingerprints(data, result, image)
    if len(axes) < 8 or len(code) < 8:
        raise EditorError("Native OLS lacks independent axis/code recognition anchors")
    ids = software_identifiers(data)
    software_label = project.metadata["sw_number"]
    if not any(identifier.endswith(software_label) for identifier in ids):
        raise EditorError("Native OLS header software number disagrees with its embedded BIN")
    key = f"NativeOLS-{digest[:8]}-{version_index}-{image_digest[:8]}"
    profile = result.report["profile_id"]
    nomenclature = {"ecu_type": project.metadata["ecu_name"], "software_label": software_label,
                    "evidence": "Native OLS project header and matching embedded Bosch software identifier."}
    previous_entries = _previous_entries(output) if previous_entries is None else previous_entries
    naming_entries = list(previous_entries)
    if not any(item.get("key") == key for item in naming_entries):
        naming_entries.append({"key": key, "title": project.title, "xdf": project.title + ".xdf",
                               "profile_id": profile, "nomenclature": nomenclature})
    title, xdf_path, nomenclature = _profile_naming(output, key, profile, project.title, ids,
                                                   naming_entries, claimed_paths)
    result.workspace.set_metadata(title, "Emil Larsson",
        f'Validated native WinOLS import: {ols_path.name}.\nOLS SHA-256: {digest}\n'
        f'Embedded {project.versions[version_index].name} BIN SHA-256: {image_digest}\n'
        'Zero-based original BIN offsets; source integer types, signedness, factors and folders retained.\n'
        'Native table COLUMN_DIR interpretation retains source X/Y assignments; '
        'unresolved, nonmonotonic or structurally inconsistent entries are omitted. '
        'Native display settings are preserved in the complete settings export; XDF displays six decimal places.\n'
        'See the accompanying import report for evidence and conversion limitations.',
        result.report["byte_order"] == "little")
    result.workspace.save_as(xdf_path)
    decoded = _verify_xdf_roundtrip(xdf_path, result)
    report_name = xdf_path.stem + ".import.json"
    identity_anchors = [{"offset": match.start(), "bytes": match.group().hex()}
                        for identifier in ids for match in re.finditer(re.escape(identifier.encode("ascii")), data)]
    result.report.update(xdf=xdf_path.name, xdf_sha256=sha256(xdf_path.read_bytes()), nomenclature=nomenclature,
                         source_image_sha256=image_digest, source_address_ranges=image.ranges,
                         xdf_roundtrip_validated=True,
                         pair_identity={"method": "Native OLS exact version slots, source header and embedded Bosch software identifiers",
                                        "software_identifiers": ids, "software_label": software_label,
                                        "validated_two_axis_headers": result.report["validated_two_axis_headers"]})
    (output / report_name).write_text(json.dumps(result.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        source_display_path = ols_path.relative_to(Path(__file__).resolve().parent).as_posix()
    except ValueError:
        source_display_path = ols_path.name
    entry = {"key": key, "title": title, "xdf": xdf_path.name, "xdf_sha256": result.report["xdf_sha256"],
             "profile_id": profile, "nomenclature": nomenclature, "usable": True, "source_type": "Native OLS",
             "byte_length": len(data), "byte_order": result.report["byte_order"], "address_offset": 0,
             "layout": f"{len(data)//0x100000} MiB original zero-based embedded BIN", "software_identifiers": ids,
             "original_sha256": [image_digest], "source_ols_sha256": digest, "source_image_sha256": image_digest,
             "source_paths": [source_display_path], "version_index": version_index,
             "map_count": len(decoded.definitions), "table_count": result.report["table_count"],
             "axis_fingerprints": axes, "code_fingerprints": code, "identity_anchors": identity_anchors,
             "pair_identity": result.report["pair_identity"], "import_report": report_name,
             "skipped_objects": result.report["skipped_count"]}
    return entry, result.report


def generate_library(source: Path, output: Path, json_pairs: list[tuple[Path, Path]] | None = None,
                     native_projects: list[Path] | None = None) -> dict:
    source, output = source.resolve(), output.resolve()
    if not source.is_dir():
        raise EditorError(f"Source map-pack directory was not found: {source}")
    if output == source or source in output.parents:
        raise EditorError("Library output must be separate from original map packs")
    output.mkdir(parents=True, exist_ok=True)
    archive_audits = {}
    audit_path = output / "archive_audit.json"
    if not audit_path.is_file():
        audit_path = Path(__file__).resolve().parent / "definitions" / "library" / "archive_audit.json"
    if audit_path.is_file():
        try:
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
            archive_audits = {item["archive_sha256"]: item for item in audit_payload.get("archives", [])}
            if audit_path.parent != output:
                (output / "archive_audit.json").write_text(json.dumps(audit_payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        except (OSError, ValueError, TypeError, KeyError):
            archive_audits = {}
    previous = {}
    if (output / "manifest.json").is_file():
        try:
            previous = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    files = inventory(source)
    definitions = {}
    images = {}
    issues = []
    for item in files:
        if item["suffix"] == ".a2l":
            definitions.setdefault(item["sha256"], []).append(item)
        elif item["suffix"] in (".hex", ".org", ".dat", ".bin") or re.fullmatch(r"\.h\d+", item["suffix"]):
            images.setdefault(item["sha256"], []).append(item)
        elif item["suffix"] in (".ols", ".dam", ".rar"):
            reason = {".ols": "WinOLS project retained; use --ols for supported native legacy-format imports.",
                      ".dam": "Legacy proprietary DAM syntax unsupported; inspected examples identify EDC15C.",
                      ".rar": "Archive not extracted; loose source files are processed."}[item["suffix"]]
            issue = {"path": item["relative_path"], "source_sha256": item["sha256"], "reason": reason}
            if item["suffix"] == ".rar" and item["sha256"] in archive_audits:
                issue.update(reason=archive_audits[item["sha256"]]["reason"], archive_audit="archive_audit.json")
            issues.append(issue)
    parsed_images = {}
    for digest, aliases in images.items():
        try:
            image = load_memory_image(aliases[0]["path"])
            data, base = external_flash(image)
            parsed_images[digest] = (image, data, base, aliases)
        except (EditorError, OSError, ValueError) as exc:
            issues.append({"paths": [item["relative_path"] for item in aliases], "source_sha256": digest,
                           "reason": str(exc)})
    entries = []
    claimed_paths = {}
    legacy = _retained_entry(output)
    if legacy:
        entries.append(legacy)
    conversions = []
    for digest, aliases in definitions.items():
        path = aliases[0]["path"]
        metadata = path.read_bytes()[:65536].decode("cp1252", errors="replace")
        if re.search(r'\bEDC\s*15[A-Za-z0-9]*\b', metadata, re.I):
            issues.append({"paths": [item["relative_path"] for item in aliases], "source_sha256": digest,
                           "reason": "Definition identifies EDC15, outside this EDC16 library."})
            continue
        parents = {item["path"].parent for item in aliases}
        paired = [(image_digest, payload) for image_digest, payload in parsed_images.items()
                  if any(item["path"].parent in parents for item in payload[3])]
        if not paired:
            issues.append({"paths": [item["relative_path"] for item in aliases], "source_sha256": digest,
                           "reason": "No paired original image: cannot validate addresses, dynamic axis lengths or recognition."})
            continue
        for image_digest, (image, data, base, image_aliases) in paired:
            try:
                result = convert_a2l(path, len(data), address_offset=base, image=data)
                validate_source_coverage(result, image, base)
                if not result.definitions:
                    raise EditorError("No usable integer definitions remain after source-image validation")
                identity_anchors = verify_pair_identity(result.report, image, data)
                axis_samples, code_samples = fingerprints(data, result, image)
                if len(axis_samples) < 8 or len(code_samples) < 8:
                    raise EditorError("Insufficient independent code/axis verification anchors for reliable BIN proposals")
                stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem)
                key = f"{stem}-{digest[:8]}-{image_digest[:8]}-{len(data):X}"
                profile = f"a2l:{digest}:{image_digest}:external-{len(data):X}"
                ET.SubElement(result.database.root.find("XDFHEADER"), "BMWBINEDITORPROFILE").text = profile
                result.workspace.database = XDFDatabase.from_tree(result.database.tree, result.database.path)
                ids = software_identifiers(data)
                title, xdf_path, nomenclature = _profile_naming(output, key, profile, path.stem, ids,
                                                              previous.get("entries", []), claimed_paths)
                result.workspace.set_metadata(title, "Emil Larsson",
                    f'Image-validated import of {path.name}. Supported integer definitions only.\n'
                    f'Source A2L SHA-256: {digest}\nSource image SHA-256: {image_digest}\n'
                    f'File offset = ECU address - 0x{base:X}; {len(data):,}-byte external-flash layout.\n'
                    'See matching import report for skipped objects, record orientation and conversion provenance.',
                    result.report["byte_order"] == "little")
                result.workspace.save_as(xdf_path)
                # XDF encode/decode must preserve all supported interpretation.
                decoded = _verify_xdf_roundtrip(xdf_path, result)
                report_name = key + ".import.json"
                result.report.update(source_paths=[item["relative_path"] for item in aliases],
                    image_paths=[item["relative_path"] for item in image_aliases], image_source_sha256=image_digest,
                    image_format=image.source_format, source_address_ranges=image.ranges,
                    excluded_address_ranges=[item for item in image.ranges if item[0] >= len(data)],
                    synthesized_holes="Missing external-flash bytes are FF only for recognition; definitions touching holes are excluded.",
                    xdf=xdf_path.name, nomenclature=nomenclature, xdf_roundtrip_validated=True)
                (output / report_name).write_text(json.dumps(result.report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
                entries.append({"key": key, "title": title, "xdf": xdf_path.name,
                    "xdf_sha256": sha256(xdf_path.read_bytes()), "profile_id": profile,
                    "nomenclature": nomenclature, "usable": True,
                    "byte_length": len(data), "byte_order": result.report["byte_order"], "address_offset": base,
                    "layout": f"{len(data)//0x100000} MiB zero-based external flash", "software_identifiers": ids,
                    "original_sha256": [sha256(data)], "source_a2l_sha256": digest,
                    "source_image_sha256": image_digest, "source_paths": result.report["source_paths"],
                    "image_paths": result.report["image_paths"], "map_count": len(decoded.definitions),
                    "table_count": sum(d.kind == "table" for d in decoded.definitions),
                    "axis_fingerprints": axis_samples, "code_fingerprints": code_samples,
                    "identity_anchors": identity_anchors, "pair_identity": result.report["pair_identity"],
                    "import_report": report_name, "skipped_objects": result.report["skipped_count"]})
                conversions.append({"key": key, "converted": result.report["converted"],
                                    "skipped": result.report["skipped_count"]})
            except (EditorError, OSError, ValueError, TypeError, KeyError) as exc:
                issues.append({"paths": [item["relative_path"] for item in aliases], "source_sha256": digest,
                               "image_sha256": image_digest, "reason": str(exc)})
    additional_sources, seen_pairs = [], set()
    json_pairs = json_pairs or []
    for json_path, image_path in json_pairs:
        try:
            json_digest, image_digest = sha256(json_path.read_bytes()), sha256(image_path.read_bytes())
            pair = (json_digest, image_digest)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            additional_sources.append({"source_name": json_path.name, "source_sha256": json_digest,
                                       "image_name": image_path.name, "image_sha256": image_digest})
            entry, report = generate_json_profile(json_path, image_path, output,
                                                  previous.get("entries", []), claimed_paths)
            entries.append(entry)
            conversions.append({"key": entry["key"], "converted": report["converted"], "skipped": report["skipped_count"]})
        except (EditorError, OSError, ValueError, TypeError, KeyError) as exc:
            issues.append({"paths": [str(json_path)], "paired_image": image_path.name, "source_type": "WinOLS JSON",
                           "reason": str(exc)})
    for ols_path in native_projects or []:
        try:
            entry, report = generate_native_profile(ols_path, output, previous_entries=previous.get("entries", []),
                                                    claimed_paths=claimed_paths)
            if not any(item["key"] == entry["key"] for item in entries):
                entries.append(entry)
                conversions.append({"key": entry["key"], "converted": report["converted"],
                                    "skipped": report["skipped_count"]})
            additional_sources.append({"source_name": ols_path.name, "source_sha256": entry["source_ols_sha256"],
                                       "source_type": "Native OLS", "version_index": entry["version_index"]})
        except (EditorError, OSError, ValueError, TypeError, KeyError) as exc:
            issues.append({"paths": [str(ols_path)], "source_type": "Native OLS", "reason": str(exc)})
    # Regeneration must retain unchanged validated native imports, including
    # those whose original OLS is outside the A2L inventory directory.
    for old in previous.get("entries", []):
        source_type = old.get("source_type")
        if source_type not in ("WinOLS JSON", "Native OLS") or (source_type == "WinOLS JSON" and json_pairs):
            continue
        if any(item["key"] == old["key"] for item in entries):
            continue
        path = (output / old["xdf"]).resolve()
        if path.parent == output and path.is_file() and sha256(path.read_bytes()) == old.get("xdf_sha256"):
            entries.append(old)
    if not json_pairs:
        additional_sources.extend(item for item in previous.get("additional_sources", []) if item not in additional_sources)
    manifest = {"format": 1, "description": "Validated XDF proposals; the user chooses whether to load a proposal.",
                "entries": entries, "sources": [{key: value for key, value in item.items() if key != "path"} for item in files],
                "additional_sources": additional_sources, "issues": issues, "conversions": conversions,
                "summary": {"original_files": len(files), "unique_a2l_sources": len(definitions),
                            "unique_image_sources": len(images), "generated_xdfs": len(entries) - bool(legacy),
                            "retained_xdfs": int(bool(legacy)), "json_profiles": sum(item.get("source_type") == "WinOLS JSON" for item in entries),
                            "native_ols_profiles": sum(item.get("source_type") == "Native OLS" for item in entries),
                            "definitions": sum(item["map_count"] for item in entries)}}
    if any(issue.get("archive_audit") for issue in issues):
        manifest["archive_audit"] = "archive_audit.json"
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Remove only unchanged outputs generated by the previous catalog, after
    # the replacement catalog is complete. Preserve edited/user-created files.
    wanted = {item["xdf"] for item in entries}
    wanted_reports = {item.get("import_report") for item in entries}
    for old in previous.get("entries", []):
        if (old.get("retained") or old.get("xdf") in wanted or
                not (old.get("source_a2l_sha256") or old.get("source_json_sha256") or old.get("source_ols_sha256"))):
            continue
        path = (output / old["xdf"]).resolve()
        if path.parent == output and path.is_file() and sha256(path.read_bytes()) == old.get("xdf_sha256"):
            path.unlink()
            report_path = (output / old.get("import_report", "")).resolve()
            if (old.get("import_report") not in wanted_reports and report_path.parent == output
                    and report_path.suffix == ".json" and report_path.is_file()):
                report_path.unlink()
    return manifest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=Path("mappacks/EDC 16"))
    parser.add_argument("--output", type=Path, default=Path("definitions/library"))
    parser.add_argument("--json-pair", type=Path, nargs=2, action="append", default=[], metavar=("JSON", "BIN"),
                        help="Import a WinOLS JSON export against its matching original image (repeatable)")
    parser.add_argument("--ols", type=Path, action="append", default=[],
                        help="Import a supported native WinOLS project with its embedded original BIN (repeatable)")
    args = parser.parse_args(argv)
    result = generate_library(args.source, args.output, args.json_pair, args.ols)
    print(json.dumps(result["summary"], indent=2))
    for issue in result["issues"]:
        if "paths" in issue:
            print("Skipped " + ", ".join(issue["paths"]) + ": " + issue["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
