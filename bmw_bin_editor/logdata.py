from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class SignalDefinition:
    arg: str
    ident: str
    result_name: str
    datatype: str
    unit: str
    internal_name: str
    label: str
    info: str

    @property
    def friendly(self) -> str:
        text = (self.label or self.info or self.internal_name or self.result_name).strip()
        return text if text and text != "-" else self.result_name


class SignalCatalog:
    def __init__(self, definitions: Iterable[SignalDefinition] = ()):
        self.definitions = list(definitions)
        self.by_result = {d.result_name: d for d in self.definitions}

    @classmethod
    def load_tsv(cls, path: Path | str) -> "SignalCatalog":
        result: list[SignalDefinition] = []
        p = Path(path)
        if not p.exists():
            return cls()
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in lines[1:]:
            parts = line.split("\t")
            if len(parts) < 9:
                continue
            result.append(
                SignalDefinition(
                    arg=parts[0].strip(),
                    ident=parts[1].strip(),
                    result_name=parts[2].strip(),
                    datatype=parts[3].strip(),
                    unit=parts[4].strip(),
                    internal_name=parts[6].strip(),
                    label=parts[7].strip(),
                    info=parts[8].strip(),
                )
            )
        return cls(result)

    def label_for(self, result_name: str) -> str:
        if result_name == "STAT_Ad_NumTyp_FreeApplic2_WERT":
            return "ECU torque gear selector (CAN-derived)"
        d = self.by_result.get(result_name)
        if d:
            return d.friendly
        return {
            FUEL_RATE_SIGNAL: "Estimated fuel flow",
            FUEL_CONSUMPTION_SIGNAL: "Estimated fuel consumption",
        }.get(result_name, result_name)

    def unit_for(self, result_name: str) -> str:
        d = self.by_result.get(result_name)
        if d:
            return d.unit
        return {
            FUEL_RATE_SIGNAL: "L/h",
            FUEL_CONSUMPTION_SIGNAL: "L/100 km",
        }.get(result_name, "")


@dataclass
class MapLogProfile:
    name: str
    x_signal: str | None
    y_signal: str | None
    traces: list[str]
    notes: list[str]
    active_signal: str | None = None
    active_value: float | None = None
    _coordinate_indices: dict = field(default_factory=dict, init=False, repr=False, compare=False)

    @property
    def required_position_signals(self) -> list[str]:
        return list(dict.fromkeys(s for s in (self.x_signal, self.y_signal, self.active_signal) if s))

    def sample_active(self, log, index: int) -> bool:
        """VE selection must be known; fractional blend states are not one table."""
        if self.active_signal is None:
            return True
        value = log.value(self.active_signal, index)
        return math.isfinite(value) and self.active_value is not None and abs(value - self.active_value) < 1e-6

    def axis_value(self, log, axis_name: str, index: int, axis) -> float:
        """Read coordinates in the axis' engineering units for files and live data."""
        signal = self.x_signal if axis_name == "x" else self.y_signal
        value = log.value(signal, index)
        if not math.isfinite(value) and signal in log.values and len(log.times):
            # DDE5 rotates request groups. Join coordinates only to a recent
            # earlier acquisition, never to a future row while scrubbing logs.
            values = log.values[signal]
            index = max(0, min(int(index), len(log.times) - 1))
            cached = self._coordinate_indices.get(signal)
            if cached is None or cached[0] is not values or cached[1] != len(values):
                cached = (values, len(values), np.flatnonzero(np.isfinite(values)))
                self._coordinate_indices[signal] = cached
            finite = cached[2]
            previous = int(np.searchsorted(finite, index, side="right")) - 1
            if previous >= 0:
                source_index = int(finite[previous])
                age = float(log.times[index] - log.times[source_index])
                if 0 <= age <= 1.0:
                    value = float(values[source_index])
        if not math.isfinite(value) or axis is None:
            return value
        source = _norm_unit(log.units.get(signal, "") or log.catalog.unit_for(signal))
        target = _norm_unit(axis.units)
        pressure = {"pa": 1.0, "hpa": 100.0, "mbar": 100.0, "kpa": 1000.0, "bar": 100000.0}
        if source in pressure and target in pressure:
            return value * pressure[source] / pressure[target]
        time = {"s": 1.0, "ms": .001, "h": 3600.0}
        if source in time and target in time:
            return value * time[source] / time[target]
        return value



