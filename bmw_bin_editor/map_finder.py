"""Read-only pattern search for possible contiguous, 16-bit calibration tables.

The layouts below are hypotheses. Matching bytes do not identify a table's
function, physical units, scaling, signedness, or whether the ECU uses it.
Scores rank pattern evidence; they are not probabilities of correctness.
"""
from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from numbers import Integral
from typing import Callable, Iterable

import numpy as np

from .core import AxisDefinition, CalibrationDefinition


@dataclass
class MapCandidate:
    address: int
    rows: int
    cols: int
    x_address: int
    y_address: int
    bits: int = 16
    signed: bool = False
    score: int = 0
    reasons: list[str] = field(default_factory=list)
    layout: str = ""
    known: bool = False
    suggestions: list = field(default_factory=list, repr=False)

    def to_definition(self) -> CalibrationDefinition:
        """Create an explicitly unverified definition with unscaled raw values."""
        return CalibrationDefinition(
            title=f"0x{self.address:06X} Suspected {self.rows} x {self.cols} table",
            description=(
                "UNVERIFIED pattern candidate. Dimensions, axis assignment, "
                "signedness, scaling, function and ECU use have not been verified. "
                "Values and axes use raw X equations; no physical units are inferred. "
                f"Hypothesized layout: {self.layout}. "
                f"Heuristic score {self.score}/100 is a ranking, not a probability.\n"
                + "\n".join(self.reasons)
            ),
            category="Suspected maps",
            categories=["Suspected maps"],
            z_address=self.address,
            rows=self.rows,
            cols=self.cols,
            z_element_bits=self.bits,
            z_signed=self.signed,
            z_equation="X",
            z_units="",
            decimal_places=0,
            x_axis=AxisDefinition(self.x_address, self.cols, self.bits, False, "X", "", 0),
            y_axis=AxisDefinition(self.y_address, self.rows, self.bits, False, "X", "", 0),
            unique_id=(f"suspected:{self.address:X}:{self.rows}x{self.cols}:"
                       f"{self.x_address:X}:{self.y_address:X}:{self.bits}"),
        )


