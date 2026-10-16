import unittest
import numpy as np
from bmw_bin_editor.eoi import estimate_end
class EOITests(unittest.TestCase):
 def test_units_and_reference(self):
  for duration,unit in [(1000,'us'),(1,'ms'),(12,'deg')]:self.assertEqual(float(estimate_end(2000,10,duration,unit,'BTDC')),2)
  self.assertEqual(float(estimate_end(2000,-10,1000,'us','ATDC')),2)
 def test_invalid(self):
  self.assertTrue(np.isnan(estimate_end([0,2000,2000],[10,10,np.nan],[1,0,1],'ms','BTDC')).all())
 def test_requires_explicit_convention(self):
  with self.assertRaises(ValueError):estimate_end(2000,10,1000,'us',None)
if __name__=='__main__':unittest.main()
