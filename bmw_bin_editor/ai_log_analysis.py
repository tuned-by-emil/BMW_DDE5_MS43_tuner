"""Deterministic log evidence for the assistant, without filling sparse channels.

Power is a declared fuel-energy scenario. A shift is a candidate, and wheel
speed cannot establish grip. Keeping those distinctions in the data prevents
the language model from turning an estimate into a measured result.
"""
from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_right
from .ai_setup import latest_facts


SIGNALS = {
    'rpm': 'STAT_MOTORDREHZAHL_WERT',
    'iq': 'STAT_EINSPRITZMENGE_AKTUELL_WERT',
    'maf': 'STAT_LUFTMASSE_PRO_HUB_WERT',
    'speed': 'STAT_GESCHWINDIGKEIT_WERT',
    'boost': 'STAT_LADEDRUCK_WERT',
    'target': 'STAT_LADEDRUCK_SOLL_WERT',
    'duty': 'STAT_LADEDRUCKSTELLER_ANSTEUERUNG_WERT',
    'coolant': 'STAT_KUEHLMITTELTEMPERATUR_WERT',
    'rail': 'STAT_RAILDRUCK_WERT',
    'rail_target': 'STAT_RAILDRUCK_SOLL_WERT',
    'requested_torque': 'STAT_MOTORMOMENT_SOLL_WERT',
    'ecu_torque_selector': 'STAT_Ad_NumTyp_FreeApplic2_WERT',
}
# BMW technical data, standard GA8HP45Z forward ratios (rows40–48):
# https://www.press.bmwgroup.com/usa/article/attachment/T0136911EN_US/387794
GEARS = [4.714, 3.143, 2.106, 1.667, 1.285, 1.0, .839, .667]
_NONNEGATIVE = {'rpm', 'iq', 'maf', 'speed', 'boost', 'target', 'rail', 'rail_target', 'ecu_torque_selector'}
_GROSS_LIMITS = {'rpm': 12000, 'iq': 1000, 'maf': 10000, 'speed': 450,
                 'boost': 10000, 'target': 10000, 'rail': 5000, 'rail_target': 5000, 'ecu_torque_selector': 255}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _read_observations(log):
    columns = log.get('columns', [])
    indexes = {key: columns.index(name) for key, name in SIGNALS.items() if name in columns}
    observations, invalid, examples = [], {}, []
    bad_timestamps = 0
    original_times = []
    for row in log.get('rows', []):
        if not isinstance(row, (list, tuple)) or not row or not _number(row[0]):
            bad_timestamps += 1
            continue
        result = {'time_s': float(row[0])}
        original_times.append(float(row[0]))
        sparse = len(row) == 2 and isinstance(row[1], dict)
        for key, index in indexes.items():
            value = row[1].get(str(index), row[1].get(index)) if sparse else row[index] if index < len(row) else None
            if value is None:
                continue
            reason = None
            if not _number(value):
                reason = 'nonfinite_or_nonnumeric'
            elif key in _NONNEGATIVE and value < 0:
                reason = 'negative'
            elif key in _GROSS_LIMITS and value > _GROSS_LIMITS[key]:
                reason = 'grossly_implausible_scaling'
            elif key == 'ecu_torque_selector' and value != int(value):
                reason = 'noninteger_selector_code'
            if reason:
                code = key + ':' + reason
                invalid[code] = invalid.get(code, 0) + 1
                if len(examples) < 8:
                    examples.append({'time_s': result['time_s'], 'channel': key, 'reason': reason})
                if key == 'speed':
                    result['_invalid_speed'] = True
            else:
                result[key] = float(value)
        observations.append(result)
    observations.sort(key=lambda r: r['time_s'])
    anomaly = {'excluded_channel_values': invalid, 'excluded_value_examples': examples,
               'invalid_timestamp_rows': bad_timestamps,
               'out_of_order_timestamp_rows': sum(b < a for a, b in zip(original_times, original_times[1:])),
               'duplicate_timestamp_rows': len(original_times) - len(set(original_times)),
               'interpretation': 'Missing sparse channels are unobserved, not zero. Invalid values are excluded from calculations; scaling warnings require checking the logger definition.'}
    return observations, anomaly


