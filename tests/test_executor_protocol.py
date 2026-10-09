"""Canonical executor contract, real-wire extraction and content-free diagnostics."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import harness as h
import durable
from test_executor_recovery import FakeRepo, CONFIG


class ProtocolTests(unittest.TestCase):
    def validate(self, text):
        value = h.parse_executor_action(text)
        h.validate_executor_action(value)
        return value

    def test_observed_live_qwen_content_is_canonical(self):
        wire = {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': '{"action":"write_file","path":"marker.txt","content":"PROTOCOL_OK\\n"}'}}]}
        diagnostic = h.executor_wire_diagnostic(wire)
        answer = h.ExecutorResponse(wire['choices'][0]['message']['content'], diagnostic)
        h.check_executor_response(answer)
        self.assertEqual(self.validate(answer), {'action':'write_file', 'path':'marker.txt', 'content':'PROTOCOL_OK\n'})

    def test_schema_and_validator_accept_each_canonical_action(self):
        actions = [{'action':'list_files'}, {'action':'read_file','path':'a.py'},
                   {'action':'write_file','path':'a.py','content':'text\n"quoted"'},
                   {'action':'replace_text','path':'a.py','old_text':'old','new_text':'new'},
                   {'action':'run_command','argv':['python','-m','pytest','-q']},
                   {'action':'done'}, {'action':'done','summary':'done'}]
        for action in actions:
            with self.subTest(action=action):
                self.assertEqual(self.validate(json.dumps(action)), action)
                schema = h.EXECUTOR_ACTION_SCHEMAS[action['action']]
                self.assertTrue(set(schema['required']).issubset(action))
                self.assertFalse(set(action)-schema['properties'].keys())
        self.assertEqual(h.EXECUTOR_SCHEMA['oneOf'], list(h.EXECUTOR_ACTION_SCHEMAS.values()))
        self.assertEqual(h.FOCUSED_EDIT_SCHEMA['oneOf'], [h.EXECUTOR_ACTION_SCHEMAS[k] for k in ('write_file','replace_text')])

    def test_invalid_responses_are_classified_without_salvaging_nested_actions(self):
        cases = [('{}','missing_action'), ('{"action":null}','null_action'),
                 ('{"action":"delete"}','unsupported_action'), ('{"action":[]}','unsupported_action'),
                 ('[]','non_object'), ('{"wrapper":{"action":"done"}}','missing_action'),
                 ('{"action":"write_file","path":"a.py","content":"unfinished','invalid_json'),
                 ('{"action":"done"}\n{"action":"done"}','trailing_data'),
                 ('{"action":"done"} explanation','trailing_data'),
                 ('{"action":"done","action":"list_files"}','duplicate_key'),
                 ('{"action":"read_file"}','missing_field'),
                 ('{"action":"write_file","path":"a.py","content":null}','invalid_content'),
                 ('{"action":"done","approved":true}','unexpected_fields')]
        for text, classification in cases:
            with self.subTest(classification=classification), self.assertRaises(h.ExecutorProtocolError) as caught:
                self.validate(text)
            self.assertEqual(caught.exception.classification, classification)
        self.assertEqual(self.validate('```json\n{"action":"done"}\n```'), {'action':'done'})

    def test_truncation_rejects_even_syntactically_complete_json(self):
        answer = h.ExecutorResponse('{"action":"done"}', h.executor_wire_diagnostic({
            'choices':[{'finish_reason':'length','message':{'content':'{"action":"done"}'}}]}))
        with self.assertRaises(h.ExecutorProtocolError) as caught:
            h.check_executor_response(answer)
        self.assertEqual(caught.exception.classification, 'truncated_response')

    def test_prompts_and_retry_share_canonical_schema(self):
        schema = json.dumps(h.EXECUTOR_SCHEMA, separators=(',',':'))
        self.assertIn(schema, h.SYSTEM)
        with tempfile.TemporaryDirectory() as temp:
            log = h.EventLog(Path(temp)/'events.jsonl')
            gateway = Mock()
            gateway.chat.side_effect = ['{}','{"action":"done"}']
            h.Workflow(FakeRepo(), gateway, CONFIG, log).implement('tiny task')
            messages = gateway.chat.call_args.args[1]
            self.assertTrue(any(m['role']=='user' and schema in m['content'] for m in messages))
        self.assertIn(json.dumps(h.FOCUSED_EDIT_SCHEMA,separators=(',',':')), h.executor_contract(focused=True))

    def test_focused_recovery_uses_same_write_schema_and_dispatches(self):
        with tempfile.TemporaryDirectory() as temp:
            log = h.EventLog(Path(temp)/'events.jsonl')
            gateway = Mock()
            gateway.chat.side_effect = [
                '{"action":"read_file","path":"textutil.py"}',
                '{"action":"read_file","path":"textutil.py"}',
                json.dumps({"action":"write_file","path":"textutil.py","content":"fixed\n"}),
            ]
            repo=FakeRepo()
            h.Workflow(repo,gateway,CONFIG,log).implement('repair')
            self.assertEqual(repo.content,'fixed\n')
            self.assertEqual(gateway.chat.call_args.args[2],'edit_recovery')
            self.assertIn(h.executor_contract(focused=True), gateway.chat.call_args.args[1][0]['content'])

    def test_wire_metadata_distinguishes_shape_tool_calls_and_null_content(self):
        for wire, classification in (
            ({},'invalid_response_shape'),
            ({'choices':[{'finish_reason':'stop','message':{'content':None}}]},'invalid_response_shape'),
            ({'choices':[{'finish_reason':'tool_calls','message':{'content':None,'tool_calls':[{}]}}]},'tool_call_response'),
            ({'choices':[{'message':{'content':'{}'}}]},'incomplete_response'),
        ):
            with self.subTest(wire=wire):
                d=h.executor_wire_diagnostic(wire)
                with self.assertRaises(h.ExecutorProtocolError) as caught:
                    h.check_executor_response(h.ExecutorResponse('',d))
                self.assertEqual(caught.exception.classification,classification)

    def test_real_gateway_invalid_wire_is_bounded_and_durable_diagnostics_exclude_secrets(self):
        with tempfile.TemporaryDirectory() as temp:
            class Store:
                directory=Path(temp)
                state={'options':{'config':{}},'round_number':1}
                records=[]
                def append(self,event,**fields):
                    self.records.append({'event':event,**copy.deepcopy(fields)})
            store=Store()
            log=durable.DurableLog(store)
            config={**CONFIG,'roles':{'code':'llm-code'},'request_timeout_seconds':2}
            gateway=h.Gateway(config,log)
            gateway.switch=Mock()
            gateway.check_active_ram=Mock()
            secret='SECRET_DO_NOT_LOG_'*10000
            wire={'choices':[{'finish_reason':'length','message':{
                'content':'{"action":"write_file","path":"textutil.py","content":"'+secret,
                'reasoning_content':secret}}], 'usage':{'completion_tokens':2200,'secret':secret},
                'timings':{'secret':secret}}
            gateway.request=Mock(return_value=wire)
            repo=FakeRepo()
            before=repo.content
            with self.assertRaisesRegex(RuntimeError,'executor_stalled'):
                h.Workflow(repo,gateway,config,log).implement('tiny task')
            self.assertEqual(repo.content,before)
            self.assertEqual(gateway.request.call_count,2)
            errors=[r for r in store.records if r['event']=='tool_error']
            self.assertEqual([r['consecutive_invalid_actions'] for r in errors],[1,2])
            for error in errors:
                d=error['protocol_diagnostic']
                self.assertEqual(d['classification'],'truncated_response')
                self.assertEqual(d['finish_reason'],'length')
                self.assertEqual(d['response_length'],len(wire['choices'][0]['message']['content']))
                self.assertEqual(d['completion_tokens'],2200)
                self.assertLess(len(json.dumps(d)),600)
            serialized=json.dumps(store.records)
            self.assertNotIn('SECRET_DO_NOT_LOG',serialized)
            self.assertNotIn('reasoning_content',serialized)
            self.assertNotIn('textutil.py',serialized)
            self.assertLess(len(serialized),6000)
            sent=gateway.request.call_args.args[2]
            self.assertEqual(sent['response_format']['json_schema']['schema'],h.EXECUTOR_SCHEMA)


if __name__=='__main__':
    unittest.main()
