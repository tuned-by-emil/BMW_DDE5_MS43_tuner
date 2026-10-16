"""MS43X presentation: standalone-style groups; source symbols remain intact."""
import hashlib
import re

PROFILE_ID = 'xdf:' + hashlib.sha256(b'MS43X001').hexdigest()[:16]


def is_ms43_definition(d):
    return d.profile_id in (PROFILE_ID, 'MS43X001')


NAMES = {
    'c_eng_disp': 'Engine displacement (cc)',
    'c_conf_load': 'Load source: MAP / MAF / Alpha-N',
    'c_inj_flow': 'Injector flow rate (cc/min)',
    'ip_ti_add_dly__vb': 'Injector dead time vs battery voltage',
    'ip_ti_fac_map__map': 'Injector pressure correction vs manifold pressure',
    'ip_ti_fac_ti__ti': 'Injector short-pulse correction',
    'ip_nr_ip_ve__vo': 'VE table selection vs valve overlap',
    'ldp_ve__nr_ip_sd': 'VE selection: valve-overlap breakpoints',
    'ldpm_ve_n': 'VE table engine-speed axis',
    'ldpm_ve_map': 'VE table manifold-pressure axis',
    'c_conf_n_max': 'Rev limiter mode: fuel cut / ignition cut',
    'c_conf_ff': 'Flex-fuel sensor enable',
    'c_conf_bc': 'Boost control mode',
    'c_conf_lc': 'Launch control enable',
    'c_conf_nls': 'No-lift shift enable',
    'c_conf_ral': 'Rolling anti-lag enable',
    'c_conf_aj_maf': 'Anti-jerk load reference',
    'ip_tps_sp_pvs__pvs__n': 'Pedal-to-throttle response map',
    'ip_pvs_dif_cr_neg__pvs_max__n': 'Throttle dashpot decay',
    'ip_pvs_dif_neg_fac__pvs_max__n': 'Throttle dashpot strength',
    'c_t_dly_pvs_cor_rpl': 'Pedal correction retrigger delay',
    'c_pvs_av_grd_max_rpl': 'Pedal correction activation rate threshold',
    'c_tps_sp_lgrd_tot': 'Throttle target rate limit',
    'c_tps_sp_lgrd_stop': 'Throttle target rate limit near end stops',
    'c_tps_sp_max': 'Maximum throttle target',
    'c_tps_sp_min': 'Minimum throttle target',
    'ip_iga_ron98_pl__n__maf': 'Ignition timing — RON 98, part/full load',
    'ip_iga_ron91_pl__n__maf': 'Ignition timing — RON 91, part/full load',
    'ip_iga_ron98_is__n__maf': 'Ignition timing — RON 98, idle',
    'ip_iga_optm_ron98__n__maf': 'Optimal ignition reference — RON 98',
    'id_n_max_mt__gear': 'Soft rev limiter by gear — manual',
    'id_n_max_at__gear': 'Soft rev limiter by gear — automatic',
    'id_n_max_max_mt__gear': 'Hard rev limiter by gear — manual',
    'id_n_max_max_at__gear': 'Hard rev limiter by gear — automatic',
}

# Conservative expansions for undocumented source symbols. Unknown components
# are not assigned guessed functions; those entries retain an address identifier.
WORDS = dict(n='engine speed', maf='air mass', map='manifold pressure',
    iga='ignition timing', ti='injection time', tco='coolant temperature', tia='intake air temperature',
    vb='battery voltage', vs='vehicle speed', toil='oil temperature', t='time',
    fac='factor', cor='correction', add='addition', max='maximum', min='minimum',
    dif='difference', dly='delay', diag='diagnostics', pvs='accelerator pedal',
    tps='throttle position', amp='atmospheric pressure', lam='lambda',
    vls='oxygen sensor voltage', up='upstream', down='downstream',
    st='start', ast='after start', is_='idle', pl='part load', fl='full load',
    cur='current', sp='target', av='actual', temp='temperature', ref='reference',
    cam='camshaft', in_='intake', ex='exhaust', vo='valve overlap',
    ve='volumetric efficiency', tq='torque', tqi='indicated torque',
    fil='filter', grd='rate', lgrd='rate limit', hys='hysteresis', thd='threshold',
    lim='limit', inc='increase', dec='decrease', ron98='RON 98', ron91='RON 91',
    e85='E85', cat='catalyst', ch='catalyst heating', knk='knock', cyl='cylinder',
    cycnr='cycle count', mt='manual transmission', at='automatic transmission',
    gear='gear', cyls='cylinders', idx='index', ctr='counter', ena='enable',
    conf='configuration', main='main', rpl='fuel restart', tra='transient',
    dtc='fault code', sum='sum', crlc='filter coefficient', ivvt='variable valve timing')
WORDS['is'] = WORDS.pop('is_')
WORDS['in'] = WORDS.pop('in_')