def observations(log):
    """Yield sorted valid channel observations; never combine different rows."""
    for row in _read_observations(log)[0]:
        yield {key: value for key, value in row.items() if not key.startswith('_')}


def _assumptions(log, setup):
    config = log.get('fuel_estimate_assumptions') or {}
    if not isinstance(config, dict):
        config = {}
    warnings = []
    cylinders = config.get('cylinders', 4)
    if isinstance(cylinders, bool) or not isinstance(cylinders, int) or not 1 <= cylinders <= 16:
        warnings.append('Invalid cylinder count; four cylinders used as an explicit default.')
        cylinders = 4
    density = config.get('density_kg_l', .835)
    if not _number(density) or not .5 <= density <= 1.2:
        warnings.append('Invalid fuel density; 0.835 kg/L used as an explicit default.')
        density = .835
    facts = latest_facts([setup])
    circumference = None
    if facts['tyres'] is not None:
        width, aspect, rim = facts['tyres']
        if 100 <= width <= 400 and 20 <= aspect <= 95 and 10 <= rim <= 30:
            circumference = math.pi * (rim * .0254 + 2 * width * aspect / 100000)
        else:
            warnings.append('Tyre-size text is outside supported bounds; gear numbering omitted.')
    final_drive = facts['ratios']
    if final_drive is not None and not 1 <= final_drive <= 8:
        final_drive = None
        warnings.append('Invalid final-drive ratio; gear numbering omitted.')
    standard_8hp = facts['gearboxes'] == '8hp45'
    if facts['gearboxes'] is not None and not standard_8hp:
        warnings.append('Declared '+facts['gearboxes'].upper()+' gear ratios are unsupported; gear numbering omitted.')
    return {'cylinders': cylinders,
            'cylinders_source': 'viewer_config' if 'cylinders' in config else 'default_four_cylinder_assumption',
            'diesel_density_kg_l': density, 'diesel_lower_heating_value_MJ_kg': 42.7,
            'brake_efficiencies': [.35, .40, .42, .46], 'stoichiometric_air_fuel_ratio': 14.5,
            'power_is': 'IQ-derived crank fuel-energy scenario, not measured dyno or tyre power; actual brake efficiency, total-cycle IQ and injector scaling must be valid. Do not add pilot IQ to total-cycle IQ.',
            'airflow_is': 'Same-row reported fresh-air mass only; calculated lambda is not measured exhaust lambda. Airflow does not independently determine brake efficiency or power.',
            'gear_ratios_8HP45': GEARS, 'standard_8HP45_declared': standard_8hp,
            'declared_gearbox': facts['gearboxes'],
            'rpm_speed_gear_numbering_supported': bool(standard_8hp and final_drive and circumference),
            'final_drive': final_drive, 'nominal_tyre_circumference_m': circumference,
            'gear_caveat': 'Gear candidates require declared standard 8HP45 ratios, final drive and tyre circumference. Converter slip and wheel-speed scaling affect assignment; grip cannot be confirmed without independent road speed.',
            'ecu_torque_selector_caveat': 'STAT_Ad_NumTyp_FreeApplic2_WERT is the observed DDE torque-map selector code, not universal actual gearbox gear. Only session-verified selector-to-map/gear bindings may be used; an 8HP gear can be spoofed or encoded separately.',
            'warnings': warnings}


