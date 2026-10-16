"""Concise local calculation results; no model, account or network request."""
from __future__ import annotations
import math


def _number(value, places=1):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return 'unavailable'
    rendered = f'{value:.{places}f}'
    return rendered.rstrip('0').rstrip('.') if places > 0 else rendered


def _time(value):
    return _number(value, 3)


def _sample_metadata(scope):
    samples = [value for value in (scope.get('peaks') or {}).values() if isinstance(value, dict)]
    for key in ('peak_power', 'peak_with_coherent_airflow', 'loaded_standstill_peak',
                'pre_first_motion_peak', 'hold_end', 'first_motion'):
        value = scope.get(key)
        if isinstance(value, dict):
            samples.append(value)
    for shift in scope.get('shift_candidates') or []:
        samples.extend(value for value in (shift.get('before'), shift.get('after')) if isinstance(value, dict))
    return samples


def _gear_summary(scope, confirmed_gear_bindings):
    # The UI-confirmed table bindings identify these three verified DDE selector
    # paths. Do not equate arbitrary selector numbers or eighth-gear spoofing
    # with physical gear, even if a diagnostic result is named "gear".
    addresses = {0xE945A: 1, 0xE94C0: 2, 0xE9526: 3}
    selector_to_gear = {}
    if isinstance(confirmed_gear_bindings, dict):
        for key, binding in confirmed_gear_bindings.items():
            if not isinstance(binding, dict) or str(key) not in {str(i) for i in range(1, 9)}:
                continue
            if not isinstance(binding.get('source'), str) or not binding['source'].strip():
                continue
            address = binding.get('table_address')
            try:
                address = int(address, 0) if isinstance(address, str) else address
            except ValueError:
                continue
            if type(address) is int and address in addresses:
                selector_to_gear[addresses[address]] = int(key)
    samples = _sample_metadata(scope)
    statistics = scope.get('coherent_channel_statistics') or {}
    codes = (statistics.get('ecu_torque_selector') or {}).get('observed_codes')
    if codes is None:
        codes = [sample['ecu_torque_selector'] for sample in samples if 'ecu_torque_selector' in sample]
    codes = sorted({int(value) for value in codes if isinstance(value, (int, float))
                    and not isinstance(value, bool) and math.isfinite(value) and value == int(value)})
    confirmed = sorted({selector_to_gear[code] for code in codes if code in selector_to_gear})
    unknown = [code for code in codes if code not in selector_to_gear]
    ratios = sorted({int(sample['candidate_gear']) for sample in samples
                     if type(sample.get('candidate_gear')) is int and 1 <= sample['candidate_gear'] <= 8})
    pieces = []
    if confirmed:
        pieces.append('Confirmed selector bindings observed gears ' + ', '.join(map(str, confirmed)) + '.')
        if unknown:
            pieces.append('Unmapped selector codes ' + ', '.join(map(str, unknown[:8])) + '.')
    elif codes:
        pieces.append('Observed selector codes ' + ', '.join(map(str, codes[:8])) + '; physical-gear bindings unconfirmed.')
    elif selector_to_gear:
        pieces.append('Gear-map bindings verified; selector not logged in this scope.')
    if ratios:
        pieces.append('RPM/speed ratio candidates ' + ', '.join(map(str, ratios)) + ' (converter slip may affect them).')
    if not pieces:
        return 'Actual gear and hookup are unconfirmed.'
    pieces.append('Exact hookup is unconfirmed.')
    return ' '.join(pieces)