CALC_EST_LAMBDA = "CALC_EST_LAMBDA_MAF_IQ"
CALC_BOOST_ERROR = "CALC_BOOST_ERROR"
FUEL_RATE_SIGNAL = "CALC_EST_FUEL_L_H"
FUEL_CONSUMPTION_SIGNAL = "CALC_EST_FUEL_L_100KM"
FUEL_ESTIMATE_MAX_AGE_S = 1.0


@dataclass(frozen=True)
class FuelEstimateConfig:
    """Assumptions for IQ-derived consumption on a four-stroke engine.

    IQ is the total quantity per cylinder cycle, not the main-injection-only
    quantity. Cylinder count and diesel density are explicit because neither
    can be determined reliably from the CSV's signal names.
    """

    cylinders: int = 4
    density_kg_l: float = 0.835
    min_speed_kmh: float = 5.0

    def __post_init__(self) -> None:
        if isinstance(self.cylinders, bool) or not isinstance(self.cylinders, int) or not 1 <= self.cylinders <= 16:
            raise ValueError("Cylinder count must be an integer between 1 and 16")
        if not math.isfinite(self.density_kg_l) or self.density_kg_l <= 0:
            raise ValueError("Diesel density must be finite and positive")
        if not math.isfinite(self.min_speed_kmh) or self.min_speed_kmh <= 0:
            raise ValueError("Minimum consumption speed must be finite and positive")


def _fuel_estimate_sample(rpm: float, iq: float, speed: float, config: FuelEstimateConfig) -> tuple[float, float]:
    if not (math.isfinite(rpm) and math.isfinite(iq)) or rpm < 0 or iq < 0:
        return math.nan, math.nan
    # Each cylinder injects once per two crankshaft revolutions. IQ55 already
    # includes the cycle quantity; adding a pilot-injection channel double counts.
    rate = iq * rpm * config.cylinders * 3.6 / (120000.0 * config.density_kg_l)
    if not math.isfinite(rate):
        return math.nan, math.nan
    consumption = rate * 100.0 / speed if math.isfinite(speed) and speed >= config.min_speed_kmh else math.nan
    return rate, consumption if math.isfinite(consumption) else math.nan


