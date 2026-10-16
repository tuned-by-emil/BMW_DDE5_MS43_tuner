
import unittest as _optional_unittest
from pathlib import Path as _OptionalPath
if not (_OptionalPath(__file__).resolve().parents[1] / 'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf').is_file():
    raise _optional_unittest.SkipTest('Optional local calibration resource not installed: definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')

import unittest
from pathlib import Path
from bmw_bin_editor.core import XDFDatabase,equation_affine
from bmw_bin_editor.ms43_help import explain,setting_tooltip,help_text

ROOT=Path(__file__).resolve().parents[1]

class HelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db=XDFDatabase(ROOT/'definitions/ms43x/Siemens_MS43_MS43X001_512K.xdf')
        cls.by={d.title:d for d in cls.db.definitions}

    def test_switches_preserve_actual_modes_and_not_false_disable(self):
        text=help_text(self.by['c_conf_load'])
        self.assertIn('0 = MAP',text);self.assertIn('1 = MAF',text);self.assertIn('2 = Alpha-N',text)
        text=help_text(self.by['c_conf_aj_maf'])
        self.assertIn('Neither value',text);self.assertIn('0 = MAF',text)
        self.assertIn('2 = clutch-operated',help_text(self.by['c_conf_lc']))
        self.assertIn('substitute values fix the blend',help_text(self.by['c_conf_ff']))

    def test_raw_zero_note_matches_loaded_anti_jerk_equation(self):
        for name in ['ip_iga_aj_ron91__n__maf','ip_iga_aj_ron98__n__maf']:
            d=self.by[name];scale,offset=equation_affine(d.z_equation)
            self.assertEqual(offset,-48.)
            self.assertEqual(-offset/scale,128)
            self.assertIn('raw 128',help_text(d));self.assertIn('raw zero is -48',help_text(d))
        self.assertIn('not been independently verified',help_text(self.by['c_tco_min_aj']))

    def test_usage_and_units_are_not_confused_with_storage(self):
        d=self.by['ip_map_ve_1__map__n']
        text=setting_tooltip(d,'3° overlap point')
        self.assertIn('100% is VE = 1.0',text);self.assertIn('3° overlap point',text)
        self.assertIn('not a fixed idle/cruise',text);self.assertIn('ip_map_ve_1__map__n',text)
        self.assertIn('Total engine capacity in cc',help_text(self.by['c_eng_disp']))
        self.assertIn('regulator reference',help_text(self.by['ip_ti_fac_map__map']))

    def test_unknown_and_conflicting_sources_are_explicit(self):
        d=next(d for d in self.db.definitions if not d.description and d.title not in ['c_eng_disp'])
        # An undocumented reference axis must not gain an invented tuning recipe.
        self.assertIn('not described',help_text(d))
        self.assertIn('conflicting description',help_text(self.by['c_ral_iga_ofs_ron98']))
        self.assertIn('unresolved linked axis',help_text(self.by['ip_ectpwm_i__tco_dif']))

    def test_help_is_read_only_and_available_for_every_definition(self):
        import xml.etree.ElementTree as E
        before=E.tostring(self.db.root)
        for d in self.db.definitions:
            tooltip=setting_tooltip(d)
            self.assertIn(d.title,tooltip)
            self.assertIn('Source:',tooltip)
        self.assertEqual(E.tostring(self.db.root),before)

if __name__=='__main__':unittest.main()