def format_local_results(analysis, prefer_launch=True, confirmed_gear_bindings=None):
    """Render up to four factual bullets from calculate_log/calculate_run.

    A whole-log result defaults to its first detected loaded-standstill launch,
    when present. A selected_run always retains its explicit window's peak.
    """
    peaks = analysis.get('peaks') or {}
    selected = analysis.get('selected_run')
    event = analysis.get('selected_launch')
    shifts = analysis.get('shift_candidates') or []
    peak = peaks.get('power')
    paired = peaks.get('power_with_coherent_airflow')
    evidence_scope = analysis
    label = 'Captured-scope peak'
    if selected:
        label = 'Selected-run peak'
        if selected.get('launch_index') is not None:
            label = 'Launch ' + str(selected['launch_index'] + 1) + ' peak'
            if selected.get('requested_start_s') is not None or selected.get('requested_end_s') is not None:
                label += ' within selected window'
    elif prefer_launch and analysis.get('launch_candidates'):
        event = analysis['launch_candidates'][0]
        label = 'Launch ' + str(event.get('launch_index', 0) + 1) + ' peak'
        peak = event.get('peak_power')
        paired = event.get('peak_with_coherent_airflow')
        shifts = event.get('shift_candidates') or []
        evidence_scope = event
    bullets = []
    if peak and (peak.get('crank_bhp_by_assumed_efficiency') or {}).get('40%') is not None:
        scenarios = peak['crank_bhp_by_assumed_efficiency']
        reference = _number(scenarios['40%'])
        interval = ''
        if scenarios.get('35%') is not None and scenarios.get('42%') is not None:
            interval = f"; {_number(scenarios['35%'])}–{_number(scenarios['42%'])} bhp at 35–42%"
        bullets.append(f'- {label}: {reference} bhp at {_time(peak.get("time_s"))} s '
                       f'(40% assumed brake efficiency{interval}). Fuel-based crank estimate, not measured power.')
    else:
        bullets.append('- Power: unavailable without a captured same-row RPM and total-cycle IQ sample.')
    if shifts:
        windows = ', '.join(f'{_time(shift.get("start_s"))}–{_time(shift.get("end_s"))} s' for shift in shifts[:3])
        suffix = f'; {len(shifts) - 3} more' if len(shifts) > 3 else ''
        bullets.append(f'- Shifts: {len(shifts)} upshift/converter candidates at {windows}{suffix}. '
                       + _gear_summary(evidence_scope, confirmed_gear_bindings))
    else:
        bullets.append('- Shifts: no qualifying RPM-drop/speed candidate in this scope. '
                       + _gear_summary(evidence_scope, confirmed_gear_bindings))
    if peak and peak.get('calculated_lambda') is not None:
        bullets.append(f'- Airflow: calculated λ {_number(peak["calculated_lambda"], 3)} at that peak, '
                       'using simultaneous reported MAF/IQ; not measured exhaust lambda.')
    elif paired and paired.get('calculated_lambda') is not None:
        bullets.append(f'- Airflow: no simultaneous MAF at the power peak. Separate paired check at '
                       f'{_time(paired.get("time_s"))} s gives calculated λ {_number(paired["calculated_lambda"], 3)}.')
    else:
        bullets.append('- Airflow: no captured same-row MAF/IQ check; separate sparse requests were not combined.')
    economy = analysis.get('economy') or {}
    cruise = economy.get('warm_stable_cruise') or []
    if cruise:
        chosen = max(cruise, key=lambda item: item.get('covered_duration_s', 0))
        bullets.append(f'- Warm cruise: {_number(chosen.get("estimated_l_100km"), 2)} estimated L/100 km at '
                       f'{_number(chosen.get("mean_speed_kmh"))} km/h over {_number(chosen.get("covered_duration_s"))} s '
                       f'({_time(chosen.get("start_s"))}–{_time(chosen.get("end_s"))} s). IQ-derived; stops excluded.')
    else:
        moving = economy.get('warm_moving') or {}
        if moving.get('estimated_l_100km') is not None:
            bullets.append(f'- Economy: {_number(moving["estimated_l_100km"], 2)} estimated L/100 km during warm moving intervals; '
                           'stops excluded. No qualifying steady warm cruise baseline in this scope.')
        else:
            bullets.append('- Economy: no qualifying warm moving/cruise evidence in this scope; no economy-patch conclusion.')
    return '\n'.join(bullets)
