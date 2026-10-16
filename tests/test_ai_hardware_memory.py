"""Offline hardware-memory regressions; uses staged modules only."""
from pathlib import Path
import copy, math, sys, threading, unittest
STAGE=Path(__file__).resolve().parent
IMPORT_ROOT=STAGE if (STAGE/'bmw_bin_editor').is_dir() else STAGE.parent
sys.path.insert(0,str(IMPORT_ROOT))
from bmw_bin_editor.ai_setup import compact_setup, latest_facts, resolve_conversation_setup
from bmw_bin_editor.ai_log_analysis import calculate_log, SIGNALS
from bmw_bin_editor.ai_evidence import EvidenceSession
from bmw_bin_editor.chatgpt_client import followup_context, ChatGPTClient


def captured(setup='',question='Analyse the launch'):
    keys=('rpm','iq','speed','maf')
    rows=[[0,3500,65,0,1200],[.3,4000,70,0,1250],[.6,4300,75,0,1280],
          [.9,4700,82,10,1220],[1.2,4200,85,25,1300],[1.5,3500,90,30,1400],
          [2,4000,85,40,1390],[5,4500,95,60,1450]]
    return {'vehicle_setup':setup,'analysis_question':question,'tables':[],
            'calibration_signature':{'bin':'captured','definition':'xdf'},
            'log':{'columns':['time_s']+[SIGNALS[k] for k in keys],'rows':rows,'complete_rows':True},
            'confirmed_gear_bindings':{'1':{'table_address':0xE945A,'source':'User-confirmed CAN'}}}