def _known_intervals(known: Iterable, length: int) -> list[tuple[int, int]]:
    """Normalize existing definition Z/axis spans or explicit half-open spans."""
    spans = []
    for item in known:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            first, last = item
            if not isinstance(first, Integral) or not isinstance(last, Integral) or last < first:
                raise ValueError("Known byte spans must be (start, end) with start <= end.")
            spans.append((int(first), int(last)))
            continue
        if isinstance(item, MapCandidate):
            item = item.to_definition()
        if not isinstance(item, CalibrationDefinition):
            raise TypeError("known must contain CalibrationDefinitions, MapCandidates or byte-span pairs.")
        spans.append((item.z_address, item.z_address + item.size * item.z_width))
        for axis in (item.x_axis, item.y_axis):
            if axis is not None and axis.address is not None and axis.count > 0:
                spans.append((axis.address, axis.address + axis.count * (axis.element_bits // 8)))
    merged = []
    for first, last in sorted((max(0, a), min(length, b)) for a, b in spans if b > 0 and a < length):
        if last <= first:
            continue
        if merged and first <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
        else:
            merged.append((first, last))
    return merged


def _axis_pattern(values: np.ndarray):
    differences = np.diff(values.astype(np.int32))
    if np.all(differences >= 0) and np.any(differences > 0):
        direction = "ascending"
    elif np.all(differences <= 0) and np.any(differences < 0):
        direction = "descending"
    else:
        return None
    return direction, bool(np.all(differences != 0))


def _grid_pattern(values: np.ndarray, rows: int, cols: int):
    """Reject flat/mostly erased grids and score raw neighboring-cell continuity."""
    low, high = int(values.min()), int(values.max())
    if low == high:
        return None
    erased = int(np.count_nonzero((values == 0) | (values == 0xFFFF)))
    if erased * 5 >= len(values) * 4:
        return None
    grid = values.astype(np.int32).reshape(rows, cols)
    x_steps, y_steps = np.diff(grid, axis=1), np.diff(grid, axis=0)
    changes = np.concatenate((np.abs(x_steps).ravel(), np.abs(y_steps).ravel()))
    median_change = float(np.median(changes)) / (high - low)
    continuity = 16 if median_change <= 0.10 else 10 if median_change <= 0.25 else 4 if median_change <= 0.40 else 0
    coherence = 0.0
    for steps in (x_steps, y_steps):
        nonzero = steps[steps != 0]
        if len(nonzero):
            coherence = max(coherence, float(max(np.count_nonzero(nonzero > 0), np.count_nonzero(nonzero < 0))) / len(nonzero))
    coherence_score = 6 if coherence >= 0.80 else 3 if coherence >= 0.65 else 0
    return continuity + coherence_score, erased, median_change, coherence


def scan_maps(
    data: bytes,
    *,
    endian: str = ">",
    start: int = 0,
    end: int | None = None,
    max_axis: int = 32,
    known: Iterable = (),
    include_known: bool = False,
    progress: Callable[[int], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_results: int = 2000,
) -> list[MapCandidate]:
    """Find possible tables whose complete hypothesized layout fits [start,end).

    Both byte alignments are inspected, so arbitrary search boundaries do not
    skip aligned tables. All fields are read as unsigned 16-bit values in the
    explicitly selected byte order. Strictly/non-strictly monotonic axes are
    accepted, including descending axes; constant axes and flat/mostly erased
    grids are rejected. Swapped axis interpretations are compared by raw grid
    continuity, retaining one interpretation for each equivalent Z interval.

    ``known`` accepts CalibrationDefinitions, MapCandidates, or half-open byte
    spans. Any overlap of the full hypothesized structure with a known Z/axis
    span sets ``known`` and is excluded unless ``include_known`` is true.
    ``progress(percent)`` receives integers from 0 to 100. ``cancelled()`` is
    polled during the scan; cancellation returns the partial ranked results.
    No input bytes are modified.
    """
    if endian not in (">", "<"):
        raise ValueError("Byte order must be '>' (big-endian) or '<' (little-endian).")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes or a byte buffer.")
    # Snapshot writable buffers to avoid a concurrent editor mutation changing
    # the scan. A bytes input is already immutable and does not require a copy.
    raw = data if isinstance(data, bytes) else bytes(data)
    length = len(raw)
    end = length if end is None else end
    if (not isinstance(start, Integral) or not isinstance(end, Integral)
            or not 0 <= start <= end <= length):
        raise ValueError("Search range must satisfy 0 <= start <= end <= BIN length.")
    if not isinstance(max_axis, Integral) or not 2 <= max_axis <= 32:
        raise ValueError("Maximum axis length must be from 2 to 32.")
    if not isinstance(max_results, Integral) or max_results < 0:
        raise ValueError("Maximum result count must be a nonnegative integer.")
    start, end = int(start), int(end)
    intervals = _known_intervals(known, length)
    interval_ends = [last for _, last in intervals]
    results: dict[tuple[int, int], MapCandidate] = {}
    last_progress = -1

    def report(percent):
        nonlocal last_progress
        percent = max(0, min(100, int(percent)))
        if progress is not None and percent != last_progress:
            last_progress = percent
            progress(percent)

    def ranked():
        return sorted(results.values(), key=lambda item: (-item.score, item.address, item.rows, item.cols))[:max_results]

    report(0)
    if max_results == 0 or end - start < 20:
        report(100)
        return []
    if cancelled is not None and cancelled():
        return []

    def consider(words, base, index, first_count, second_count, first_axis, second_axis, sequential):
        z_index = second_axis + second_count
        z_end = z_index + first_count * second_count
        if z_end > len(words):
            return
        a = _axis_pattern(words[first_axis:first_axis + first_count])
        b = _axis_pattern(words[second_axis:second_axis + second_count])
        if a is None or b is None:
            return
        grid = words[z_index:z_end]
        nominal = _grid_pattern(grid, first_count, second_count)
        if nominal is None:
            return
        swapped = _grid_pattern(grid, second_count, first_count)
        reverse = swapped is not None and swapped[0] > nominal[0]
        evidence = swapped if reverse else nominal
        rows, cols = (second_count, first_count) if reverse else (first_count, second_count)
        y_index, x_index = (second_axis, first_axis) if reverse else (first_axis, second_axis)
        address = base + z_index * 2
        header = base + index * 2
        interval_end = base + z_end * 2
        overlap_index = bisect_right(interval_ends, header)
        overlaps = overlap_index < len(intervals) and intervals[overlap_index][0] < interval_end
        if overlaps and not include_known:
            return
        if sequential:
            layout = "[cols, X axis, rows, Y axis, Z]" if reverse else "[rows, Y axis, cols, X axis, Z]"
        else:
            layout = "[cols, rows, X axis, Y axis, Z]" if reverse else "[rows, cols, Y axis, X axis, Z]"
        score = (25 + sum(12 if strict else 7 for _, strict in (a, b))
                 + 10 + evidence[0] + (6 if min(rows, cols) >= 4 else 0)
                 + (8 if min(rows, cols) >= 8 else 0)
                 + (5 if evidence[1] == 0 else 0) + (2 if header % 2 == 0 else 0))
        reasons = [
            f"Two 16-bit dimension counts ({first_count}, {second_count}) at byte offset 0x{header:X} fit the search range.",
            f"First axis is {'strictly ' if a[1] else ''}{a[0]}; second axis is {'strictly ' if b[1] else ''}{b[0]}.",
            f"The {rows} x {cols} raw grid is nonconstant; {evidence[1]}/{len(grid)} cells are zero or 0xFFFF.",
        ]
        if evidence[0]:
            reasons.append("Adjacent raw grid values show some continuity; this does not identify a calibration function.")
        reasons.append("Swapped axis orientation ranked higher; axis assignment remains unverified." if reverse
                       else "Conventional axis orientation shown; alternate axis assignment remains possible.")
        if overlaps:
            reasons.append("The hypothesized byte span overlaps an existing definition.")
        candidate = MapCandidate(address, rows, cols, base + x_index * 2, base + y_index * 2,
                                 score=min(100, score), reasons=reasons, layout=layout, known=overlaps)
        key = (address, interval_end)
        old = results.get(key)
        if old is None or candidate.score > old.score:
            results[key] = candidate
        # Bound memory on unusual files containing many pattern matches while
        # still retaining the highest-ranked candidates, rather than stopping
        # early and missing better patterns later in the BIN.
        if len(results) > max(256, max_results * 2):
            best = ranked()
            results.clear()
            results.update(((c.address, c.address + c.rows * c.cols * 2), c) for c in best)

    # Numpy prefilters dimension-sized words; Python examines only those sparse
    # positions. Views are read-only and allocate no full decoded BIN copy.
    for parity in range(2):
        base = start + parity
        word_count = (end - base) // 2
        if word_count <= 0:
            continue
        words = np.frombuffer(raw, dtype=np.dtype(endian + "u2"), count=word_count, offset=base)
        indices = np.flatnonzero((words >= 2) & (words <= max_axis))
        for counter, raw_index in enumerate(indices):
            index = int(raw_index)
            if counter % 128 == 0:
                report(50 * parity + (50 * (base + index * 2 - start) // max(1, end - start)))
                if cancelled is not None and cancelled():
                    return ranked()
            first = int(words[index])
            if index + 1 >= word_count:
                continue
            # Two dimension words followed by their respective axis arrays.
            second = int(words[index + 1])
            if 2 <= second <= max_axis:
                consider(words, base, index, first, second, index + 2, index + 2 + first, False)
            # Each axis is immediately preceded by its own dimension word.
            second_header = index + 1 + first
            if second_header < word_count:
                second = int(words[second_header])
                if 2 <= second <= max_axis:
                    consider(words, base, index, first, second, index + 1, second_header + 1, True)
        report(50 * (parity + 1))
        if cancelled is not None and cancelled():
            return ranked()
    report(100)
    return ranked()
