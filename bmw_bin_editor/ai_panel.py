"""Opt-in, background ChatGPT tips for the map and saved/live log context."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import threading
import time

from PySide6.QtCore import QStandardPaths, QThread, QTimer, Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
                              QComboBox, QPlainTextEdit, QTextBrowser, QSpinBox, QCheckBox,
                              QFileDialog, QDialog, QTabWidget)
from PySide6.QtWidgets import QLineEdit, QScrollArea
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl

from .ai_context import build_snapshot, calibration_signature, digest
from .ai_patch import apply_proposal
from .ai_log_analysis import calculate_log, calculate_run
from .local_results import format_local_results
from .ai_evidence import validate_proposal
from .core import EditorError
from .chatgpt_client import AccountStore, ChatGPTClient, ChatGPTError, Cancelled, PLAN_SCOPES, requests_required, compact_tips, followup_context


class AIWorker(QThread):
    result = Signal(object)
    error = Signal(str)
    delta = Signal(str)

    def __init__(self, function, parent=None):
        super().__init__(parent)
        self.function = function
        self.cancel = threading.Event()

    def run(self):
        try:
            value = self.function(self.cancel, self.delta.emit)
            if not self.cancel.is_set():
                self.result.emit(value)
        except ChatGPTError as exc:
            self.error.emit(str(exc))
        except Exception:
            # No exception repr/traceback: authentication payloads may contain secrets.
            self.error.emit("ChatGPT operation could not complete. Retry, or reinstall the updated requirements.")


class AIPanel(QWidget):
    idle = Signal()

    def __init__(self, window, *, store=None):
        super().__init__(window)
        self.window = window
        self.settings = window.settings
        self.worker = None
        self.current_snapshot = None
        self.last_fingerprint = None
        self.request_times = []
        self.records = []
        self.stream_text = ""
        self.stream_dirty = False
        self.result_displayed = False
        self.client = None
        self._request_kind = "analysis"
        self._analysis_parent = None
        self.analysis_mode = str(self.settings.value("ai/analysis_mode", "both"))
        if self.analysis_mode not in {"tables", "log", "both"}: self.analysis_mode = "both"
        self.shutting_down = False
        try:
            directory = Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)) / "BMW_Bin_Editor" / "chatgpt"
            self.store = store if store is not None else AccountStore(directory)
            self.client = ChatGPTClient(self.store)
            storage_error = ""
        except ChatGPTError as exc:
            storage_error = str(exc)
            self.store = None
        layout = QVBoxLayout(self)
        title = QLabel("<b>ChatGPT tuning tips</b>")
        layout.addWidget(title)
        note = QLabel("Uses your authorised ChatGPT plan. Relevant captured tables/log evidence is sent to OpenAI; full audit sends all values. "
                      "Tips are advice; BIN edits stay under your control.")
        note.setWordWrap(True); layout.addWidget(note)
        outer_layout = layout
        self.tabs = QTabWidget()
        tips_page, setup_page = QWidget(), QWidget()
        self.tabs.addTab(tips_page, "Tips")
        self.tabs.addTab(setup_page, "Connection & setup")
        outer_layout.addWidget(self.tabs, 1)
        layout = QVBoxLayout(setup_page)
        self.accounts = QComboBox()
        self.accounts.currentIndexChanged.connect(self.select_account)
        layout.addWidget(self.accounts)
        row = QHBoxLayout()
        self.login = QPushButton("Continue with ChatGPT")
        self.login.clicked.connect(self.sign_in)
        self.logout = QPushButton("Sign out")
        self.logout.clicked.connect(self.sign_out)
        row.addWidget(self.login); row.addWidget(self.logout); layout.addLayout(row)
        usage = QPushButton("Manage ChatGPT usage")
        usage.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://chatgpt.com/#settings/Usage")))
        layout.addWidget(usage)
        row = QHBoxLayout()
        self.models = QComboBox(); self.models.setMinimumContentsLength(12)
        self.refresh = QPushButton("Refresh models"); self.refresh.clicked.connect(self.refresh_models)
        row.addWidget(self.models, 1); row.addWidget(self.refresh); layout.addLayout(row)
        effort_row = QHBoxLayout(); effort_row.addWidget(QLabel("Reasoning"))
        self.effort = QComboBox()
        for label,value in [("Low","low"),("Medium","medium"),("High","high"),("Extra high","xhigh"),("Maximum","max")]:
            self.effort.addItem(label,value)
        self.effort.setCurrentIndex(max(0,self.effort.findData(str(self.settings.value("ai/reasoning_effort","xhigh")))))
        self.effort.setToolTip("Extra high uses more reasoning. Manual evidence lookup is bounded to three model requests; automatic tips have their own cap.")
        self.effort.currentIndexChanged.connect(lambda: self.settings.setValue("ai/reasoning_effort",self.effort.currentData()))
        effort_row.addWidget(self.effort); layout.addLayout(effort_row)
        layout.addWidget(QLabel("Vehicle, hardware and tuning goal"))
        self.vehicle = QPlainTextEdit(str(self.settings.value("ai/vehicle_setup", "")))
        self.vehicle.setPlaceholderText("Engine/ECU, injectors/turbo, fuel, modifications, tuning goal…")
        self.vehicle.setMaximumHeight(85); layout.addWidget(self.vehicle)
        self.vehicle.textChanged.connect(lambda: self.settings.setValue("ai/vehicle_setup", self.vehicle.toPlainText()[:6000]))
        self.gear_can_confirmed = QCheckBox("MaxxECU actual-gear CAN: gears 1–3 confirmed")
        self.gear_can_confirmed.setChecked(self.settings.value("ai/actual_gear_can_confirmed",True,type=bool))
        self.gear_can_confirmed.setToolTip("User-confirmed MaxxECU actual-gear CAN, corroborated by the ECU selector and torque-ceiling code paths. Applies only to the 361830 selector tables.")
        self.gear_can_confirmed.toggled.connect(lambda value:self.settings.setValue("ai/actual_gear_can_confirmed",value))
        layout.addWidget(self.gear_can_confirmed)
        layout.addStretch(1)
        tips_layout = QVBoxLayout(tips_page)
        controls_page = QWidget()
        controls_scroll = QScrollArea()
        controls_scroll.setWidgetResizable(True)
        controls_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        controls_scroll.setMaximumHeight(320); controls_scroll.setMinimumHeight(140)
        controls_scroll.setStyleSheet("QScrollArea { border: 0; }")
        controls_scroll.setWidget(controls_page)
        tips_layout.addWidget(controls_scroll)
        layout = QVBoxLayout(controls_page)
        self.focus = QComboBox()
        self.focus.addItems(["General tuning review", "Map smoothness and transitions", "Boost and N75 tracking",
                             "Rail pressure and injection", "Cruise economy evidence", "Find anomalies and missing evidence"])
        layout.addWidget(self.focus)
        self.question = QPlainTextEdit()
        self.question.setPlaceholderText("Optional question about the current map/log…")
        self.question.setMaximumHeight(60); layout.addWidget(self.question)
        row = QHBoxLayout(); row.addWidget(QLabel("Log scope"))
        self.log_scope = QComboBox()
        self.log_scope.addItem("Window / selection (sampled)", "sampled")
        self.log_scope.addItem("Window / selection (every row)", "full_window")
        self.log_scope.addItem("Whole saved log (every row)", "whole")
        self.log_scope.setCurrentIndex(max(0, self.log_scope.findData(str(self.settings.value("ai/log_scope", "sampled")))))
        row.addWidget(self.log_scope, 1); layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Log window (s)")); self.seconds = QSpinBox(); self.seconds.setRange(1, 86400)
        self.seconds.setValue(self.settings.value("ai/log_seconds", 60, type=int))
        row.addWidget(self.seconds); layout.addLayout(row)
        self.log_scope.currentIndexChanged.connect(lambda: self.seconds.setEnabled(self.log_scope.currentData() != "whole"))
        self.seconds.setEnabled(self.log_scope.currentData() != "whole")
        self.log_scope.currentIndexChanged.connect(lambda: self.settings.setValue("ai/log_scope", self.log_scope.currentData()))
        self.seconds.valueChanged.connect(lambda value: self.settings.setValue("ai/log_seconds", value))
        hint = QLabel("Saved log: selected graph range, or the window around its cursor. Live log: latest window. "
                      "Choose tables, log, or both. Tables mode includes every decoded XDF table. Questions get calculated results; general reviews give short problem/fix bullets.")
        hint.setWordWrap(True); layout.addWidget(hint)
        row = QHBoxLayout()
        self.map_button = QPushButton("Tables only")
        self.log_button = QPushButton("Log only")
        self.analyse_button = QPushButton("Tables + log")
        self.analysis_buttons = {"tables": self.map_button, "log": self.log_button, "both": self.analyse_button}
        for mode, button in self.analysis_buttons.items():
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, mode=mode: self.analyse_mode(mode))
            row.addWidget(button)
        self.set_analysis_mode(self.analysis_mode)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.preview_button = QPushButton("Preview selected capture"); self.preview_button.clicked.connect(self.preview)
        row.addWidget(self.preview_button)
        layout.addLayout(row)
        self.local_results_button = QPushButton("Local results (no allowance)")
        self.local_results_button.clicked.connect(self.show_local_results)
        layout.addWidget(self.local_results_button)
        self.full_review = QCheckBox("Full audit: every table / selected log row (more allowance)")
        self.full_review.setChecked(self.settings.value("ai/full_review",False,type=bool))
        self.full_review.toggled.connect(lambda value:self.settings.setValue("ai/full_review",value))
        self.full_review.setToolTip("Off: answer your question with all captured data available through exact evidence tools. On: review every raw table and log batch before synthesis.")
        layout.addWidget(self.full_review)
        self.auto = QCheckBox("Automatic tips while working (uses plan allowance)")
        self.auto.toggled.connect(self.auto_changed); layout.addWidget(self.auto)
        row = QHBoxLayout()
        row.addWidget(QLabel("Every (s)")); self.interval = QSpinBox(); self.interval.setRange(30, 86400); self.interval.setValue(90)
        row.addWidget(self.interval); row.addWidget(QLabel("Auto max/hour"))
        self.cap = QSpinBox(); self.cap.setRange(1, 1000); self.cap.setValue(12); row.addWidget(self.cap); layout.addLayout(row)
        self.cancel_button = QPushButton("Cancel request"); self.cancel_button.clicked.connect(self.cancel_request)
        layout.addWidget(self.cancel_button)
        layout = tips_layout
        self.status = QLabel(storage_error or "Sign in to enable analysis. Automatic tips start disabled.")
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); outer_layout.addWidget(self.status)
        self.history = QComboBox(); self.history.currentIndexChanged.connect(self.show_record); layout.addWidget(self.history)
        self.reply_picker = QComboBox(); self.reply_picker.currentIndexChanged.connect(self.show_turn); layout.addWidget(self.reply_picker)
        self.reply_question = QLabel(""); self.reply_question.setTextFormat(Qt.PlainText)
        self.reply_question.setWordWrap(True); self.reply_question.hide(); layout.addWidget(self.reply_question)
        self.output = QTextBrowser(); self.output.setOpenExternalLinks(False); self.output.setMinimumHeight(120); layout.addWidget(self.output, 1)
        self.followup_question = QLineEdit()
        self.followup_question.setMaxLength(2000)
        self.followup_question.setPlaceholderText("Ask a follow-up about this review…")
        self.followup_question.setToolTip("Keeps this chat and captured data across restarts. New analysis captures new edits or another log.")
        self.followup_question.returnPressed.connect(self.ask_followup)
        layout.addWidget(self.followup_question)
        self.followup_send = QPushButton("Ask follow-up")
        self.followup_send.clicked.connect(self.ask_followup)
        self.fix_button = QPushButton("Show me the fix")
        self.fix_button.clicked.connect(self.show_fix)
        row = QHBoxLayout(); row.addWidget(self.followup_send); row.addWidget(self.fix_button); layout.addLayout(row)
        self.patch_picker = QComboBox(); self.patch_picker.setVisible(False)
        self.apply_patch_button = QPushButton("Review / apply cells…")
        self.apply_patch_button.clicked.connect(self.review_patch); self.apply_patch_button.setVisible(False)
        patch_row=QHBoxLayout();patch_row.addWidget(self.patch_picker,1);patch_row.addWidget(self.apply_patch_button);layout.addLayout(patch_row)
        export = QPushButton("Save review…"); export.clicked.connect(self.export)
        open_review = QPushButton("Open saved review…"); open_review.clicked.connect(self.open_review)
        new_chat=QPushButton("New chat");new_chat.clicked.connect(self.new_chat)
        row = QHBoxLayout(); row.addWidget(new_chat);row.addWidget(export); row.addWidget(open_review); layout.addLayout(row)
        self.poll = QTimer(self); self.poll.setInterval(2000); self.poll.timeout.connect(self.tick); self.poll.start()
        self.render_timer = QTimer(self); self.render_timer.setInterval(150); self.render_timer.timeout.connect(self.render_stream); self.render_timer.start()
        self.next_auto = 0
        self.reload_accounts()
        self.update_controls()
        self.chat_path = (self.store.path.parent if self.store else Path(QStandardPaths.writableLocation(QStandardPaths.GenericDataLocation)) / "BMW_Bin_Editor" / "chatgpt") / "last-review.json"
        if self.chat_path.exists():
            self.load_review(self.chat_path)
        self.tabs.setCurrentIndex(0 if self.store and self.store.active() else 1)
        if self.client and self.store.active() and self.store.active().get("access_token"):
            QTimer.singleShot(0, self.refresh_models)

    def log_source(self):
        return "live" if self.window.editor.log_panel.live_mode else "saved"

    def set_analysis_mode(self, mode):
        if mode not in {"tables", "log", "both"}: raise ValueError("Unknown analysis mode")
        self.analysis_mode = mode
        self.settings.setValue("ai/analysis_mode", mode)
        for key, button in self.analysis_buttons.items(): button.setChecked(key == mode)

    def analyse_mode(self, mode):
        if self.worker: return
        self.set_analysis_mode(mode)
        self.analyse(self.log_source())

    def snapshot(self, source, *, mode=None):
        mode = mode or self.analysis_mode
        include_tables, include_log = mode != "log", mode != "tables"
        editor = self.window.editor
        question = self.focus.currentText() + ". " + self.question.toPlainText()
        if include_tables and (self.window.document is None or not self.window.document.xdf.tables):
            raise ChatGPTError("Load the primary BIN and matching XDF tables, or choose Log only.")
        if include_log and (editor.log_panel.log is None or not editor.log_panel.log.sample_count):
            raise ChatGPTError("Load a saved log or start live logging, or choose Tables only.")
        source = self.log_source() if include_log else "map"
        try:
            captured = build_snapshot(self.window.document if include_tables else None,
                                  editor.definition if include_tables else None,
                                  getattr(editor.engine, "selection", None) if include_tables else None, editor.log_panel,
                                  source=source, seconds=self.seconds.value(),
                                  vehicle=self.vehicle.toPlainText(), question=question,
                                  compare_bytes=editor.compare_bytes if include_tables else None,
                                  compare_name=editor.compare_name if include_tables else "",
                                  whole_log=self.log_scope.currentData() == "whole",
                                  all_rows=self.log_scope.currentData() in {"whole", "full_window"})
            captured["analysis_mode"] = mode
            captured["data_sources"] = {"tables": include_tables, "log": include_log,
                "scope_note": "Excluded sources were deliberately omitted by the user. Earlier chat findings are historical; only this capture is current evidence."}
            captured["_full_review"] = self.full_review.isChecked()
            captured["confirmed_gear_bindings"] = self.gear_bindings(captured)
            return captured
        except ChatGPTError:
            raise
        except Exception:
            raise ChatGPTError("Could not decode this map/log context. Check the XDF and selected log range.") from None

    def reload_accounts(self):
        self.accounts.blockSignals(True); self.accounts.clear()
        if self.store:
            for key, email, connected in self.store.profiles():
                self.accounts.addItem(f"{email} · {key[:6]}" + ("" if connected else " · signed out"), key)
            self.accounts.setCurrentIndex(self.accounts.findData(self.store.data["active"]))
        self.accounts.addItem("Add account / workspace…", None)
        if self.store and not self.store.active():
            self.accounts.setCurrentIndex(self.accounts.count() - 1)
        self.accounts.blockSignals(False)

    def select_account(self, index):
        if self.worker or not self.store:
            return
        self.auto.setChecked(False); self.models.clear()
        self.last_fingerprint = None
        key = self.accounts.currentData()
        if key:
            self.store.select(key)
            self.refresh_models()
        self.update_controls()

    def update_controls(self):
        busy = self.worker is not None
        available = self.client is not None
        for control in (self.accounts, self.login, self.logout, self.refresh):
            control.setEnabled(available and not busy)
        self.logout.setEnabled(available and not busy and bool(self.accounts.currentData()))
        self.refresh.setEnabled(available and not busy and bool(self.accounts.currentData()))
        for control in self.analysis_buttons.values():
            control.setEnabled(available and not busy and bool(self.models.currentData()))
        self.models.setEnabled(not busy)
        self.effort.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.local_results_button.setEnabled(not busy)
        self.fix_button.setEnabled(available and not busy and bool(self.records) and self.history.currentIndex()>=0)
        self.followup_send.setEnabled(available and not busy and bool(self.records) and self.history.currentIndex()>=0)
        self.followup_question.setEnabled(available and not busy and bool(self.records) and self.history.currentIndex()>=0)
        self.history.setEnabled(not busy)
        self.reply_picker.setEnabled(not busy and bool(self.records))
        self.apply_patch_button.setEnabled(not busy and bool(self.records))
        self.cap.setEnabled(self.auto.isChecked())

    def start(self, function, done, message):
        if self.worker or not self.client:
            return
        self.status.setText(message)
        worker = AIWorker(function, self)
        self.worker = worker
        worker.result.connect(done)
        worker.error.connect(self.failed)
        worker.delta.connect(self.on_delta)
        worker.finished.connect(self.finished)
        self.update_controls(); worker.start()

    def finished(self):
        old = self.worker
        self.worker = None
        if old:
            old.deleteLater()
        self.update_controls(); self.idle.emit()
        if getattr(self, "_load_models_after_login", False) and not self.shutting_down:
            self._load_models_after_login = False
            QTimer.singleShot(0, self.refresh_models)
        elif self.records and self.result_displayed and old and not old.cancel.is_set():
            self.show_record(self.history.currentIndex())

    def failed(self, message):
        self.auto.setChecked(False)
        self.result_displayed = False
        self.status.setText(message)
        if self.current_snapshot is not None:
            self.render_stream(force=True)
            self.output.append("\n[NOT COMPLETED] " + message)
            self.current_snapshot = None

    def sign_in(self):
        self.auto.setChecked(False)
        key = self.accounts.currentData()
        self.start(lambda cancel, delta: self.client.sign_in(cancel, key), self.signed_in,
                   "Complete sign-in in your browser. Waiting up to five minutes…")

    def signed_in(self, message):
        self.status.setText(message); self.reload_accounts()
        self.tabs.setCurrentIndex(0)
        self._load_models_after_login = True

    def sign_out(self):
        self.auto.setChecked(False)
        self.start(lambda cancel, delta: self.client.sign_out(), self.signed_out, "Signing out…")

    def signed_out(self, message):
        self.models.clear(); self.reload_accounts(); self.status.setText(message)

    def refresh_models(self):
        if not self.client or self.accounts.currentData() is None:
            return
        self.start(lambda cancel, delta: self.client.models(), self.models_loaded, "Loading models available to your account…")

    def models_loaded(self, models):
        selected = self.models.currentData() or str(self.settings.value("ai/model", ""))
        self.models.clear()
        for slug, label in models:
            self.models.addItem(label, slug)
        index = self.models.findData(selected)
        self.models.setCurrentIndex(max(0, index))
        self.status.setText("ChatGPT plan connected. Ready for map/log analysis." if models else "No eligible models returned for this account.")

    def analyse(self, source, automatic=False, snapshot_override=None):
        if self.worker:
            return
        if not self.models.currentData():
            self.failed("Sign in and refresh the available models before requesting analysis."); return
        now = time.monotonic()
        self.request_times = [t for t in self.request_times if now - t < 3600]
        if automatic and len(self.request_times) >= self.cap.value():
            self.failed("Your local hourly request cap was reached. Automatic tips stopped; increase the cap or wait."); return
        try:
            snapshot = snapshot_override if snapshot_override is not None else self.snapshot(source)
            source = snapshot.get("source", source)
            needed = requests_required(snapshot)
            fingerprint = digest({"snapshot": snapshot, "model": self.models.currentData(), "reasoning":self.effort.currentData()})
        except ChatGPTError as exc:
            if not automatic:
                self.failed(str(exc))
            else:
                self.status.setText("Automatic tips waiting: " + str(exc))
            return
        if automatic and fingerprint == self.last_fingerprint:
            self.status.setText("Automatic tips waiting for new log data or map changes."); return
        if automatic and source == "live" and snapshot.get("log") and snapshot["log"]["total_rows"] < 10:
            self.status.setText("Automatic tips waiting for at least 10 live samples."); return
        if automatic and len(self.request_times) + needed > self.cap.value():
            self.failed(f"All-table review needs {needed} requests, exceeding the remaining local hourly cap. Increase Max/hour or wait."); return
        if automatic:
            self.request_times.extend([now] * needed)
        self.last_fingerprint = fingerprint
        model = self.models.currentData(); self.settings.setValue("ai/model", model)
        self.client.reasoning_effort = self.effort.currentData()
        self._automatic_extra_remaining = max(0,self.cap.value()-len(self.request_times))
        self.client.extra_request_callback = self.consume_extra_auto if automatic else None
        index=self.history.currentIndex()
        self._analysis_parent = self.records[index] if 0<=index<len(self.records) else None
        request_snapshot=dict(snapshot)
        if self._analysis_parent:
            request_snapshot["_chat_review"]=dict(self._analysis_parent.get("review_context",{}),initial_findings=self._analysis_parent["tips"])
            request_snapshot["_chat_review"].setdefault("initial_question",self._analysis_parent["snapshot"].get("analysis_question",""))
            request_snapshot["_chat_turns"]=list(self._analysis_parent.get("conversation",[]))
            request_snapshot["_chat_previous_vehicle_setup"]=self._analysis_parent["snapshot"].get("vehicle_setup","")
            request_snapshot["editor_actions"]=list(self._analysis_parent.get("applied_proposals",[]))
        self.current_snapshot = snapshot
        self._request_kind = "analysis"
        self.result_displayed = False
        self.stream_text = ""; self.output.clear()
        log = snapshot.get("log")
        coverage = snapshot["table_coverage"]
        self._request_label = (("Log only · " if snapshot.get("analysis_mode") == "log" else f"{coverage['defined']} tables · ") +
                              (f"{source} · {log['start_s']:.1f}–{log['end_s']:.1f}s" if log else "all-table review"))
        if snapshot.get("comparison"):
            self._request_label += f" · {snapshot['bin']['name']} vs {snapshot['comparison']['name']}"
        if log:
            self._request_label += f" · {log.get('name') or 'log'} · {log['sent_rows']}/{log['total_rows']} rows"
        if coverage["unavailable"]:
            self._request_label += f" · {coverage['unavailable']} unavailable"
        self._request_model = model
        self.status.setText("Sending analysis…")
        self.start(lambda cancel, delta: self.client.analyse(model, request_snapshot, cancel, delta), self.analysed,
                   f"Analysing {self._request_label}" + (f" · auto {len(self.request_times)}/{self.cap.value()} requests this hour" if automatic else ""))

    def on_delta(self, text):
        if self.current_snapshot is not None:
            self.stream_text = text if text.startswith(("- Reviewing batch ","- Checking captured evidence")) else self.stream_text + text
            self.stream_dirty = True

    def show_fix(self):
        index = self.history.currentIndex()
        if not 0 <= index < len(self.records):
            return
        record = self.records[index]
        turn = self.reply_picker.currentIndex()
        answer = record["conversation"][turn - 1]["answer"] if turn > 0 else record["tips"]
        self.ask_followup("Show me the fix for the selected answer in short steps. Propose a patch only if the evidence supports it; otherwise state the concrete next step.")

    def ask_followup(self, question=None):
        if self.worker:
            return
        if not isinstance(question, str):
            question = self.followup_question.text().strip()
        index = self.history.currentIndex()
        if not question or not 0 <= index < len(self.records):
            self.status.setText("Complete/select a review, then type a follow-up question."); return
        model = self.models.currentData()
        if not model:
            self.status.setText("Sign in and choose a model before asking a follow-up."); return
        record = self.records[index]
        try:
            captured = dict(record["snapshot"], confirmed_gear_bindings=self.gear_bindings(record["snapshot"]))
            context = followup_context(captured, record.get("review_context", {"initial_findings": record["tips"]}),
                                       record.get("conversation", []), question, current_setup=self.vehicle.toPlainText())
            turn_index = self.reply_picker.currentIndex()
            selected = record["conversation"][turn_index - 1] if turn_index > 0 else {"question": record.get("review_context",{}).get("initial_question",record["snapshot"].get("analysis_question", "")), "answer": record["tips"]}
            context["selected_reply"] = {"question": selected["question"][:2000], "answer": selected["answer"]}
            context["editor_actions"] = record.get("applied_proposals",[])
        except ChatGPTError as exc:
            self.failed(str(exc)); return
        self.auto.setChecked(False)
        self.client.reasoning_effort = self.effort.currentData()
        self.client.extra_request_callback = None
        self._request_kind = "follow_up"
        self._followup_record = record
        self._followup_pending_question = question
        self._request_model = model
        self.current_snapshot = dict(record["snapshot"], analysis_question=question)
        self.stream_text = ""; self.stream_dirty = False; self.result_displayed = False
        self.output.clear()
        self.start(lambda cancel, delta: self.client.follow_up(model, context, cancel, delta), self.followed_up,
                   "Answering follow-up on the captured review · up to 3 model requests")

    def followed_up(self, answer):
        record = self._followup_record
        record.setdefault("conversation", []).append({"question": self._followup_pending_question,
                                                      "answer": answer, "model": self._request_model,
                                                      "created_at": datetime.now().isoformat(timespec="seconds"),
                                                      "proposals": list(self.client.last_proposals), "api_requests":self.client.request_count,
                                                      "reasoning_effort":self.client.reasoning_effort})
        self.current_snapshot = None; self.stream_dirty = False; self.result_displayed = True
        self.followup_question.clear()
        self.save_chat(record)
        self.show_record(self.history.currentIndex())

    def render_stream(self, force=False):
        if self.stream_dirty or force:
            limit = 4 if self.current_snapshot and self.current_snapshot.get("analysis_question", "").startswith("Show me the fix") else 8
            self.output.setPlainText(self.stream_text)
            self.stream_dirty = False

    def analysed(self, text):
        if self.current_snapshot is None:
            return
        captured=self.current_snapshot
        identity={"bin":(captured.get("bin") or {}).get("name"),"log":(captured.get("log") or {}).get("name"),
                  "start_s":(captured.get("log") or {}).get("start_s"),"end_s":(captured.get("log") or {}).get("end_s")}
        record=self._analysis_parent
        if record is not None:
            record.setdefault("conversation",[]).append({"question":"Updated capture: "+str(identity)+". "+captured.get("analysis_question",""),
                "answer":text,"model":self._request_model,"created_at":datetime.now().isoformat(timespec="seconds"),
                "proposals":list(self.client.last_proposals),"api_requests":self.client.request_count,"reasoning_effort":self.client.reasoning_effort,
                "capture_identity":identity})
            origin=record.get("review_context",{}).get("initial_question",record["snapshot"].get("analysis_question",""))
            record["snapshot"]=captured;record["label"]=self._request_label
            record["review_context"]={"initial_findings":record["tips"],"initial_question":origin,"latest_findings":text}
            record.setdefault("capture_history",[]).append(identity)
            self.records=[r for r in self.records if r is not record]
        else:
            record={"created_at":datetime.now().isoformat(timespec="seconds"),"model":self._request_model,"label":self._request_label,
                "snapshot":captured,"tips":text,"conversation":[],"proposals":list(self.client.last_proposals),
                "api_requests":self.client.request_count,"reasoning_effort":self.client.reasoning_effort,
                "initial_capture":identity,"capture_history":[identity],
                "review_context":dict(self.client.last_review_context) or {"initial_findings":text}}
            record["review_context"]["initial_question"]=captured.get("analysis_question","")
        self.records.insert(0,record);self.records=self.records[:12]
        self.current_snapshot = None; self.stream_dirty = False
        self.result_displayed = True
        self.save_chat(record)
        self.refresh_history()

    def save_chat(self, record):
        """Persist analysis and all turns locally; account secrets are never included."""
        try:
            self.chat_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.chat_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            temporary.replace(self.chat_path)
        except (OSError, ValueError):
            self.status.setText("Reply completed, but chat could not be saved locally. Use Save review.")

    def refresh_history(self):
        self.history.blockSignals(True); self.history.clear()
        for r in self.records:
            self.history.addItem(r["created_at"][11:] + " · " + r["label"])
        self.history.blockSignals(False); self.history.setCurrentIndex(0)
        self.show_record(0)

    def open_review(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open saved ChatGPT review", "", "JSON (*.json)")
        if path:
            self.load_review(path)

    def load_review(self, path):
        if self.worker:
            return
        try:
            file = Path(path)
            if file.stat().st_size > 70_000_000:
                raise ValueError("Oversized review")
            def invalid_constant(value):
                raise ValueError("Non-finite JSON")
            saved = json.loads(file.read_text(encoding="utf-8"), parse_constant=invalid_constant)
            snapshot = saved["snapshot"]
            if not isinstance(snapshot, dict) or not isinstance(saved["tips"], str):
                raise ValueError("Invalid review")
            allowed = {"calibration_signature", "vehicle_setup", "analysis_question", "source", "bin", "comparison", "map", "tables", "table_coverage", "log", "_captured_log_rows", "confirmed_gear_bindings", "_full_review", "analysis_mode", "data_sources"}
            snapshot = {k: v for k, v in snapshot.items() if k in allowed}
            if not isinstance(snapshot.get("calibration_signature"), dict):
                raise ValueError("Missing captured identity")
            tables = snapshot.get("tables", [])
            if not isinstance(tables, list) or any(not isinstance(t, dict) or not isinstance(t.get("definition"), dict)
                    or not isinstance(t["definition"].get("title"), str) for t in tables):
                raise ValueError("Invalid table evidence")
            conversation = saved.get("conversation", [])
            if not isinstance(conversation, list) or any(not isinstance(t, dict) or not isinstance(t.get("question"), str)
                    or not isinstance(t.get("answer"), str) for t in conversation):
                raise ValueError("Invalid conversation")
            memory = saved.get("review_context") or {"initial_findings": saved["tips"]}
            if not isinstance(memory, dict):
                raise ValueError("Invalid review context")
            # Validate the evidence used by follow-up without making a request.
            followup_context(snapshot, memory, conversation, "Continue this saved review.")
            record = {"created_at": str(saved.get("created_at") or datetime.now().isoformat(timespec="seconds")),
                      "label": str(saved.get("label") or file.stem), "model": str(saved.get("model", "")),
                      "snapshot": snapshot, "tips": saved["tips"], "conversation": conversation, "review_context": memory, "proposals":saved.get("proposals", []), "applied_proposals":saved.get("applied_proposals",[]), "initial_capture":saved.get("initial_capture"), "capture_history":saved.get("capture_history",[])}
        except (OSError, ValueError, KeyError, TypeError, AttributeError, ChatGPTError):
            self.status.setText("Could not open this review. Use a JSON exported by Save selected analysis."); return
        self.auto.setChecked(False)
        self.records.insert(0, record); self.records = self.records[:12]
        self.result_displayed = True
        self.save_chat(record)
        self.refresh_history(); self.update_controls()

    def record_stale(self, record):
        if record["snapshot"].get("analysis_mode") == "log": return False
        editor = self.window.editor
        return record["snapshot"]["calibration_signature"] != calibration_signature(self.window.document, editor.definition, editor.compare_bytes, editor.compare_name)

    def show_record(self, index):
        if (self.worker and self.current_snapshot is not None) or not 0 <= index < len(self.records):
            return
        record = self.records[index]
        self.result_displayed = True
        self.reply_picker.blockSignals(True); self.reply_picker.clear()
        self.reply_picker.addItem("Initial analysis")
        for i, turn in enumerate(record.get("conversation", [])):
            self.reply_picker.addItem(f"Follow-up {i + 1}: {turn['question'][:55]}")
        self.reply_picker.setCurrentIndex(self.reply_picker.count() - 1)
        self.reply_picker.blockSignals(False)
        self.show_turn(self.reply_picker.currentIndex())
        self.status.setText(("STALE: BIN, XDF or comparison changed since this analysis. " if self.record_stale(record)
                             else "Completed for the captured map/log snapshot. ") + record["label"])

    def show_turn(self, index):
        if (self.worker and self.current_snapshot is not None) or not 0 <= self.history.currentIndex() < len(self.records):
            return
        record = self.records[self.history.currentIndex()]
        if index <= 0:
            text, question = record["tips"], record.get("review_context",{}).get("initial_question",record["snapshot"].get("analysis_question", ""))
            if record.get("initial_capture"):
                self.reply_question.setText("Initial capture: "+str(record["initial_capture"]));self.reply_question.show()
            else:
                self.reply_question.hide()
        else:
            conversation = record.get("conversation", [])
            if index > len(conversation):
                return
            turn = conversation[index - 1]
            text, question = turn["answer"], turn["question"]
            self.reply_question.setText("Q: " + question[:120] + ("…" if len(question) > 120 else ""))
            self.reply_question.setToolTip(question); self.reply_question.show()
        limit = 4 if question.startswith("Show me the fix") else 8
        self.output.setMarkdown(text)
        proposals = record.get("proposals",[]) if index<=0 else conversation[index-1].get("proposals",[])
        self.patch_picker.clear()
        for proposal in proposals:
            if isinstance(proposal,dict): self.patch_picker.addItem(proposal.get("label","Cell proposal"),proposal)
        self.patch_picker.setVisible(bool(self.patch_picker.count()))
        self.apply_patch_button.setVisible(bool(self.patch_picker.count()))

    def new_chat(self):
        if self.worker:return
        self.auto.setChecked(False);self.history.setCurrentIndex(-1);self.reply_picker.clear();self.output.clear()
        self.question.clear();self.followup_question.clear();self.patch_picker.hide();self.apply_patch_button.hide()
        self.reply_question.hide();self.result_displayed=False
        self.status.setText("New chat. Analyse the loaded tables/log to begin; previous chats remain in History.")
        self.update_controls()

    def consume_extra_auto(self):
        if self._automatic_extra_remaining <= 0:
            raise ChatGPTError("Automatic evidence lookup reached the local hourly cap. No patch was applied.")
        self._automatic_extra_remaining -= 1
        self.request_times.append(time.monotonic())

    def gear_bindings(self,snapshot):
        if not self.gear_can_confirmed.isChecked(): return {}
        software=(snapshot.get("bin") or {}).get("software_numbers",[])
        if not any("361830" in str(number) for number in software): return {}
        addresses={1:0xE945A,2:0xE94C0,3:0xE9526}
        present={t["definition"].get("z_address"):t["definition"].get("title","") for t in snapshot.get("tables",[])}
        return {str(gear):{"table_address":address,"source":"User-confirmed MaxxECU actual-gear CAN, corroborated by the 361830 ECU selector and gear torque-ceiling lookup paths."}
                for gear,address in addresses.items() if address in present and "selector" in present[address].lower()}

    def review_patch(self):
        proposal=self.patch_picker.currentData()
        index=self.history.currentIndex()
        if self.worker or not isinstance(proposal,dict) or not 0<=index<len(self.records): return
        record=self.records[index]
        snapshot=dict(record["snapshot"],confirmed_gear_bindings=self.gear_bindings(record["snapshot"]))
        if self.record_stale(record):
            self.status.setText("Capture a fresh review of the current BIN/XDF before applying a proposal.");return
        try:
            _,proposal=apply_proposal(self.window.document,snapshot,proposal,compare_bytes=self.window.editor.compare_bytes,compare_name=self.window.editor.compare_name,dry_run=True)
        except (ValueError,TypeError,KeyError,EditorError) as exc:
            self.status.setText("No cells applied: "+str(exc));return
        dialog=QDialog(self); dialog.setWindowTitle("Review proposed BIN cells");dialog.resize(860,550)
        layout=QVBoxLayout(dialog)
        details=QPlainTextEdit();details.setReadOnly(True)
        lines=[proposal.get("label","Cell proposal"),proposal.get("expected_effect",""),"", "Changes are applied in memory as one undo step. Save BIN separately.",""]
        for cell in proposal.get("changes",[]):
            lines.append(f"{cell.get('table_title','Table')} · row {cell.get('row')} col {cell.get('col')} (x={cell.get('x')}, y={cell.get('y')}) · {cell.get('old')} → {cell.get('new')}")
            lines.append(str(cell.get("reason","")))
        details.setPlainText("\n".join(lines));layout.addWidget(details)
        row=QHBoxLayout();back=QPushButton("Back");back.clicked.connect(dialog.reject)
        apply=QPushButton("Apply these cells");apply.clicked.connect(dialog.accept)
        row.addWidget(back);row.addWidget(apply);layout.addLayout(row)
        if dialog.exec()!=QDialog.Accepted:return
        try:
            changed,plan=apply_proposal(self.window.document,snapshot,proposal,compare_bytes=self.window.editor.compare_bytes,compare_name=self.window.editor.compare_name)
            if changed:
                self.window.editor.refresh();self.window._document_changed()
                record.setdefault("applied_proposals",[]).append({"label":plan["label"],"changes":plan["changes"],"created_at":datetime.now().isoformat(timespec="seconds")})
                self.save_chat(record)
            self.status.setText("Proposed cells applied in memory. Undo is available; save BIN separately." if changed else "The proposal made no byte changes.")
        except (ValueError,KeyError,TypeError,EditorError) as exc:
            self.status.setText("No cells applied: "+str(exc))

    def auto_changed(self, enabled):
        self.cap.setEnabled(enabled)
        self.next_auto = time.monotonic() + self.interval.value()
        if enabled:
            if not self.models.currentData():
                self.auto.setChecked(False)
                self.status.setText("Sign in and select a model before enabling automatic tips.")
            else:
                self.status.setText("Automatic tips enabled. New context will be analysed at the chosen interval.")

    def tick(self):
        if self.worker or not self.isVisible():
            return
        if self.auto.isChecked() and time.monotonic() >= self.next_auto:
            self.next_auto = time.monotonic() + self.interval.value()
            panel = self.window.editor.log_panel
            source = self.log_source() if panel.log is not None else "map"
            self.analyse(source, automatic=True)
        elif self.records and self.result_displayed:
            record = self.records[max(0, self.history.currentIndex())]
            if self.record_stale(record):
                self.status.setText("STALE: BIN, XDF or comparison changed since this analysis. " + record["label"])

    def cancel_request(self):
        self.auto.setChecked(False)
        if self.worker:
            self.worker.cancel.set()
            self.result_displayed = False
            if self.current_snapshot is not None:
                self.render_stream(force=True)
                self.output.append("\n[NOT COMPLETED — cancelled]")
            self.current_snapshot = None
            self.stream_dirty = False
            self.status.setText("Cancellation requested. Waiting for the network worker to finish…")

    def shutdown(self):
        self.shutting_down = True
        self.poll.stop(); self.render_timer.stop(); self.auto.setChecked(False)
        if self.worker:
            self.cancel_request()
            return False
        if self.store:
            self.store.close()
        return True

    def hideEvent(self, event):
        self.auto.setChecked(False)
        super().hideEvent(event)

    def show_local_results(self):
        if self.worker:return
        try:
            snapshot=self.snapshot(self.log_source(), mode="both" if self.window.document and self.window.document.xdf.tables else "log")
        except ChatGPTError as exc:
            self.status.setText(str(exc));return
        log=snapshot["log"]
        raw_log=dict(log,rows=snapshot.get("_captured_log_rows",log.get("rows",[])))
        setup=snapshot.get("analysis_question","")+"\n"+self.vehicle.toPlainText()
        analysis=calculate_log(raw_log,setup=setup)
        dialog=QDialog(self);dialog.setWindowTitle("Local log calculations — no allowance used");dialog.resize(800,470)
        layout=QVBoxLayout(dialog)
        basis=QLabel(f"{log.get('name') or 'Captured log'} · {log['start_s']:.3f}–{log['end_s']:.3f} s · {analysis['computed_from_rows']} rows")
        basis.setWordWrap(True);layout.addWidget(basis)
        scope=QComboBox();scope.addItem("Entire captured scope",None)
        for event in analysis.get("launch_candidates",[]):
            i=event["launch_index"]
            scope.addItem(f"Launch {i+1}: {event['hold_start_s']:.3f}–{event['end_s']:.3f} s",i)
        layout.addWidget(scope)
        view=QTextBrowser();view.setOpenExternalLinks(False);layout.addWidget(view,1)
        def refresh():
            try:
                index=scope.currentData()
                selected=analysis if index is None else calculate_run(raw_log,setup=setup,launch_index=index)
                view.setMarkdown(format_local_results(selected,prefer_launch=False,confirmed_gear_bindings=snapshot.get("confirmed_gear_bindings",{})))
            except (ValueError,KeyError,TypeError):
                view.setPlainText("This launch could not be calculated from the captured rows.")
        scope.currentIndexChanged.connect(refresh)
        if scope.count()>1:scope.setCurrentIndex(1)
        else:refresh()
        close=QPushButton("Close");close.clicked.connect(dialog.accept);layout.addWidget(close)
        dialog.exec()

    def preview(self):
        try:
            source = self.log_source() if self.window.editor.log_panel.log is not None else "map"
            snapshot = self.snapshot(source)
        except ChatGPTError as exc:
            self.status.setText(str(exc)); return
        dialog = QDialog(self); dialog.setWindowTitle("Captured data available to ChatGPT"); dialog.resize(850, 600)
        layout = QVBoxLayout(dialog)
        view = QPlainTextEdit(); view.setReadOnly(True)
        view.setPlainText(json.dumps(snapshot, indent=2, ensure_ascii=False)); layout.addWidget(view)
        close = QPushButton("Close"); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec()

    def export(self):
        index = self.history.currentIndex()
        if not 0 <= index < len(self.records):
            self.status.setText("Complete an analysis before saving it."); return
        path, _ = QFileDialog.getSaveFileName(self, "Save analysis and evidence", "chatgpt-analysis.json", "JSON (*.json)")
        if path:
            record = dict(self.records[index], stale_at_export=self.record_stale(self.records[index]))
            try:
                Path(path).write_text(json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
                self.status.setText("Saved analysis and its map/log evidence.")
            except OSError:
                self.status.setText("Could not save the analysis. Check the destination folder.")