def _sample(row, assumptions):
    out = {k: round(v, 4) for k, v in row.items() if not k.startswith('_')}
    rpm, iq = row.get('rpm'), row.get('iq')
    cylinders = assumptions['cylinders']
    if rpm is not None and iq is not None and rpm >= 0 and iq >= 0:
        flow = iq * cylinders * rpm * 30 / 1e6
        out['fuel_kg_h'] = round(flow, 3)
        out['fuel_l_h'] = round(flow / assumptions['diesel_density_kg_l'], 3)
        out['crank_bhp_by_assumed_efficiency'] = {
            str(int(e * 100)) + '%': round(flow * 42.7 / 3.6 * e / .7457, 1)
            for e in assumptions['brake_efficiencies']}
        out['crank_nm_by_assumed_efficiency'] = {
            str(int(e * 100)) + '%': round(iq * cylinders * 42.7 * e / (4 * math.pi), 1)
            for e in assumptions['brake_efficiencies']} if rpm > 0 else None
        if row.get('speed', 0) >= 5:
            out['fuel_l_100km'] = round(flow / assumptions['diesel_density_kg_l'] * 100 / row['speed'], 3)
        if 'maf' in row and iq > 0 and rpm > 0:
            out['air_kg_h'] = round(row['maf'] * cylinders * rpm * 30 / 1e6, 3)
            out['calculated_lambda'] = round(row['maf'] / iq / 14.5, 3)
    if 'boost' in row and 'target' in row:
        out['boost_actual_minus_requested_hpa'] = round(row['boost'] - row['target'], 3)
    if row.get('speed', 0) > 5 and row.get('rpm', 0) > 0:
        out['rpm_per_kmh'] = round(row['rpm'] / row['speed'], 3)
        tyre, diff = assumptions['nominal_tyre_circumference_m'], assumptions['final_drive']
        if tyre and diff and assumptions['standard_8HP45_declared']:
            ratio = row['rpm'] / row['speed'] * tyre * .06 / diff
            gear = min(range(8), key=lambda i: abs(GEARS[i] - ratio))
            out['apparent_transmission_ratio'] = round(ratio, 3)
            out['candidate_gear'] = gear + 1 if abs(ratio / GEARS[gear] - 1) < .12 else None
    return out


def _power_peak(rows, assumptions, *, airflow=False, speed=False):
    valid = [r for r in rows if r.get('rpm', 0) > 0 and r.get('iq', 0) > 0
             and (not airflow or 'maf' in r) and (not speed or 'speed' in r)]
    return _sample(max(valid, key=lambda r: r['rpm'] * r['iq']), assumptions) if valid else None


def _channel_stats(rows):
    result = {}
    for key in SIGNALS:
        values = [r[key] for r in rows if key in r]
        if values:
            if key == 'ecu_torque_selector':
                codes = sorted(set(int(value) for value in values))
                result[key] = {'observations': len(values), 'observed_codes': codes[:16],
                               'distinct_code_count': len(codes),
                               'interpretation': 'Observed DDE torque selector codes, not confirmed actual gears.'}
                continue
            result[key] = {'observations': len(values), 'min': round(min(values), 3),
                           'max': round(max(values), 3), 'mean': round(statistics.mean(values), 3)}
    for name, left, right in (('boost_actual_minus_requested_hpa', 'boost', 'target'),
                              ('rail_actual_minus_requested_bar', 'rail', 'rail_target')):
        values = [r[left] - r[right] for r in rows if left in r and right in r]
        if values:
            result[name] = {'observations': len(values), 'min': round(min(values), 3),
                           'max': round(max(values), 3), 'mean': round(statistics.mean(values), 3)}
    return result


def _shifts(rows, assumptions):
    shifts, previous = [], None
    for row in rows:
        if not all(k in row for k in ('rpm', 'iq', 'speed')):
            continue
        if (previous and 0 < row['time_s'] - previous['time_s'] <= 1
                and previous['rpm'] - row['rpm'] >= 250
                and row['speed'] >= previous['speed'] * .98
                and previous['speed'] > 5 and previous['iq'] >= 30 and row['iq'] >= 30):
            after = _sample(row, assumptions)
            if shifts and previous['time_s'] - shifts[-1]['end_s'] < .6:
                shifts[-1]['end_s'] = row['time_s']
                shifts[-1]['after'] = after
            else:
                shifts.append({'start_s': previous['time_s'], 'end_s': row['time_s'],
                               'before': _sample(previous, assumptions), 'after': after,
                               'interpretation': 'Possible upshift/converter change; no actual gear channel. This does not establish tyre hookup.'})
        previous = row
    for event in shifts:
        event['rpm_drop'] = round(event['before']['rpm'] - event['after']['rpm'], 3)
        event['indicated_speed_change_kmh'] = round(event['after']['speed'] - event['before']['speed'], 3)
    return shifts