class _FuelEstimateMixin:
    """Shared derived-channel policy for recorded and streaming sparse data."""

    def set_fuel_estimate_config(self, config: FuelEstimateConfig) -> None:
        if not isinstance(config, FuelEstimateConfig):
            raise TypeError("Expected a FuelEstimateConfig")
        self.fuel_estimate_config = config
        self._add_fuel_estimates()

    def _add_fuel_estimates(self) -> None:
        for signal in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL):
            self.values.pop(signal, None)
            self.units.pop(signal, None)
            self.info.pop(signal, None)
            if signal in self.columns:
                self.columns.remove(signal)
        if SIG_RPM not in self.values or SIG_IQ not in self.values:
            return
        rpm, iq = self.values[SIG_RPM], self.values[SIG_IQ]
        rate = np.full(len(self.times), np.nan, dtype=float)
        valid = np.isfinite(rpm) & np.isfinite(iq) & (rpm >= 0) & (iq >= 0)
        config = self.fuel_estimate_config
        with np.errstate(over="ignore", invalid="ignore"):
            rate[valid] = iq[valid] * rpm[valid] * config.cylinders * 3.6 / (120000.0 * config.density_kg_l)
        rate[~np.isfinite(rate)] = np.nan
        self._store_fuel_estimate(FUEL_RATE_SIGNAL, rate)
        if SIG_SPEED in self.values:
            speed = self.values[SIG_SPEED]
            consumption = np.full(len(self.times), np.nan, dtype=float)
            valid_speed = np.isfinite(rate) & np.isfinite(speed) & (speed >= config.min_speed_kmh)
            with np.errstate(over="ignore", invalid="ignore"):
                consumption[valid_speed] = rate[valid_speed] * 100.0 / speed[valid_speed]
            consumption[~np.isfinite(consumption)] = np.nan
            self._store_fuel_estimate(FUEL_CONSUMPTION_SIGNAL, consumption)

    def _store_fuel_estimate(self, signal: str, values: np.ndarray) -> None:
        self.values[signal] = values
        if signal not in self.columns:
            self.columns.append(signal)
        if signal == FUEL_RATE_SIGNAL:
            self.units[signal] = "L/h"
            self.info[signal] = "Estimated fuel flow (IQ/RPM)"
        else:
            self.units[signal] = "L/100 km"
            self.info[signal] = "Estimated fuel consumption (IQ/RPM/speed)"

    def fuel_estimate_display(self, signal: str, index: int) -> tuple[float, float]:
        """Return a coherent estimate and its age without filling stored samples.

        L/100 km can hold the preceding coherent result across an omitted speed
        request, for at most one second. A stopped, invalid, or stale speed clears
        it immediately. RPM and IQ must both be valid at the cursor row; a missing
        or invalid fuel input must never reuse an earlier consumption estimate.
        """
        if signal not in (FUEL_RATE_SIGNAL, FUEL_CONSUMPTION_SIGNAL) or not len(self.times):
            return math.nan, 0.0
        index = max(0, min(int(index), len(self.times) - 1))
        rate_values = self.values.get(FUEL_RATE_SIGNAL)
        if rate_values is None or not math.isfinite(float(rate_values[index])):
            return math.nan, 0.0
        if signal == FUEL_RATE_SIGNAL:
            return float(rate_values[index]), 0.0
        values = self.values.get(FUEL_CONSUMPTION_SIGNAL)
        if values is None:
            return math.nan, 0.0
        if math.isfinite(float(values[index])):
            return float(values[index]), 0.0
        observed = np.flatnonzero(self._fuel_speed_observed[:index + 1])
        if not len(observed):
            return math.nan, 0.0
        speed_index = int(observed[-1])
        speed = float(self.values[SIG_SPEED][speed_index])
        if not math.isfinite(speed) or speed < self.fuel_estimate_config.min_speed_kmh:
            return math.nan, 0.0
        # Only the last speed acquisition may be held; an invalid intervening IQ
        # or RPM acquisition must also prevent a previous valid estimate returning.
        if not math.isfinite(float(values[speed_index])) or not np.isfinite(rate_values[speed_index:index + 1]).all():
            return math.nan, 0.0
        age = float(self.times[index] - self.times[speed_index])
        if age < 0 or age > FUEL_ESTIMATE_MAX_AGE_S:
            return math.nan, 0.0
        return float(values[speed_index]), age

