"""Read-only calibration consistency review; does not predict engine output."""
from __future__ import annotations
import bisect
import hashlib
from .core import EditorError


def interpolate(doc, definition, x, y):
    axes = [doc.axis_values(definition.x_axis), doc.axis_values(definition.y_axis)]
    values = doc.map_values(definition)
    def bracket(axis, value):
        if not axis or any(b <= a for a, b in zip(axis, axis[1:])):
            raise EditorError('Audit needs strictly increasing physical axes')
        if len(axis) == 1: return 0, 0, 0.0
        high = min(max(bisect.bisect_right(axis, value), 1), len(axis)-1)
        low = high-1
        weight = min(1.0, max(0.0, (value-axis[low])/(axis[high]-axis[low])))
        return low, high, weight
    xl, xh, wx = bracket(axes[0], x)
    yl, yh, wy = bracket(axes[1], y)
    lo = values[yl][xl]*(1-wx)+values[yl][xh]*wx
    hi = values[yh][xl]*(1-wx)+values[yh][xh]*wx
    return lo*(1-wy)+hi*wy


def calibration_review(doc):
    if getattr(doc.xdf, 'profile_id', '') != '361830':
        return ('Calibration consistency review\n' + str(doc.path) + '\nCurrent SHA-256: ' +
                hashlib.sha256(doc.data).hexdigest() + '\nThis review uses code traces and addresses specific to '
                'the 361830 definition. No software-specific checks are applied to the loaded XDF.')
    by = {d.z_address:d for d in doc.xdf.tables if d.rows > 1}
    lines = ['Calibration consistency review', str(doc.path),
             'Current SHA-256: '+hashlib.sha256(doc.data).hexdigest(),
             'Read-only. Values are decoded with the loaded XDF. This is not a power or fuel-consumption measurement.',
             'Quantity units follow the existing 0.01 XDF scaling; legacy mg and mm3 labels are inconsistent.', '']
    families = [('Boost target', [0xF9394,0xF95D8,0xF981C]),
                ('N75 feed-forward', [0xF7F44,0xF8900,0xF8478,0xF86BC])]
    for title, addresses in families:
        lines.append(title)
        present = [by[a] for a in addresses if a in by]
        if len(present) != len(addresses):
            lines.append('Required definitions unavailable; family skipped.');continue
        first = present[0]
        for d in present:
            axis = doc.axis_values(d.x_axis)
            same = doc.map_raw(d) == doc.map_raw(first)
            lines.append(f'0x{d.z_address:X}: quantity axis ends at {axis[-1]:g}; Z identical to A: {same}; physical grid matches A: {doc.grids_match(first,d)}')
        for quantity in (65.0,70.0,80.0):
            vals = [f'0x{d.z_address:X}={interpolate(doc,d,quantity,2500):.2f}' for d in present]
            lines.append(f'2500 rpm / quantity {quantity:g}: '+', '.join(vals))
        lines.append('Different grids can create different requests despite identical Z bytes. Runtime selection and actuator effect still need logs.\n')
    torque = [by[a] for a in (0xE80EA,0xE832E,0xE8572) if a in by]
    if len(torque)==3:
        lines.append('Torque-to-quantity family comparison')
        for rpm in (1500,2000,2500,3000,4000):
            lines.append(f'{rpm} rpm / 300 Nm: '+', '.join(f'0x{d.z_address:X}={interpolate(doc,d,300,rpm):.2f}' for d in torque))
            lines.append(f'{rpm} rpm / 500 Nm: A={interpolate(doc,torque[0],500,rpm):.2f}')
        lines.append('These are table requests, not measured injected mass or brake torque. Differences can be intentional; verify active selection.\n')
    soi = [by[a] for a in (0xEA80C,0xEAA50,0xEAC94,0xEAED8,0xEB11C) if a in by]
    if len(soi)==5:
        lines.append('Main SOI bases at cruise-like coordinates')
        for rpm in (1500,2000,2500):
            vals=[interpolate(doc,d,20,rpm) for d in soi]
            lines.append(f'{rpm} rpm / quantity 20: '+', '.join(f'{v:.3f}' for v in vals)+f' deg; spread {max(vals)-min(vals):.3f} deg')
        lines.append('Base angles exclude corrections. Event selection must be identified before changing timing for economy.\n')
    if all(a in by for a in (0xE77D2,0xE7A16)):
        lines.append('Fuel/air family under the legacy lambda interpretation')
        for rpm in (1500,2000,2500,4000):
            lines.append(f'{rpm} rpm / air-axis 750: A={interpolate(doc,by[0xE77D2],750,rpm):.3f}, B={interpolate(doc,by[0xE7A16],750,rpm):.3f}')
        lines.append('If B is an active lambda floor, values below 1 permit a richer request. This is conditional: active selection, signal units and actual air/fuel must be verified. It does not prove observed smoke or economy loss.\n')
    lines.append('Atlas v0.5 trace corrections')
    lines.append('FA828 is the rail controller instance; F75D4 is the boost instance. Both call shared MPC helper 0x4F214. Do not transfer gain scalings between instances.')
    lines.append('FC75A is pressure-axis shaped in r005, not an IQ-to-pressure request. A zero endpoint does not establish zero rail pressure in DFCO.')
    lines.append('Final rail bounds depend on r13 displacement 0x9F73 bit 0. Clear: FD1A0 upper curve / FD2E4 lower scalar. Set: FD1DC upper map / FD2F2 lower map. Bit identity and active state remain unresolved.')
    lines.append('\nEGS D7 acceptance and torque ceilings (companion-FLS trace)')
    if len(doc.data)>0xE9254:
        lines.append(f'E9254 variant gate: {doc.data[0xE9254]} (exactly 1 passes); E9177 receive enable: {doc.data[0xE9177]} (nonzero enables). These checks describe calibration, not live reception or DFCO state.')
        addresses=[0xE9408,0xE945A,0xE94C0,0xE9526,0xE958C,0xE95F2,0xE9658,0xE96BE]
        for a in addresses:
            count=25 if a==0xE9408 else 15
            values=[int.from_bytes(doc.data[a+i*2:a+i*2+2],'big',signed=True)*0.1 for i in range(count)]
            lines.append(f'0x{a:X}: gear-state ceiling range {min(values):.1f} to {max(values):.1f} Nm (inherited scaling).')
    lines.append('D7 is stored negative internally, then subtracted in external-FLS torque arbitration. 8D36 is an upper clamp; one path also has the decreasing EGS request 8D32 as an upper clamp. The exact Schub/injection release remains unresolved. See EGS_D7_Gating_v0_5.md.')
    return '\n'.join(lines)
