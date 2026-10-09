"""Real truncation metrics and bounded incremental source edits."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import harness as h
import manager
from test_executor_recovery import FakeRepo, CONFIG


class PayloadTests(unittest.TestCase):
    def action(self, **fields):
        action = h.parse_executor_action(json.dumps(fields))
        h.validate_executor_action(action)
        return action

    def workflow(self, repo, answers):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        gateway = Mock()
        gateway.chat.side_effect = answers
        log = h.EventLog(Path(temp.name) / 'events.jsonl')
        return h.Workflow(repo, gateway, CONFIG, log), gateway, log

    def test_exact_failed_run_metrics_stop_and_discard_truncation_before_retry(self):
        # Historical truncation metrics; private run identifier omitted.
        for lengths in ((8486, 8486), (8691, 8678)):
            answers = [h.ExecutorResponse('x' * length, {
                'response_shape':'chat_completion', 'finish_reason':'length',
                'content_type':'text', 'response_length':length, 'tool_call_count':0,
                'completion_tokens':2200}) for length in lengths]
            workflow, gateway, log = self.workflow(FakeRepo(), answers)
            prompts=[]
            def reply(*args, **kwargs):
                prompts.append(copy.deepcopy(args[1]))
                return answers[len(prompts)-1]
            gateway.chat.side_effect=reply
            before=workflow.repo.content
            with self.assertRaisesRegex(RuntimeError, 'executor_stalled'):
                workflow.implement('Create a realistic module')
            self.assertEqual(len(prompts),2)
            self.assertFalse(any(m['role']=='assistant' for m in prompts[1]))
            self.assertIn('small complete scaffold',prompts[1][-1]['content'])
            self.assertEqual(before,workflow.repo.content)
            errors=[json.loads(r) for r in log.path.read_text().splitlines() if json.loads(r)['event']=='tool_error']
            self.assertEqual([r['protocol_diagnostic']['classification'] for r in errors],['truncated_response']*2)

    def test_truncated_prefix_hint_is_content_free_and_never_dispatched(self):
        text='{"action":"write_file","path":"SECRET.py","content":"unfinished'
        d=h.executor_wire_diagnostic({'choices':[{'finish_reason':'length','message':{'content':text}}]})
        self.assertEqual(d['leading_action'],'write_file')
        self.assertNotIn('SECRET',json.dumps(d))
        with self.assertRaises(h.ExecutorProtocolError) as caught:
            h.check_executor_response(h.ExecutorResponse(text,d))
        self.assertEqual(caught.exception.classification,'truncated_response')
        d=h.executor_wire_diagnostic({'choices':[{'finish_reason':'length','message':{'content':'{"action":"SECRET"'}}]})
        self.assertEqual(d['leading_action'],'unknown')

    def test_focused_persistence_failure_aborts_without_retry(self):
        repo=FakeRepo();repo.write=Mock(side_effect=RuntimeError('Durable evidence persistence failed'))
        answers=[json.dumps(dict(action='read_file',path='textutil.py'))]*2+[
            json.dumps(dict(action='write_file',path='textutil.py',content='fixed'))]
        workflow,gateway,_=self.workflow(repo,answers)
        with self.assertRaisesRegex(RuntimeError,'Durable evidence persistence failed'):
            workflow.implement('Repair')
        self.assertEqual(gateway.chat.call_count,3)

    def test_valid_long_multiline_write_and_incremental_edit_grow_beyond_action_limit(self):
        source='import json\n\ndef encode(value):\n    return json.dumps(value, ensure_ascii=False)\n'
        source += '# bounded source padding\n' * ((h.EXECUTOR_TEXT_CHARS-len(source))//25)
        source += '#' * (h.EXECUTOR_TEXT_CHARS-len(source))
        write=self.action(action='write_file',path='textutil.py',content=source)
        repo=FakeRepo()
        h.apply_text_action(repo,write)
        compile(repo.content,'textutil.py','exec')
        addition='\ndef decode(text):\n    return json.loads(text)\n'
        edit=self.action(action='replace_text',path='textutil.py',old_text='import json\n',
                         new_text='import json\n'+addition)
        h.apply_text_action(repo,edit)
        self.assertGreater(len(repo.content),h.EXECUTOR_TEXT_CHARS)
        self.assertIn(addition,repo.content)
        compile(repo.content,'textutil.py','exec')

    def test_oversized_payload_rejected_by_same_schema_and_validator(self):
        for fields, field in ((dict(action='write_file',path='a.py',content='x'*(h.EXECUTOR_TEXT_CHARS+1)), 'content'),
                             (dict(action='replace_text',path='a.py',old_text='x',new_text='x'*(h.EXECUTOR_TEXT_CHARS+1)), 'new_text')):
            with self.subTest(action=fields['action']), self.assertRaises(h.ExecutorProtocolError) as caught:
                self.action(**fields)
            self.assertEqual(caught.exception.classification,'payload_too_large')
            self.assertEqual(h.EXECUTOR_ACTION_SCHEMAS[fields['action']]['properties'][field]['maxLength'],h.EXECUTOR_TEXT_CHARS)

    def test_replace_text_fails_closed_for_missing_duplicate_empty_and_noop_anchors(self):
        repo=FakeRepo();repo.content='anchor\nanchor\n'
        before=repo.content
        overlap=FakeRepo();overlap.content='aaa'
        with self.assertRaises(h.ExecutorProtocolError):
            h.apply_text_action(overlap,self.action(action='replace_text',path='textutil.py',old_text='aa',new_text='x'))
        self.assertEqual(overlap.content,'aaa')
        for old,new in (('missing','new'),('anchor','new'),('','new'),('anchor\nanchor\n','anchor\nanchor\n')):
            with self.subTest(old=old),self.assertRaises(h.ExecutorProtocolError):
                h.apply_text_action(repo,self.action(action='replace_text',path='textutil.py',old_text=old,new_text=new))
            self.assertEqual(repo.content,before)

    def test_replace_text_uses_existing_scoped_write_gate(self):
        repo=manager.ScopedRepository.__new__(manager.ScopedRepository)
        repo.scope={'allowed_paths':['allowed.py'],'forbidden_paths':[]};repo.run_forbidden=[]
        repo.read=Mock(return_value='old')
        edit=self.action(action='replace_text',path='forbidden.py',old_text='old',new_text='new')
        with patch('durable.DurableRepository.write') as write:
            with self.assertRaisesRegex(ValueError,'outside'):
                h.apply_text_action(repo,edit)
            write.assert_not_called()

    def test_focused_recovery_can_edit_a_file_larger_than_a_single_action(self):
        repo=FakeRepo();repo.content='# unchanged\n'*220+'value = 1\n'
        edit=dict(action='replace_text',path='textutil.py',old_text='value = 1',new_text='value = 2')
        workflow,gateway,_=self.workflow(repo,[json.dumps(dict(action='read_file',path='textutil.py'))]*2+[json.dumps(edit)])
        workflow.implement('Change the value')
        self.assertTrue(repo.content.endswith('value = 2\n'))
        self.assertEqual(gateway.chat.call_count,3)
        self.assertEqual(gateway.chat.call_args.args[2],'edit_recovery')

    def test_truncation_then_valid_edit_resets_counter_without_resending_partial_code(self):
        truncated=h.ExecutorResponse('unfinished',{'response_shape':'chat_completion','finish_reason':'length',
            'content_type':'text','response_length':10,'tool_call_count':0,'completion_tokens':2200})
        answers=[truncated,json.dumps(dict(action='replace_text',path='textutil.py',old_text='slugify = None',new_text='# fixed')),
                 '{}',json.dumps(dict(action='done',summary='MODEL CLAIM ONLY'))]
        workflow,gateway,_=self.workflow(FakeRepo(),answers)
        self.assertEqual(workflow.implement('Repair'),'MODEL CLAIM ONLY')
        self.assertEqual(gateway.chat.call_count,4)
        # Workflow returns an untrusted claim; Manager trust gates are exercised separately.


if __name__=='__main__':
    unittest.main()