class TestOLog(_FuelEstimateMixin):
    """Parsed TestO CSV with duplicate-timestamp cleanup and numeric signal arrays."""

    def __init__(self, path: Path | str, catalog: SignalCatalog | None = None, *, fuel_estimate_config: FuelEstimateConfig | None = None):
        self.path = Path(path)
        self.catalog = catalog or SignalCatalog()
        self.columns: list[str] = []
        self.values: dict[str, np.ndarray] = {}
        self.units: dict[str, str] = {}
        self.info: dict[str, str] = {}
        self.times = np.asarray([], dtype=float)
        self.raw_time_scale = 1.0
        self.duplicates_removed = 0
        self.complementary_rows_merged = 0
        self.fuel_estimate_config = FuelEstimateConfig() if fuel_estimate_config is None else fuel_estimate_config
        if not isinstance(self.fuel_estimate_config, FuelEstimateConfig):
            raise TypeError("Expected a FuelEstimateConfig")
        self._fuel_speed_observed = np.asarray([], dtype=bool)
        self._parse()

    @staticmethod
    def _number(text: str | None) -> float:
        if text is None:
            return math.nan
        value = str(text).strip()
        if not value:
            return math.nan
        try:
            return float(value.replace(",", "."))
        except ValueError:
            return math.nan

    def _parse(self) -> None:
        with self.path.open("r", encoding="utf-8-sig", errors="replace") as handle:
            sample = handle.read(16384)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel

        with self.path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.DictReader(handle, dialect=dialect)
            if not reader.fieldnames:
                raise ValueError("TestO CSV has no header")
            self.columns = [str(x) for x in reader.fieldnames]
            rows = list(reader)

        if "time" not in self.columns:
            raise ValueError("TestO CSV has no 'time' column")

        numeric_columns = [
            name for name in self.columns
            if name != "time" and (name.endswith("_WERT") or name.startswith("STAT_") or name in self.catalog.by_result)
        ]
        # Equal timestamps may be duplicate broadcasts or complementary request
        # groups. Merge only non-conflicting samples at that same timestamp; a
        # later timestamp must retain its missing values rather than being filled.
        unique_rows: list[dict[str, str]] = []
        raw_times: list[float] = []
        last_time: float | None = None
        for row_number, row in enumerate(rows, start=2):
            t = self._number(row.get("time"))
            if not math.isfinite(t):
                raise ValueError(f"TestO CSV row {row_number} has an invalid timestamp")
            if last_time is not None and t < last_time:
                raise ValueError(f"TestO CSV row {row_number} has a decreasing timestamp")
            if unique_rows and t == last_time:
                previous = unique_rows[-1]
                previous_values = {name: self._number(previous.get(name)) for name in numeric_columns}
                incoming_values = {name: self._number(row.get(name)) for name in numeric_columns}
                conflicts = any(
                    math.isfinite(previous_values[name]) and math.isfinite(incoming_values[name])
                    and previous_values[name] != incoming_values[name]
                    for name in numeric_columns
                )
                if not conflicts:
                    additions = [name for name in numeric_columns
                                 if not math.isfinite(previous_values[name]) and math.isfinite(incoming_values[name])]
                    for name in additions:
                        previous[name] = row[name]
                    for name, value in row.items():
                        if not previous.get(name) and value:
                            previous[name] = value
                    if additions:
                        self.complementary_rows_merged += 1
                    else:
                        self.duplicates_removed += 1
                    continue
                # Conflicting readings are distinct acquisitions. Keeping both is
                # safer than choosing one merely because their timestamps match.
            unique_rows.append(row)
            raw_times.append(t)
            last_time = t

        if not raw_times:
            raise ValueError("TestO CSV has no numeric timestamps")

        diffs = [b - a for a, b in zip(raw_times, raw_times[1:]) if b > a]
        median_diff = statistics.median(diffs) if diffs else 0.0
        # TestO's normal export uses milliseconds (typically ~100-120 per sample).
        explicit_unit = unique_rows[0].get("time_unit", "") if unique_rows else ""
        self.raw_time_scale = (1.0 if explicit_unit == "s" else 0.001 if explicit_unit == "ms" else 0.001 if median_diff > 10.0 else 1.0)
        t0 = raw_times[0]
        self.times = np.asarray([(t - t0) * self.raw_time_scale for t in raw_times], dtype=float)
        self._fuel_speed_observed = np.asarray([bool(str(row.get(SIG_SPEED) or "").strip()) for row in unique_rows], dtype=bool)

        for name in numeric_columns:
            arr = np.asarray([self._number(row.get(name)) for row in unique_rows], dtype=float)
            if np.isfinite(arr).any():
                self.values[name] = arr

        # TestO commonly carries unit/info columns beside every WERT field.
        first = unique_rows[0] if unique_rows else {}
        for name in self.values:
            stem = name[:-5] if name.endswith("_WERT") else name
            self.units[name] = str(first.get(stem + "_EINH", "") or self.catalog.unit_for(name) or "").strip()
            self.info[name] = str(first.get(stem + "_INFO", "") or self.catalog.label_for(name) or "").strip()

        self._add_calculated_fields()

    def _add_calculated_fields(self) -> None:
        """Add derived calibration-aid channels without pretending they are ECU measurements."""
        maf_name = "STAT_LUFTMASSE_PRO_HUB_WERT"
        iq_name = "STAT_EINSPRITZMENGE_AKTUELL_WERT"
        boost_name = "STAT_LADEDRUCK_WERT"
        boost_set_name = "STAT_LADEDRUCK_SOLL_WERT"
        if maf_name in self.values and iq_name in self.values:
            maf = self.values[maf_name].astype(float)
            iq = self.values[iq_name].astype(float)
            out = np.full_like(maf, np.nan, dtype=float)
            valid = np.isfinite(maf) & np.isfinite(iq) & (iq >= 5.0)
            out[valid] = maf[valid] / (iq[valid] * 14.5)
            self.values[CALC_EST_LAMBDA] = out
            self.units[CALC_EST_LAMBDA] = "λ"
            self.info[CALC_EST_LAMBDA] = "Estimated lambda (MAF/IQ)"
            if CALC_EST_LAMBDA not in self.columns:
                self.columns.append(CALC_EST_LAMBDA)
        if boost_set_name in self.values and boost_name in self.values:
            self.values[CALC_BOOST_ERROR] = self.values[boost_set_name].astype(float) - self.values[boost_name].astype(float)
            self.units[CALC_BOOST_ERROR] = self.units.get(boost_set_name, "") or "hPa"
            self.info[CALC_BOOST_ERROR] = "Boost error (target - actual)"
            if CALC_BOOST_ERROR not in self.columns:
                self.columns.append(CALC_BOOST_ERROR)
        self._add_fuel_estimates()

    @property
    def sample_count(self) -> int:
        return len(self.times)

    @property
    def duration(self) -> float:
        return float(self.times[-1]) if len(self.times) else 0.0

    def has(self, signal: str | None) -> bool:
        return bool(signal and signal in self.values and np.isfinite(self.values[signal]).any())

    def series(self, signal: str) -> np.ndarray:
        return self.values[signal]

    def value(self, signal: str | None, index: int) -> float:
        if not self.has(signal):
            return math.nan
        index = max(0, min(int(index), self.sample_count - 1))
        return float(self.values[signal][index])

    def nearest_index(self, seconds: float) -> int:
        if not len(self.times):
            return 0
        right = int(np.searchsorted(self.times, seconds))
        if right <= 0:
            return 0
        if right >= len(self.times):
            return len(self.times) - 1
        left = right - 1
        return left if seconds - self.times[left] <= self.times[right] - seconds else right

    def available_signals(self) -> list[str]:
        return sorted(self.values, key=lambda s: self.catalog.label_for(s).casefold())


