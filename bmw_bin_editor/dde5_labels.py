"""English display labels only; storage identities and diagnostic IDs stay intact."""
import re


MAP_NAMES = {
    0xF9394: 'Boost Target A', 0xF95D8: 'Boost Target B', 0xF981C: 'Boost Target C',
    0xF7F44: 'Turbo control — N75 A', 0xF8900: 'Turbo control — N75 B',
    0xF8478: 'Turbo control — N75 C', 0xF86BC: 'Turbo control — N75 D',
    0xFA3DC: 'Turbo control upper limit',
    0xE77D2: 'Fuel / air limit A (lambda)', 0xE7A16: 'Fuel / air limit B (lambda)',
    0xE80EA: 'Torque to fuel quantity A (candidate)',
    0xE832E: 'Torque to fuel quantity B (candidate)',
    0xE8572: 'Torque to fuel quantity C (candidate)',
    0xE0672: 'Accelerator torque request A', 0xE08B6: 'Accelerator torque request B',
    0xE0660: 'Accelerator pedal axis', 0xE0640: 'Engine speed axis',
    0xE9408: 'Torque limit — default gear state', 0xE95F2: 'Torque limit — gear state 5',
    0xE945A: 'Torque limit — gear state 1', 0xE958C: 'Torque limit — gear state 4',
    0xE96BE: 'Torque limit — reverse state', 0xE94C0: 'Torque limit — gear state 2',
    0xE9658: 'Torque limit — gear state 6', 0xE9526: 'Torque limit — gear state 3',
    0xEA80C: 'Main injection timing A', 0xEAA50: 'Main injection timing B',
    0xEAC94: 'Main injection timing C', 0xEAED8: 'Main injection timing D',
    0xEB11C: 'Main injection timing E',
    0xEC71C: 'Pilot injection 1 timing separation', 0xEEBE8: 'Pilot injection 1 quantity',
    0xEF114: 'Pilot fuel quantity — event bit 0x20 (candidate)',
    0xF1A36: 'Pilot fuel quantity — event bit 0x40 (candidate)',
    0xF5364: 'Injection event phase — default', 0xF57C8: 'Injection event phase — selector 11',
    0xF5C2C: 'Injection event phase — selector 8', 0xF6090: 'Injection event phase — selector 7',
    0xF64F4: 'Injection event phase — selector 9', 0xF6958: 'Injection event phase — selector 15',
    0xEDF26: 'Injection event base — normal state', 0xEE16A: 'Injection event base — state -1',
    0xFC9D0: 'Rail pressure target reference / limit',
    0xFCE64: 'Rail pressure target — mode other than 2',
    0xFCC00: 'Rail pressure target — mode 2',
    0xFD18C: 'Rail pressure target preliminary upper limit',
    0xFD1DC: 'Rail pressure target upper limit — flag set',
    0xFD1A0: 'Rail pressure target upper limit — flag clear',
    0xFD2E4: 'Rail pressure target lower limit — flag clear',
    0xFD2F2: 'Rail pressure target lower limit — flag set',
    0xF7154: 'Minimum rail pressure to enable injection',
    0xE3612: 'Torque limit rate of change', 0xE1F16: 'EGR / air actuator control',
    0xE6D86: 'Torque protection gear enable', 0xEA64E: 'Main injection timing dynamic correction',
    0xF500C: 'Injection quantity drift compensation',
    0xF9132: 'Regeneration boost correction vs intake temperature',
    0xFAAB4: 'Regeneration turbo precontrol vs DPF soot mass',
    0xFC6FC: 'Rail valve close-hold precontrol offset',
    0xFC70E: 'Rail valve fast pressure reduction offset',
    0xFD06E: 'Minimum rail pressure vs battery voltage',
    0xE3856: 'Manifold pressure sensor scaling',
    0xE9254: 'Transmission positive torque request enable',
    0xE9177: 'Transmission message reception enable',
    0xE11AC: 'Internal normalization divisor', 0xF8CA4: 'Turbo actuator correction / limit',
    0xE3AA8: 'Torque limit candidate A', 0xE3B0E: 'Torque limit candidate B',
    0xE3B6C: 'Torque limit candidate C',
    0xE87DA: 'Fuel quantity correction lookup A', 0xE8A40: 'Fuel quantity correction lookup B',
    0xE93D6: 'Default gear torque limit axis',
    0xFBCD6: 'Rail control table A', 0xFC0D4: 'Rail control table B',
    0xFC75A: 'Rail control table C',
}


