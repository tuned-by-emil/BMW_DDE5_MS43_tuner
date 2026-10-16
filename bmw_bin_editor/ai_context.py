"""Bounded, immutable analysis snapshots from decoded maps and sparse logs."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import numpy as np

from .bin_analysis import bin_metadata
from .ai_log_analysis import calculate_log, table_smoothness
from .chatgpt_client import ChatGPTError
from .logdata import FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL
from .logdata import LiveTestOLog


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def finite(value):
    value = float(value)
    return value if math.isfinite(value) else None


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return finite(value)
    return value


def calibration_signature(document, definition, compare_bytes=None, compare_name=""):
    if document is None:
        return {"bin": None, "definition": None}
    return {"bin": hashlib.sha256(document.data).hexdigest(),
            "bin_name": document.path.name,
            "definition": digest(clean([asdict(d) for d in document.xdf.tables])),
            "xdf": str(document.xdf.path),
            "endian": document.endian,
            "comparison": hashlib.sha256(compare_bytes).hexdigest() if compare_bytes is not None else None,
            "comparison_name": compare_name if compare_bytes is not None else None}


def build_snapshot(document, definition, selection, panel, *, source, seconds=60,
                   vehicle="", question="", max_rows=180, compare_bytes=None, compare_name="", whole_log=False, all_rows=False):
    signature = calibration_signature(document, definition, compare_bytes, compare_name)
    payload = {"calibration_signature": signature, "vehicle_setup": vehicle[:6000],
               "analysis_question": question[:2000], "source": source,
               "bin": dict(bin_metadata(bytes(document.data)), name=document.path.name) if document else None,
               "comparison": None,
               "map": None, "tables": [], "table_coverage": {"defined": 0, "decoded": 0, "unavailable": 0}, "log": None}
    if document is not None:
        if compare_bytes is not None:
            if len(compare_bytes) != len(document.data):
                raise ChatGPTError("The comparison BIN size does not match the primary BIN. Load a matching comparison file.")
            payload["comparison"] = dict(bin_metadata(compare_bytes), name=compare_name,
                primary_name=document.path.name,
                changed_bytes=int(np.count_nonzero(np.frombuffer(compare_bytes, dtype=np.uint8) != np.frombuffer(document.data, dtype=np.uint8))),
                cell_format="[row, column, primary_value, comparison_value, comparison_minus_primary]",
                original_meaning="changes_from_original describes in-memory edits versus the originally loaded PRIMARY BIN, not the comparison file or a verified stock BIN.",
                unavailable_tables=0)
        for table in document.xdf.tables:
            entry = {"definition": clean(asdict(table))}
            try:
                values = clean(document.map_values(table))
                original = clean(document.map_values(table, original=True))
                entry.update(values=values,
                             x_values=clean(document.axis_values(table.x_axis)),
                             y_values=clean(document.axis_values(table.y_axis)),
                             changes_from_original=[[r, c, original[r][c], value]
                                for r, row in enumerate(values) for c, value in enumerate(row)
                                if value != original[r][c]])
                for name, axis in (("x", table.x_axis), ("y", table.y_axis)):
                    axis_original = clean(document.axis_values(axis, original=True))
                    if axis_original != entry[name + "_values"]:
                        entry["original_" + name + "_values"] = axis_original
                payload["table_coverage"]["decoded"] += 1
            except Exception:
                entry["unavailable"] = "Table or axis could not be decoded from this BIN; check definition addresses, size and scaling."
                payload["table_coverage"]["unavailable"] += 1
            if compare_bytes is not None:
                try:
                    reference = clean(document.map_values_from_source(table, compare_bytes))
                    entry["comparison_values"] = reference
                    entry["primary_vs_comparison_cells"] = [[r, c, value, reference[r][c],
                        reference[r][c] - value if value is not None and reference[r][c] is not None else None]
                        for r, row in enumerate(entry["values"]) for c, value in enumerate(row)
                        if reference[r][c] != value]
                    entry["comparison_x_values"] = clean(document.axis_values_from_source(table.x_axis, compare_bytes))
                    entry["comparison_y_values"] = clean(document.axis_values_from_source(table.y_axis, compare_bytes))
                except Exception:
                    entry["comparison_unavailable"] = "Comparison table or axes could not be decoded with this XDF."
                    payload["comparison"]["unavailable_tables"] += 1
            if "values" in entry:
                entry["smoothness_evidence"] = table_smoothness(entry)
            payload["tables"].append(entry)
        payload["table_coverage"]["defined"] = len(payload["tables"])
    if definition is not None and document is not None:
        payload["map"] = {"definition": clean(asdict(definition)),
                          "selected_cells": [list(cell) for cell in selection.cells()] if selection else []}
    if source != "map":
        log = panel.log
        if log is None or not log.sample_count:
            raise ChatGPTError("Load a saved log or start live logging before requesting log tips.")
        if source == "live" and not panel.live_mode:
            raise ChatGPTError("Start live logging to analyse its latest window.")
        times = np.asarray(log.times)
        if whole_log:
            if panel.live_mode or isinstance(log, LiveTestOLog):
                raise ChatGPTError("Whole saved log requires opening the saved CSV. The live buffer may contain only recent samples.")
            start, end = float(times[0]), float(times[-1])
            all_rows = True
        elif source == "live":
            end = float(times[-1]); start = max(float(times[0]), end - seconds)
        elif panel.range_seconds:
            start, end = panel.range_seconds
        else:
            cursor = float(times[max(0, min(panel.cursor_index, len(times) - 1))])
            start = max(float(times[0]), cursor - seconds / 2)
            end = min(float(times[-1]), start + seconds)
            start = max(float(times[0]), end - seconds)
        indices = np.flatnonzero(np.isfinite(times) & (times >= start) & (times <= end))
        if not len(indices):
            raise ChatGPTError("The selected log window contains no samples.")
        signals = []
        extrema = set()
        for name, series in log.values.items():
            values = np.asarray(series)[indices]
            valid = np.isfinite(values)
            observed = indices[valid]
            if not len(observed) and not all_rows:
                continue
            if not len(observed):
                signals.append({"name": name, "label": log.info.get(name) or log.catalog.label_for(name),
                                "units": log.units.get(name) or log.catalog.unit_for(name),
                                "observations": 0, "missing_rows": int(len(indices)), "min": None, "max": None})
                continue
            actual = values[valid]
            imin = int(observed[np.argmin(actual)]); imax = int(observed[np.argmax(actual)])
            extrema.update((imin, imax))
            signals.append({"name": name, "label": log.info.get(name) or log.catalog.label_for(name),
                            "units": log.units.get(name) or log.catalog.unit_for(name),
                            "observations": len(observed), "missing_rows": int(len(indices) - len(observed)),
                            "first_observed_s": finite(times[observed[0]]), "last_observed_s": finite(times[observed[-1]]),
                            "max_observation_gap_s": finite(np.max(np.diff(times[observed]))) if len(observed) > 1 else None,
                            "min": finite(np.min(actual)), "max": finite(np.max(actual)),
                            "mean": finite(np.mean(actual)), "min_at_s": finite(times[imin]), "max_at_s": finite(times[imax])})
        # Stable selection, never silently omit an acquired channel.
        signals.sort(key=lambda s: s["name"])
        if len(signals) > 256:
            raise ChatGPTError("This log has over 256 channels. Use a smaller logging profile for AI analysis.")
        if not signals:
            raise ChatGPTError("The selected log window contains no numeric channel observations.")
        selected = set(map(int, indices)) if all_rows else set(map(int, indices[np.linspace(0, len(indices) - 1, min(max_rows, len(indices)), dtype=int)]))
        selected.update(extrema)
        if not all_rows and len(selected) > max_rows:
            selected = set(sorted(extrema)[:max_rows - 2]) | {int(indices[0]), int(indices[-1])}
            slots = max_rows - len(selected)
            if slots:
                selected.update(map(int, indices[np.linspace(0, len(indices) - 1, slots, dtype=int)]))
        names = [s["name"] for s in signals]
        if all_rows:
            rows = [[finite(times[i]), {str(j + 1): finite(log.values[name][i]) for j, name in enumerate(names)
                                      if math.isfinite(float(log.values[name][i]))}] for i in sorted(selected)]
        else:
            rows = [[finite(times[i])] + [finite(log.values[name][i]) for name in names] for i in sorted(selected)]
        config = getattr(log, "fuel_estimate_config", None)
        reading_index = len(times) - 1 if panel.live_mode else max(0, min(panel.cursor_index, len(times) - 1))
        readings = {}
        for name in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            value, age = log.fuel_estimate_display(name, reading_index)
            readings[name] = {"value": finite(value), "age_s": finite(age),
                              "timestamp_s": finite(times[reading_index]), "units": log.catalog.unit_for(name),
                              "method": "Viewer's IQ/RPM-derived estimate; L/100 km also uses vehicle speed. May briefly hold a coherent previous estimate."}
        payload["log"] = {"name": log.path.name, "mode": "live" if panel.live_mode else "saved",
                          "start_s": float(times[indices[0]]), "end_s": float(times[indices[-1]]),
                          "total_rows": len(indices), "sent_rows": len(rows),
                          "scope": "whole_saved_log" if whole_log else "window_or_selection",
                          "complete_rows": all_rows,
                          "row_encoding": "[time_s, {column_index: observed_value}]; indexes refer to columns, time is index 0; omitted indexes are unobserved, not zero" if all_rows else "Dense rows aligned to columns; null means unobserved",
                          "sampling": "Every parsed numeric timestamp row included, without downsampling." if all_rows else "Bounded timestamp rows including extrema where budget allows; full-window per-channel statistics.",
                          "columns": ["time_s"] + names, "rows": rows, "channels": signals,
                          "fuel_estimate_assumptions": asdict(config) if config else None,
                          "viewer_calculated_consumption": readings,
                          "axis_binding": {"x": getattr(panel.profile, "x_signal", None),
                                           "y": getattr(panel.profile, "y_signal", None)},
                          "missing_data": "null = unobserved at this timestamp; no forward fill or fabricated simultaneous samples"}
    if payload.get("log"):
        # Compute peaks/events from every observation in scope, before prompt sampling.
        analysis_log = dict(payload["log"], rows=[[finite(times[i]), {str(j + 1): finite(log.values[name][i])
            for j, name in enumerate(names) if math.isfinite(float(log.values[name][i]))}] for i in indices])
        payload["log"]["computed_analysis"] = calculate_log(analysis_log, vehicle)
        if not all_rows:
            payload["_captured_log_rows"] = analysis_log["rows"]
    if not payload["tables"] and payload["map"] is None and payload["log"] is None:
        raise ChatGPTError("Load a BIN with table definitions or load a log to analyse.")
    encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > 64_000_000:
        raise ChatGPTError("Complete context exceeds the 64 MB local limit. Nothing was sent; use a smaller file or log segment.")
    return payload
