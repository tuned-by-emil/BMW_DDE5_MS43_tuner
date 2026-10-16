"""Reviewable, atomic MS43X throttle comfort calibration preset."""
from dataclasses import dataclass
from hashlib import sha256
from .core import EditorError
from .ms43 import is_ms43x, FULL_SIZE, CAL_SIZE

SOURCE = 'https://www.ms4x.net/index.php?title=Siemens_MS43'
RULES = {
    'c_pvs_cor_max_rpl': 'Disable pedal-increase smoothing using the documented zero activation threshold.',
    'id_tps_req_ltc_gear_at__gear': 'Disable comfort throttle-closing correction in every automatic gearbox gear.',
    'id_tps_req_ltc_gear_mt__gear': 'Disable comfort throttle-closing correction in every manual gearbox gear.',
    'ip_pvs_dif_cr_neg__pvs_max__n': 'Remove pedal-release dashpot correction. Inference: the published algorithm multiplies both new and retained correction by this factor; zero cancels both. Requires MS43X vehicle validation.',
    'ip_iga_aj_ron91__n__maf': 'Neutralize RON 91 anti-jerk ignition correction at every breakpoint (engineering zero, not raw zero).',
    'ip_iga_aj_ron98__n__maf': 'Neutralize RON 98 anti-jerk ignition correction at every breakpoint (engineering zero, not raw zero).',
}
EXPECTED = {
    'c_pvs_cor_max_rpl': (0x707F6, 1, 1, 16, False, '0.0018310547*X', 'row_major'),
    'id_tps_req_ltc_gear_at__gear': (0x75F2F, 6, 1, 8, False, '1.0*X', 'row_major'),
    'id_tps_req_ltc_gear_mt__gear': (0x75F35, 6, 1, 8, False, '1.0*X', 'row_major'),
    'ip_pvs_dif_cr_neg__pvs_max__n': (0x77D27, 12, 8, 8, False, '0.0019531373*X', 'row_major'),
    'ip_iga_aj_ron91__n__maf': (0x76CDD, 8, 6, 8, False, '0.375*X-48.0', 'row_major'),
    'ip_iga_aj_ron98__n__maf': (0x76D0D, 8, 6, 8, False, '0.375*X-48.0', 'row_major'),
}
LABELS = {
    'c_pvs_cor_max_rpl': 'Pedal-increase comfort smoothing threshold',
    'id_tps_req_ltc_gear_at__gear': 'Throttle-closing comfort correction — automatic gears',
    'id_tps_req_ltc_gear_mt__gear': 'Throttle-closing comfort correction — manual gears',
    'ip_pvs_dif_cr_neg__pvs_max__n': 'Pedal-release dashpot smoothing factor',
    'ip_iga_aj_ron91__n__maf': 'Anti-jerk ignition correction — RON 91',
    'ip_iga_aj_ron98__n__maf': 'Anti-jerk ignition correction — RON 98',
}


@dataclass(frozen=True)
class Cell:
    symbol: str
    name: str
    row: int
    col: int
    address: int
    before: bytes
    after: bytes
    old_value: float
    new_value: float
    units: str
    reason: str


@dataclass(frozen=True)
class Preview:
    snapshot: str
    cells: tuple[Cell, ...]

    @property
    def changed(self):
        return tuple(c for c in self.cells if c.before != c.after)


def build_preview(document):
    if not is_ms43x(document.xdf) or len(document.data) not in (FULL_SIZE, CAL_SIZE):
        raise EditorError('This preset requires an MS43X001 definition and a 512 KB or 64 KB BIN.')
    cells = []
    occupied = set()
    for symbol, reason in RULES.items():
        matches = [d for d in document.xdf.definitions if d.title == symbol]
        if len(matches) != 1 or matches[0].discovery_unverified:
            raise EditorError(f'Preset requires one verified definition for {symbol}.')
        d = matches[0]
        expected = EXPECTED[symbol]
        offset = 0x70000 if len(document.data) == CAL_SIZE else 0
        actual = (d.z_address + offset, d.rows, d.cols, d.z_element_bits,
                  d.z_signed, d.z_equation, d.storage_order)
        if actual != expected or not document.xdf.default_lsb_first:
            raise EditorError(f'{symbol}: definition differs from the supported MS43X001 layout.')
        raw = document.physical_to_raw(0, d.z_equation, d.z_element_bits, d.z_signed)
        value = document.raw_to_physical(raw, d.z_equation)
        if abs(value) > 1e-9:
            raise EditorError(f'{symbol}: engineering zero cannot be represented exactly.')
        after = document._pack_one(raw, d.z_element_bits, d.z_signed)
        values = document.map_values(d)  # Also validates the complete read range.
        for row in range(d.rows):
            for col in range(d.cols):
                address = document.cell_address(d, row, col)
                positions = set(range(address, address + len(after)))
                if occupied & positions:
                    raise EditorError('Preset definitions overlap; no changes were applied.')
                occupied.update(positions)
                cells.append(Cell(symbol, LABELS[symbol], row, col, address,
                                  bytes(document.data[address:address + len(after)]), after,
                                  values[row][col], value, d.z_units, reason))
    return Preview(sha256(document.data).hexdigest(), tuple(cells))


def apply_preview(document, preview):
    # Rebuild against current definitions as well as bytes: never apply an old
    # review after a calibration or definition was changed.
    if build_preview(document) != preview:
        raise EditorError('The BIN or definitions changed after this preview. Open a fresh preview.')
    return document.apply_bytes('MS43X drift throttle response',
                                [(c.address, c.after) for c in preview.changed])