def table_name(definition):
    """Retain uncertainty; never assign 361830 aliases to another ECU or raw discovery."""
    if definition.profile_id != '361830' or definition.discovery_unverified:
        return None
    original = definition.title or ''
    unverified = '[UV]' in original
    if not unverified and definition.z_address in MAP_NAMES:
        return MAP_NAMES[definition.z_address]
    title = re.sub(r'^0x[0-9a-f]+\s*', '', original, flags=re.I)
    title = re.sub(r'\[[^]]*\]', '', title).strip()
    axis = re.fullmatch(r'Axis_[0-9A-F]+_(.+?)_(\d+)(?:\s.*)?', title)
    if axis:
        units = {'RPM': 'Engine speed', 'mm3/Stk': 'Fuel volume', 'mm3/stk': 'Fuel volume',
                 'mg/Stk': 'Fuel quantity', 'Nm': 'Torque', 'C': 'Temperature',
                 'TEMP_C': 'Temperature', 'hPa': 'Pressure', 'Bar': 'Pressure',
                 'bar': 'Pressure', 'mA': 'Current', '%': 'Percentage', 'factor': 'Factor'}
        name = units.get(axis[1], axis[1].replace('_', ' ')) + ' axis — ' + axis[2] + ' points'
    elif title == 'Axis_TqGear':
        name = 'Gear torque axis'
    elif title.startswith('Shared controller (ownership unresolved) - '):
        name = 'Shared controller — ' + title.split(' - ', 1)[1]
    else:
        name = re.sub(r'\s*\[(?:lookup|call|record).*?\]', '', title)
        name = re.sub(r'\s*\[(?:lookup|call|record).*$', '', name)
        name = re.sub(r'\b0x[0-9A-Fa-f]+\b', '', name)
        name = re.sub(r'\s*\([^)]*(?:unproven|code-proven|legacy|confirmed)[^)]*\)', '', name)
        name = re.sub(r'\s*[-—]\s*(?:exact function|function|no caller|no active caller|no proven caller|defined object).*$', '', name)
        name = re.sub(r'^\s*Legacy\s+', '', name, flags=re.I)
        name = name.replace('RAIL PRESSURE CTRL - ', 'Rail controller — ').replace('RAIL CTRL - ', 'Rail controller — ')
        name = name.replace('Rail-region scalar', 'Rail calibration value').replace('Alternate donor-scaled view', 'Alternate scaling view')
        name = name.replace('Unknown 12x16 calibration', 'Unidentified calibration (12 × 16)').replace('Unknown 16x16 calibration', 'Unidentified calibration (16 × 16)')
        name = name.replace('Unknown 8x8 calibration', 'Unidentified calibration (8 × 8)').replace('unknown12', 'Unidentified calibration')
        name = name.replace('IQ LIMIT', 'Fuel quantity limit').replace('Axis_', 'Axis ')
        name = re.sub(r'\s+', ' ', name).strip(' -—:')
        name = re.sub(r'\s+(?:at|of)$', '', name)
        if name.startswith('Duplicate view'):
            name = 'Alternate calibration view'
        elif re.fullmatch(r'Axis [0-9A-F]+', name):
            name = 'Unidentified axis'
        elif name == 'Scalar':
            name = 'Unidentified calibration value'
        name = name[:1].upper() + name[1:]
    replacements = {'final output upper clamp': 'maximum output', 'final output lower clamp': 'minimum output',
                    'final actuator output upper clamp': 'maximum actuator output',
                    'final actuator output lower clamp': 'minimum actuator output',
                    'accumulated correction factor upper bound': 'maximum accumulated correction',
                    'accumulated correction factor lower bound': 'minimum accumulated correction',
                    'active input transform coefficient': 'input conversion coefficient',
                    'closed-loop state initialization factor curve': 'initial correction factor',
                    'integrator update gain, small-error region': 'integral correction gain — small error',
                    'integrator update gain, positive large-error region': 'integral correction gain — large positive error',
                    'integrator update gain, negative large-error region': 'integral correction gain — large negative error'}
    for old, new in replacements.items():
        name = name.replace(old, new)
    if unverified:
        name += ' (unverified)'
    if 'NO-START RISK' in original:
        name += ' — no-start risk'
    return name or 'Unidentified calibration'


