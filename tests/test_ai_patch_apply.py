import copy,json,struct,tempfile,unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from bmw_bin_editor.ai_context import build_snapshot
from bmw_bin_editor.ai_evidence import EvidenceSession
from bmw_bin_editor.ai_patch import apply_proposal
from bmw_bin_editor.core import AxisDefinition,CalibrationDefinition,CalibrationDocument

class PatchApplyTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  a=CalibrationDefinition('N75 A','Code-confirmed base command','N75',0,1,3,z_equation='X/100',z_units='%',x_axis=AxisDefinition(None,3,labels=['10','15','20']))
  b=CalibrationDefinition('N75 B','Code-confirmed base command','N75',6,1,3,z_equation='X/100',z_units='%',x_axis=AxisDefinition(None,3,labels=['10','15','20']))
  path=self.root/'test.bin';path.write_bytes(struct.pack('<6H',6700,6770,6900,6700,6770,6900))
  self.doc=CalibrationDocument(SimpleNamespace(default_lsb_first=True,path=self.root/'test.xdf',tables=[a,b]),path)
  self.snapshot=build_snapshot(self.doc,a,None,SimpleNamespace(),source='map',question='Smooth selected cells')
  session=EvidenceSession(self.snapshot,question='Smooth selected cells')
  self.assertTrue(session.call('inspect_tables',{'query':'','table_ids':[],'offset':0,'limit':8,'include_values':True})['ok'])
  changes=[{'table_address':hex(d.z_address),'table_id':None,'row':0,'col':1,'old':67.70,'new':68.44,'reason':'Explicit local smoothing experiment','expected_effect':'Blend a requested numerical trough, not a proven performance improvement.'} for d in self.doc.xdf.tables]
  result=session.call('propose_patch',{'goal':'smoothing','label':'Two-map blend','changes':changes})
  self.assertTrue(result['ok'],result);self.proposal=result['proposal'];self.original=bytes(self.doc.data)
 def tearDown(self):self.temp.cleanup()
 def test_multi_map_application_is_one_atomic_undo_step(self):
  changed,plan=apply_proposal(self.doc,self.snapshot,self.proposal)
  self.assertTrue(changed);self.assertEqual(len(self.doc.undo_stack),1)
  for d in self.doc.xdf.tables:self.assertEqual(self.doc.cell_value(d,0,1),68.44)
  self.doc.undo();self.assertEqual(bytes(self.doc.data),self.original)
 def test_stale_bin_applies_no_cells(self):
  self.doc.data[0]^=1;before=bytes(self.doc.data)
  with self.assertRaisesRegex(ValueError,'changed'):apply_proposal(self.doc,self.snapshot,self.proposal)
  self.assertEqual(bytes(self.doc.data),before);self.assertFalse(self.doc.undo_stack)
 def test_invalid_second_cell_leaves_first_untouched(self):
  bad=copy.deepcopy(self.proposal);bad['changes'][1]['old']=1
  with self.assertRaisesRegex(ValueError,'Old value mismatch'):apply_proposal(self.doc,self.snapshot,bad)
  self.assertEqual(bytes(self.doc.data),self.original);self.assertFalse(self.doc.undo_stack)
 def test_imported_changed_definition_cannot_bypass_current_xdf(self):
  altered=copy.deepcopy(self.snapshot);altered['tables'][0]['definition']['description']='Different claim'
  with self.assertRaisesRegex(ValueError,'definition differs'):apply_proposal(self.doc,altered,self.proposal)
  self.assertEqual(bytes(self.doc.data),self.original)
 def test_replacement_outside_storage_is_not_silently_clamped(self):
  bad=copy.deepcopy(self.proposal);bad['changes'][0]['new']=1e10
  with self.assertRaises(ValueError):apply_proposal(self.doc,self.snapshot,bad)
  self.assertEqual(bytes(self.doc.data),self.original)

 def test_forged_captured_axes_cannot_mislabel_applied_cells(self):
  altered=copy.deepcopy(self.snapshot);altered['tables'][0]['x_values'][1]=999
  with self.assertRaisesRegex(ValueError,'axes differ'):apply_proposal(self.doc,altered,self.proposal,dry_run=True)
  self.assertEqual(bytes(self.doc.data),self.original);self.assertFalse(self.doc.undo_stack)
 def test_review_dry_run_verifies_without_editing_or_undo_entry(self):
  changed,plan=apply_proposal(self.doc,self.snapshot,self.proposal,dry_run=True)
  self.assertFalse(changed);self.assertEqual(plan['cell_count'],2)
  self.assertEqual(bytes(self.doc.data),self.original);self.assertFalse(self.doc.undo_stack)

if __name__=='__main__':unittest.main()