def _traction(rows, assumptions):
    coherent = [r for r in rows if all(k in r for k in ('speed', 'rpm', 'iq'))]
    falls = []
    peak = None
    for row in coherent:
        if row['iq'] < 30 or row['speed'] <= 5:
            peak = None
            continue
        if peak is None or row['time_s'] - peak['time_s'] > 5 or row['speed'] >= peak['speed']:
            peak = row
        elif peak['speed'] - row['speed'] >= 10:
            if falls and falls[-1]['start_s'] == peak['time_s']:
                falls[-1]['end_s'] = row['time_s']
                falls[-1]['after'] = _sample(row, assumptions)
            else:
                falls.append({'start_s': peak['time_s'], 'end_s': row['time_s'],
                              'before': _sample(peak, assumptions), 'after': _sample(row, assumptions)})
    return {'loaded_indicated_speed_falls': falls[:8],
            'confirmed_hookup_time_s': None, 'confirmed_gear': None,
            'interpretation': 'Falling indicated speed during substantial fueling is consistent with wheelspin subsiding, changing gear/converter state, or deceleration. Wheel-derived speed alone cannot separate these causes or establish a traction torque ceiling.',
            'needed_for_traction_limit': ['actual selected gear', 'independent ground speed',
                                         'transmission/ECU torque request and reduction acknowledgement',
                                         'launch or transbrake-release state']}


def _launches(rows, assumptions):
    launches, hold = [], []
    coherent = [r for r in rows if all(k in r for k in ('rpm', 'iq', 'speed'))]
    for row in coherent:
        if row['speed'] <= 3 and row['rpm'] >= 1500 and row['iq'] >= 30:
            if hold and row['time_s'] - hold[-1]['time_s'] > 1:
                hold = []
            hold.append(row)
        elif hold:
            if (len(hold) >= 2 and hold[-1]['time_s'] - hold[0]['time_s'] >= .2
                    and row['speed'] > 3 and row['time_s'] - hold[-1]['time_s'] <= 1):
                motion = row['time_s']
                end = min(rows[-1]['time_s'], motion + 30)
                end_reason = '30-second analysis boundary' if end == motion + 30 else 'end of captured log'
                low_start = None
                for following in rows:
                    if following['time_s'] < motion or following['time_s'] > end or 'iq' not in following:
                        continue
                    if following['iq'] < 30:
                        if low_start is None:
                            low_start = following['time_s']
                        elif following['time_s'] - low_start >= .4:
                            end, end_reason = low_start, 'first sustained low-fuel/lift interval (IQ below 30 mg/cycle for at least 0.4 s)'
                            break
                    else:
                        low_start = None
                region = [r for r in rows if hold[0]['time_s'] <= r['time_s'] <= end]
                pre_motion = [r for r in region if r['time_s'] < motion]
                event = {'launch_index': len(launches), 'hold_start_s': hold[0]['time_s'],
                         'motion_at_s': motion, 'end_s': end, 'end_reason': end_reason,
                         'hold_end': _sample(hold[-1], assumptions), 'first_motion': _sample(row, assumptions),
                         'loaded_standstill_peak': _power_peak(hold, assumptions),
                         'pre_first_motion_peak': _power_peak(pre_motion, assumptions),
                         'pre_first_motion_caveat': 'The highest RPM/IQ row before first observed movement may not contain speed. Only loaded_standstill_peak is directly paired with standstill speed.',
                         'peak_power': _power_peak(region, assumptions),
                         'peak_with_coherent_airflow': _power_peak(region, assumptions, airflow=True),
                         'coherent_channel_statistics': _channel_stats(region),
                         'shift_candidates': _shifts(region, assumptions),
                         'traction_evidence': _traction(region, assumptions),
                         'transbrake': 'Loaded-standstill launch candidate; applied/released transbrake state was not logged.',
                         'scope_caveat': 'Sequence ends at a sustained lift or analysis boundary, not a confirmed hookup time.'}
                launches.append(event)
            hold = []
    return launches


