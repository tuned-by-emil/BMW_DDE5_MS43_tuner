
from optional_resources import requires_resource
from pathlib import Path
import tempfile,sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor.core import *
class RegressionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();p=Path(self.tmp.name)/'test.bin';p.write_bytes(bytes(256))
  class X:default_lsb_first=False
  self.doc=CalibrationDocument(X(),p)
 def tearDown(self):self.tmp.cleanup()
 def test_outside_write_rejected_atomically(self):
  for address in [-1,256,300]:
   with self.assertRaises(EditorError):self.doc.apply_bytes('bad',[(0,b'X'),(address,b'AB')])
   self.assertEqual(self.doc.data,bytes(256));self.assertFalse(self.doc.undo_stack)
 def test_overlap_conflict_rejected(self):
  with self.assertRaises(EditorError):self.doc.apply_bytes('conflict',[(0,b'AB'),(1,b'C')])
  self.assertEqual(self.doc.data,bytes(256))
 def test_identical_overlap_roundtrip(self):
  self.doc.apply_bytes('edit',[(0,b'AB'),(1,b'BC')]);self.assertEqual(self.doc.data[:3],b'ABC')
  self.doc.undo();self.assertEqual(self.doc.data,bytes(256))
  self.doc.redo();self.assertEqual(self.doc.data[:3],b'ABC')
 def test_restore_saved_revision_stays_dirty(self):
  self.doc.apply_bytes('edit',[(0,b'A')]);self.doc.save_as(Path(self.tmp.name)/'saved.bin')
  self.doc.restore_original();self.assertTrue(self.doc.dirty);self.assertEqual(self.doc.data,bytes(256))
 def test_restore_unchanged_is_clean(self):
  self.doc.apply_bytes('edit',[(0,b'A')]);self.doc.restore_original();self.assertFalse(self.doc.dirty)
 def test_non_affine_three_point_alias_rejected(self):
  with self.assertRaises(EditorError):equation_affine('X + X*(X-1)*(X-2)')
  self.assertEqual(CalibrationDocument._scale_values([3],'X + X*(X-1)*(X-2)'),[9.0])
 def test_affine_equations(self):
  for eq,scale,offset in [('X/351843.72088832',1/351843.72088832,0),('(X-2731)*0.1',.1,-273.1),('2*(X+5)',2,10)]:
   a,b=equation_affine(eq);self.assertAlmostEqual(a,scale);self.assertAlmostEqual(b,offset)
 def test_different_physical_grid_skips_link(self):
  a=CalibrationDefinition('a','','',0,1,2,x_axis=AxisDefinition(64,2))
  b=CalibrationDefinition('b','','',8,1,2,x_axis=AxisDefinition(128,2))
  self.doc.apply_bytes('axes',[(64,b'\0\0\0\x0a'),(128,b'\0\0\0\x14')])
  changed,warnings=self.doc.set_cells_linked(a,{(0,1):100},[b],'absolute','link')
  self.assertTrue(changed);self.assertTrue(warnings);self.assertEqual(self.doc.map_raw(b),[[0,0]])
 @requires_resource('definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')
 def test_bundled_axes_and_rail_records(self):
  x=XDFDatabase(Path(__file__).resolve().parents[1]/'definitions/EDC16C31_361830_v1_22_R005_REVIEW.xdf')
  defs={d.z_address:d for d in x.tables}
  self.assertEqual(defs[0xF8900].x_axis.address,0xF88E0)
  self.assertEqual(defs[0xF86BC].x_axis.address,0xF869C)
  self.assertEqual((defs[0xFD2F2].rows,defs[0xFD2F2].cols),(2,2))
  self.assertEqual(defs[0xFD1A0].x_axis.address,0xFD190)
  self.assertEqual(defs[0xE9408].cols,25)
  self.assertEqual(defs[0xE93D6].cols,25)
  constants={d.z_address:d for d in x.constants}
  self.assertEqual(constants[0xE9254].z_element_bits,8)
  self.assertEqual(constants[0xE9177].z_element_bits,8)
if __name__=='__main__':unittest.main(verbosity=2)