def readable_name(d):
    n = d.title
    if n in NAMES:
        return NAMES[n]
    ve = re.fullmatch(r'ip_map_ve_([1-8])__map__n', n)
    if ve:
        return 'VE Table ' + ve[1]
    for prefix, text in [('ip_pvs_cor_max_rpl_', 'Pedal correction activation threshold'),
                         ('ip_pvs_cor_rpl_lgrd_', 'Pedal correction rate limit'),
                         ('ip_t_pvs_cor_rpl_', 'Pedal correction duration')]:
        if n.startswith(prefix):
            gear = n[len(prefix):].split('_')[0]
            return text + (' — neutral' if gear == '0' else ' — gear ' + gear)
    if d.description and not d.description.startswith('UNVERIFIED'):
        text = d.description.splitlines()[0].strip()
        text = re.sub(r'^Configuration switch\s*-\s*', '', text)
        return text.replace('effiency', 'efficiency').rstrip('.')
    tokens = [t for t in re.sub(r'^(?:c|ip|id|ldp|ldpm)_', '', n).split('_') if t]
    if tokens and all(t in WORDS or t.isdigit() for t in tokens):
        return ' '.join(WORDS.get(t,t) for t in tokens).capitalize()
    kind = 'Axis' if d.category == 'Axis' else 'Setting' if d.kind == 'constant' else 'Table'
    return f'{d.category} {kind.lower()} — 0x{d.z_address:X} (undocumented)'


def category_path(d):
    n, c = d.title, d.category
    if n == 'c_eng_disp': return ('Fuel', 'Engine setup')
    if re.fullmatch(r'ip_map_ve_[1-8]__map__n',n): return ('Fuel',)
    if n in ('ip_nr_ip_ve__vo','ldp_ve__nr_ip_sd'): return ('Fuel','VE selection')
    if c == 'Axis': return ('Advanced','Reference axes')
    if n.startswith(('c_af_stoich','c_fuel_density','ip_af_target')): return ('Fuel','Air-fuel targets')
    if n.startswith(('c_inj_flow','ip_ti_add_dly','ip_ti_fac_')): return ('Fuel','Injectors')
    groups = {
        'Injection': ('Fuel','Injection'), 'Fuel System': ('Fuel','Fuel settings'),
        'Lambda Controller': ('Fuel','Lambda control'), 'Flex Fuel': ('Fuel','Flex fuel'),
        'Fuel Pump': ('Fuel','Fuel pump'), 'Trailing Throttle Fuel Cut': ('Fuel','Overrun fuel cut'),
        'Ignition': ('Ignition',), 'Knock': ('Ignition','Knock control'),
        'Throttle': ('Throttle and pedal',), 'Anti Jerk': ('Throttle and pedal','Anti-jerk damping'),
        'Torque': ('Torque',), 'Engine Speed': ('Engine limiters',),
        'Vehicle Speed': ('Engine limiters','Vehicle speed'),
        'Load Acquisition': ('Air and load','Load calculation'),
        'Sensor Definitions': ('Air and load','Sensor calibration'),
        'Vanos': ('Cam timing',), 'DISA': ('Air and load','Intake flap'),
        'Intake Model': ('Air and load','Intake model'), 'Intake Air': ('Air and load','Intake temperature'),
        'Boost Controller': ('Boost','Control'), 'Overboost Protection': ('Boost','Overboost protection'),
        'Launch Control': ('Motorsport','Launch control'), 'No Lift Shift': ('Motorsport','No-lift shift'),
        'Rolling Anti Lag': ('Motorsport','Rolling anti-lag'),
        'Config Switches': ('Configuration',),
    }
    if c in groups: return groups[c]
    if c in ('Diagnostic Trouble Codes','DTC Suppression','System Diagnosis','System Monitoring','OBD','Misfire'):
        return ('Diagnostics',c)
    return ('Advanced',c)


def drift_shortcuts(d):
    n = d.title
    if n == 'c_eng_disp': return ('Drift setup','Engine setup')
    if n == 'ip_tps_sp_pvs__pvs__n': return ('Drift setup','Pedal response')
    if n.startswith(('ip_pvs_cor_max_rpl_', 'ip_pvs_cor_rpl_lgrd_', 'ip_t_pvs_cor_rpl_')) or n in (
            'c_t_dly_pvs_cor_rpl','c_pvs_av_grd_max_rpl','c_tps_sp_lgrd_tot'):
        return ('Drift setup','Pedal smoothing and rate limits')
    if n in ('ip_pvs_dif_cr_neg__pvs_max__n','ip_pvs_dif_neg_fac__pvs_max__n'):
        return ('Drift setup','Throttle dashpot')
    if d.category == 'Anti Jerk' or n.startswith('ip_iga_aj_'):
        return ('Drift setup','Anti-jerk damping')
    if n.startswith(('id_n_max_', 'c_n_max_')) or n == 'c_conf_n_max':
        return ('Drift setup','Rev limiters')
    if d.category == 'Torque' and 'gear' in n and not any(t in n for t in ('diag','mon')):
        return ('Drift setup','Torque by gear')
    return None


def ve_selection_labels(document):
    """Label actual selector anchors; do not invent interpolation/switch ranges."""
    selector = next((d for d in document.xdf.definitions if d.title=='ip_nr_ip_ve__vo'),None)
    if selector is None: return {}
    values = [v for row in document.map_values(selector) for v in row]
    axis = selector.y_axis if selector.rows > 1 else selector.x_axis
    points = document.axis_values(axis)
    result = {}
    for table in range(1,9):
        angles = [angle for angle, value in zip(points,values) if abs(value-table)<1e-6]
        result[table] = (', '.join(f'{a:g}°' for a in angles) + ' overlap point' + ('s' if len(angles)>1 else '')
                         if angles else 'no exact selector point')
    return result