def _integrated_economy(rows, assumptions, reference_rows=None, stationary=False):
    """Integrate adjacent coherent observations; exclude gaps instead of filling."""
    fuel_l = distance_km = duration = 0.0
    accepted = 0
    observed_speed = [r for r in (reference_rows if reference_rows is not None else rows)
                      if 'speed' in r or r.get('_invalid_speed')]
    speed_indexes = {id(row): index for index, row in enumerate(observed_speed)}
    for left, right in zip(rows, rows[1:]):
        dt = right['time_s'] - left['time_s']
        if not 0 < dt <= 1:
            continue
        if stationary:
            if not all('speed' in r and 0 <= r['speed'] <= 3 for r in (left, right)):
                continue
        elif left.get('speed', 0) < 5 or right.get('speed', 0) < 5:
            continue
        if speed_indexes.get(id(right), -1) != speed_indexes.get(id(left), -1) + 1:
            continue
        if not all('rpm' in r and 'iq' in r for r in (left, right)):
            continue
        flows = [r['iq'] * r['rpm'] * assumptions['cylinders'] * 30 / 1e6 / assumptions['diesel_density_kg_l'] for r in (left, right)]
        fuel_l += (flows[0] + flows[1]) * .5 * dt / 3600
        distance_km += (left['speed'] + right['speed']) * .5 * dt / 3600
        duration += dt
        accepted += 1
    return {'covered_duration_s': round(duration, 3), 'indicated_distance_km': round(distance_km, 5),
            'estimated_fuel_l': round(fuel_l, 5),
            'estimated_l_100km': round(fuel_l / distance_km * 100, 3) if distance_km > 0 and not stationary else None,
            'integrated_intervals': accepted}


