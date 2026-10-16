"""The distributable retains the exact economy selection without duplicates."""
import json
from unittest.mock import patch
from test_serial_logging_gui import SerialLoggingGuiTests, logging

ECONOMY = ['INMOT','IMEIA','IPLAD','SPLAD','IALDS','IPRDR','ILMMG','IWHE1',
           'SMOMO','IVKMH','ITKUM','IDHE1','ITANS','SPRDR']


class EconomyTests(SerialLoggingGuiTests):
    def test_economy_available_on_clean_settings_and_valid_for_serial(self):
        dialog=self.dialog()
        dialog.profile_box.setCurrentText('economy')
        dialog.transport.setCurrentIndex(2)
        config=dialog.config()
        self.assertEqual(config['profile']['name'],'economy')
        self.assertEqual(config['profile']['parameters'],ECONOMY)
        self.assertEqual(config['profile']['interval_ms'],0)
        self.assertEqual([len(g) for g in logging.make_groups(ECONOMY,dialog.catalog)],[10,6])

    def test_existing_saved_economy_wins_without_duplicate_and_delete_restores_default(self):
        custom=dict(version=1,ecu='D50M47A',name='economy',parameters=logging.FAST,interval_ms=200)
        self.settings.setValue('live126/profiles',json.dumps([custom]))
        dialog=self.dialog()
        dialog.profile_box.setCurrentText('economy')
        self.assertEqual(sum(dialog.profile_box.itemText(i)=='economy' for i in range(dialog.profile_box.count())),1)
        self.assertEqual(dialog.profile(),custom)
        with patch.object(logging.QMessageBox,'question',return_value=logging.QMessageBox.Yes):
            dialog.delete_profile()
        self.assertEqual(dialog.profile()['parameters'],ECONOMY)
        self.assertEqual(dialog.profile()['interval_ms'],0)
        self.assertEqual(dialog.profile_box.currentText(),'economy')


def load_tests(loader,tests,pattern):
    return loader.loadTestsFromNames([
        'test_economy_available_on_clean_settings_and_valid_for_serial',
        'test_existing_saved_economy_wins_without_duplicate_and_delete_restores_default'],EconomyTests)