SIG_RPM = "STAT_MOTORDREHZAHL_WERT"
SIG_IQ = "STAT_EINSPRITZMENGE_AKTUELL_WERT"
SIG_IQ_SET = "STAT_EINSPRITZMENGE_SOLL_WERT"
SIG_PEDAL = "STAT_FAHRERWUNSCH_PEDAL_WERT"
SIG_PEDAL1 = "STAT_PWG1_WERT"
SIG_PEDAL2 = "STAT_PWG2_WERT"
SIG_BOOST = "STAT_LADEDRUCK_WERT"
SIG_BOOST_SET = "STAT_LADEDRUCK_SOLL_WERT"
SIG_N75 = "STAT_LADEDRUCKSTELLER_ANSTEUERUNG_WERT"
SIG_SOI = "STAT_BEZUGSWINKEL_BEGINN_HAUPTEINSPRITZUNG1_WERT"
SIG_RAIL = "STAT_RAILDRUCK_WERT"
SIG_RAIL_SET = "STAT_RAILDRUCK_SOLL_WERT"
SIG_TORQUE = "STAT_MOTORMOMENT_SOLL_WERT"
SIG_TORQUE_LIMIT = "STAT_MOTORMOMENT_BEGRENZUNG_WERT"
SIG_DRIVER_TORQUE = "STAT_FAHRERWUNSCHMOMENT_ROH_WERT"
SIG_MAF = "STAT_LUFTMASSE_PRO_HUB_WERT"
SIG_MAF_SET = "STAT_LUFTMASSE_SOLL_WERT"
SIG_SPEED = "STAT_GESCHWINDIGKEIT_WERT"


def _norm_unit(unit: str) -> str:
    return (unit or "").lower().replace(" ", "").replace("°", "deg")


