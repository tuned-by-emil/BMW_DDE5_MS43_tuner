import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import io
from dataclasses import replace
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bmw_bin_editor import chatgpt_client as client
from bmw_bin_editor.ai_context import build_snapshot, calibration_signature
from bmw_bin_editor.core import CalibrationDefinition, AxisDefinition, CalibrationDocument, Selection
from bmw_bin_editor.logdata import LiveTestOLog, SignalCatalog, TestOLog
from bmw_bin_editor.logdata import SIG_RPM, SIG_IQ, SIG_SPEED, FUEL_CONSUMPTION_SIGNAL


class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.protector = patch.object(client, "protect", side_effect=lambda data, decrypt=False: data[5:] if decrypt else b"test:" + data)
        self.protector.start()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = client.AccountStore(self.root)
        self.api = client.ChatGPTClient(self.store)
        self.definition = CalibrationDefinition("N75", "Verified scaling", "boost", 0, 2, 2,
                                               z_equation="X / 100", z_units="%",
                                               x_axis=AxisDefinition(None, 2, units="rpm", labels=["1000", "2000"]))
        bin_path = self.root / "test.bin"; bin_path.write_bytes(struct.pack("<4H", 1000, 2000, 3000, 4000))
        self.doc = CalibrationDocument(SimpleNamespace(default_lsb_first=True, path=self.root / "test.xdf", tables=[self.definition]), bin_path)
        self.log = LiveTestOLog(SignalCatalog())
        self.log.append_sample(0, {"actual": 100})
        self.log.append_sample(1, {"target": 120})
        self.log.append_sample(2, {"actual": 110, "target": 115})
        self.panel = SimpleNamespace(log=self.log, live_mode=True, cursor_index=2, range_seconds=None, profile=None)

    def tearDown(self):
        self.protector.stop()
        self.temp.cleanup()

    def profile(self, **updates):
        p = dict(client_id="issued-client", subject="person", issuer=client.AUTH, email="test@example.com",
                 access_token="secret-access", refresh_token="secret-refresh", id_token="secret-id",
                 scopes=list(client.PLAN_SCOPES), expires_at=time.time() + 3600)
        p.update(updates)
        self.store.data["profiles"]["account"] = p; self.store.data["active"] = "account"
        return p

    def test_dpapi_roundtrip_and_no_plaintext_tokens(self):
        self.protector.stop()
        try:
            client.protect(b"test")
        except client.ChatGPTError as exc:
            self.skipTest("Windows sandbox does not supply a DPAPI user key: " + str(exc))
        self.profile(); self.store.save()
        self.assertNotIn(b"secret-access", self.store.path.read_bytes())
        reloaded = client.AccountStore(self.root)
        self.assertEqual(reloaded.active()["access_token"], "secret-access")
        self.assertEqual(reloaded.data["host"], self.store.data["host"])

    def test_verified_identity_rejects_bad_nonce_audience_and_signature(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key())); public["kid"] = "test"
        claims = dict(iss=client.AUTH, sub="person", aud="issued", exp=time.time() + 100, nonce="nonce")
        token = jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})
        discovery = {"jwks_uri": client.AUTH + "/.well-known/jwks.json"}
        with patch.object(client, "_json", return_value={"keys": [public]}):
            self.assertEqual(client.validate_id_token(token, "issued", "nonce", discovery)["sub"], "person")
            for aud, nonce in [("wrong", "nonce"), ("issued", "wrong")]:
                with self.assertRaises(client.ChatGPTError):
                    client.validate_id_token(token, aud, nonce, discovery)
            bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            forged = jwt.encode(claims, bad_key, algorithm="RS256", headers={"kid": "test"})
            with self.assertRaises(client.ChatGPTError):
                client.validate_id_token(forged, "issued", "nonce", discovery)

    def test_snapshot_preserves_sparse_observations_and_units_without_edits(self):
        original = bytes(self.doc.data)
        s = build_snapshot(self.doc, self.definition, Selection(), self.panel, source="live")
        self.assertEqual(s["tables"][0]["values"], [[10, 20], [30, 40]])
        self.assertEqual(s["tables"][0]["x_values"], [1000, 2000])
        self.assertEqual(s["log"]["rows"], [[0, 100, None], [1, None, 120], [2, 110, 115]])
        self.assertEqual(s["log"]["channels"][0]["observations"], 2)
        self.assertEqual(bytes(self.doc.data), original)
        self.assertFalse(self.doc.undo_stack)
        json.dumps(s, allow_nan=False)

    def test_saved_range_and_cursor_window(self):
        csv = self.root / "saved.csv"
        csv.write_text("time,time_unit,STAT_A_WERT\n0,s,1\n1,s,2\n2,s,3\n")
        self.panel.log = TestOLog(csv); self.panel.live_mode = False
        self.panel.range_seconds = (1, 2)
        s = build_snapshot(None, None, None, self.panel, source="saved")
        self.assertEqual(s["log"]["total_rows"], 2)
        self.panel.range_seconds = None; self.panel.cursor_index = 1
        s = build_snapshot(None, None, None, self.panel, source="saved", seconds=1)
        self.assertEqual(s["log"]["end_s"], 1)
        with self.assertRaises(client.ChatGPTError):
            build_snapshot(None, None, None, self.panel, source="live")

    def test_signature_changes_on_bytes_and_definition_units(self):
        a = calibration_signature(self.doc, self.definition)
        self.definition.z_units = "raw"
        self.assertNotEqual(a, calibration_signature(self.doc, self.definition))
        self.definition.z_units = "%"; self.doc.data[0] ^= 1
        self.assertNotEqual(a, calibration_signature(self.doc, self.definition))

    def test_model_scope_and_refresh_rotation(self):
        p = self.profile(scopes=["openid"])
        with self.assertRaises(client.ChatGPTError):
            self.api.access_token()
        p["scopes"] = list(client.PLAN_SCOPES); p["expires_at"] = 0
        with patch.object(client, "_json", return_value={"access_token": "new", "refresh_token": "rotated", "expires_in": 3600}) as post:
            self.assertEqual(self.api.access_token(), "new")
            self.assertEqual(p["refresh_token"], "rotated")
            self.assertEqual(post.call_args.kwargs["body"]["client_id"], "issued-client")
            self.assertNotIn("scope", post.call_args.kwargs["body"])

    def test_stream_requires_completed_and_reports_late_usage_failure(self):
        self.profile()
        delta = {"type": "response.output_text.delta", "delta": "tip"}
        def stream(events):
            return io.BytesIO(b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events))
        seen = []
        with patch.object(client, "request", return_value=stream([delta, {"type": "response.completed"}])) as post:
            self.assertEqual(self.api.analyse("available-model", {}, threading.Event(), seen.append), "tip")
            body = post.call_args.kwargs["body"]
            self.assertFalse(body["store"]); self.assertTrue(body["stream"])
            self.assertEqual(post.call_args.args[0], client.API + "/responses")
            self.assertNotIn("temperature", body)
        for terminal in ([], [{"type": "response.failed", "response": {"error": {"code": "subscription_sharing_usage_limit_exceeded"}}}]):
            with patch.object(client, "request", return_value=stream([delta] + terminal)):
                with self.assertRaises(client.ChatGPTError):
                    self.api.analyse("model", {}, threading.Event(), seen.append)

    def test_sign_in_pkce_and_issued_client_callback(self):
        discovery = {"issuer": client.AUTH, "authorization_endpoint": client.AUTH + "/api/accounts/authorize",
                     "token_endpoint": client.AUTH + "/api/accounts/oauth/token", "jwks_uri": client.AUTH + "/.well-known/jwks.json"}
        captured = {}
        def browser(url):
            params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            captured.update(params)
            def callback():
                # Mismatched state must not consume the pending sign-in attempt.
                try:
                    urlopen(params["redirect_uri"] + "?code=wrong&state=wrong", timeout=2)
                except Exception:
                    pass
                urlopen(params["redirect_uri"] + "?" + urlencode(dict(state=params["state"], code="valid", client_id="issued-client")), timeout=2).close()
            t = threading.Thread(target=callback, daemon=True); t.start()
            return True
        def server_json(url, **kwargs):
            if "openid-configuration" in url:
                return discovery
            captured["exchange"] = kwargs["body"]
            return dict(access_token="secret-access", refresh_token="secret-refresh", id_token="signed-token", scope=client.SCOPES)
        def identity(token, cid, nonce, doc):
            self.assertEqual(cid, "issued-client"); self.assertEqual(nonce, captured["nonce"])
            return {"iss": client.AUTH, "sub": "person", "email": "test@example.com"}
        with patch.object(client.webbrowser, "open", side_effect=browser), patch.object(client, "_json", side_effect=server_json), patch.object(client, "validate_id_token", side_effect=identity):
            self.assertIn("enabled", self.api.sign_in(threading.Event()))
        self.assertEqual(captured["client_id"], "dynamic_agent_client")
        self.assertEqual(captured["exchange"]["client_id"], "issued-client")
        self.assertEqual(captured["code_challenge_method"], "S256")
        self.assertEqual(captured["exchange"]["redirect_uri"], captured["redirect_uri"])
        self.assertTrue(captured["redirect_uri"].startswith("http://127.0.0.1:"))
        self.assertNotIn("client_secret", captured["exchange"])

    def test_sign_out_preserves_mapping_and_clears_tokens_on_network_failure(self):
        self.profile()
        with patch.object(client, "_json", side_effect=client.ChatGPTError("offline")):
            message = self.api.sign_out()
        self.assertIn("not confirmed", message)
        self.assertEqual(self.store.active()["client_id"], "issued-client")
        self.assertNotIn("access_token", self.store.active())

    def test_other_process_cannot_race_this_account_store(self):
        script = "from bmw_bin_editor.chatgpt_client import AccountStore,ChatGPTError; import sys\ntry:\n AccountStore(sys.argv[1])\nexcept ChatGPTError:\n sys.exit(42)\n"
        child = subprocess.run([sys.executable, "-c", script, str(self.root)],
                               cwd=Path(__file__).resolve().parents[1], timeout=10, capture_output=True)
        self.assertEqual(child.returncode, 42)

    def test_long_live_window_is_bounded_and_keeps_statistics(self):
        log = LiveTestOLog()
        for i in range(500):
            log.append_sample(i / 10, {"actual": -500 if i == 253 else i})
        self.panel.log = log
        s = build_snapshot(None, None, None, self.panel, source="live")
        self.assertLessEqual(s["log"]["sent_rows"], 180)
        self.assertEqual(s["log"]["total_rows"], 500)
        self.assertEqual(s["log"]["channels"][0]["min"], -500)
        self.assertIn([25.3, -500], s["log"]["rows"])

    def test_all_tables_are_included_without_an_open_table_and_bad_entries_reported(self):
        other = replace(self.definition, title="Other table", z_address=2, rows=1, cols=2)
        invalid = replace(self.definition, title="Invalid table", z_address=99999)
        self.doc.xdf.tables.extend([other, invalid])
        s = build_snapshot(self.doc, None, None, self.panel, source="map")
        self.assertEqual([t["definition"]["title"] for t in s["tables"]], ["N75", "Other table", "Invalid table"])
        self.assertEqual(s["tables"][1]["values"], [[20, 30]])
        self.assertEqual(s["table_coverage"], {"defined": 3, "decoded": 2, "unavailable": 1})
        self.assertIn("unavailable", s["tables"][2])

    def test_edit_in_unopened_table_changes_signature_and_preserves_original_evidence(self):
        other = replace(self.definition, title="Other table", z_address=4, rows=1, cols=2)
        self.doc.xdf.tables.append(other)
        signature = calibration_signature(self.doc, self.definition)
        other.z_units = "changed-unit"
        self.assertNotEqual(signature, calibration_signature(self.doc, self.definition))
        self.doc.data[4:6] = struct.pack("<H", 3500)
        s = build_snapshot(self.doc, self.definition, Selection(), self.panel, source="map")
        self.assertEqual(s["tables"][1]["changes_from_original"], [[0, 0, 30, 35]])

    def test_batching_covers_every_table_and_synthesizes_short_findings(self):
        tables = [{"definition": {"title": f"Table {i}"}, "values": [[1]], "description": "x" * 140000} for i in range(6)]
        snapshot = {"tables": tables, "table_coverage": {"defined": 6, "decoded": 6, "unavailable": 0}}
        batches = client.analysis_batches(snapshot)
        self.assertEqual([t for b in batches for t in b["tables"]], tables)
        self.assertEqual(len(batches), 3)
        self.assertEqual(client.requests_required(snapshot), 4)
        with patch.object(self.api, "_analyse_request", return_value="- Check boost.") as call:
            self.api.analyse("model", snapshot, threading.Event(), lambda _: None)
            self.assertEqual(call.call_count, 4)
            self.assertEqual(len(call.call_args.args[1]["completed_batch_reviews"]), 3)

    def test_visible_reply_is_bounded_bullet_points(self):
        long = "\n".join("- " + "word " * 100 for _ in range(20))
        compact = client.compact_tips(long)
        self.assertEqual(len(compact.splitlines()), 3)
        self.assertTrue(all(line.startswith("- ") for line in compact.splitlines()))
        self.assertLessEqual(len(compact.split()), 3 * 62)

    def test_optional_fix_offer_is_preserved_without_an_essay(self):
        response = "- Boost low — check VNT.\n\nCan I show you how to fix this problem?"
        self.assertEqual(client.compact_tips(response), response)

    def test_failed_batch_does_not_return_a_completed_all_table_review(self):
        tables = [{"definition": {"title": str(i)}, "data": "x" * 140000} for i in range(4)]
        with patch.object(self.api, "_analyse_request", side_effect=["- Tip", client.ChatGPTError("unavailable")]) as call:
            with self.assertRaises(client.ChatGPTError):
                self.api.analyse("model", {"tables": tables}, threading.Event(), lambda _: None)
            self.assertEqual(call.call_count, 2)

    def test_primary_and_comparison_cells_axes_and_identity_are_supplied(self):
        self.definition.x_axis = AxisDefinition(4, 2, equation="X / 100", units="mg/stroke")
        reference = struct.pack("<4H", 1500, 2000, 7500, 9000)
        s = build_snapshot(self.doc, self.definition, Selection(), self.panel, source="map",
                           compare_bytes=reference, compare_name="r007.bin")
        self.assertEqual(s["bin"]["name"], "test.bin")
        self.assertEqual(s["comparison"]["name"], "r007.bin")
        self.assertGreater(s["comparison"]["changed_bytes"], 0)
        table = s["tables"][0]
        self.assertEqual(table["comparison_values"], [[15, 20], [75, 90]])
        self.assertEqual(table["primary_vs_comparison_cells"][0], [0, 0, 10, 15, 5])
        self.assertEqual(table["comparison_x_values"], [75, 90])
        self.assertEqual(table["changes_from_original"], [])
        self.assertNotEqual(calibration_signature(self.doc, self.definition), s["calibration_signature"])

    def test_viewer_calculated_consumption_and_log_samples_are_in_context(self):
        self.panel.log = LiveTestOLog()
        self.panel.log.append_sample(0, {SIG_RPM: 1800, SIG_IQ: 15, SIG_SPEED: 100})
        self.panel.cursor_index = 0
        s = build_snapshot(self.doc, None, None, self.panel, source="live")
        reading = s["log"]["viewer_calculated_consumption"][FUEL_CONSUMPTION_SIGNAL]
        self.assertAlmostEqual(reading["value"], self.panel.log.value(FUEL_CONSUMPTION_SIGNAL, 0))
        self.assertEqual(reading["units"], "L/100 km")
        self.assertIn(FUEL_CONSUMPTION_SIGNAL, s["log"]["columns"])
        self.assertEqual(s["log"]["total_rows"], 1)

    def test_whole_saved_log_ignores_selection_and_preserves_every_row(self):
        csv = self.root / "long.csv"
        csv.write_text("time,time_unit,STAT_A_WERT,STAT_B_WERT\n" +
                       "".join(f"{i},s,{i if i % 2 == 0 else ''},{i if i % 2 else ''}\n" for i in range(2101)))
        self.panel.log = TestOLog(csv); self.panel.live_mode = False
        self.panel.range_seconds = (10, 20); self.panel.cursor_index = 15
        s = build_snapshot(self.doc, None, None, self.panel, source="saved", whole_log=True)
        log = s["log"]
        self.assertEqual(log["scope"], "whole_saved_log")
        self.assertEqual(log["sent_rows"], 2101)
        self.assertEqual(log["total_rows"], 2101)
        self.assertEqual([r[0] for r in log["rows"]], list(range(2101)))
        self.assertEqual(log["rows"][0], [0, {"1": 0}])
        self.assertEqual(log["rows"][1], [1, {"2": 1}])

    def test_full_row_batches_are_lossless_and_synthesis_does_not_resend_whole_log(self):
        rows = [[i, {"1": i, "2": "x" * 500}] for i in range(1500)]
        snapshot = {"tables": [{"definition": {"title": "Map"}, "values": [[1]]}],
                    "log": {"complete_rows": True, "rows": rows, "total_rows": 1500, "sent_rows": 1500, "columns": ["time_s", "A", "B"]}}
        batches = client.analysis_batches(snapshot)
        self.assertGreater(len(batches), 1)
        self.assertEqual([r for b in batches for r in b["log"]["rows"]], rows)
        self.assertEqual([t for b in batches for t in b["tables"]], snapshot["tables"])
        with patch.object(self.api, "_analyse_request", return_value="- Check trend.") as call:
            self.api.analyse("model", snapshot, threading.Event(), lambda _: None)
            final = call.call_args.args[1]
            self.assertEqual(final["log"]["rows_reviewed_across_batches"], 1500)
            self.assertEqual(final["log"]["rows"], [])
            self.assertEqual(call.call_count, client.requests_required(snapshot))

    def test_whole_log_rejects_limited_live_buffer(self):
        self.panel.live_mode = False
        with self.assertRaisesRegex(client.ChatGPTError, "saved CSV"):
            build_snapshot(self.doc, None, None, self.panel, source="saved", whole_log=True)

    def test_1800_second_window_can_include_every_row(self):
        log = LiveTestOLog()
        for i in range(2001):
            log.append_sample(i, {"actual": i})
        self.panel.log = log
        s = build_snapshot(self.doc, None, None, self.panel, source="live", seconds=1800, all_rows=True)
        self.assertEqual(s["log"]["start_s"], 200)
        self.assertEqual(s["log"]["end_s"], 2000)
        self.assertEqual(s["log"]["sent_rows"], 1801)

    def test_followup_selects_relevant_table_and_replays_prior_turns_with_bounded_rows(self):
        tables = [{"definition": {"title": f"Unrelated {i}", "z_address": i}, "values": [[i]]} for i in range(20)]
        tables.append({"definition": {"title": "N75 VNT precontrol", "z_address": 99}, "values": [[75]]})
        snapshot = {"tables": tables, "log": {"rows": [[i, i] for i in range(1000)], "complete_rows": True}}
        history = [{"question": "Why?", "answer": "- Check N75."}]
        context = client.followup_context(snapshot, {"initial_findings": "- Check N75."}, history, "Explain N75 at 500 s.")
        self.assertTrue(context["follow_up"])
        self.assertGreaterEqual(len(context["tables"]), 1)
        self.assertEqual(context["tables"][0]["definition"]["title"], "N75 VNT precontrol")
        self.assertEqual(context["previous_turns"], history)
        self.assertTrue(all(485 <= row[0] <= 515 for row in context["log"]["rows"]))
        self.assertLessEqual(len(context["log"]["rows"]), 200)

    def test_followup_keeps_long_conversation_and_unsampled_rows(self):
        turns = [{"question": f"Question {i}", "answer": f"Answer {i}"} for i in range(30)]
        snapshot = {"tables": [], "log": {"rows": [[0, 1]], "columns": ["time_s", "A"]},
                    "_captured_log_rows": [[i, {"1": i}] for i in range(3000)]}
        context = client.followup_context(snapshot, {"initial_findings": "Initial"}, turns, "Explain the log.")
        self.assertEqual(len(context["previous_turns"]), 30)
        self.assertEqual(len(context["log"]["rows"]), 3000)
        self.assertNotIn("_captured_log_rows", context)
        turns.append({"question": "Next", "answer": "Next"})
        self.assertEqual(len(context["previous_turns"]), 30)
        self.assertNotIn("_captured_log_rows", client.analysis_batches(snapshot)[0])

    def test_local_power_uses_coherent_rows_and_declared_efficiency(self):
        from bmw_bin_editor.ai_log_analysis import calculate_log
        columns = ["time_s", "STAT_MOTORDREHZAHL_WERT", "STAT_EINSPRITZMENGE_AKTUELL_WERT", "STAT_LUFTMASSE_PRO_HUB_WERT"]
        log = {"columns": columns, "rows": [[1, {"1": 4412.946, "2": 83.029, "3": 1283.381}],
                                              [2, {"1": 6000}], [3, {"2": 100}]]}
        result = calculate_log(log)
        peak = result["peaks"]["power"]
        self.assertEqual(result["coherent_fuel_rpm_observations"], 1)
        self.assertEqual(peak["time_s"], 1)
        self.assertAlmostEqual(peak["fuel_kg_h"], 43.968, places=3)
        self.assertEqual(peak["crank_bhp_by_assumed_efficiency"]["40%"], 279.7)
        self.assertEqual(peak["calculated_lambda"], 1.066)

    def test_shift_candidates_use_known_setup_and_do_not_confirm_traction(self):
        from bmw_bin_editor.ai_log_analysis import calculate_log
        columns = ["time_s", "STAT_MOTORDREHZAHL_WERT", "STAT_EINSPRITZMENGE_AKTUELL_WERT", "STAT_GESCHWINDIGKEIT_WERT"]
        result = calculate_log({"columns": columns, "rows": [[1, 4000, 80, 80], [1.3, 3100, 75, 81]]}, "8HP45 and 3.07 rear end, 225/45 R17")
        self.assertEqual(result["assumptions"]["final_drive"], 3.07)
        self.assertGreater(result["assumptions"]["nominal_tyre_circumference_m"], 1.9)
        self.assertEqual(len(result["shift_candidates"]), 1)
        self.assertIn("no actual gear", result["shift_candidates"][0]["interpretation"])
        self.assertIn("independent road speed", result["assumptions"]["gear_caveat"])

    def test_smoothing_uses_irregular_axes_and_preserves_linear_surface(self):
        from bmw_bin_editor.ai_log_analysis import table_smoothness
        linear = {"values": [[0, 10, 30]], "x_values": [0, 1, 3]}
        evidence = table_smoothness(linear)["largest_local_curvature_cells"]
        self.assertEqual(evidence[0]["axis_residuals"][0]["residual"], 0)
        spike = table_smoothness({"values": [[0, 40, 30]], "x_values": [0, 1, 3]})
        self.assertEqual(spike["largest_local_curvature_cells"][0]["axis_residuals"][0]["linear_neighbor_value"], 10)
        self.assertIn("not faults", spike["interpretation"])

    def test_followup_is_one_direct_request_without_batching(self):
        context = {"follow_up": True, "analysis_question": "Why?"}
        with patch.object(self.api, "_analyse_request", return_value="- Because the values differ.") as send, patch.object(client, "analysis_batches", side_effect=AssertionError("No full review")):
            self.assertIn("differ", self.api.follow_up("model", context, threading.Event(), lambda _: None))
            self.assertEqual(send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
