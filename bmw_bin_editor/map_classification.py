"""Conservative, empirical hypotheses for four calibration table families.

No address, software identity or vehicle-specific label is used. Byte patterns
cannot prove a function, measurement unit, engineering scale or active ECU use.
The ordinary map finder continues to produce raw definitions; these optional
interpretations must be chosen deliberately by the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib

import numpy as np

from .core import AxisDefinition, CalibrationDefinition, equation_affine
from .map_finder import MapCandidate
from .scaling_evidence import compatible_axes, load_scaling_profiles, transferred_suggestions


@dataclass
class FamilySuggestion:
    family: str
    score: int
    reasons: list[str]
    x_units: str
    y_units: str
    z_units: str
    x_equation: str
    y_equation: str
    z_equation: str
    z_signed: bool
    rows: int
    cols: int
    x_address: int
    y_address: int
    scaling_sources: list[str] = field(default_factory=list)
    scaling_profile_count: int = 0
    scaling_label: str = "Empirical range hypothesis"
    source_scales: list[str] = field(default_factory=list)
    x_signed: bool = False
    y_signed: bool = False

    def to_definition(self, candidate: MapCandidate) -> CalibrationDefinition:
        raw = candidate.to_definition()
        return replace(raw,
            title=f"0x{candidate.address:06X} Likely {self.family} (unverified)",
            description=("UNVERIFIED semantic hypothesis inferred from axis breakpoints, value ranges and surface behavior. "
                         "The function, event, scaling, units, signedness and active ECU use require independent verification. "
                         "Suggested engineering scales are empirical hypotheses, not validated ECU specifications. "
                         f"Heuristic ranking {self.score}/100 is not a probability.\n"
                         + "\n".join(self.reasons) + "\n\nObserved structural evidence:\n" + "\n".join(candidate.reasons)),
            category=self.family, categories=[self.family], rows=self.rows, cols=self.cols,
            z_signed=self.z_signed, z_equation=self.z_equation, z_units=self.z_units,
            decimal_places=2 if self.family != "Boost target" else 0,
            x_axis=AxisDefinition(self.x_address, self.cols, candidate.bits, self.x_signed, self.x_equation, self.x_units, 2),
            y_axis=AxisDefinition(self.y_address, self.rows, candidate.bits, self.y_signed, self.y_equation, self.y_units, 2),
            unique_id=raw.unique_id + ":interpretation:" + self.family + ":" + hashlib.sha256(
                repr((self.x_equation, self.y_equation, self.z_equation, self.x_units, self.y_units,
                      self.z_units, self.z_signed, self.x_signed, self.y_signed)).encode()).hexdigest()[:12])


def _monotonic(axis):
    delta = np.diff(axis)
    return bool(np.all(delta > 0) or np.all(delta < 0))


def _rpm_axis(raw):
    ordered = np.sort(raw)
    if len(raw) < 5 or not _monotonic(raw):
        return None
    scale = 0.25 if ordered[-1] > 8500 else 1.0
    values = ordered * scale
    if not (0 <= values[0] <= 1500 and 2600 <= values[-1] <= 8500 and np.ptp(values) >= 1800):
        return None
    # Broad engine-speed coverage and familiar round breakpoints distinguish
    # this from temperature-offset axes and arbitrary monotonic numeric fields.
    roundness = float(np.mean(np.abs(values / 25 - np.round(values / 25)) <= 0.12))
    if roundness < 0.70 or np.max(np.diff(values)) > np.ptp(values) * 0.55:
        return None
    if np.count_nonzero((values >= 400) & (values <= 1500)) < 1 or np.count_nonzero(values >= 2000) < 2:
        return None
    strength = 68 + int(10 * roundness) + (10 if 400 <= values[0] <= 1300 else 0 if values[0] >= 150 else -8)
    encoding = "raw RPM" if scale == 1 else "quarter-RPM hypothesis (raw / 4)"
    return scale, strength, f"Engine-speed-like axis spans {values[0]:g}–{values[-1]:g} rpm using {encoding}; units remain unverified."


def _pedal_axis(raw):
    ordered = np.sort(raw)
    if not _monotonic(raw) or len(raw) < 5:
        return None
    for full_scale, equation in ((100, "X"), (1000, "X * 0.1"), (10000, "X * 0.01"), (8192, "X * 100 / 8192")):
        if ordered[0] <= full_scale * 0.03 and full_scale * 0.97 <= ordered[-1] <= full_scale * 1.03:
            return 100 / full_scale, equation, f"The other axis covers about 0–100% if raw full scale {full_scale} denotes pedal demand; normalized load is an alternative."
    return None


def _load_axis(raw):
    ordered = np.sort(raw)
    return bool(_monotonic(raw) and len(raw) >= 5 and 0 <= ordered[0] <= ordered[-1] * 0.15
                and 20 <= ordered[-1] <= 20000 and np.ptp(ordered) >= 20)


def _smooth(values):
    span = float(np.ptp(values))
    if span <= 0 or len(np.unique(values)) < 7:
        return False
    changes = np.concatenate((np.abs(np.diff(values, axis=0)).ravel(), np.abs(np.diff(values, axis=1)).ravel()))
    return bool(np.median(changes) <= span * 0.12 and np.quantile(changes, 0.90) <= span * 0.40)


def _trend(values, dimension):
    # Evaluate direction in increasing physical-axis order. Average several
    # endpoints so a single sentinel cell does not decide a table's family.
    profile = np.mean(values, axis=1 - dimension)
    band = max(1, len(profile) // 5)
    net = float(np.mean(profile[-band:]) - np.mean(profile[:band]))
    differences = np.diff(values, axis=dimension)
    tolerance = max(float(np.ptp(values)) * 0.01, 1e-9)
    rising = float(np.mean(differences >= -tolerance))
    falling = float(np.mean(differences <= tolerance))
    return net, rising, falling


def _quantization(values, scale, quantum):
    # Unique values avoid a repeated ceiling overwhelming the evidence.
    values = np.unique(values)
    values = values[values != 0]
    if len(values) < 7:
        return 0.0
    engineering = values * scale / quantum
    tolerance = min(0.18, scale / quantum * 0.6 + 0.005)
    return float(np.mean(np.abs(engineering - np.round(engineering)) <= tolerance))


def _valid_scaled_surface(family, values, rpm_axis):
    """Apply the same family checks to learned equations as to heuristic ones."""
    if not _smooth(values):
        return False
    rpm_dimension = 1 if rpm_axis == 0 else 0
    load_dimension = 1 - rpm_dimension
    span = float(np.ptp(values))
    rpm_net, rpm_rising, rpm_falling = _trend(values, rpm_dimension)
    load_net, load_rising, load_falling = _trend(values, load_dimension)
    low, high = float(values.min()), float(values.max())
    if family == "Main SOI":
        variations = np.ptp(values, axis=load_dimension)
        return bool(low >= -25 and 4 <= high <= 45 and span >= 4 and rpm_net >= 2
                    and rpm_rising >= .65 and np.mean(values > 0) >= .30
                    and np.mean(variations) >= span * .08 and np.mean(variations >= span * .04) >= .25)
    if family == "Boost target":
        return bool(650 <= low <= 1400 and 1550 <= high <= 4500 and np.quantile(values, .10) <= 1450
                    and span >= 450 and load_net >= 200 and load_rising >= .80)
    if family == "N75 / VNT duty":
        falling = max(rpm_falling if rpm_net <= -5 else 0, load_falling if load_net <= -5 else 0)
        edge = float(np.mean(np.take(values, 0, axis=load_dimension)))
        request_like = (load_net >= span * .25 and load_rising >= .90) or (edge <= span * .10 and load_net > span * .10)
        return bool(low >= 0 and high <= 100.1 and span >= 12 and 8 <= np.median(values) <= 95
                    and falling >= .70 and not request_like)
    demand = np.mean(values, axis=rpm_dimension)
    return bool(0 <= low <= 60 and 150 <= high <= 1100 and load_net >= 120
                and load_rising >= .92 and demand[0] <= span * .10)


def classify_candidate(data, candidate: MapCandidate, *, endian=">", scaling_profiles=None) -> list[FamilySuggestion]:
    """Rank optional family hypotheses, abstaining where evidence is weak.

    Surface orientation is preserved. RPM may be on X or Y; flipping an axis
    for evidence measurement does not transpose or change the stored BIN bytes.
    Scores are relative evidence rankings, never confidence probabilities.
    """
    if endian not in (">", "<"):
        raise ValueError("Byte order must be '>' or '<'")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be a byte buffer")
    if not isinstance(candidate, MapCandidate):
        raise TypeError("candidate must be a MapCandidate")
    if candidate.bits != 16 or min(candidate.rows, candidate.cols) < 5 or candidate.rows * candidate.cols < 36:
        return []
    if max(candidate.rows, candidate.cols) > 64:
        return []
    profiles = load_scaling_profiles() if scaling_profiles is None else tuple(scaling_profiles)

    def encodings(family, defaults):
        alternatives = list(defaults)
        for profile in profiles:
            if profile.family == family:
                scale, offset = equation_affine(profile.z_equation)
                if abs(offset) < 1e-12 and not any(abs(scale - s) < 1e-12 for s, _ in alternatives):
                    alternatives.append((scale, profile.z_equation))
        return alternatives
    snapshot = data if isinstance(data, bytes) else bytes(data)
    requests = ((candidate.x_address, candidate.cols), (candidate.y_address, candidate.rows),
                (candidate.address, candidate.rows * candidate.cols))
    if any(type(offset) is not int or offset < 0 or offset + count * 2 > len(snapshot) for offset, count in requests):
        return []
    arrays = [np.frombuffer(snapshot, dtype=endian + "u2", count=count, offset=offset).astype(np.float64)
              for offset, count in requests]
    x, y, raw = arrays
    if not _monotonic(x) or not _monotonic(y):
        return []
    rpm_roles = [_rpm_axis(x), _rpm_axis(y)]
    if not any(rpm_roles):
        return []
    if all(rpm_roles) and abs(rpm_roles[0][1] - rpm_roles[1][1]) < 8:
        return []  # Two equally plausible RPM axes do not establish operating roles.
    suggestions = []
    for rpm_axis, role in enumerate(rpm_roles):
        if role is None:
            continue
        other = y if rpm_axis == 0 else x
        if not _load_axis(other):
            continue
        if all(rpm_roles) and role[1] < max(r[1] for r in rpm_roles) - 10:
            continue
        rpm_dimension = 1 if rpm_axis == 0 else 0
        load_dimension = 1 - rpm_dimension
        grid = raw.reshape(candidate.rows, candidate.cols)
        if x[0] > x[-1]:
            grid = grid[:, ::-1]
        if y[0] > y[-1]:
            grid = grid[::-1, :]
        signed = np.where(grid >= 32768, grid - 65536.0, grid)
        pedal = _pedal_axis(other)
        base_score = (role[1] - 60) // 4 + (4 if min(candidate.rows, candidate.cols) >= 8 else 0)
        load_reason = ("The other axis begins near zero and spans increasing quantity/load; its identity and scale are ambiguous, so it remains raw. "
                       "Injection quantity, torque and another normalized demand can use similar breakpoints.")

        def add(family, score, reasons, z_units, z_equation, z_signed=False, pedal_equation=None):
            units = ["load (raw)", "load (raw)"]
            equations = ["X", "X"]
            units[rpm_axis] = "rpm"
            equations[rpm_axis] = "X" if role[0] == 1 else "X * 0.25"
            if pedal_equation is not None:
                units[1 - rpm_axis], equations[1 - rpm_axis] = "%", pedal_equation
            suggestions.append(FamilySuggestion(family, max(0, min(100, int(score + base_score))),
                [role[2], pedal[2] if pedal_equation is not None else load_reason] + reasons,
                units[0], units[1], z_units, equations[0], equations[1], z_equation, z_signed,
                candidate.rows, candidate.cols, candidate.x_address, candidate.y_address))

        if _smooth(grid):
            for scale, equation in encodings("Boost target", ((1.0, "X"), (0.1, "X * 0.1"))):
                pressure = grid * scale
                low, high = float(pressure.min()), float(pressure.max())
                net, rising, _ = _trend(pressure, load_dimension)
                if (650 <= low <= 1400 and 1550 <= high <= 4500 and np.quantile(pressure, .10) <= 1450
                        and high - low >= 450 and net >= 200 and rising >= .80):
                    add("Boost target", 76 + int(rising * 6),
                        [f"Positive pressure-like values span {low:g}–{high:g} hPa under the proposed {equation} scale.",
                         f"The surface generally rises with load (net {net:g} hPa), with a smooth RPM schedule.",
                         "A pressure target or limiter is plausible; manifold pressure, rail pressure and other functions still require verification."],
                        "hPa", equation)

            duty_scales = [(1.0, "X")] if grid.max() <= 105 else [(0.01, "X * 0.01")]
            q13 = _quantization(grid, 100 / 8192, .5)
            decimal = _quantization(grid, .01, .5)
            if grid.max() <= 8192 and q13 >= .35 and q13 >= decimal + .12:
                duty_scales = [(100 / 8192, "X * 100 / 8192")]
            for scale, equation in encodings("N75 / VNT duty", duty_scales):
                duty = grid * scale
                rpm_net, _, rpm_falling = _trend(duty, rpm_dimension)
                load_net, load_rising, load_falling = _trend(duty, load_dimension)
                falling = max(rpm_falling if rpm_net <= -5 else 0, load_falling if load_net <= -5 else 0)
                span = float(np.ptp(duty))
                low_demand_edge = float(np.mean(np.take(duty, 0, axis=load_dimension)))
                # Torque/quantity requests can fit a percentage range and
                # decline with RPM too. A strongly increasing demand surface,
                # particularly one originating near zero, is insufficient
                # evidence for an actuator duty interpretation.
                request_like = ((load_net >= span * .25 and load_rising >= .90)
                                or (low_demand_edge <= span * .10 and load_net > span * .10))
                if (0 <= duty.min() and duty.max() <= 100.1 and np.ptp(duty) >= 12 and 8 <= np.median(duty) <= 95
                        and falling >= .70 and not request_like):
                    add("N75 / VNT duty", 73 + (8 if falling >= .90 else 0) + (5 if equation.endswith("8192") else 0),
                        [f"The proposed duty scale {equation} gives {duty.min():g}–{duty.max():g}%, bounded within 0–100%.",
                         f"A smooth scheduled actuator-like surface declines along load or RPM (RPM change {rpm_net:g} pp; load change {load_net:g} pp).",
                         "VNT/N75 feed-forward is one explanation; EGR or another actuator duty can produce the same pattern. Polarity is not established."],
                        "%", equation)

        timing_raw = signed if np.any(grid >= 32768) else grid
        timing_span = float(np.ptp(timing_raw))
        load_variation = np.ptp(timing_raw, axis=load_dimension)
        load_dependent = (timing_span > 0 and np.mean(load_variation) >= timing_span * .08
                          and np.mean(load_variation >= timing_span * .04) >= .25)
        if _smooth(timing_raw) and load_dependent:
            timing_encodings = encodings("Main SOI", [(1.0, "X"), (.01, "X * 0.01"), (3 / 128, "X * 3 / 128")])
            valid_timing = []
            for scale, equation in timing_encodings:
                timing = timing_raw * scale
                net, rising, _ = _trend(timing, rpm_dimension)
                if (timing.min() >= -25 and 4 <= timing.max() <= 45 and np.ptp(timing) >= 4
                        and net >= 2 and rising >= .65 and np.mean(timing > 0) >= .30):
                    alignment = _quantization(timing_raw, scale, .1)
                    valid_timing.append((alignment, scale, equation, timing, net, rising))
            for alignment, scale, equation, timing, net, rising in sorted(valid_timing, key=lambda item: (item[0], -item[1]), reverse=True):
                negative = bool(np.any(timing_raw < 0))
                add("Main SOI", 69 + (10 if negative else 0) + (7 if alignment >= .60 else 0),
                    [f"Signed integer values yield {timing.min():g}–{timing.max():g} crank degrees under the empirical {equation} hypothesis; "
                     + ("negative values are present." if negative else "this snapshot contains only nonnegative values, so storage signedness remains a hypothesis."),
                     f"Timing-like advance increases over RPM (net {net:g} degrees), with a smooth load-dependent surface.",
                     "The injection event is unresolved: main SOI, another event, a timing correction or another scheduled quantity can resemble this table."],
                    "deg", equation, True)

        if pedal is not None and _smooth(grid):
            for scale, equation in encodings("Driver's wish", ((1.0, "X"), (.1, "X * 0.1"))):
                torque = grid * scale
                net, rising, _ = _trend(torque, load_dimension)
                demand = np.mean(torque, axis=rpm_dimension)
                if (0 <= torque.min() <= 60 and 150 <= torque.max() <= 1100 and net >= 120 and rising >= .92
                        and demand[0] <= np.ptp(torque) * .10):
                    add("Driver's wish", 82 + int(rising * 4),
                        [f"The proposed torque scale {equation} gives {torque.min():g}–{torque.max():g} Nm.",
                         "The near-zero demand edge produces little output, and requests increase consistently towards full pedal over RPM.",
                         "Driver-demand torque is plausible; quantity or another normalized request remains an alternative, and torque units are unverified."],
                        "Nm", equation, pedal_equation=pedal[1])

        # A genuinely new catalog equation (including an offset) can establish
        # a family hypothesis even when no old fixed encoding fits its bytes.
        # It must pass the same surface and physical-axis checks as a fallback.
        for profile in profiles:
            if not compatible_axes(profile, x, y, rpm_axis):
                continue
            physical_raw = signed if profile.z_signed else grid
            scale, offset = equation_affine(profile.z_equation)
            physical = physical_raw * scale + offset
            if not _valid_scaled_surface(profile.family, physical, rpm_axis):
                continue
            add(profile.family, 70,
                ["A declared source equation yields a plausible family surface; its transfer is unverified."],
                profile.z_units, profile.z_equation, profile.z_signed,
                pedal_equation=profile.load.equation if profile.family == "Driver's wish" and pedal else None)

    # Distinct plausible scales remain reviewable. Equivalent mathematical
    # forms are merged; mg/cycle and mm³/stroke are separate hypotheses.
    learned = []
    for suggestion in suggestions:
        rpm_axis = 0 if suggestion.x_units == "rpm" else 1
        learned.extend(transferred_suggestions(suggestion, profiles, x, y, grid, rpm_axis, _valid_scaled_surface))
    best = {}
    for suggestion in learned + suggestions:
        key = (suggestion.family, equation_affine(suggestion.z_equation),
               equation_affine(suggestion.x_equation), equation_affine(suggestion.y_equation),
               suggestion.x_units, suggestion.y_units, suggestion.z_units,
               suggestion.z_signed, suggestion.x_signed, suggestion.y_signed)
        if key not in best or suggestion.score > best[key].score:
            best[key] = suggestion
    ranked = sorted(best.values(), key=lambda suggestion: (-suggestion.score, -suggestion.scaling_profile_count,
                                                           suggestion.family, suggestion.z_equation))
    return [suggestion for suggestion in ranked if suggestion.score >= max(68, ranked[0].score - 20)] if ranked else []