def _axis_signal(definition, axis, axis_name: str) -> tuple[str | None, str | None]:
    if axis is None:
        return None, None
    unit = _norm_unit(getattr(axis, "units", ""))
    title = (getattr(definition, "title", "") + " " + getattr(definition, "category", "")).casefold()

    if "rpm" in unit or "1/min" in unit:
        return SIG_RPM, None
    if unit == "%" or "percent" in unit:
        if any(k in title for k in ("driver", "fahrer", "wish", "pedal")):
            return SIG_PEDAL, "Pedal position"
    if unit == "nm":
        if "driver" in title or "fahrer" in title or "wish" in title:
            return SIG_DRIVER_TORQUE, "Driver-wish torque"
        return SIG_TORQUE, "Torque"
    if "mg/" in unit or "mgper" in unit or "mg/cyc" in unit or "mg/hub" in unit:
        if "air" in title or "maf" in title or "luft" in title:
            return SIG_MAF, "Air mass"
        return SIG_IQ, "Injection quantity"
    if "bar" in unit or "hpa" in unit:
        if "rail" in title:
            return SIG_RAIL_SET, "Rail pressure"
        if "boost" in title or "lade" in title or getattr(definition, "category", "") in {"Boost", "N75"}:
            return SIG_BOOST_SET, "Boost pressure"
    if unit in {"degc", "c", "celsius"}:
        if any(k in title for k in ("coolant", "kuehl", "kühl", "tclnt")):
            return "STAT_KUEHLMITTELTEMPERATUR_WERT", "Coolant temperature"
        if any(k in title for k in ("intake-air", "intake air", "ansaugluft", "air-temperature")):
            return "STAT_ANSAUGLUFTTEMPERATUR_WERT", "Intake air temperature"
        if any(k in title for k in ("charge air", "ladeluft")):
            return "STAT_LADELUFTTEMPERATUR_WERT", "Charge air temperature"
        if any(k in title for k in ("fuel temp", "kraftstofftemp")):
            return "STAT_KRAFTSTOFFTEMPERATURK_WERT", "Fuel temperature"
    if unit == "v" and any(k in title for k in ("battery", "batt", "ubatt")):
        return "STAT_UBATT_WERT", "Battery voltage"
    if unit == "h" and "operating" in (title + " " + getattr(definition, "description", "").casefold()):
        return "STAT_BETRIEBSSTUNDENZAEHLER_WERT", "Engine operating time (seconds converted to hours)"
    if unit == "gsoot":
        return "STAT_RUSSMASSE_IM_PARTIKELFILTER_WERT", "DPF soot mass"
    return None, f"No automatic log binding for {axis_name.upper()} axis unit '{getattr(axis, 'units', '') or 'unknown'}'"


