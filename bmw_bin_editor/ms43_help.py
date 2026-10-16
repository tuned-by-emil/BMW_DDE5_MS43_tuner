"""Read-only MS43X setting explanations, with source and uncertainty kept explicit.

The supplied XDF is the authority for exact symbols, units and switch encodings.
The MS43X manual supplies feature context. Base-MS43 guidance is identified
separately; no calibration change is implied by displaying this help.
"""
from dataclasses import dataclass
import re
from html import escape
from .ms43_navigation import readable_name, NAMES

MS43X = 'https://www.ms4x.net/index.php?title=MS43X_Custom_Firmware'
MS43 = 'https://www.ms4x.net/index.php?title=Siemens_MS43'


@dataclass(frozen=True)
class Help:
    text: str
    related: tuple[str, ...] = ()
    source: str = 'Supplied MS43X001 XDF description'


HELP = {
    'c_eng_disp': Help('Total engine capacity in cc, across all cylinders. Enter the displacement of your actual engine. '
        'The editor converts cc to the firmware storage scale; one stored step is about 0.977 cc.',
        ('c_inj_flow','ip_nr_ip_ve__vo')),
    'c_conf_load': Help('Selects how engine load is obtained: 0 = MAP sensor, 1 = MAF sensor, 2 = Alpha-N. '
        'Match this to the installed sensors and calibration.',('id_map_tab__v_map','id_maf_tab__v_maf','c_map_min_diag','c_map_max_diag')),
    'c_inj_flow': Help('Injector flow capacity in cc/min. Use injector characterization at the intended pressure differential. '
        'This scalar is part of the fuel calculation; dead time and pressure/short-pulse corrections are separate.',
        ('ip_ti_add_dly__vb','ip_ti_fac_map__map','ip_ti_fac_ti__ti')),
    'ip_ti_add_dly__vb': Help('Additional injector opening-delay compensation versus battery voltage. '
        'Use the dead-time curve supplied or measured for the injectors. Editing flow rate does not replace this curve.',('c_inj_flow',)),
    'ip_ti_fac_map__map': Help('Injector pulse-width multiplier versus manifold pressure. '
        '1.0 leaves the calculated pulse unchanged; values above/below 1.0 increase/decrease it.',('c_inj_flow',)),
    'ip_ti_fac_ti__ti': Help('Injector pulse-width multiplier versus injection time. '
        'Used to characterize nonlinearity, particularly at short pulses. 1.0 is an unchanged multiplier.',('c_inj_flow',)),
    'ip_nr_ip_ve__vo': Help('Assigns a VE table number to each valve-overlap breakpoint. '
        'Changing these numbers changes which of the eight VE tables is selected. '
        'The navigator reads these assignments from the current BIN; its labels describe exact points, not exclusive operating bands.',
        ('ldp_ve__nr_ip_sd',)),
    'ldp_ve__nr_ip_sd': Help('Valve-overlap breakpoints for the VE table selector, in crankshaft degrees. '
        'Changing an axis point moves the corresponding selector anchor.',('ip_nr_ip_ve__vo',)),
    'ldpm_ve_n': Help('Shared engine-speed breakpoints for the VE maps, in RPM. '
        'Changing a breakpoint changes where the existing table values are located; it does not resample their values.'),
    'ldpm_ve_map': Help('Shared manifold-pressure breakpoints for the VE maps. '
        'Changing the axis relocates existing table values; it does not resample the VE surfaces.'),
    'ip_tps_sp_pvs__pvs__n': Help('Throttle-position target as a function of accelerator-pedal position and engine speed. '
        'This is the main pedal-to-throttle response map. Rate limits, pedal corrections and anti-jerk can still affect the resulting response.',
        ('c_tps_sp_lgrd_tot','ip_pvs_cor_rpl_lgrd_1__npvscor','ip_iga_aj_ron98__n__maf')),
    'c_tps_sp_lgrd_tot': Help('Limits how quickly the throttle-position target may change over the operating range. '
        'A larger permitted gradient allows a faster target change; this is a rate limit, not an opening-angle target.',
        ('ip_tps_sp_pvs__pvs__n','c_tps_sp_lgrd_stop')),
    'c_tps_sp_lgrd_stop': Help('Throttle-target rate limit near the end-stop regions. '
        'The associated lower/upper boundaries determine where this limit is used. Treat it separately from the general response map.',
        ('c_tps_sp_bol','c_tps_sp_tol','c_tps_sp_lgrd_tot')),
    'c_tps_sp_max': Help('Upper bound for the requested throttle-position target. '
        'Keep its units and relationship to the throttle calibration in mind; it is not a percentage pedal map.'),
    'c_tps_sp_min': Help('Lower bound for the requested throttle-position target. '
        'Changing it can affect low-opening operation as well as response.'),
    'ip_pvs_dif_cr_neg__pvs_max__n': Help('Pedal-release dashpot decay factor. '
        'The base MS43 algorithm multiplies both new and retained correction by this factor. '
        'Setting it to zero is therefore inferred to cancel dashpot smoothing; MS43X vehicle behavior remains untested.',
        ('ip_pvs_dif_neg_fac__pvs_max__n',), MS43+'#Drivers_Wish_Dashpot'),
    'ip_pvs_dif_neg_fac__pvs_max__n': Help('XDF-defined throttle dashpot factor. '
        'It is a separate control from the pedal-to-throttle target. A verified disabling value is not documented in the supplied XDF.',
        ('ip_pvs_dif_cr_neg__pvs_max__n','ip_tps_sp_pvs__pvs__n')),
    'c_t_dly_pvs_cor_rpl': Help('Waiting time before another pedal-correction cycle may start. '
        'This changes retrigger timing; it is not the duration of the correction that is already active.'),
    'c_pvs_av_grd_max_rpl': Help('Pedal-input rate threshold for pedal-increase correction. '
        'Base MS43 documentation states that setting this or c_pvs_cor_max_rpl to zero disables that smoothing function.',
        ('c_pvs_cor_max_rpl',), MS43+'#Drivers_Wish_Input_Correction'),
    'c_pvs_cor_max_rpl': Help('Upper pedal-input threshold for pedal-increase correction. '
        'Base MS43 documentation specifies zero to disable this comfort smoothing function. '
        'The drift preset uses this gate rather than rewriting each gear gradient and timer.',
        ('c_pvs_av_grd_max_rpl',), MS43+'#Drivers_Wish_Input_Correction'),
    'c_conf_aj_maf': Help('Chooses the load reference for anti-jerk: 0 = MAF signal, 1 = MAF substitute. '
        'Neither value is documented as disabling anti-jerk.',('c_tco_min_aj','ip_iga_aj_ron98__n__maf')),
    'c_tco_min_aj': Help('Minimum coolant temperature at which anti-jerk may operate. '
        'A higher minimum excludes operation below that temperature. '
        'Base MS43 guidance describes approximately 142.5 °C as an inhibit setting; this has not been independently verified in MS43X code.',
        ('ip_iga_aj_ron91__n__maf','ip_iga_aj_ron98__n__maf'),MS43+'#Anti-Jerk (base MS43 guidance) + supplied XDF'),
    'c_conf_n_max': Help('Selects the engine-speed limiting method: 0 = fuel cut, 1 = ignition cut. '
        'The RPM thresholds and limiter hysteresis are separate settings.',('id_n_max_mt__gear','id_n_max_at__gear','c_n_max_hys_max')),
    'c_n_max_hys_max': Help('Engine-speed hysteresis for re-enabling all ignition coils after limiting. '
        'It defines recovery behavior around the limiter; it does not set the main RPM limit.',('c_conf_n_max',)),
    'c_conf_bc': Help('Boost-control mode: 0 = disabled, 1 = fixed duty/open loop, 2 = closed loop. '
        'Choose the matching control and target tables before enabling a mode.',('ip_bc_fixed_pwm__n','ip_bc_map_req_ron98__gear__n')),
    'c_bc_pwm_min': Help('Minimum allowed boost-control solenoid duty cycle, in percent. '
        'This bounds controller output; it is not the pressure target.',('c_bc_pwm_max','c_conf_bc')),
    'c_bc_pwm_max': Help('Maximum allowed boost-control solenoid duty cycle, in percent. '
        'Increasing this only raises the output ceiling; it does not define a calibrated boost target.',('c_bc_pwm_min','c_conf_bc')),
    'c_bc_map_thr': Help('Minimum manifold pressure for enabling fixed-duty/open-loop boost operation.',('c_conf_bc','ip_bc_fixed_pwm__n')),
    'c_bc_map_req_thr': Help('Requested-pressure threshold for enabling closed-loop boost control. '
        'The XDF identifies the wastegate spring cracking pressure as a reference for this threshold.',('c_conf_bc',)),
    'c_bc_i_max_thr': Help('Pressure-target error threshold for changing between dynamic and steady-state closed-loop boost operation.'),
    'c_conf_ff': Help('Flex-fuel sensor configuration: 0 = sensor absent, 1 = sensor present. '
        'Check the substitute blend settings as well as the fuel maps before changing it.',
        ('ip_ff__v_ff','c_ff_fac_ti_diag_sub','c_ff_fac_iga_diag_sub')),
    'c_ff_lgrd': Help('Rate limit for the flex-fuel blend percentage, expressed in percent per 100 ms. '
        'It controls how quickly the blend may change, not the final ethanol percentage.'),
    'c_conf_lc': Help('Launch-control mode: 0 = off, 1 = clutch-operated, '
        '2 = clutch-operated with the cruise-control LED active. Pedal and vehicle-speed conditions also apply.',
        ('c_lc_vs_max','c_lc_pvs_min','c_lc_n_max')),
    'c_lc_vs_max': Help('Vehicle-speed threshold that deactivates launch control. '
        'It determines how long launch limiting remains available as the car starts moving.',('c_lc_n_max','c_conf_lc')),
    'c_lc_n_max': Help('Engine-speed ceiling while launch control is active. '
        'The normal engine limiter is a separate calibration.',('c_conf_lc','c_lc_vs_max')),
    'c_conf_nls': Help('No-lift shift configuration: 0 = off, 1 = clutch-operated. '
        'Vehicle speed and accelerator-pedal thresholds are separate conditions.',('c_nls_vs_min','c_nls_pvs_min','c_nls_act_ofs_n_max')),
    'c_conf_ral': Help('Rolling anti-lag configuration: 0 = off, 1 = activation using the cruise-control minus button. '
        'A separate accelerator-pedal threshold also applies.',('c_ral_pvs_min','c_ral_act_ofs_n_max')),
    'c_conf_icl': Help('M-cluster shift-light configuration: 0 = disabled, 1 = enabled. '
        'The segment and upshift-speed tables determine the display.',('id_icl_seg__toil','id_icl_seg__n','id_icl_sup_n__toil')),
}

