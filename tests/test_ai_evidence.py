"""Evidence retrieval and patch boundary regressions, without paid requests."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
try:
    from bmw_bin_editor.ai_evidence import EvidenceSession, validate_proposal, evidence_tools
except ImportError:
    # Allows running the staged module before the root copies it into the project.
    spec = importlib.util.spec_from_file_location("bmw_bin_editor.ai_evidence", Path(__file__).with_name("ai_evidence.py"))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    EvidenceSession,validate_proposal,evidence_tools = module.EvidenceSession,module.validate_proposal,module.evidence_tools


def table(address=0xF7F44,title="N75 A",description="Code-confirmed base command. Active state meanings remain unresolved.",rows=3,cols=3):
    return {"definition":{"title":title,"description":description,"category":"N75","z_address":address,"rows":rows,"cols":cols,
        "z_element_bits":16,"z_signed":True,"z_equation":"X/100","z_units":"%","storage_order":"row_major"},
        "values":[[67.0,67.7,69.0] for _ in range(rows)],"x_values":[60,70,90],"y_values":[1750,2000,2250],
        "comparison_values":[[67.0,66.7,69.0] for _ in range(rows)],"primary_vs_comparison_cells":[[1,1,67.7,66.7,-1.0]]}


def snapshot(tables=None):
    return {"calibration_signature":{"bin":"abc","definition":"def","endian":">"},"bin":{"byte_length":2_000_000},
        "tables":tables or [table()],"map":{"definition":{"title":"Different open table","z_address":0x1234}},
        "log":{"name":"launch.csv","columns":["time_s","RPM","IQ","speed"],"complete_rows":True,
            "channels":[{"name":"RPM","label":"Engine RPM","units":"rpm"},{"name":"IQ","label":"Fuel","units":"mg/cycle"}],
            "rows":[[i,{"1":1000+i,"2":40}] if i%2 else [i,{"3":i}] for i in range(13)]}}


def cell(address=0xF7F44,old=67.7,new=68.44,**updates):
    result = {"table_address":address,"table_id":None,"row":1,"col":1,"old":old,"new":new,
        "reason":"Localized axis-aware trough blend","expected_effect":"Numerical blend; spool improvement is unverified."}
    result.update(updates)
    return result


class EvidenceTests(unittest.TestCase):
    def inspected(self, snap=None, question="Smooth the selected N75 cells"):
        session = EvidenceSession(snap or snapshot(),question=question)
        self.assertTrue(session.call("inspect_tables",{"query":"","table_ids":[],"offset":0,"limit":8,"include_values":True})["ok"])
        return session

    def test_namespaced_schemas_are_strict_and_only_four_bounded_functions(self):
        namespace = evidence_tools()[0]
        self.assertEqual(namespace["type"],"namespace");self.assertEqual(namespace["name"],"editor")
        self.assertEqual({t["name"] for t in namespace["tools"]},{"inspect_tables","inspect_log","calculate_run","propose_patch"})
        for tool in namespace["tools"]:
            self.assertTrue(tool["strict"])
            p=tool["parameters"];self.assertFalse(p["additionalProperties"])
            self.assertEqual(set(p["required"]),set(p["properties"]))
        json.dumps(namespace,allow_nan=False)

    def test_all_tables_retrievable_not_only_open_map(self):
        tables=[table(0x1000+i*20,title=f"table {i}") for i in range(207)]
        tables[-1]["definition"]["title"]="Launch torque target at end of XDF"
        session=EvidenceSession(snapshot(tables))
        found=session.call("editor.inspect_tables",{"query":"launch torque","table_ids":[],"offset":0,"limit":4,"include_values":True})
        self.assertTrue(found["ok"]);self.assertEqual(found["captured_tables"],207)
        self.assertEqual(found["tables"][0]["table_index"],206)
        offset,seen=0,[]
        while offset is not None:
            page=session.call("inspect_tables",{"query":"","table_ids":[],"offset":offset,"limit":8,"include_values":False})
            seen.extend(t["table_id"] for t in page["tables"]);offset=page["next_offset"]
        self.assertEqual(len(set(seen)),207)

    def test_table_values_axes_and_comparison_return_exact_copy(self):
        original=snapshot();session=EvidenceSession(original)
        found=session.inspect_tables(table_ids=["F7F44"])["tables"][0]
        self.assertEqual(found["x_values"],[60,70,90]);self.assertEqual(found["values"][1][1],67.7)
        self.assertEqual(found["primary_vs_comparison_cells"],[[1,1,67.7,66.7,-1.0]])
        found["values"][1][1]=999
        self.assertEqual(session.inspect_tables()["tables"][0]["values"][1][1],67.7)
        self.assertEqual(original["tables"][0]["values"][1][1],67.7)

    def test_log_rows_paginate_without_dropping_sparse_or_empty_observations(self):
        snap=snapshot();snap["log"]["rows"][6]=[6,{}]
        session=EvidenceSession(snap);offset=0;rows=[]
        while offset is not None:
            page=session.call("inspect_log",{"start_s":2,"end_s":9,"columns":["Fuel","RPM"],"offset":offset,"limit":3})
            self.assertTrue(page["ok"]);rows.extend(page["rows"]);offset=page["next_offset"]
        self.assertEqual([r[0] for r in rows],list(range(2,10)))
        self.assertEqual(rows[1],[3,{"1":40,"2":1003}]);self.assertEqual(rows[4],[6,{}])
        self.assertEqual(page["columns"],["time_s","IQ","RPM"])

    def test_dense_and_unsampled_captured_rows_take_precedence(self):
        snap=snapshot();snap["log"]["rows"]=[[0,1000,None,0]];snap["log"]["complete_rows"]=False
        snap["_captured_log_rows"]=[[0,{"1":1000}],[1,{"2":30}],[2,{}]]
        result=EvidenceSession(snap).inspect_log()
        self.assertTrue(result["captured_complete_rows"]);self.assertEqual(len(result["rows"]),3)
        del snap["_captured_log_rows"]
        dense=EvidenceSession(snap).inspect_log()["rows"]
        self.assertEqual(dense,[[0,{"1":1000,"3":0}]])

    def test_invalid_tool_names_and_arguments_return_concise_errors(self):
        session=EvidenceSession(snapshot())
        for name,args in [("exec",{"cmd":"delete"}),("inspect_log",{"start_s":10,"end_s":1}),("inspect_log",{"columns":["bogus"]}),
                          ("inspect_log",{"limit":501}),("inspect_log",{"offset":-1}),("inspect_tables",{"limit":9}),
                          ("inspect_log",{"start_s":float("nan")})]:
            result=session.call(name,args);self.assertFalse(result["ok"]);self.assertLess(len(result["error"]),351)
        self.assertFalse(session.proposals)

    def test_calculate_run_uses_all_exact_rows_current_setup_and_run_scope(self):
        snap=snapshot();session=EvidenceSession(snap,setup="8HP45, 3.07 rear end, 225/45R17")
        with patch("bmw_bin_editor.ai_log_analysis.calculate_run",create=True,return_value={"selected_run":{"start_s":2,"end_s":9},"peaks":{}}) as calc:
            result=session.call("calculate_run",{"start_s":2,"end_s":9,"launch_index":None})
            self.assertTrue(result["ok"])
            self.assertEqual(calc.call_args.args[0]["rows"],snap["log"]["rows"])
            self.assertEqual(calc.call_args.args[1],session.setup)
            self.assertEqual(calc.call_args.kwargs,{"start_s":2.0,"end_s":9.0,"launch_index":None})

    def test_smoothing_requires_explicit_request_and_table_inspection(self):
        session=self.inspected(question="What caused launch boost lag?")
        result=session.call("propose_patch",{"goal":"smoothing","label":"Fill numerical trough","changes":[cell()]})
        self.assertFalse(result["ok"]);self.assertIn("not requested",result["error"])
        disguised=session.call("propose_patch",{"goal":"spool","label":"Fill numerical trough","changes":[cell()]})
        self.assertFalse(disguised["ok"]);self.assertIn("relabeled",disguised["error"])
        uninspected=EvidenceSession(snapshot(),question="Please smooth N75")
        self.assertFalse(uninspected.call("propose_patch",{"goal":"smoothing","label":"Blend","changes":[cell()]})["ok"])

    def test_supported_smoothing_stages_exact_cells_without_mutating_snapshot(self):
        snap=snapshot();before=copy.deepcopy(snap);session=self.inspected(snap)
        result=session.call("propose_patch",{"goal":"smoothing","label":"N75 localized blend","changes":[cell()]})
        self.assertTrue(result["ok"],result);self.assertEqual(snap,before)
        plan=result["proposal"];self.assertEqual(plan["changes"][0]["raw"],6844)
        self.assertAlmostEqual(plan["changes"][0]["new"],68.44)
        self.assertFalse(plan["applied"]);self.assertEqual(len(session.proposals),1)
        self.assertEqual(validate_proposal(snap,plan)["id"],plan["id"])

    def test_old_values_nonfinite_bounds_raw_range_noops_and_overlap_blocked_atomically(self):
        session=self.inspected()
        invalid=[cell(old=67.71),cell(new=float("inf")),cell(row=3),cell(col=-1),cell(new=1_000_000),cell(new=67.704)]
        for change in invalid:
            result=session.call("propose_patch",{"goal":"smoothing","label":"Invalid","changes":[change]})
            self.assertFalse(result["ok"],change)
        result=session.call("propose_patch",{"goal":"smoothing","label":"Duplicate","changes":[cell(),cell()]})
        self.assertFalse(result["ok"]);self.assertFalse(session.proposals)
        result=session.call("propose_patch",{"goal":"smoothing","label":"Mixed valid and invalid","changes":[cell(),cell(row=100)]})
        self.assertFalse(result["ok"]);self.assertFalse(session.proposals)

    def test_unmapped_gear_not_accepted_from_selector_or_model_claims(self):
        gear=table(0xE945A,"Gear-state torque ceiling selector 1","Selector identity/physical gear numbering still needs upstream proof.")
        snap=snapshot([gear]);session=self.inspected(snap,question="Apply first gear torque ceiling")
        change=cell(0xE945A,new=60,reason="Selector 1 means gear 1",expected_effect="Reduce first gear wheelspin")
        result=session.call("propose_patch",{"goal":"gear_torque","label":"First gear","changes":[change]})
        self.assertFalse(result["ok"]);self.assertIn("mapping unverified",result["error"])
        session.question="Smooth first gear torque"
        result=session.call("propose_patch",{"goal":"smoothing","label":"Smooth first gear","changes":[change]})
        self.assertFalse(result["ok"]);self.assertFalse(session.proposals)
        session.question="Smooth the selected torque curve numerically"
        neutral=cell(0xE945A,new=60,reason="Reduce numerical curvature",expected_effect="Numerical blending only")
        for goal in ("other","smoothing"):
            bypass=session.call("propose_patch",{"goal":goal,"label":"Selected curve blend","changes":[neutral]})
            self.assertFalse(bypass["ok"]);self.assertIn("mapping unverified",bypass["error"])

    def test_trusted_confirmed_binding_supersedes_old_selector_uncertainty(self):
        gear=table(0xE945A,"Gear-state torque ceiling selector 1","Selector identity/physical gear numbering unresolved.")
        snap=snapshot([gear]);snap["confirmed_gear_bindings"]={"1":{"table_address":0xE945A,"source":"User-confirmed MaxxECU actual-gear CAN plus code trace"}}
        session=self.inspected(snap,question="Apply first gear torque ceiling")
        change=cell(0xE945A,new=60,reason="Lower first gear ceiling",expected_effect="Test reduced wheelspin in first gear")
        result=session.call("propose_patch",{"goal":"gear_torque","label":"First gear","changes":[change]})
        self.assertTrue(result["ok"],result)
        self.assertEqual(result["proposal"]["changes"][0]["gear_binding"]["physical_gear"],1)
        self.assertTrue(validate_proposal(snap,result["proposal"]))
        no_confirmation=copy.deepcopy(snap);del no_confirmation["confirmed_gear_bindings"]
        with self.assertRaisesRegex(ValueError,"mapping unverified"):
            validate_proposal(no_confirmation,result["proposal"])
        conflicting=cell(0xE945A,new=60,reason="Lower second gear ceiling",expected_effect="Reduce second gear wheelspin")
        self.assertFalse(session.call("propose_patch",{"goal":"gear_torque","label":"Second gear","changes":[conflicting]})["ok"])

    def test_protection_and_smoke_limit_bypass_blocked(self):
        for title,description in [("Thermal protection","Keep temperature protection"),("N75 upper-command-bound","Upper command bound contributor")]:
            session=self.inspected(snapshot([table(title=title,description=description)]))
            self.assertFalse(session.call("propose_patch",{"goal":"smoothing","label":"Blend","changes":[cell()]})["ok"])
        session=self.inspected(snapshot([table(title="Smoke limiter",description="Maximum fuel by airflow")]))
        self.assertFalse(session.call("propose_patch",{"goal":"smoothing","label":"Blend","changes":[cell()]})["ok"])
        session=self.inspected()
        self.assertFalse(session.call("propose_patch",{"goal":"smoothing","label":"Disable protective limiter","changes":[cell()]})["ok"])

    def test_ambiguous_address_requires_id_and_column_major_location_is_correct(self):
        snap=snapshot([table(),table(title="Alias")]);session=self.inspected(snap)
        self.assertFalse(session.call("propose_patch",{"goal":"smoothing","label":"Blend","changes":[cell()]})["ok"])
        result=session.call("propose_patch",{"goal":"smoothing","label":"Blend","changes":[cell(table_id="0xF7F44:0")]})
        self.assertTrue(result["ok"],result)
        t=table();t["definition"]["storage_order"]="column_major";t["values"][0][2]=67.7
        plan=self.inspected(snapshot([t])).propose_patch("smoothing","Column-major blend",[cell(row=0,col=2)])["proposal"]
        self.assertEqual(plan["changes"][0]["byte_address"],0xF7F44+12)

    def test_replayed_plan_recomputes_fields_and_rejects_stale_values_and_signatures(self):
        snap=snapshot();plan=self.inspected(snap).propose_patch("smoothing","Blend",[cell()])["proposal"]
        forged=copy.deepcopy(plan);forged["validated"]=True;forged["changes"][0].update(raw=0,byte_address=0,definition_sha256="fake",table_index=999)
        normalized=validate_proposal(snap,forged)
        self.assertEqual(normalized["changes"][0]["raw"],6844);self.assertEqual(normalized["changes"][0]["table_index"],0)
        stale=copy.deepcopy(snap);stale["calibration_signature"]["bin"]="changed"
        with self.assertRaisesRegex(ValueError,"changed"):
            validate_proposal(stale,plan)
        stale=copy.deepcopy(snap);stale["tables"][0]["values"][1][1]=68
        with self.assertRaisesRegex(ValueError,"Old value mismatch"):
            validate_proposal(stale,plan)
        replay=self.inspected(snap);first=replay.propose_patch("smoothing","Blend",[cell()]);second=replay.propose_patch("smoothing","Blend",[cell()])
        self.assertTrue(second["already_staged"]);self.assertEqual(len(replay.proposals),1)

    def test_replayed_displayed_new_and_requested_encoding_must_agree(self):
        snap=snapshot();plan=self.inspected(snap).propose_patch("smoothing","Blend",[cell()])["proposal"]
        for field,value in [("new",1e10),("new",68.4401),("requested_new",69.0)]:
            forged=copy.deepcopy(plan);forged["changes"][0][field]=value
            with self.assertRaisesRegex(ValueError,"Displayed replacement"):
                validate_proposal(snap,forged)
        forged=copy.deepcopy(plan);forged["changes"][0].update(new=100,raw=6844,requested_new=68.44)
        with self.assertRaisesRegex(ValueError,"Displayed replacement"):
            validate_proposal(snap,forged)
        # Derived raw fields may be regenerated, while reviewed old/new stay exact.
        raw_forgery=copy.deepcopy(plan);raw_forgery["changes"][0]["raw"]=1
        self.assertEqual(validate_proposal(snap,raw_forgery)["changes"][0]["raw"],6844)
        # First-stage requests can round to stored resolution; the staged display
        # is the actual encodable value and survives exact revalidation.
        rounded=self.inspected(snap).propose_patch("smoothing","Quantized blend",[cell(new=68.444)])["proposal"]
        self.assertAlmostEqual(rounded["changes"][0]["new"],68.44)
        self.assertAlmostEqual(validate_proposal(snap,rounded)["changes"][0]["new"],68.44)

    def test_calculation_setup_override_uses_declared_scenario_without_changing_patch_bindings(self):
        snap=snapshot();snap['confirmed_gear_bindings']={'1':{'table_address':0xE945A,'source':'User-confirmed CAN'}}
        setup='8HP45, diff 3.07, 225/45R17'
        session=EvidenceSession(snap,setup=setup,question='Calculate the scenario with 3.91 rear end and 235/40R18 tyres')
        before=copy.deepcopy(session.snapshot)
        result=session.call('calculate_run',{'start_s':None,'end_s':None,'launch_index':None,'setup_override':'8HP45, final drive 3.91, 235/40 R18'})
        self.assertTrue(result['ok'],result)
        assumptions=result['analysis']['assumptions']
        self.assertEqual(assumptions['final_drive'],3.91)
        expected=3.141592653589793*(18*.0254+2*235*40/100000)
        self.assertAlmostEqual(assumptions['nominal_tyre_circumference_m'],expected)
        self.assertTrue(result['setup_override_used']);self.assertEqual(session.setup,setup)
        self.assertEqual(session.snapshot,before);self.assertFalse(session.proposals)
        default=session.calculate_run()['analysis']['assumptions']
        self.assertEqual(default['final_drive'],3.07)
        for override in ('diff 3.73','245/35R18','8HP70','x'*6001):
            invalid=session.call('calculate_run',{'setup_override':override})
            self.assertFalse(invalid['ok'],override)
        self.assertEqual(session.snapshot,before)


if __name__=="__main__":
    unittest.main()