def profile_for_definition(definition, log: TestOLog | None = None) -> MapLogProfile:
    """Conservative table-aware bindings. Missing channels remain explicit."""
    from .ms43_navigation import is_ms43_definition
    if is_ms43_definition(definition):
        from .table_tracing import ms43_profile
        return ms43_profile(definition)
    category = (getattr(definition, "category", "") or "").casefold()
    title = (getattr(definition, "title", "") or "").casefold()
    x_signal, x_note = _axis_signal(definition, getattr(definition, "x_axis", None), "x")
    y_signal, y_note = _axis_signal(definition, getattr(definition, "y_axis", None), "y")
    typed_x_signal, typed_y_signal = x_signal, y_signal
    notes = [n for n in (x_note, y_note) if n]

    traces: list[str]
    name = getattr(definition, "category", "Table") or "Table"
    if category == "boost" or "boost target" in title or "ladedruck" in title:
        traces = [SIG_BOOST_SET, SIG_BOOST]
        name = "Boost target / actual"
        x_signal = x_signal or SIG_IQ
        y_signal = y_signal or SIG_RPM
    elif category == "n75" or "n75" in title or "vnt" in title:
        traces = [SIG_N75]
        name = "N75 / VNT command"
        x_signal = x_signal or SIG_IQ
        y_signal = y_signal or SIG_RPM
    elif category == "soi" or "soi" in title or "injection timing" in title:
        traces = [SIG_SOI]
        name = "Main injection SOI"
        x_signal = x_signal or SIG_IQ
        y_signal = y_signal or SIG_RPM
    elif category == "rail" or "rail" in title:
        traces = [SIG_RAIL_SET, SIG_RAIL]
        name = "Rail target / actual"
        x_signal = x_signal or SIG_IQ
        y_signal = y_signal or SIG_RPM
    elif ((category == "nm to iq") and ("torque->quantity" in title or "torque to quantity" in title or "trq" in title)) or ("trq" in title and "iq" in title):
        traces = [SIG_IQ_SET, SIG_IQ]
        name = "Torque to IQ"
        x_signal = x_signal or SIG_TORQUE
        y_signal = y_signal or SIG_RPM
    elif "driver" in title or "fahrer" in title or "wish" in title:
        traces = [SIG_DRIVER_TORQUE, SIG_TORQUE]
        name = "Driver's Wish"
        x_signal = x_signal or SIG_PEDAL
        y_signal = y_signal or SIG_RPM
    elif category in {"air maf", "smoke"} or "maf" in title or "air mass" in title:
        traces = [SIG_MAF_SET, SIG_MAF]
        name = "Air / smoke"
        y_signal = y_signal or SIG_RPM
    else:
        # Keep the log useful even for an unmapped calibration: show the first
        # position-driving signal that is relevant rather than inventing semantics.
        traces = []

    if getattr(definition, "discovery_unverified", False):
        # Suggested family names do not establish physical axis units. Keep
        # context traces, but do not position raw load axes using mg/cycle logs.
        # Transferred library scaling still does not establish the candidate's
        # quantity kind. In particular mg/cycle and mm³/stroke cannot share a
        # TestO IQ binding without independent verification of this table.
        x_signal = typed_x_signal if typed_x_signal == SIG_RPM else None
        y_signal = typed_y_signal if typed_y_signal == SIG_RPM else None
        notes.append("Unverified map suggestion: configure physical axis bindings explicitly before using log positions.")

    # A category fallback cannot turn an explicitly different physical quantity
    # (temperature, operating hours, controller input) into IQ or engine speed.
    # Singleton dimensions never need a runtime channel.
    for which, typed in (("x", typed_x_signal), ("y", typed_y_signal)):
        axis = getattr(definition, which + "_axis", None)
        if axis is None or axis.count <= 1:
            if which == "x":
                x_signal = None
            else:
                y_signal = None
        elif typed is None and _norm_unit(axis.units):
            if which == "x":
                x_signal = None
            else:
                y_signal = None

    # Drop redundant "no automatic binding" notes once a category/title fallback
    # has provided a concrete axis signal. Keep explicit fallback notes.
    if x_signal is not None:
        notes = [n for n in notes if not n.startswith("No automatic log binding for X axis")]
    if y_signal is not None:
        notes = [n for n in notes if not n.startswith("No automatic log binding for Y axis")]

    # If the preferred pedal signal is missing but one linearized track exists,
    # offer it as a clearly noted fallback rather than silently substituting it.
    if x_signal == SIG_PEDAL and log is not None and not log.has(SIG_PEDAL):
        fallback = SIG_PEDAL1 if log.has(SIG_PEDAL1) else SIG_PEDAL2 if log.has(SIG_PEDAL2) else None
        if fallback:
            x_signal = fallback
            notes.append(f"Filtered pedal is not logged; using {fallback} as a fallback")

    if x_signal or y_signal:
        notes.append("Coordinates may hold the most recent finite axis reading for up to 1 s between request groups; future samples are never used.")

    return MapLogProfile(name=name, x_signal=x_signal, y_signal=y_signal, traces=traces, notes=notes)