# Brief manual-specific additions; longer per-setting descriptions above come
# from the user-supplied XDF, scaling definitions and control interpretation.
MANUAL_NOTES = {
    'c_conf_load': 'Load-sensor faults can select Alpha-N fallback.',
    'ip_nr_ip_ve__vo': 'Mapping every selector point to 8 selects VE Table 8 only.',
    'ip_af_target_ron98__maf__n': 'Departure from stoichiometric also controls injection full-load state.',
    'ip_af_target_e85__maf__n': 'Departure from stoichiometric also controls injection full-load state.',
    'ip_ti_fac_map__map': 'Match correction to regulator reference: the supplied default assumes manifold-referenced pressure.',
    'c_conf_n_max': 'Throttle-safety faults can restore fuel-cut limiting.',
    'c_conf_ff': 'With the sensor disabled, substitute values fix the blend rather than automatically selecting RON98.',
    'c_abc_inc_load': 'Zero disables load-fault detection and Alpha-N fallback.',
    'id_icl_seg__toil': 'E39 M clusters support only four segments.',
    'id_icl_seg__n': 'E39 M clusters support only four segments.',
}

GROUP_HELP = {
    'Drift setup': 'Shortcuts to the original response, damping and limiter settings. Selecting a shortcut applies no calibration preset.',
    'Fuel': 'Engine capacity, volumetric efficiency, air-fuel targets and injector characterization.',
    'Ignition': 'Ignition-angle targets and corrections. Check whether a value is an absolute target, an offset or a multiplier.',
    'Throttle and pedal': 'Pedal demand, throttle targets and response shaping. Controller diagnostics and mechanical calibration are distinct settings.',
    'Anti-jerk damping': 'Temporary ignition intervention that smooths changes in delivered torque. Load reference, activation gates and corrections are separate controls.',
    'Pedal smoothing and rate limits': 'Per-gear pedal correction thresholds, duration and rate limits. Their functions differ; zero is not a universal disable value.',
    'Throttle dashpot': 'Release/return response controls. Supplied descriptions do not establish a universal disabling value.',
    'Rev limiters': 'RPM limits, limiting mode and recovery hysteresis. A mode switch does not set the RPM threshold.',
    'VE selection': 'Maps valve-overlap points to VE table numbers. The labels in Fuel reflect the open BIN.',
    'Injectors': 'Flow, opening delay and pulse/pressure corrections must describe the installed injectors and fuel system.',
    'Boost': 'Solenoid output, pressure targets and feedback-control settings.',
    'Advanced': 'Less commonly edited definitions and reference axes; undocumented behavior remains explicitly unidentified.',
}


