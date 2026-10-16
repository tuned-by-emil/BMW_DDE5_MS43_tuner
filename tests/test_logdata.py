
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/EDC16C31_361830_v4c_A2L_EXACT_IMPORT_SAFE.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/EDC16C31_361830_v4c_A2L_EXACT_IMPORT_SAFE.xdf')

from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bmw_bin_editor.logdata import SignalCatalog, TestOLog, profile_for_definition, CALC_EST_LAMBDA, CALC_BOOST_ERROR
from bmw_bin_editor.core import XDFDatabase

CAT = ROOT / 'definitions' / 'testo_signals.tsv'
XDF = ROOT / 'definitions' / 'EDC16C31_361830_v4c_A2L_EXACT_IMPORT_SAFE.xdf'


def main():
    catalog = SignalCatalog.load_tsv(CAT)
    assert len(catalog.definitions) == 214, len(catalog.definitions)
    assert catalog.unit_for('STAT_FAHRERWUNSCH_PEDAL_WERT') == '%'
    assert 'Pedal' in catalog.label_for('STAT_FAHRERWUNSCH_PEDAL_WERT')

    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / 'testo.csv'
        p.write_text(
            'time;STAT_MOTORDREHZAHL_EINH;STAT_MOTORDREHZAHL_WERT;'
            'STAT_EINSPRITZMENGE_AKTUELL_EINH;STAT_EINSPRITZMENGE_AKTUELL_WERT;'
            'STAT_LUFTMASSE_PRO_HUB_EINH;STAT_LUFTMASSE_PRO_HUB_WERT;'
            'STAT_LADEDRUCK_SOLL_EINH;STAT_LADEDRUCK_SOLL_WERT;'
            'STAT_LADEDRUCK_EINH;STAT_LADEDRUCK_WERT\n'
            '100;rpm;2000;mg/cyc;25;mg/Hub;725;hPa;1200;hPa;1180\n'
            '100;rpm;2000;mg/cyc;25;mg/Hub;725;hPa;1200;hPa;1180\n'
            '210;rpm;2050;mg/cyc;27;mg/Hub;783;hPa;1250;hPa;1230\n',
            encoding='utf-8'
        )
        log = TestOLog(p, catalog)
        assert log.sample_count == 2
        assert log.duplicates_removed == 1
        assert abs(log.duration - 0.110) < 1e-9
        assert log.has('STAT_MOTORDREHZAHL_WERT')
        assert log.value('STAT_EINSPRITZMENGE_AKTUELL_WERT', 1) == 27
        assert log.has(CALC_EST_LAMBDA) and abs(log.value(CALC_EST_LAMBDA, 0) - 2.0) < 1e-9
        assert log.has(CALC_BOOST_ERROR) and log.value(CALC_BOOST_ERROR, 0) == 20

        xdf = XDFDatabase(XDF)
        boost = next(d for d in xdf.tables if d.z_address == 0xF9394)
        profile = profile_for_definition(boost, log)
        assert profile.x_signal == 'STAT_EINSPRITZMENGE_AKTUELL_WERT'
        assert profile.y_signal == 'STAT_MOTORDREHZAHL_WERT'
        assert profile.traces == ['STAT_LADEDRUCK_SOLL_WERT', 'STAT_LADEDRUCK_WERT']

        driver = next(d for d in xdf.tables if d.z_address == 0xE0672)
        driver_profile = profile_for_definition(driver, log)
        assert driver_profile.x_signal == 'STAT_FAHRERWUNSCH_PEDAL_WERT'
        assert not log.has(driver_profile.x_signal)

    print('PASS')
    print('TestO signal catalogue: PASS')
    print('Duplicate timestamp cleanup: PASS')
    print('Boost table bindings: PASS')
    print('Missing pedal detection: PASS')
    print('Calculated lambda/boost-error channels: PASS')


if __name__ == '__main__':
    main()