def _economy(rows, assumptions):
    moving = [r for r in rows if r.get('speed', 0) >= 5 and 'rpm' in r and 'iq' in r]
    stationary_rows = [r for r in rows if 'speed' in r and 0 <= r['speed'] <= 3
                       and 'rpm' in r and 'iq' in r]
    stationary = _integrated_economy(stationary_rows, assumptions, rows, stationary=True)
    stationary['interpretation'] = 'Estimated fuel during observed standstill/creep intervals (same-row indicated speed 0–3 km/h), integrated separately. It is not a tank measurement or a complete accounting of unobserved stationary time.'
    thermal = [r for r in rows if 'coolant' in r]
    thermal_times = [r['time_s'] for r in thermal]
    warm = []
    for row in moving:
        if 'coolant' in row:
            temperature = row['coolant']
        else:
            index = bisect_right(thermal_times, row['time_s']) - 1
            temperature = thermal[index]['coolant'] if index >= 0 and row['time_s'] - thermal[index]['time_s'] <= 1 else None
        if temperature is not None and temperature >= 80:
            warm.append(row)
    eligible = [r for r in warm if 40 <= r['speed'] <= 140 and 1000 <= r['rpm'] <= 3500
                and 1 <= r['iq'] <= 35]
    speed_indexes = {id(row): index for index, row in enumerate(r for r in rows if 'speed' in r or r.get('_invalid_speed'))}
    groups, group = [], []
    minimum_speed = maximum_speed = minimum_rpm = maximum_rpm = None
    for row in eligible:
        if group:
            if (row['time_s'] - group[-1]['time_s'] > 1
                    or speed_indexes[id(row)] != speed_indexes[id(group[-1])] + 1
                    or max(maximum_speed, row['speed']) - min(minimum_speed, row['speed']) > 5
                    or max(maximum_rpm, row['rpm']) - min(minimum_rpm, row['rpm']) > 200):
                if group[-1]['time_s'] - group[0]['time_s'] >= 10:
                    groups.append(group)
                group = []
        if not group:
            minimum_speed = maximum_speed = row['speed']
            minimum_rpm = maximum_rpm = row['rpm']
        else:
            minimum_speed = min(minimum_speed, row['speed'])
            maximum_speed = max(maximum_speed, row['speed'])
            minimum_rpm = min(minimum_rpm, row['rpm'])
            maximum_rpm = max(maximum_rpm, row['rpm'])
        group.append(row)
    if group and group[-1]['time_s'] - group[0]['time_s'] >= 10:
        groups.append(group)
    summaries = []
    for group in groups:
        integrated = _integrated_economy(group, assumptions, rows)
        if integrated['covered_duration_s'] < 10:
            continue
        summaries.append(dict(integrated, start_s=group[0]['time_s'], end_s=group[-1]['time_s'],
                              mean_speed_kmh=round(statistics.mean(r['speed'] for r in group), 2),
                              mean_rpm=round(statistics.mean(r['rpm'] for r in group), 1),
                              mean_iq_mg_cycle=round(statistics.mean(r['iq'] for r in group), 2),
                              coherent_channel_statistics=_channel_stats(group),
                              speed_range_kmh=[round(min(r['speed'] for r in group), 2), round(max(r['speed'] for r in group), 2)]))
    summaries.sort(key=lambda r: r['covered_duration_s'], reverse=True)
    bands = []
    for lo, hi in ((40, 60), (60, 80), (80, 100), (100, 120), (120, 140)):
        selected = [r for r in warm if lo <= r['speed'] < hi and 1 <= r['iq'] <= 35]
        integrated = _integrated_economy(selected, assumptions, rows)
        if integrated['covered_duration_s'] >= 10:
            bands.append(dict(integrated, indicated_speed_band_kmh=[lo, hi]))
    return {'overall_moving': _integrated_economy(moving, assumptions, rows),
            'warm_moving': _integrated_economy(warm, assumptions, rows),
            'stationary_observed': stationary,
            'consumption_coverage': 'moving intervals only; stops, uncovered gaps and 3–5 km/h transition intervals are excluded from L/100 km. Do not label these values total-trip consumption. Stationary fuel is supplied separately.',
            'warm_stable_cruise': summaries[:6], 'stable_cruise_segments_found': len(summaries),
            'warm_light_load_speed_bands': bands,
            'speed_bands_are_steady_cruise': False,
            'warm_threshold_c': 80,
            'coolant_classification': 'A same-row coolant reading, or a preceding reading at most 1 s old, classifies warm running. Coolant is not inserted into raw rows or used to pair fuel and airflow.',
            'method': 'Trapezoidal integration of adjacent same-row RPM/IQ/speed observations with gaps no greater than 1 s. Consumption is integrated fuel divided by indicated distance, not mean instantaneous L/100 km.',
            'cruise_filter': 'At least 10 s, coolant at least 80 C, indicated speed 40–140 km/h varying no more than 5 km/h, RPM 1000–3500 varying no more than 200, IQ 1–35 mg/cycle; not proof of level-road steady load.',
            'caveats': ['IQ-derived estimate, not tank-measured consumption; injector scaling and fuel density matter.',
                        'Wheelspin inflates indicated distance and can make estimated consumption look artificially low.',
                        'Road gradient, wind, traffic, converter state and air-conditioning load are unobserved; speed bands alone do not justify a calibration patch.',
                        'Overall moving estimate excludes stops and gaps; it is not complete trip/tank consumption.']}