PARAMETER_NAMES = {
    'IDKLI': 'Air conditioning compressor output', 'ISKLV': 'Air conditioning installed',
    'IKLIS': 'Air conditioning compressor status', 'OPSCD_stpOEL': 'Oil pressure sensor signal',
    'INLKG': 'Normalized air mass', 'ILMKG': 'Air mass flow', 'RLMKG': 'Air mass sensor raw reading',
    'ILMMG': 'Air mass per cylinder stroke', 'RLMPE': 'Air mass sensor raw period',
    'ITZUH': 'Auxiliary heater duty cycle', 'IFPWG': 'Accelerator pedal (filtered)',
    'IPPW1': 'Accelerator pedal sensor 1 position', 'IPPW2': 'Accelerator pedal sensor 2 position',
    'IKICK': 'Kickdown switch', 'IUPW1': 'Accelerator pedal sensor 1 voltage',
    'IUPW2': 'Accelerator pedal sensor 2 voltage', 'IPUMG': 'Atmospheric pressure',
    'IUUMG': 'Atmospheric pressure sensor voltage', 'IMARD': 'Anti-jerk torque request',
    'IFWUR': 'Driver torque request (raw)', 'AddPCD_stCo_mp': 'Fuel additive controller state',
    'AddPCD_volAddTot': 'Total injected fuel additive volume',
    'AddPCD_volAddTotFine_mp': 'Total injected fuel additive volume (fine)',
    'AddPmp_volAddTotDem': 'Requested fuel additive quantity since service',
    'SLMMG': 'Air mass target', 'ILMRE': 'Air mass control error',
    'IGENL': 'Alternator load', 'IALDS': 'Turbo actuator duty cycle (N75)',
    'BPSCD_pLin': 'Boost pressure (linearized average)', 'IPLAD': 'Boost pressure',
    'IULDF': 'Boost pressure sensor voltage', 'IUBAT': 'Battery voltage',
    'ISBLS': 'Brake light switch (raw)', 'ISBLT': 'Redundant brake switch (raw)',
    'BrkCD_tiDebTmrPlaus_mp': 'Brake signal plausibility debounce timer',
    'ITKUM': 'Coolant temperature', 'CTSCD_tClntLin': 'Coolant temperature (linearized)',
    'RUTKU': 'Coolant temperature sensor raw voltage', 'ITKAS': 'Coolant temperature rise since start',
    'ITKET': 'Coolant temperature at end of test', 'IZTKT': 'Coolant plausibility test elapsed time',
    'IAMMM': 'Actual torque / maximum torque ratio', 'ISABU': 'Active engine shutdown reason',
    'SMIBA': 'Limited engine torque target before anti-jerk control', 'SMOMO': 'Engine torque target',
    'IMBEG': 'Engine torque limit', 'IVABS': 'Total fuel consumption', 'IMWVO': 'Drive torque request',
    'RSKUP': 'Clutch switch (raw)', 'IGAFG': 'Cruise control gear', 'ISFGL': 'Cruise control indicator',
    'IMANF': 'Cruise control torque request', 'SGEFG': 'Cruise control speed target',
    'DSMDur_ctDfctDur1': 'Fault duration counter 1', 'DSMDur_ctDfctDur1_OBD': 'OBD fault duration counter 1',
    'DSMDur_ctDfctDur2': 'Fault duration counter 2', 'DSMDur_stGlobalDefCnt_mp': 'Global fault counter state',
    'IAOBF': 'Stored OBD fault count', 'DSM_ctRdyActCyc_mp': 'Readiness status for current drive cycle',
    'DSM_ctRdyTstCompr_mp': 'Components readiness tests completed', 'DSM_ctRdyTstEGR_mp': 'EGR readiness tests completed',
    'DSM_ctRdyTstFuel_mp': 'Fuel system readiness tests completed', 'DSM_ctRdyTstMisf_mp': 'Misfire readiness tests completed',
    'IFEHAN': 'Fault paths tested', 'DSM_stCycles': 'Diagnostic drive / warm-up cycle state',
    'ISMIL': 'Check engine light request', 'ISRAB': 'OBD readiness bytes A and B',
    'ISRCD': 'OBD readiness bytes C and D', 'ISSLA': 'System warning lamp request',
    'DSM_tiDCDelay_mp': 'Drive cycle delay timer', 'IAAGR': 'EGR actuator duty cycle',
    'Ad_NumTyp_FreeApplic3_CSTR': 'Application parameter structure 3',
    'ISSYN': 'Crankshaft / camshaft synchronization status', 'IMORE': 'Engine friction torque',
    'INMOT': 'Engine speed', 'INMOTCARB': 'Average engine speed (OBD)', 'Ertm_tiload_u32': 'ECU processor load',
    'IMOAI': 'Actual calculated internal engine torque', 'ITKRS': 'Fuel temperature',
    'ISKRS': 'Fuel temperature sensor raw voltage', 'FanCD_dcycRaw_mp': 'Cooling fan duty cycle (voltage corrected)',
    'FlSys_volDetRefuel': 'Fuel level before refuelling detection', 'ITTAN': 'Intake air temperature signal duty cycle',
    'FlSys_volTotalRef_mp': 'Reference fuel level before refuelling', 'ISKME': 'Vehicle odometer',
    'INELU': 'Electric cooling fan speed', 'IAHEI': 'Heater power request over CAN',
    'IBKLI': 'Air conditioning ready', 'ISKLI': 'Air conditioning switch', 'ITUMG': 'Ambient temperature',
    'Ad_NumTyp_FreeApplic2_CSTR': 'ECU torque gear selector (CAN-derived)', 'IGETT': 'Transmission type',
    'GlwCD_RawVal_mp': 'Glow controller request register (raw)', 'IAGLU': 'Glow plug request',
    'IAGLL': 'Glow plug indicator state', 'HWEMon_numRecovery': 'Last ECU reset / recovery source',
    'HWEMon_recLocHigh_mp': 'Recovery address high word', 'HWEMon_recLocLow0_mp': 'Recovery address low word 0',
    'HWEMon_recLocLow1_mp': 'Recovery address low word 1', 'ITANS': 'Intake air temperature',
    'ITLAL': 'Charge air temperature after intercooler', 'AFSCD_tAir_mp': 'Air sensor temperature (linearized)',
    'ITLLL': 'Charge air temperature (linearized)', 'RSLAL': 'Charge air temperature sensor raw voltage',
    'SPEKA': 'Intake swirl flap position target', 'IWHE1': 'Main injection 1 reference start angle',
    'IWNE1': 'Post injection 1 reference start angle', 'InjCrv_phiPoI2Des': 'Post injection 2 base angle',
    'IMHE1': 'Main injection 1 fuel quantity target', 'ISVE1': 'Pilot injection 1 enable status',
    'ISVE2': 'Pilot injection 2 enable status', 'IDGH1': 'Main injection 1 estimated duration',
    'IMEIA': 'Injection quantity', 'SMEIN': 'Injection quantity target',
    'SMEIO': 'Injection quantity target before balancing', 'IDHE1': 'Main injection 1 duration',
    'IDNE1': 'Post injection 1 duration', 'IDNE2': 'Post injection 2 duration',
    'LIGov_nSetpoint': 'Idle governor lower speed boundary', 'ISLLR': 'Idle speed controller status',
    'IMLLR': 'Idle speed controller torque', 'IAMRV': 'Fuel metering valve duty cycle',
    'IIMRV': 'Fuel metering valve actual current', 'SIMRV': 'Fuel metering valve target current',
    'ISOED': 'Oil pressure switch', 'ITAVO': 'Exhaust temperature before oxidation catalyst',
    'RUAVO': 'Exhaust temperature sensor before catalyst (raw)', 'SPLAD': 'Boost pressure target',
    'ISLDR': 'Boost controller shutdown status', 'IARDS': 'Rail pressure valve duty cycle',
    'PCVCD_iActVal': 'Rail pressure valve actual current', 'ITAVP': 'Exhaust temperature before DPF',
    'RUTVP': 'Exhaust temperature sensor before DPF raw voltage', 'IVABP': 'Calculated DPF exhaust volume flow',
    'IKOAD': 'Additive ash correction factor', 'ISLRE': 'Odometer at last successful regeneration',
    'PFlt_mAsh_mp': 'DPF ash mass', 'IMRUP': 'DPF soot mass', 'IWSMP': 'DPF flow resistance as particulate mass',
    'IMPAK': 'DPF particulate mass (additive corrected)', 'IMPAS': 'DPF particulate mass (simulated)',
    'IREPM': 'Regeneration action for DPF / engine state', 'IREAN': 'Total regeneration request',
    'IPDIP': 'DPF differential pressure', 'IVTLR': 'Fuel consumed since last successful regeneration',
    'IWSTO': 'DPF flow resistance offset', 'RWSTR': 'DPF flow resistance (raw)', 'IWSTR': 'DPF flow resistance',
    'ISPFL1': 'DPF status — low word, low byte', 'ISPFL': 'DPF status — low byte',
    'ISPFH1': 'DPF status — high word, low byte', 'ISPFH': 'DPF status — high byte',
    'IBEMO': 'Engine operating point', 'ITVPG': 'Exhaust temperature before DPF (filtered)',
    'IVKMG': 'Vehicle speed (filtered)', 'PSPCD_dvolOut': 'Fuel pre-supply pump output',
    'IPRDR': 'Rail pressure', 'IURDR': 'Rail pressure sensor raw reading',
    'SVRDR': 'Rail pressure control volume flow target', 'SPRDR': 'Rail pressure target',
    'FlFCD_stFlFltRawVal': 'Fuel filter temperature / pressure monitor', 'StSys_trqStrt': 'Starting internal torque',
    'RWKUP': 'Wake-up signal (raw)', 'TVACD_rOut': 'Throttle valve actuator duty cycle',
    'VSACD_rOut': 'Intake swirl actuator duty cycle', 'IAFZG': 'Vehicle acceleration',
    'IVKMH': 'Vehicle speed', 'VSSCD_vSensVal': 'Vehicle speed sensor reading',
    'IVVZN': 'Vehicle speed / engine speed ratio', 'IZBST': 'Engine operating time',
}
for n in range(1, 4):
    PARAMETER_NAMES.update({f'IWVE{n}': f'Pilot injection {n} start angle component',
                            f'IMVE{n}': f'Pilot injection {n} fuel quantity target',
                            f'IDVE{n}': f'Pilot injection {n} duration'})
for n in (1, 2):
    PARAMETER_NAMES[f'IMGV{n}'] = f'Pilot injection {n} base fuel quantity'
for n in range(1, 7):
    PARAMETER_NAMES[f'INZZ{n}'] = f'Cylinder {n} engine speed'
    PARAMETER_NAMES[f'IMKZ{n}'] = f'Cylinder {n} fuel balancing correction'
for n in range(1, 6):
    PARAMETER_NAMES[f'PFlt_stEngPOp{n}_mp'] = f'DPF engine operating range {n} state'
for suffix, word in [('10', 'low word, low byte'), ('11', 'low word, high byte'),
                     ('20', 'high word, low byte'), ('21', 'high word, high byte')]:
    PARAMETER_NAMES[f'AirCtl_stAirCtlBits{suffix}_mp'] = f'Air controller status — {word}'
    PARAMETER_NAMES[f'CrCtl_stShutOffCode{suffix}_mp'] = f'Cruise control shutdown code — {word}'
    PARAMETER_NAMES[f'CrCtl_stShutOffFrz{suffix}_mp'] = f'Cruise control latched shutdown code — {word}'