class LiveTestOLog(_FuelEstimateMixin):
    """In-memory TestO-compatible log used by the experimental MCP live reader."""

    def __init__(self, catalog: SignalCatalog | None = None, label: str = "TestO LIVE", *, fuel_estimate_config: FuelEstimateConfig | None = None):
        self.path = Path(label)
        self.catalog = catalog or SignalCatalog()
        self.columns: list[str] = ["time"]
        self.values: dict[str, np.ndarray] = {}
        self.units: dict[str, str] = {}
        self.info: dict[str, str] = {}
        self.times = np.asarray([], dtype=float)
        self.raw_time_scale = 1.0
        self.duplicates_removed = 0
        self._start_monotonic: float | None = None
        self.fuel_estimate_config = FuelEstimateConfig() if fuel_estimate_config is None else fuel_estimate_config
        if not isinstance(self.fuel_estimate_config, FuelEstimateConfig):
            raise TypeError("Expected a FuelEstimateConfig")
        self._fuel_speed_observed = np.asarray([], dtype=bool)

    def append_sample(self, seconds: float, sample: dict[str, float], units: dict[str, str] | None = None) -> None:
        seconds = float(seconds)
        old_n = len(self.times)
        self.times = np.append(self.times, seconds)
        self._fuel_speed_observed = np.append(self._fuel_speed_observed, SIG_SPEED in sample)
        all_signals = set(self.values) | set(sample)
        for signal in all_signals:
            if signal not in self.values:
                self.values[signal] = np.full(old_n, np.nan, dtype=float)
                self.columns.append(signal)
                self.units[signal] = (units or {}).get(signal, "") or self.catalog.unit_for(signal)
                self.info[signal] = self.catalog.label_for(signal)
            value = sample.get(signal, math.nan)
            try:
                value = float(value)
            except Exception:
                value = math.nan
            self.values[signal] = np.append(self.values[signal], value)
        # Existing channels absent from this job result need a NaN at this timestamp.
        for signal in set(self.values) - set(sample):
            if len(self.values[signal]) == old_n:
                self.values[signal] = np.append(self.values[signal], math.nan)
        self._update_calculated_latest(old_n)
        # Bound the plotting buffer; the CSV recorder retains the full session.
        if len(self.times) > 20000:
            self.times = self.times[-20000:]
            self.values = {k: v[-20000:] for k, v in self.values.items()}
            self._fuel_speed_observed = self._fuel_speed_observed[-20000:]

    def _update_calculated_latest(self, old_n: int) -> None:
        def _latest(name: str) -> float:
            arr = self.values.get(name)
            if arr is None or len(arr) == 0:
                return math.nan
            try:
                return float(arr[-1])
            except Exception:
                return math.nan

        derived = {}
        maf = _latest("STAT_LUFTMASSE_PRO_HUB_WERT")
        iq = _latest("STAT_EINSPRITZMENGE_AKTUELL_WERT")
        if math.isfinite(maf) and math.isfinite(iq) and iq >= 5.0:
            derived[CALC_EST_LAMBDA] = maf / (iq * 14.5)
        bst = _latest("STAT_LADEDRUCK_SOLL_WERT")
        bsa = _latest("STAT_LADEDRUCK_WERT")
        if math.isfinite(bst) and math.isfinite(bsa):
            derived[CALC_BOOST_ERROR] = bst - bsa
        if SIG_RPM in self.values and SIG_IQ in self.values:
            rate, consumption = _fuel_estimate_sample(_latest(SIG_RPM), iq, _latest(SIG_SPEED), self.fuel_estimate_config)
            derived[FUEL_RATE_SIGNAL] = rate
            if SIG_SPEED in self.values:
                derived[FUEL_CONSUMPTION_SIGNAL] = consumption
        meta = {
            CALC_EST_LAMBDA: ("λ", "Estimated lambda (MAF/IQ)"),
            CALC_BOOST_ERROR: ("hPa", "Boost error (target - actual)"),
            FUEL_RATE_SIGNAL: ("L/h", "Estimated fuel flow (IQ/RPM)"),
            FUEL_CONSUMPTION_SIGNAL: ("L/100 km", "Estimated fuel consumption (IQ/RPM/speed)"),
        }
        for signal, value in derived.items():
            if signal not in self.values:
                self.values[signal] = np.full(old_n, np.nan, dtype=float)
                self.columns.append(signal)
                self.units[signal], self.info[signal] = meta[signal]
                self.values[signal] = np.append(self.values[signal], value)
            else:
                # append_sample may already have appended a NaN for an existing synthetic channel.
                if len(self.values[signal]) == old_n:
                    self.values[signal] = np.append(self.values[signal], value)
                else:
                    self.values[signal][-1] = value

    @property
    def sample_count(self) -> int:
        return len(self.times)

    @property
    def duration(self) -> float:
        return float(self.times[-1]) if len(self.times) else 0.0

    def has(self, signal: str | None) -> bool:
        return bool(signal and signal in self.values and np.isfinite(self.values[signal]).any())

    def series(self, signal: str) -> np.ndarray:
        return self.values[signal]

    def value(self, signal: str | None, index: int) -> float:
        if not self.has(signal):
            return math.nan
        index = max(0, min(int(index), self.sample_count - 1))
        return float(self.values[signal][index])

    def nearest_index(self, seconds: float) -> int:
        if not len(self.times):
            return 0
        right = int(np.searchsorted(self.times, seconds))
        if right <= 0:
            return 0
        if right >= len(self.times):
            return len(self.times) - 1
        left = right - 1
        return left if seconds - self.times[left] <= self.times[right] - seconds else right

    def available_signals(self) -> list[str]:
        return sorted(self.values, key=lambda s: self.catalog.label_for(s).casefold())