class HardwareMemoryTests(unittest.TestCase):
    def context(self,snap=None,turns=None,question='Recalculate the gears',current=None,review=None):
        return followup_context(snap or captured(),review or {'initial_findings':'Review'},turns or [],question,current_setup=current)

    def test_initial_question_only_setup_survives_followup_and_override(self):
        snap=captured(question='8HP45, diff 3.07, 225/45R17. Analyse launch')
        context=self.context(snap)
        facts=latest_facts([context['resolved_vehicle_setup']])
        self.assertEqual(facts,{'tyres':(225,45,17),'ratios':3.07,'gearboxes':'8hp45'})
        session=EvidenceSession(snap,setup=context['resolved_vehicle_setup'],question=context['analysis_question'])
        result=session.call('calculate_run',{'launch_index':0,'setup_override':'8HP45; final drive 3.07; 225/45R17'})
        self.assertTrue(result['ok'],result)
        self.assertEqual(result['analysis']['assumptions']['final_drive'],3.07)
        self.assertEqual(result['analysis']['selected_run']['launch_index'],0)

    def test_initial_question_survives_updated_capture_question(self):
        snap=captured(question='Review this updated capture')
        context=self.context(snap,review={'initial_findings':'Initial','initial_question':'8HP45, diff 3.07, 225/45R17; first launch'})
        self.assertEqual(latest_facts([context['resolved_vehicle_setup']])['tyres'],(225,45,17))

    def test_unchanged_setup_field_follows_recent_user_corrections(self):
        setup='8HP45, diff 3.07, 225/45R17'
        turns=[{'question':'Actually my diff is 3.91 and tyres 235/40R18','answer':'Earlier assistant guess: diff 4.44, 255/35R18'}]
        context=self.context(captured(setup),turns,current=setup)
        self.assertEqual(latest_facts([context['resolved_vehicle_setup']]),{'tyres':(235,40,18),'ratios':3.91,'gearboxes':'8hp45'})
        self.assertEqual(context['previous_turns'],turns)

    def test_genuinely_edited_setup_field_precedes_earlier_questions(self):
        turns=[{'question':'diff 3.91, 235/40R18','answer':'Okay'}]
        context=self.context(captured('8HP45, diff 3.07, 225/45R17'),turns,current='8HP45, diff 2.56, 245/40R17')
        self.assertEqual(latest_facts([context['resolved_vehicle_setup']]),{'tyres':(245,40,17),'ratios':2.56,'gearboxes':'8hp45'})

    def test_latest_question_precedes_edited_setup_field(self):
        context=self.context(captured('8HP45, diff 3.07, 225/45R17'),question='Calculate with 3.91 rear end',current='8HP45, diff 2.56, 245/40R17')
        self.assertEqual(latest_facts([context['resolved_vehicle_setup']]),{'tyres':(245,40,17),'ratios':3.91,'gearboxes':'8hp45'})

    def test_partial_correction_preserves_other_still_current_facts(self):
        setup=resolve_conversation_setup('My tyre size is 235/40R18','8HP45, diff 3.07','8HP45, diff 3.07',[], '225/45R17')
        self.assertEqual(latest_facts([setup]),{'tyres':(235,40,18),'ratios':3.07,'gearboxes':'8hp45'})

    def test_long_history_compacts_calculations_but_keeps_full_actual_turns(self):
        turns=[{'question':('Detailed prior question '*40)+str(i),'answer':'Saved answer'} for i in range(30)]
        turns[0]['question']='8HP45, diff 3.07, 225/45R17. '+turns[0]['question']
        turns[20]['question']='Actually 3.91 rear end. '+turns[20]['question']
        original=copy.deepcopy(turns)
        context=self.context(captured(),turns)
        self.assertLess(len(context['resolved_vehicle_setup']),12000)
        self.assertEqual(context['previous_turns'],original)
        session=EvidenceSession(context['_evidence_snapshot'],setup=context['resolved_vehicle_setup'],question=context['analysis_question'])
        self.assertEqual(session.calculate_run()['analysis']['assumptions']['final_drive'],3.91)
        self.assertEqual(turns,original)

    def test_old_assistant_guesses_never_enter_arithmetic_facts(self):
        turns=[{'question':'Can you calculate power?','answer':'Try8HP45 diff 3.07 with 225/45R17'}]
        context=self.context(captured(),turns)
        self.assertEqual(context['resolved_vehicle_setup'],'')
        self.assertFalse(context['log']['computed_analysis']['assumptions']['standard_8HP45_declared'])

    def test_latest_unsupported_gearbox_suppresses_old8hp45(self):
        for gearbox in ('6HP26','8HP50'):
            setup=gearbox+'\n8HP45 diff 3.07 225/45R17'
            result=calculate_log(captured()['log'],setup)
            self.assertFalse(result['assumptions']['standard_8HP45_declared'])
            self.assertEqual(result['assumptions']['declared_gearbox'],gearbox.lower())
            self.assertTrue(any('unsupported' in text for text in result['assumptions']['warnings']))
            self.assertNotIn('candidate_gear',result['peaks']['power'])

    def test_override_rejects_obsolete_facts_even_if_present_in_old_setup(self):
        setup='8HP45; diff 3.91; 235/40R18\nOld: diff 3.07, 225/45R17'
        session=EvidenceSession(captured(),setup=setup,question='Recalculate the launch')
        for override in ('diff 3.07','225/45R17','8HP70','diff 4.44'):
            result=session.call('calculate_run',{'setup_override':override})
            self.assertFalse(result['ok'],(override,result))

    def test_current_question_can_explicitly_request_old_scenario(self):
        session=EvidenceSession(captured(),setup='8HP45 diff 3.91 235/40R18',question='Compare with the old scenario: diff 3.07, 225/45R17')
        before=copy.deepcopy(session.snapshot)
        result=session.calculate_run(start_s=.9,end_s=2,setup_override='8HP45 diff 3.07 225/45R17')
        self.assertTrue(result['ok'])
        self.assertEqual(result['analysis']['assumptions']['final_drive'],3.07)
        self.assertEqual(result['analysis']['selected_run']['start_s'],.9)
        self.assertEqual(result['analysis']['peaks']['power']['time_s'],.9)
        self.assertEqual(session.snapshot,before)
        self.assertEqual(session.setup,'8HP45 diff 3.91 235/40R18')
        self.assertFalse(session.proposals)

    def test_override_grammar_matches_arithmetic_and_rejects_invented_newline_gearbox(self):
        session=EvidenceSession(captured(),setup='',question='Calculate power')
        result=session.call('calculate_run',{'setup_override':'8\nHP45'})
        self.assertFalse(result['ok'])
        self.assertFalse(calculate_log(captured()['log'],'8\nHP45')['assumptions']['standard_8HP45_declared'])
        session=EvidenceSession(captured(),setup='8 HP 45 diff 3,07 225/45 R17',question='Power')
        result=session.calculate_run(setup_override='8 hp 45; final drive 3.07; 225/45R17')
        self.assertTrue(result['analysis']['assumptions']['standard_8HP45_declared'])

    def test_multiple_scenario_choices_need_separate_calculations(self):
        session=EvidenceSession(captured(),setup='8HP45 diff 3.07',question='Compare diff 3.07 and 3.91 rear end')
        self.assertFalse(session.call('calculate_run',{'setup_override':'diff 3.07; diff 3.91'})['ok'])
        self.assertTrue(session.calculate_run(setup_override='diff 3.91')['ok'])

    def test_edited_setup_metadata_used_by_updated_analysis(self):
        snap=captured('8HP45 diff 2.56 245/40R17')
        snap['_full_review']=False
        snap['_chat_previous_vehicle_setup']='8HP45 diff 3.07 225/45R17'
        snap['_chat_review']={'initial_findings':'Old review','initial_question':'8HP45 diff 3.07 225/45R17'}
        snap['_chat_turns']=[{'question':'Actually diff 3.91','answer':'Okay'}]
        client=ChatGPTClient.__new__(ChatGPTClient)
        def finish(model,context,cancel,on_delta):
            facts=latest_facts([context['resolved_vehicle_setup']])
            self.assertEqual(facts['ratios'],2.56)
            self.assertEqual(facts['tyres'],(245,40,17))
            self.assertEqual(client._evidence.calculate_run()['analysis']['assumptions']['final_drive'],2.56)
            return '- Review'
        client._analyse_request=finish
        client.analyse('mock',snap,threading.Event(),lambda _:None)


if __name__=='__main__':
    unittest.main(verbosity=2)

class InitialQuestionPrecedenceTests(unittest.TestCase):
    def test_initial_question_correction_beats_unchanged_original_field(self):
        from bmw_bin_editor.ai_setup import resolve_conversation_setup
        from bmw_bin_editor.ai_log_analysis import calculate_log
        resolved=resolve_conversation_setup('What gear?', '8HP45, diff 3.07, 225/45R17', '8HP45, diff 3.07, 225/45R17', [], 'Actually diff 3.91 and 235/40R18; estimate launch power')
        assumptions=calculate_log({'columns':[],'rows':[]},resolved)['assumptions']
        self.assertEqual(assumptions['final_drive'],3.91)
        self.assertIn('235/40R18',resolved)