def _clean(text):
    return (text or '').replace('\r','').replace('\ufffdCRK','° crank').replace('\ufffdPVS','° pedal').replace('\ufffdC','°C')


def explain(d):
    n=d.title
    if n in HELP:
        h=HELP[n]
    elif re.fullmatch(r'ip_map_ve_[1-8]__map__n',n):
        h=Help('Volumetric efficiency versus engine speed and manifold pressure. The editor displays percent: 100% is VE = 1.0. '
            'This table belongs to a valve-overlap selection point, not a fixed idle/cruise/full-load mode.',('ip_nr_ip_ve__vo','c_eng_disp'))
    elif n.startswith('ip_iga_aj_'):
        h=Help('Anti-jerk ignition-angle correction for the named fuel grade. It smooths torque transitions through temporary ignition retard. '
            'In this XDF, engineering 0° corresponds to raw 128; raw zero is -48°. '
            'Changing these tables does not disable the other response-shaping functions.',('c_tco_min_aj','c_conf_aj_maf'),MS43+'#Anti-Jerk (base MS43 context) + supplied XDF')
    elif n.startswith('ip_pvs_cor_max_rpl_'):
        h=Help('Minimum pedal input for activating the per-gear pedal correction. '
            'The threshold, correction duration and permitted change rate are separate controls.')
    elif n.startswith('ip_pvs_cor_rpl_lgrd_'):
        h=Help('Permitted change gradient for the per-gear pedal correction. '
            'This limits the correction rate, not the throttle motor directly. A verified disabling value is not documented.')
    elif n.startswith('ip_t_pvs_cor_rpl_'):
        h=Help('Duration of the per-gear pedal-input correction. '
            'Compare with its activation threshold, gradient and retrigger delay.',('c_t_dly_pvs_cor_rpl',))
    elif n.startswith(('c_af_stoich_','c_fuel_density_')):
        h=Help(_clean(d.description).strip()+'\nUsed for the named fuel in the injector/fuel calculation. '
            'Keep fuel properties distinct from the operating air-fuel target.',('c_inj_flow',))
    elif n.startswith('ip_af_target_'):
        h=Help('Requested air-to-fuel mass ratio for the named fuel. '
            'For a fixed fuel, a smaller AFR number requests a richer mixture and a larger number a leaner mixture. '
            'This is AFR, not lambda.',('c_af_stoich_ron98','c_af_stoich_e85'))
    elif n.startswith('c_ff_fac_') and n.endswith('_diag_sub'):
        h=Help('Substitute blend factor for the named subsystem when the flex-fuel input is unavailable. '
            '0.0 selects its RON98 side; 1.0 selects its E85 side.',('c_conf_ff',))
    elif n.startswith('ip_ff_fac_'):
        h=Help('Blending factor for the named subsystem versus flex-fuel blend. '
            '0.0 selects its RON98 side; 1.0 selects its E85 side.',('c_conf_ff',))
    elif n.startswith(('c_nls_act_ofs_n_max','c_ral_act_ofs_n_max')):
        h=Help('RPM offset added to current engine speed when the named feature activates. '
            'This is an offset, not an absolute RPM ceiling.')
    elif n == 'c_ral_iga_ofs_ron98':
        h=Help('The symbol and feature grouping identify a RON98 rolling anti-lag ignition offset, '
            'but its XDF description says no-lift shift. The manual repeats that conflicting description. '
            'Treat this function label as needing firmware-code or hardware confirmation.',('c_conf_ral',),
            'Supplied XDF and '+MS43X+'#Rolling_Anti_Lag (source discrepancy)')
    elif n.startswith(('c_lc_iga_ofs_','c_nls_iga_ofs_','c_ral_iga_ofs_')):
        feature='launch control' if n.startswith('c_lc_') else 'no-lift shift' if n.startswith('c_nls_') else 'rolling anti-lag'
        h=Help('Ignition-angle offset while '+feature+' is active, for the named fuel. '
            'Interpret the sign as an offset; it is not the normal base ignition target.')
    elif n.startswith(('c_lc_af_target_','c_nls_af_target_','c_ral_af_target_')):
        h=Help('Air-fuel ratio target while the named feature is active. '
            'It applies to that feature/fuel combination, rather than the normal operating target map.')
    elif n.startswith('c_ob_thr_hys_'):
        h=Help('Overboost deactivation hysteresis for the named fuel. '
            'It is separate from the pressure threshold that triggers protection.')
    elif n.startswith('c_ob_thr_'):
        h=Help('Manifold-pressure threshold for overboost protection for the named fuel. '
            'The separate hysteresis determines release behavior.')
    elif n.startswith('ip_bc_map_req_') and '__gear__n' in n:
        h=Help('Manifold-pressure request by engine speed and gear for the named fuel. '
            'This is a target; solenoid duty limits and controller gains are separate.',('c_conf_bc',))
    elif n=='ip_bc_fixed_pwm__n':
        h=Help('Fixed boost-solenoid duty versus RPM for open-loop operation.',('c_conf_bc',))
    elif d.description and not d.description.startswith('UNVERIFIED'):
        h=Help(_clean(d.description).strip())
    else:
        h=Help('Detailed behavior is not described in the supplied XDF or this curated help. '
            'The displayed name alone does not establish activation conditions, tuning direction or a disabling value.')
    if n in MANUAL_NOTES:
        return Help(h.text+'\nMS43X manual: '+MANUAL_NOTES[n],h.related,h.source+'; '+MS43X)
    return h


def help_text(d):
    h=explain(d)
    result=h.text
    if d.description.startswith('UNVERIFIED'):
        result=_clean(d.description.splitlines()[0])+'\n\n'+result
    if h.related:result+='\n\nRelated settings: '+', '.join(NAMES.get(name,name) for name in h.related)
    result+='\n\nSource: '+h.source
    return result


def setting_tooltip(d,usage_note=None):
    text=readable_name(d)+'\n\n'+help_text(d)
    if usage_note:
        text+='\n\nCurrent BIN selector: '+usage_note+'. Exact points only; between-point behavior is not inferred.'
    units=_clean(d.z_units) or 'not specified by the XDF'
    text+=f'\n\nTechnical reference: {d.title}\nFile offset: 0x{d.z_address:06X}; {d.rows} × {d.cols}; units: {units}'
    return text


def tooltip_html(text):
    """Constrain hover help to a readable width and escape all XDF text."""
    return '<qt><table width="480"><tr><td>' + escape(text).replace('\n','<br>') + '</td></tr></table></qt>'