def _calculate(log, setup, rows=None, anomalies=None):
    if rows is None:
        rows, anomalies = _read_observations(log)
    assumptions = _assumptions(log, setup)
    peaks = {}
    for key in ('rpm', 'iq', 'maf', 'boost'):
        valid = [r for r in rows if key in r]
        if valid:
            peaks[key] = _sample(max(valid, key=lambda r: r[key]), assumptions)
    peak = _power_peak(rows, assumptions)
    if peak is not None:
        peaks['power'] = peak
    paired_peak = _power_peak(rows, assumptions, airflow=True)
    if paired_peak is not None:
        peaks['power_with_coherent_airflow'] = paired_peak
    near_lambda = [r for r in rows if r.get('iq', 0) > 0 and r.get('rpm', 0) > 0
                   and 'maf' in r and 1.19 <= r['maf'] / r['iq'] / 14.5 <= 1.25]
    if near_lambda:
        peaks['power_near_lambda_1_22'] = _power_peak(near_lambda, assumptions)
    gaps = {}
    for key in SIGNALS:
        times = [r['time_s'] for r in rows if key in r]
        if times:
            gaps[key] = {'observations': len(times), 'max_gap_s': round(max((b - a for a, b in zip(times, times[1:])), default=0), 3)}
    anomalies = dict(anomalies or {}, channel_observation_gaps=gaps)
    matched = sum(r.get('rpm', 0) > 0 and r.get('iq', 0) > 0 and 'maf' in r for r in rows)
    shifts = _shifts(rows, assumptions)
    launches = _launches(rows, assumptions)
    return {'computed_from_rows': len(rows),
            'coherent_fuel_rpm_observations': sum(r.get('rpm', 0) > 1000 and r.get('iq', 0) > 0 for r in rows),
            'matched_rpm_iq_maf_observations': matched,
            'assumptions': assumptions, 'peaks': peaks, 'coherent_channel_statistics': _channel_stats(rows),
            'shift_candidates': shifts[:64], 'shift_candidate_count': len(shifts),
            'launch_candidates': launches[:12], 'launch_candidate_count': len(launches),
            'event_summary_limit': 'At most 64 shifts and 12 launches are displayed chronologically. Counts describe the full captured scope; calculate_run can retrieve any detected launch by index.',
            'traction_evidence': _traction(rows, assumptions),
            'economy': _economy(rows, assumptions), 'anomalies': anomalies,
            'scope': log.get('scope'),
            'start_s': rows[0]['time_s'] if rows else None, 'end_s': rows[-1]['time_s'] if rows else None,
            'sampling_note': 'Calculated over every captured observation in scope before AI downsampling. RPM and total-cycle IQ must share a row; airflow/lambda requires MAF in that same row. No sparse-channel forward fill.'}


def calculate_log(log, setup=''):
    """Calculate whole captured scope, including every request group's RPM/IQ."""
    return _calculate(log, setup)


