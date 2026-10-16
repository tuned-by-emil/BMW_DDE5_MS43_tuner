import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from pathlib import Path
import sys
import tempfile
import struct
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from bmw_bin_editor import gui
from bmw_bin_editor.ai_panel import AIPanel
from bmw_bin_editor.chatgpt_client import AccountStore
from bmw_bin_editor.core import CalibrationDefinition, CalibrationDocument
from bmw_bin_editor.logdata import LiveTestOLog, TestOLog, SIG_RPM, SIG_IQ, SIG_SPEED, FUEL_CONSUMPTION_SIGNAL

APP = QApplication.instance() or QApplication([])


class GuiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings_patch = patch.object(gui, "QSettings", side_effect=lambda *_: QSettings(str(Path(self.temp.name) / "settings.ini"), QSettings.IniFormat))
        self.settings_patch.start()
        self.window = gui.MainWindow()
        binary = Path(self.temp.name) / "r005.bin"
        binary.write_bytes(struct.pack("<4H", 1000, 2000, 3000, 4000))
        table = CalibrationDefinition("Test table", "", "", 0, 2, 2, z_equation="X / 100")
        xdf = SimpleNamespace(default_lsb_first=True, path=Path(self.temp.name) / "test.xdf", tables=[table])
        self.window.document = CalibrationDocument(xdf, binary)
        self.panel = AIPanel(self.window, store=AccountStore(self.temp.name))
        self.panel.models.addItem("Available model", "test-model")
        log = LiveTestOLog()
        for i in range(20):
            log.append_sample(i, {SIG_RPM: 1800 + i, SIG_IQ: 15})
        self.window.editor.log_panel.log = log
        self.window.editor.log_panel.live_mode = True
        self.window.editor.log_panel.cursor_index = 19
        self.panel.show()
        APP.processEvents()

    def tearDown(self):
        self.panel.shutdown()
        if self.panel.worker:
            self.panel.worker.wait(5000)
            APP.processEvents()
        self.window.editor.log_panel.live_mode = False
        self.window.close(); self.panel.close()
        APP.processEvents()
        self.settings_patch.stop(); self.temp.cleanup()

    def wait_worker(self):
        deadline = time.monotonic() + 5
        while self.panel.worker and time.monotonic() < deadline:
            APP.processEvents(); time.sleep(.01)
        self.assertIsNone(self.panel.worker)

    def test_worker_streaming_keeps_ui_responsive_and_final_result_visible(self):
        ticks = []
        timer = QTimer(); timer.setInterval(10); timer.timeout.connect(lambda: ticks.append(1)); timer.start()
        def response(model, snapshot, cancel, delta):
            delta("Check "); time.sleep(.1); delta("boost.")
            return "Check boost."
        with patch.object(self.panel.client, "analyse", side_effect=response):
            self.panel.analyse("live")
            self.wait_worker()
        timer.stop()
        self.assertGreater(len(ticks), 2)
        self.assertIn("Check boost.", self.panel.output.toPlainText())
        self.assertIn("Completed", self.panel.status.text())
        self.assertEqual(len(self.panel.records), 1)
        self.assertEqual(self.panel.records[0]["snapshot"]["log"]["total_rows"], 20)

    def test_auto_cap_duplicates_hide_and_cancel(self):
        with patch.object(self.panel.client, "analyse", return_value="Tip") as analysis:
            self.panel.analyse("live", automatic=True); self.wait_worker()
            self.panel.analyse("live", automatic=True)
            self.assertEqual(analysis.call_count, 1)
            self.panel.cap.setValue(1)
            self.panel.auto.setChecked(True)
            self.panel.analyse("live", automatic=True)
            self.assertFalse(self.panel.auto.isChecked())
            self.assertIn("cap", self.panel.status.text())
        self.panel.auto.setChecked(True); self.panel.hide()
        self.assertFalse(self.panel.auto.isChecked())

    def test_manual_requests_bypass_automatic_cap(self):
        self.panel.cap.setValue(1)
        self.panel.request_times = [time.monotonic()]
        with patch.object(self.panel.client, "analyse", return_value="- Estimated result.") as send:
            self.panel.analyse("live"); self.wait_worker()
            self.assertEqual(send.call_count, 1)
        with patch.object(self.panel.client, "follow_up", return_value="- Next result.") as send:
            self.panel.ask_followup("Estimate power."); self.wait_worker()
            self.assertEqual(send.call_count, 1)
        self.assertEqual(len(self.panel.request_times), 1)

    def test_chat_restores_all_replies_without_network_or_loaded_files(self):
        with patch.object(self.panel.client, "analyse", return_value="- Initial result."):
            self.panel.analyse("live"); self.wait_worker()
        with patch.object(self.panel.client, "follow_up", return_value="- Calculated answer."):
            self.panel.ask_followup("8HP45, 3.07 rear end; estimate power."); self.wait_worker()
        self.assertTrue(self.panel.chat_path.exists())
        with patch.object(self.panel.client, "analyse") as network:
            restored = AIPanel(self.window, store=AccountStore(self.temp.name))
            self.assertEqual(len(restored.records[0]["conversation"]), 1)
            self.assertIn("Calculated answer", restored.output.toPlainText())
            self.assertFalse(network.called)
            restored.shutdown(); restored.close()

    def test_cancellation_does_not_save_partial_result(self):
        def response(model, snapshot, cancel, delta):
            delta("Partial")
            cancel.wait(2)
            return "Unfinished"
        with patch.object(self.panel.client, "analyse", side_effect=response):
            self.panel.analyse("live")
            APP.processEvents(); self.panel.cancel_request(); self.wait_worker()
        self.assertFalse(self.panel.records)
        self.assertIn("NOT COMPLETED", self.panel.output.toPlainText())

    def test_menu_exposes_panel_and_no_login_is_started_on_construction(self):
        actions = [a for menu in self.window.menuBar().actions() if menu.menu() for a in menu.menu().actions()]
        self.assertTrue(any("ChatGPT tuning tips" in a.text() for a in actions))
        self.assertFalse(self.panel.auto.isChecked())
        self.assertIsNone(self.panel.worker)

    def test_batch_cost_respects_hourly_cap_before_any_request(self):
        snapshot = {"tables": [{"definition": {"title": str(i)}, "data": "x" * 140000} for i in range(6)],
                    "table_coverage": {"defined": 6, "decoded": 6, "unavailable": 0}}
        self.panel.cap.setValue(2)
        with patch.object(self.panel, "snapshot", return_value=snapshot), patch.object(self.panel.client, "analyse") as send:
            self.panel.analyse("map", automatic=True)
            self.assertFalse(send.called)
            self.assertIn("4 requests", self.panel.status.text())

    def test_show_fix_reuses_the_captured_evidence(self):
        with patch.object(self.panel.client, "analyse", return_value="- Boost low — check VNT."):
            self.panel.analyse("live"); self.wait_worker()
        original = self.panel.records[0]["snapshot"]
        self.window.editor.log_panel.log.append_sample(200, {SIG_RPM: 4000})
        with patch.object(self.panel.client, "follow_up", return_value="- Inspect vacuum hose.") as send, patch.object(self.panel.client, "analyse") as redo:
            self.panel.show_fix(); self.wait_worker()
            sent = send.call_args.args[1]
            self.assertEqual(sent["log"]["rows"], original["_captured_log_rows"])
            self.assertTrue(sent["analysis_question"].startswith("Show me the fix"))
            self.assertFalse(redo.called)
            self.assertEqual(len(self.panel.records), 1)
            self.assertEqual(len(self.panel.records[0]["conversation"]), 1)

    def test_all_tables_action_includes_loaded_log_and_calculated_consumption(self):
        panel = self.window.editor.log_panel
        panel.log.append_sample(20, {SIG_RPM: 1800, SIG_IQ: 15, SIG_SPEED: 100})
        panel.live_mode = False; panel.cursor_index = 20
        with patch.object(self.panel.client, "analyse", return_value="- Review cruise.") as send:
            self.panel.analyse("map"); self.wait_worker()
            snapshot = send.call_args.args[1]
            self.assertEqual(snapshot["source"], "saved")
            self.assertGreater(snapshot["log"]["total_rows"], 0)
            self.assertGreater(snapshot["log"]["viewer_calculated_consumption"][FUEL_CONSUMPTION_SIGNAL]["value"], 0)

    def test_three_buttons_capture_only_the_selected_sources(self):
        self.panel.update_controls()
        for mode, button in self.panel.analysis_buttons.items():
            with self.subTest(mode=mode), patch.object(self.panel.client, "analyse", return_value="- Result.") as send:
                button.click(); self.wait_worker()
                snap=send.call_args.args[1]
                self.assertEqual(snap["analysis_mode"], mode)
                self.assertEqual(bool(snap["tables"]), mode != "log")
                self.assertEqual(bool(snap["bin"]), mode != "log")
                self.assertEqual(bool(snap["log"]), mode != "tables")
                self.assertTrue(button.isChecked())

    def test_modes_require_only_their_chosen_sources(self):
        self.window.editor.log_panel.log=None
        self.panel.set_analysis_mode("tables")
        self.assertIsNone(self.panel.snapshot("saved")["log"])
        self.panel.set_analysis_mode("both")
        with self.assertRaisesRegex(Exception,"Load a saved log"): self.panel.snapshot("map")
        self.window.editor.log_panel.log=LiveTestOLog()
        self.window.editor.log_panel.log.append_sample(0,{SIG_RPM:2000,SIG_IQ:20})
        self.window.document=None
        self.panel.set_analysis_mode("log")
        snap=self.panel.snapshot("live")
        self.assertEqual(snap["tables"],[])
        self.assertIsNone(snap["comparison"])
        self.assertFalse(self.panel.record_stale({"snapshot":snap}))
        self.panel.set_analysis_mode("both")
        with self.assertRaisesRegex(Exception,"Load the primary BIN"): self.panel.snapshot("live")

    def test_log_only_followup_does_not_reintroduce_tables(self):
        self.panel.set_analysis_mode("log")
        with patch.object(self.panel.client,"analyse",return_value="- Log result."):
            self.panel.analyse("live"); self.wait_worker()
        with patch.object(self.panel.client,"follow_up",return_value="- Follow-up.") as send:
            self.panel.ask_followup("Explain this peak"); self.wait_worker()
        context=send.call_args.args[1]
        self.assertEqual(context["analysis_mode"],"log")
        self.assertEqual(context["tables"],[])
        self.assertEqual(context["table_index"],[])
        self.assertFalse(context["confirmed_gear_bindings"])

    def test_loaded_comparison_is_passed_and_switching_it_marks_result_stale(self):
        self.window.editor.compare_bytes = struct.pack("<4H", 1100, 2000, 3000, 4000)
        self.window.editor.compare_name = "r007.bin"
        with patch.object(self.panel.client, "analyse", return_value="- Review changed cell.") as send:
            self.panel.analyse("live"); self.wait_worker()
            self.assertEqual(send.call_args.args[1]["comparison"]["name"], "r007.bin")
        record = self.panel.records[0]
        self.assertFalse(self.panel.record_stale(record))
        self.window.editor.compare_bytes = struct.pack("<4H", 1200, 2000, 3000, 4000)
        self.assertTrue(self.panel.record_stale(record))

    def test_scope_control_accepts_1800_seconds_and_whole_log(self):
        self.panel.seconds.setValue(1800)
        self.assertEqual(self.panel.seconds.value(), 1800)
        csv = Path(self.temp.name) / "whole.csv"
        csv.write_text("time,time_unit,STAT_A_WERT\n" + "".join(f"{i},s,{i}\n" for i in range(301)))
        viewer = self.window.editor.log_panel
        viewer.log = TestOLog(csv); viewer.live_mode = False; viewer.cursor_index = 10
        viewer.range_seconds = (5, 15)
        self.panel.log_scope.setCurrentIndex(self.panel.log_scope.findData("whole"))
        self.assertFalse(self.panel.seconds.isEnabled())
        snapshot = self.panel.snapshot("saved")
        self.assertEqual(snapshot["log"]["sent_rows"], 301)
        self.assertTrue(snapshot["log"]["complete_rows"])

    def initial_review(self):
        with patch.object(self.panel.client, "analyse", return_value="- Test table: verify cell 0,0."):
            self.panel.analyse("live"); self.wait_worker()
        return self.panel.records[0]

    def test_followups_are_one_request_and_keep_history_without_reanalysis(self):
        record = self.initial_review()
        before = len(self.panel.request_times)
        with patch.object(self.panel.client, "analyse") as redo, patch.object(self.panel, "snapshot", side_effect=AssertionError("Should not rebuild context")), patch.object(self.panel.client, "follow_up", return_value="- It differs from the comparison.") as follow:
            self.panel.followup_question.setText("Why is that a problem?")
            self.panel.ask_followup(); self.wait_worker()
            self.assertFalse(redo.called)
            self.assertEqual(len(self.panel.request_times), before)
            self.assertEqual(len(self.panel.records), 1)
            self.assertEqual(record["conversation"][0]["question"], "Why is that a problem?")
            self.assertEqual(self.panel.followup_question.text(), "")
            self.panel.followup_question.setText("Which cell should I inspect?")
            self.panel.ask_followup(); self.wait_worker()
            self.assertEqual(len(follow.call_args.args[1]["previous_turns"]), 1)
            self.assertEqual(self.panel.reply_picker.count(), 3)
            self.assertEqual(len(record["conversation"]), 2)
        self.panel.reply_picker.setCurrentIndex(0)
        self.assertIn("verify cell", self.panel.output.toPlainText())

    def test_followup_still_answers_the_captured_review_after_current_files_change(self):
        record = self.initial_review()
        self.window.document.data[0] ^= 1
        self.assertTrue(self.panel.record_stale(record))
        with patch.object(self.panel.client, "follow_up", return_value="- The earlier value was 10.") as follow, patch.object(self.panel.client, "analyse") as redo:
            self.panel.ask_followup("What was the value in that review?"); self.wait_worker()
            self.assertFalse(redo.called)
            self.assertEqual(follow.call_args.args[1]["tables"][0]["values"][0][0], 10)

    def test_cancelled_followup_preserves_question_and_does_not_append_reply(self):
        record = self.initial_review()
        def response(model, context, cancel, delta):
            cancel.wait(2); return "Unfinished"
        with patch.object(self.panel.client, "follow_up", side_effect=response):
            self.panel.followup_question.setText("Why that value?")
            self.panel.ask_followup(); self.panel.cancel_request(); self.wait_worker()
        self.assertFalse(record["conversation"])
        self.assertEqual(self.panel.followup_question.text(), "Why that value?")

    def test_saved_review_can_resume_without_loading_files_or_redoing_analysis(self):
        record = self.initial_review()
        file = Path(self.temp.name) / "review.json"
        file.write_text(json.dumps(record), encoding="utf-8")
        self.window.document = None; self.window.editor.log_panel.log = None
        self.panel.load_review(file)
        with patch.object(self.panel.client, "follow_up", return_value="- Use the captured cells.") as follow, patch.object(self.panel.client, "analyse") as redo:
            self.panel.ask_followup("Explain that fix."); self.wait_worker()
            self.assertTrue(follow.called)
            self.assertFalse(redo.called)
            self.assertEqual(self.panel.records[0]["conversation"][0]["question"], "Explain that fix.")


if __name__ == "__main__":
    unittest.main()
