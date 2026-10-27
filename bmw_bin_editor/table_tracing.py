"""MS43X coordinates from original linked-axis symbols and logged quantities.

Only quantities exposed by the bundled ADX are automatic. Similar names such as
start temperature, peak pedal, controller error and raw sensor voltage must not
silently reuse their current/physical counterparts.
"""
from __future__ import annotations

import re

from .logdata import MapLogProfile, _norm_unit


_SIGNALS = {
    "n": "N", "n_32": "N", "map": "MAP", "ve_n": "N", "ve_map": "MAP",
    "bc_n": "N", "bc_map_req": "MAPREQ", "bc_pvs_av": "PVS",
    "pvs": "PVS", "pvs_av": "PVS", "tps_av": "TPS",
    "tco": "TCO", "tia": "TIA", "toil": "TOIL", "tco_ex": "TCOEX",
    "vb": "VB", "vs": "VS", "gear": "GEAR", "amp_mes": "AMP",
    "maf_iga": "MAFIGA", "maf_ivvt": "MAFIVVT", "maf_mmv": "MAFMMV",
    "maf_mes": "MAFMES", "maf_kgh": "MAF", "cam_av_in": "CAMAVIN",
    "ff_fac": "FFBLEND",
}


def _source_quantity(source_name: str) -> str:
    if source_name.startswith("ldpm_"):
        return re.sub(r"_\d+$", "", source_name[5:])
    if source_name.startswith("ldp_"):
        return source_name[4:].split("__")[0]
    return ""


def _signal(definition, axis, which: str):
    if axis is None or axis.count <= 1:
        return None, None
    if "unresolved" in axis.units:
        return None, f"{which.upper()} axis has an unresolved XDF link; choose a verified axis before tracing."
    quantity = _source_quantity(getattr(axis, "source_name", ""))
    # Linked symbols take priority: e.g. n__maf is stored Y/X in this XDF.
    if not quantity and definition.category != "Axis":
        suffixes = definition.title.split("__")[1:]
        if len(suffixes) == 1:
            quantity = suffixes[0]
        elif len(suffixes) == 2:
            quantity = suffixes[1 if which == "x" else 0]
    signal = _SIGNALS.get(quantity)
    if quantity == "maf":
        # Generic load uses the subsystem's own ADX load reference. Other
        # generic/intermediate load quantities remain explicitly unbound.
        signal = {"Ignition": "MAFIGA", "Knock": "MAFIGA",
                  "Injection": "MAFTI", "Lambda Controller": "MAFTI",
                  "Vanos": "MAFIVVT"}.get(definition.category)
    if signal:
        return signal, None
    if not quantity:
        unit = _norm_unit(axis.units)
        signal = {"rpm": "N", "kpa": "MAP", "degpvs": "PVS", "degtps": "TPS"}.get(unit)
        if signal:
            return signal, None
    label = quantity or axis.units or "unknown quantity"
    return None, f"No logged MS43X quantity for {which.upper()} axis '{label}'; bind a verified channel manually."


def ms43_profile(definition) -> MapLogProfile:
    if definition.kind == "constant" or definition.category == "Axis" or definition.size <= 1:
        return MapLogProfile(definition.category, None, None, [],
                             ["Reference axes and constants do not have a runtime lookup position."])
    x, x_note = _signal(definition, definition.x_axis, "x")
    y, y_note = _signal(definition, definition.y_axis, "y")
    notes = [note for note in (x_note, y_note) if note]
    traces = list(dict.fromkeys(s for s in (x, y, "N", "PVS", "TPS", "MAP", "AFTARGET", "INJDC") if s))
    profile = MapLogProfile(definition.category, x, y, traces, notes)
    ve = re.fullmatch(r"ip_map_ve_([1-8])__map__n", definition.title)
    if ve:
        profile.active_signal = "ACTIVEVE"
        profile.active_value = float(ve[1])
        profile.traces.append("ACTIVEVE")
        notes.append("VE tracing includes only samples selecting this table exactly; fractional blended or unknown selections are excluded.")
    return profile
