"""Learn transferable scale hypotheses from the hash-validated XDF catalog.

Only declared integer storage, affine equations and meaningful base-map names
are learned. Addresses and BIN identity never participate in scale transfer.
Repeated maps in one software version count as one source, and different
quantity units remain different interpretations.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import threading
import xml.etree.ElementTree as ET

import numpy as np

from .core import EditorError, XDFDatabase, equation_affine
from .definition_library import discover_entries, library_root


@dataclass(frozen=True)
class AxisScale:
    role: str
    units: str
    equation: str
    signed: bool = False
    bits: int = 16

    @property
    def affine(self):
        return equation_affine(self.equation)


@dataclass(frozen=True)
class ScalingProfile:
    family: str
    z_equation: str
    z_units: str
    z_signed: bool
    rpm: AxisScale
    load: AxisScale
    sources: tuple[str, ...]
    source_ids: tuple[str, ...]
    examples: tuple[str, ...] = ()
    source_scales: tuple[str, ...] = ()
    z_bits: int = 16

    @property
    def source_count(self):
        return len(set(self.source_ids))


def canonical_equation(equation):
    """Readable affine form; only represent an exact coefficient as a fraction."""
    scale, offset = equation_affine(equation)
    if abs(offset) < 1e-12:
        offset = 0.0
    if scale == 1:
        base = "X"
    elif math.isclose(scale, 100 / 8192, rel_tol=1e-12):
        base = "X * 100 / 8192"
    else:
        fraction = Fraction(scale).limit_denominator(8192)
        if fraction.denominator in (128, 8192) and math.isclose(float(fraction), scale, rel_tol=1e-12):
            base = f"X * {fraction.numerator} / {fraction.denominator}"
        else:
            base = f"X * {scale:.12g}"
    if offset:
        base += f" {'+' if offset > 0 else '-'} {abs(offset):.12g}"
    return base


def _near_equation(left, right):
    a, b = equation_affine(left)
    c, d = equation_affine(right)
    return math.isclose(a, c, rel_tol=0.0002, abs_tol=1e-12) and math.isclose(b, d, rel_tol=0.0002, abs_tol=1e-9)


def source_family(title, description=""):
    """Recognize base maps, never just a broad category or incidental prose."""
    if re.search(r"\[UV\]|UNVERIFIED|unknown/unverified", title + " " + description, re.I):
        return None
    id_match = re.search(r"\bIdName:\s*([\w]+)", description)
    identity = id_match.group(1) if id_match else title
    if re.search(r"InjCrv_(?:phiMI1Bas[\w]*|Bas\d+)\b", identity, re.I) or re.search(
            r"Main SOI base|Injection timing\s*[-–]\s*Main Injection Base", title, re.I):
        return "Main SOI"
    if re.search(r"PCR_(?:pBDesBas[\w]*|DesBas)\b", identity, re.I) or re.search(
            r"Boost[- ]pressure target|Boost target", title, re.I):
        return "Boost target"
    if re.search(r"PCR_(?:rCtlBas(?:_MAP)?|rBPCtlBas(?:AirCtlOff|FDC|Rgn\d+)?(?:_MAP)?|VGT_DC)\b", identity, re.I) or re.search(
            r"N75\s+[A-Z]\b.*(?:base|feed.forward)|Variable Geometry Duty Cycle", title, re.I):
        # A command-bound contribution can share native duty units without
        # being evidence for a transferable base/feed-forward map.
        if re.search(r"upper[- ](?:command[- ])?bound|command ceiling", title, re.I):
            return None
        return "N75 / VNT duty"
    if re.search(r"AccPed_trqEng(?:High|Low)?(?:_MAP)?\b", identity, re.I) or re.search(
            r"Driver(?:'s)?[- ](?:wish|demand)|Fahrerwunsch", title, re.I):
        return "Driver's wish"
    return None


def _axis_scale(axis, family):
    if axis is None or axis.address is None or axis.count < 5 or axis.element_bits != 16:
        return None
    units = axis.units.lower().replace("³", "3").replace(" ", "")
    if units in ("rpm", "1/min", "min-1", "min^-1"):
        role, display = "rpm", "rpm"
    elif units.startswith("mm3/"):
        role, display = "quantity-volume", "mm³/stroke"
    elif units.startswith("mg/"):
        role, display = "quantity-mass", "mg/cycle"
    elif units == "nm":
        role, display = "torque", "Nm"
    elif "%" in units and family == "Driver's wish":
        role, display = "pedal", "%"
    else:
        return None  # Absent units are not a license to invent a quantity.
    try:
        scale, offset = equation_affine(axis.equation)
        if not math.isfinite(scale) or not math.isfinite(offset) or scale <= 0:
            return None
        return AxisScale(role, display, canonical_equation(axis.equation), axis.signed, axis.element_bits)
    except EditorError:
        return None


def _table_profile(definition, entry):
    family = source_family(definition.title, definition.description)
    if not family or min(definition.rows, definition.cols) < 5 or definition.z_element_bits != 16:
        return None
    axes = [_axis_scale(axis, family) for axis in (definition.x_axis, definition.y_axis)]
    if any(axis is None for axis in axes) or sum(axis.role == "rpm" for axis in axes) != 1:
        return None
    rpm = next(axis for axis in axes if axis.role == "rpm")
    load = next(axis for axis in axes if axis.role != "rpm")
    allowed = {"pedal"} if family == "Driver's wish" else {"quantity-mass", "quantity-volume", "torque"}
    if load.role not in allowed:
        return None
    units = definition.z_units.lower()
    if family == "Main SOI":
        if not definition.z_signed or "deg" not in units:
            return None
        z_units = "deg"
    elif family == "Boost target":
        if units.strip().lower() not in ("hpa", "mbar"):
            return None
        z_units = "hPa"
    elif family == "N75 / VNT duty":
        if "%" not in units:
            return None
        z_units = "%"
    else:
        if units.strip().lower() != "nm":
            return None
        z_units = "Nm"
    try:
        scale, offset = equation_affine(definition.z_equation)
        if not math.isfinite(scale) or not math.isfinite(offset) or scale <= 0:
            return None
        equation = canonical_equation(definition.z_equation)
    except EditorError:
        return None
    ids = entry.metadata.get("software_identifiers") or []
    source_id = ",".join(sorted(ids)) or entry.metadata.get("source_a2l_sha256") or entry.metadata.get("profile_id") or entry.key
    label = entry.title + (" (" + ", ".join(ids) + ")" if ids and not any(i in entry.title for i in ids) else "")
    return ScalingProfile(family, equation, z_units, definition.z_signed, rpm, load,
                          (label,), (source_id,), (definition.title,),
                          (f"{label}: {equation} {definition.z_units}; {load.equation} {load.units}",))


def _same_group(left, right):
    return (left.family == right.family and left.z_units == right.z_units and left.z_bits == right.z_bits
            and left.z_signed == right.z_signed and left.load.role == right.load.role
            and left.load.units == right.load.units and left.rpm.signed == right.rpm.signed
            and left.load.signed == right.load.signed
            and _near_equation(left.z_equation, right.z_equation)
            and _near_equation(left.rpm.equation, right.rpm.equation)
            and _near_equation(left.load.equation, right.load.equation))


def extract_scaling_profiles(entries):
    """Stream relevant tables only, keeping memory independent of catalog size."""
    profiles = []
    for entry in entries:
        entry_profiles = []
        header = None
        root = None
        try:
            for event, node in ET.iterparse(entry.path, events=("start", "end")):
                if root is None:
                    root = node
                if event != "end":
                    continue
                if node.tag == "XDFHEADER":
                    header = node
                elif node.tag in ("XDFTABLE", "XDFCONSTANT"):
                    if node.tag == "XDFTABLE" and header is not None and source_family(
                            node.findtext("title") or "", node.findtext("description") or ""):
                        temporary = ET.Element("XDFFORMAT")
                        temporary.append(header)
                        temporary.append(node)
                        definitions = XDFDatabase.from_tree(ET.ElementTree(temporary), entry.path).definitions
                        for definition in definitions:
                            profile = _table_profile(definition, entry)
                            if profile:
                                entry_profiles.append(profile)
                    root.remove(node)
                    node.clear()
        except (OSError, ET.ParseError, EditorError, ValueError):
            continue
        profiles.extend(entry_profiles)
    groups = []
    for profile in profiles:
        group = next((group for group in groups if _same_group(group[0], profile)), None)
        if group is None:
            groups.append([profile])
        else:
            group.append(profile)
    result = []
    for group in groups:
        # Choose the exact formula supported by most independent software IDs,
        # rather than by the number of repeated maps in a pack.
        supported = {}
        for profile in group:
            supported.setdefault(profile.z_equation, set()).update(profile.source_ids)
        representative = max(group, key=lambda p: (len(supported[p.z_equation]), len(p.z_equation)))
        result.append(replace(representative,
            sources=tuple(sorted({value for p in group for value in p.sources})),
            source_ids=tuple(sorted({value for p in group for value in p.source_ids})),
            examples=tuple(sorted({value for p in group for value in p.examples}))[:5],
            source_scales=tuple(sorted({value for p in group for value in p.source_scales}))))
    return tuple(sorted(result, key=lambda p: (p.family, -p.source_count, p.load.units, p.z_equation)))


_cache = {}
_cache_lock = threading.Lock()


def _catalog_signature(root):
    manifest = root / "manifest.json"
    try:
        stat = manifest.stat()
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("entries", []), list):
            return None
        files = []
        for index, record in enumerate(payload.get("entries", [])):
            try:
                path = (root / record["xdf"]).resolve()
                path.relative_to(root.resolve().parent)
                file_stat = path.stat()
                files.append((str(path), file_stat.st_mtime_ns, file_stat.st_size, record.get("xdf_sha256")))
            except (OSError, ValueError, KeyError, TypeError):
                files.append(("invalid or unavailable entry", index, repr(record)))
        return stat.st_mtime_ns, stat.st_size, tuple(files)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def load_scaling_profiles(root=None):
    """Reuse the compact corpus until manifest or source file identity changes."""
    root = (Path(root) if root is not None else library_root()).resolve()
    signature = _catalog_signature(root)
    if signature is None:
        return ()
    with _cache_lock:
        previous = _cache.get(str(root))
        if previous is not None and previous[0] == signature:
            return previous[1]
        profiles = extract_scaling_profiles(discover_entries(root))
        _cache[str(root)] = (signature, profiles)
        return profiles


def compatible_axes(profile, x, y, rpm_axis):
    """Use declared axis equations only when their physical ranges fit the role."""
    converted = []
    for axis, raw in ((profile.rpm, (x, y)[rpm_axis]), (profile.load, (x, y)[1 - rpm_axis])):
        values = np.asarray(raw, dtype=float)
        if axis.signed:
            values = np.where(values >= 32768, values - 65536, values)
        scale, offset = axis.affine
        values = np.sort(values * scale + offset)
        low, high = float(values[0]), float(values[-1])
        if axis.role == "rpm":
            valid = (0 <= low <= 1500 and 2600 <= high <= 8500 and high - low >= 1800
                     and np.mean(np.abs(values / 25 - np.round(values / 25)) <= .12) >= .7)
        elif axis.role == "pedal":
            valid = 0 <= low <= 3 and 97 <= high <= 103
        elif axis.role == "quantity-mass":
            valid = 0 <= low <= high * .15 and 20 <= high <= 250
        elif axis.role == "quantity-volume":
            valid = 0 <= low <= high * .15 and 20 <= high <= 300
        else:
            valid = 0 <= low <= high * .15 and 100 <= high <= 1300
        if not valid:
            return False
        converted.append(values)
    return True


def transferred_suggestions(suggestion, profiles, x, y, raw, rpm_axis, valid_surface):
    """Return scale alternatives with provenance, without transferring addresses."""
    result = []
    for profile in profiles:
        if profile.family != suggestion.family or not compatible_axes(profile, x, y, rpm_axis):
            continue
        values = np.asarray(raw, dtype=float)
        if profile.z_signed:
            values = np.where(values >= 32768, values - 65536, values)
        scale, offset = equation_affine(profile.z_equation)
        physical = values * scale + offset
        if not valid_surface(profile.family, physical, rpm_axis):
            continue
        axes = [profile.load, profile.load]
        axes[rpm_axis] = profile.rpm
        note = (f"Library-derived scaling hypothesis supported by {profile.source_count} independent software "
                f"profile{'s' if profile.source_count != 1 else ''}; repeated maps count once. "
                "Transfer uses axis roles, integer storage and physical ranges, never addresses or software-ID matching.")
        reasons = [note,
                   f"Z: {profile.z_equation} {profile.z_units}; physical range {physical.min():g}–{physical.max():g}. "
                   f"X: {axes[0].equation} {axes[0].units}; Y: {axes[1].equation} {axes[1].units}.",
                   "Sources: " + "; ".join(profile.sources),
                   "Source examples: " + "; ".join(profile.examples),
                   "Units, event and active ECU use remain hypotheses for this unknown image."]
        # Preserve structural ordering near the upper end; clipping a fixed
        # bonus to 100 would turn different RPM-role strengths into a tie.
        bonus = max(1, ((100 - suggestion.score) * (4 + min(6, profile.source_count))) // 20)
        score = min(100, suggestion.score + bonus)
        inherited = [suggestion.reasons[-1]] if suggestion.reasons else []
        if profile.family == "Main SOI":
            inherited.append("Negative signed values are present." if np.any(values < 0) else
                             "This snapshot contains only nonnegative values; signed storage is transferred from the source definitions.")
        result.append(replace(suggestion, score=score,
            reasons=reasons + inherited,
            x_units=axes[0].units, y_units=axes[1].units,
            x_equation=axes[0].equation, y_equation=axes[1].equation,
            z_equation=profile.z_equation, z_units=profile.z_units,
            z_signed=profile.z_signed or profile.family == "Main SOI",
            scaling_sources=list(profile.sources), scaling_profile_count=profile.source_count,
            scaling_label="Library-derived hypothesis", source_scales=list(profile.source_scales),
            x_signed=axes[0].signed, y_signed=axes[1].signed))
    return result