def calculate_run(log, setup='', start_s=None, end_s=None, launch_index=None):
    """Recompute from captured rows for an exact window or detected launch.

    ``launch_index`` is zero based and chronological. Supplied start/end bounds
    further intersect that launch. No bounds selects the whole captured log.
    """
    for name, value in (('start_s', start_s), ('end_s', end_s)):
        if value is not None and not _number(value):
            raise ValueError(name + ' must be a finite number or null')
    if start_s is not None and end_s is not None and start_s > end_s:
        raise ValueError('start_s must not exceed end_s')
    if launch_index is not None and (isinstance(launch_index, bool) or not isinstance(launch_index, int) or launch_index < 0):
        raise ValueError('launch_index must be a nonnegative integer or null')
    rows, anomalies = _read_observations(log)
    assumptions = _assumptions(log, setup)
    all_launches = _launches(rows, assumptions)
    selected_launch = None
    lo = rows[0]['time_s'] if rows else start_s
    hi = rows[-1]['time_s'] if rows else end_s
    reason = 'whole captured log'
    if launch_index is not None:
        if launch_index >= len(all_launches):
            raise ValueError('launch_index is not present; detected ' + str(len(all_launches)) + ' launch candidates')
        selected_launch = all_launches[launch_index]
        lo, hi = selected_launch['hold_start_s'], selected_launch['end_s']
        reason = 'detected loaded-standstill launch sequence'
    if start_s is not None:
        lo = max(lo, start_s) if lo is not None else start_s
        reason += '; explicit start bound'
    if end_s is not None:
        hi = min(hi, end_s) if hi is not None else end_s
        reason += '; explicit end bound'
    selected = [r for r in rows if lo is not None and hi is not None and lo <= r['time_s'] <= hi]
    scoped_raw = [row for row in log.get('rows', []) if isinstance(row, (list, tuple))
                  and row and _number(row[0]) and lo is not None and hi is not None and lo <= row[0] <= hi]
    _, scoped_anomalies = _read_observations(dict(log, rows=scoped_raw))
    result = _calculate(dict(log, scope='selected_run'), setup, selected, scoped_anomalies)
    original_launch_indexes = {event['hold_start_s']: event['launch_index'] for event in all_launches}
    for event in result['launch_candidates']:
        event['launch_index'] = original_launch_indexes.get(event['hold_start_s'], event['launch_index'])
    result['selected_run'] = {'requested_start_s': start_s, 'requested_end_s': end_s,
                              'start_s': selected[0]['time_s'] if selected else None,
                              'end_s': selected[-1]['time_s'] if selected else None,
                              'reason': reason, 'launch_index': launch_index,
                              'captured_rows_in_scope': len(selected)}
    result['whole_log_peak_power'] = _power_peak(rows, assumptions)
    result['detected_launch_count'] = len(all_launches)
    result['coherent_channel_statistics'] = _channel_stats(selected)
    if selected_launch is not None:
        result['selected_launch'] = selected_launch
    result['scope_note'] = 'peaks belongs to selected_run. whole_log_peak_power is supplied separately and must never be substituted for launch/window peak. selected_launch describes the complete detected sequence when explicit bounds further narrow the window.'
    return result


def table_smoothness(table):
    """Axis-aware linear residuals, offered as candidates rather than automatic fixes."""
    values = table.get('values', [])
    candidates = []
    axes = {'x': table.get('x_values', []), 'y': table.get('y_values', [])}
    for r, row in enumerate(values):
        for c, value in enumerate(row):
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                continue
            residuals = []
            for direction, position, length in [('x', c, len(row)), ('y', r, len(values))]:
                if not 0 < position < length - 1:
                    continue
                try:
                    left, right = (row[c - 1], row[c + 1]) if direction == 'x' else (values[r - 1][c], values[r + 1][c])
                    axis = axes[direction]
                    x0, x1, x2 = (axis[position - 1], axis[position], axis[position + 1]) if len(axis) == length else (position - 1, position, position + 1)
                    if not all(isinstance(v, (int, float)) and math.isfinite(v) for v in (left, right, x0, x1, x2)) or not (x0 < x1 < x2 or x0 > x1 > x2):
                        continue
                    interpolated = left + (right - left) * (x1 - x0) / (x2 - x0)
                    residuals.append({'direction': direction, 'linear_neighbor_value': round(interpolated, 6), 'residual': round(value - interpolated, 6)})
                except (IndexError, TypeError, ZeroDivisionError):
                    continue
            if residuals:
                candidates.append({'row': r, 'column': c, 'value': value, 'axis_residuals': residuals})
    candidates.sort(key=lambda item: max(abs(v['residual']) for v in item['axis_residuals']), reverse=True)
    return {'largest_local_curvature_cells': candidates[:8],
            'interpretation': 'Linear residuals identify inspection candidates, not faults or recommended replacements. Intended ridges, breakpoints, limits and plateaus must be preserved; use related maps and log evidence before recommending a selective blend.'}
