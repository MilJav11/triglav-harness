"""Convergence regressions: scripted wire decisions, real Manager verification/trust gates."""
import copy
import io
import json
import os
import posixpath
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

import harness as h
import manager as m
from recovery import EpisodeStop, command_observation, shrink_failure, failed_tests, targeted_check, request_timed_out
from test_executor_recovery import CONFIG
from test_manager import ManagerCase, Planner, RepairPlanner, Executor, Critic, Reviewer, Forbidden
from test_manager import step, wrong, ADD_OK, ADD_OK_DOC, LOOSE, CHECK1, clean_critic


class Repo:
    def __init__(self):
        self.contents = {'calc.py': 'value = 0\n', 'test_calc.py': 'from calc import value\n'}
        self.writes = []
        self.commands = []

    def files(self):
        return list(self.contents)

    def read(self, path):
        if path not in self.contents:
            raise ValueError('File missing')
        return self.contents[path]

    def write(self, path, content):
        self.contents[path] = content
        self.writes.append(path)

    def execute(self, argv):
        self.commands.append(argv)
        failed = argv[0] == 'python' and 'value = 0' in self.contents['calc.py']
        return {'argv': argv, 'exit_code': 1 if failed else 0, 'duration_seconds': 0.12,
                'stdout': 'FAILED test_calc.py::test_value\n' if failed else '1 passed\n',
                'stderr': 'AssertionError: expected 1\n' if failed else '', 'output_truncated': False}


def action(kind, **fields):
    return json.dumps({'action': kind, **fields})


CHECK = ['python', '-m', 'pytest', 'test_calc.py', '-q']


class SameGoalPlanner(Planner):
    def plan(self, context, tier):
        self.calls.append((copy.deepcopy(context), tier))
        value = step()
        failure = context.get('recovery_evidence')
        if failure:
            value['recovery_from'] = {'round': failure['round'], 'fingerprint': failure['fingerprint']}
        return value


class EpisodeConvergenceTests(unittest.TestCase):
    def workflow(self, answers, repo=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        log = h.EventLog(Path(temp.name) / 'events.jsonl')
        gateway = Mock()
        gateway.prompts = []
        responses = iter(answers)
        def reply(role, messages, phase, max_tokens):
            gateway.prompts.append(copy.deepcopy(messages))
            return next(responses)
        gateway.chat.side_effect = reply
        return h.Workflow(repo or Repo(), gateway, CONFIG, log), gateway, log

    def stop(self, workflow, reason, context=None):
        with self.assertRaises(EpisodeStop) as caught:
            workflow.implement('Repair calc.py', episode_context=context)
        self.assertEqual(caught.exception.reason, reason)

    def test_failed_pytest_observation_drives_focused_repair_and_pass(self):
        answers = [action('run_command', argv=CHECK), action('read_file', path='calc.py'),
                   action('replace_text', path='calc.py', old_text='value = 0', new_text='value = 1'),
                   action('run_command', argv=CHECK), action('done', summary='claim')]
        workflow, gateway, log = self.workflow(answers)
        self.assertEqual(workflow.implement('Repair calc.py'), 'claim')
        observed = json.loads(gateway.prompts[1][-1]['content'].split(': ', 1)[1])
        self.assertEqual(observed['argv'], CHECK)
        self.assertEqual(observed['exit_code'], 1)
        self.assertIn('FAILED test_calc.py', observed['stdout'])
        self.assertIn('AssertionError', observed['stderr'])
        self.assertEqual(observed['duration_seconds'], 0.12)
        self.assertFalse(observed['output_truncated'])
        self.assertEqual(workflow.repo.writes, ['calc.py'])
        records = [json.loads(x) for x in log.path.read_text(encoding='utf-8').splitlines()]
        self.assertEqual([r['observation']['exit_code'] for r in records
                          if r['event'] == 'executor_observation' and r['kind'] == 'command'], [1, 0])

    def test_same_file_third_mutation_blocked_then_ignored_requirement_stops_at_four_calls(self):
        answers = [action('write_file', path='calc.py', content=f'value = {i}\n') for i in range(1, 33)]
        workflow, gateway, _ = self.workflow(answers)
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 4)
        self.assertEqual(len(workflow.repo.writes), 2)
        blocked = json.loads(gateway.prompts[3][-1]['content'])
        self.assertEqual(blocked['outcome'], 'observation_required')
        self.assertFalse(blocked['mutation_executed'])

    def test_unchanged_file_listing_stops_at_two_calls(self):
        workflow, gateway, _ = self.workflow([action('list_files')] * 32)
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 2)

    def test_path_aliases_cannot_evade_mutation_limit(self):
        class Aliases(Repo):
            def read(inner, path):
                return super().read(posixpath.normpath(path).lower())
            def write(inner, path, content):
                return super().write(posixpath.normpath(path).lower(), content)
        paths = ['calc.py', './calc.py', 'CALC.py' if os.name == 'nt' else 'calc.py', 'calc.py']
        answers = [action('write_file', path=p, content=f'value = {i}\n') for i, p in enumerate(paths, 1)]
        workflow, gateway, _ = self.workflow(answers, Aliases())
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 4)
        self.assertEqual(len(workflow.repo.writes), 2)

    def churn(self):
        return [action('write_file', path='calc.py', content=f'value = {i}\n') for i in (1, 2, 3)]

    def test_forced_same_path_read_permits_focused_work_without_applying_blocked_edit(self):
        answers = self.churn() + [action('read_file', path='calc.py'),
            action('replace_text', path='calc.py', old_text='value = 2', new_text='value = 4'), action('done')]
        workflow, gateway, _ = self.workflow(answers)
        workflow.implement('Repair')
        self.assertEqual(workflow.repo.contents['calc.py'], 'value = 4\n')
        self.assertEqual(len(workflow.repo.writes), 3)
        self.assertIn('value = 2', gateway.prompts[4][-1]['content'])

    def test_unrelated_read_does_not_satisfy_required_observation(self):
        workflow, gateway, _ = self.workflow(self.churn() + [action('read_file', path='test_calc.py')])
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 4)
        self.assertEqual(len(workflow.repo.writes), 2)

    def test_relevant_targeted_test_and_diff_satisfy_required_observation(self):
        for argv in (CHECK, ['git', 'diff']):
            with self.subTest(argv=argv):
                workflow, _, _ = self.workflow(self.churn() + [action('run_command', argv=argv),
                    action('replace_text', path='calc.py', old_text='value = 2', new_text='value = 4'), action('done')])
                workflow.implement('Repair')
                self.assertEqual(len(workflow.repo.writes), 3)

    def test_unrelated_targeted_check_or_diff_cannot_reset_required_path(self):
        for argv in (['python', '-m', 'pytest', 'other_test.py', '-q'], ['git', 'status']):
            with self.subTest(argv=argv):
                workflow, _, _ = self.workflow(self.churn() + [action('run_command', argv=argv)])
                self.stop(workflow, 'NO_PROGRESS')
                self.assertNotIn(argv, workflow.repo.commands)

    def test_forced_observation_opportunity_is_once_per_episode(self):
        workflow, gateway, _ = self.workflow(self.churn() + [action('read_file', path='calc.py')] +
            [action('write_file', path='calc.py', content=f'value = {i}\n') for i in (4, 5, 6)])
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 7)
        self.assertEqual(len(workflow.repo.writes), 4)

    def test_forced_observation_cannot_repeat_via_another_path(self):
        workflow, gateway, _ = self.workflow(self.churn() + [action('read_file', path='calc.py')] +
            [action('write_file', path='other.py', content=f'value = {i}\n') for i in (1, 2, 3)])
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 7)
        self.assertEqual(workflow.repo.contents['other.py'], 'value = 2\n')

    def test_forced_observation_cannot_be_satisfied_by_done_or_malformed_output(self):
        for answer in (action('done'), 'not an action'):
            with self.subTest(answer=answer):
                workflow, gateway, _ = self.workflow(self.churn() + [answer])
                self.stop(workflow, 'NO_PROGRESS')
                self.assertEqual(gateway.chat.call_count, 4)

    def test_forced_timeout_is_not_a_completed_useful_observation(self):
        class TimedOut(Repo):
            def execute(inner, argv):
                result = super().execute(argv)
                if argv == CHECK: result['exit_code'] = None
                return result
        workflow, _, _ = self.workflow(self.churn() + [action('run_command', argv=CHECK)], TimedOut())
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(len(workflow.repo.writes), 2)

    def test_heartbeats_and_progress_do_not_satisfy_required_observation(self):
        workflow, gateway, log = self.workflow(self.churn() + [action('write_file', path='calc.py', content='value = 9\n')])
        original = gateway.chat.side_effect
        def reply(*args, **kwargs):
            log.status('heartbeat', phase='model request', elapsed=10)
            log.emit('progress', elapsed=10)
            return original(*args, **kwargs)
        gateway.chat.side_effect = reply
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(len(workflow.repo.writes), 2)

    def test_executor_prompt_uses_canonical_progress_limits(self):
        from recovery import executor_progress_policy, MUTATIONS_WITHOUT_OBSERVATION, MAX_FORCED_OBSERVATIONS
        self.assertIn(executor_progress_policy(), h.SYSTEM)
        self.assertEqual((MUTATIONS_WITHOUT_OBSERVATION, MAX_FORCED_OBSERVATIONS), (2, 1))

    def test_legitimate_edit_read_edit_read_workflow_is_allowed(self):
        answers = []
        for i in range(1, 5):
            answers += [action('replace_text', path='calc.py', old_text=f'value = {i-1}', new_text=f'value = {i}'),
                        action('read_file', path='calc.py')]
        answers.append(action('done'))
        workflow, _, _ = self.workflow(answers)
        workflow.implement('Increment with observation')
        self.assertEqual(len(workflow.repo.writes), 4)

    def test_anchor_failure_requires_read_before_any_mutation(self):
        answers = [action('replace_text', path='calc.py', old_text='missing', new_text='new'),
                   action('write_file', path='calc.py', content='value = 1\n')]
        workflow, gateway, _ = self.workflow(answers)
        self.stop(workflow, 'NO_PROGRESS')
        self.assertIn('anchor', gateway.prompts[1][-1]['content'].lower())
        self.assertEqual(workflow.repo.writes, [])

    def test_near_identical_bad_anchors_stop_even_with_intervening_read(self):
        answers = [action('replace_text', path='calc.py', old_text='missing one', new_text='a'),
                   action('read_file', path='calc.py'),
                   action('replace_text', path='calc.py', old_text='missing two', new_text='b')]
        workflow, gateway, log = self.workflow(answers)
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 3)
        events = [json.loads(x) for x in log.path.read_text(encoding='utf-8').splitlines()]
        failures = [r for r in events if r['event'] == 'executor_tool_failure']
        self.assertEqual([r['classification'] for r in failures], ['anchor_not_found'] * 2)

    def test_identical_failed_command_unrelated_write_does_not_reset(self):
        answers = [action('run_command', argv=CHECK), action('write_file', path='notes.py', content='note = 1\n'),
                   action('run_command', argv=CHECK)]
        workflow, gateway, _ = self.workflow(answers)
        self.stop(workflow, 'NO_PROGRESS')
        self.assertEqual(gateway.chat.call_count, 3)
        self.assertEqual(workflow.repo.commands.count(CHECK), 1)

    def test_done_after_failed_post_mutation_check_stops(self):
        answers = [action('write_file', path='calc.py', content='value = 0 # candidate\n'),
                   action('run_command', argv=CHECK), action('done', summary='all good')]
        workflow, gateway, _ = self.workflow(answers)
        self.stop(workflow, 'KNOWN_CHECK_FAILED')
        self.assertEqual(gateway.chat.call_count, 3)

    def test_fresh_workflow_call_drops_prior_episode_transcript(self):
        workflow, gateway, _ = self.workflow([action('read_file', path='calc.py'), action('done'), action('done')])
        workflow.implement('ROUND_ONE_OPAQUE_MARKER')
        workflow.implement('Repair from durable inputs')
        self.assertEqual(len(gateway.prompts[-1]), 2)
        self.assertNotIn('ROUND_ONE_OPAQUE_MARKER', json.dumps(gateway.prompts[-1]))

    def test_unrelated_mutation_cannot_hide_known_targeted_check_failure(self):
        answers = [action('write_file', path='calc.py', content='value = 0 # changed\n'),
                   action('run_command', argv=CHECK), action('write_file', path='notes.py', content='note = 1\n'),
                   action('done')]
        workflow, _, _ = self.workflow(answers)
        self.stop(workflow, 'KNOWN_CHECK_FAILED')

    def test_untrusted_existing_candidate_requires_inspection(self):
        workflow, gateway, _ = self.workflow([action('write_file', path='calc.py', content='value = 1\n'),
                                              action('read_file', path='calc.py'),
                                              action('replace_text', path='calc.py', old_text='value = 0', new_text='value = 1'),
                                              action('done')])
        workflow.implement('Repair', episode_context={'workspace': {'untrusted_paths': ['calc.py']}})
        self.assertIn('Read the current file', gateway.prompts[1][-1]['content'])
        self.assertEqual(len(workflow.repo.writes), 1)

    def test_noop_writes_and_duplicate_anchors_fail_closed(self):
        for source, actions, classification in [
            ('value = 0\n', [action('write_file', path='calc.py', content='value = 0\n')] * 2, 'no_change_edit'),
            ('x x', [action('replace_text', path='calc.py', old_text='x', new_text='y'),
                     action('read_file', path='calc.py'), action('replace_text', path='calc.py', old_text='x', new_text='z')], 'anchor_multiple_matches')]:
            with self.subTest(classification=classification):
                repo = Repo(); repo.contents['calc.py'] = source
                workflow, _, log = self.workflow(actions, repo)
                self.stop(workflow, 'NO_PROGRESS')
                self.assertEqual(repo.writes, [])
                self.assertIn(classification, log.path.read_text(encoding='utf-8'))


class RoundConvergenceTests(ManagerCase):
    def test_production_executor_candidate_failure_drives_fresh_round_and_checkpoint(self):
        self.make(budgets=LOOSE)
        replies = iter([action('write_file', path='calc.py', content=wrong(1)),
                        action('run_command', argv=CHECK1), action('done'),
                        action('read_file', path='calc.py'),
                        action('replace_text', path='calc.py', old_text='return 0  # attempt 1', new_text='return a + b'),
                        action('run_command', argv=CHECK1), action('done')])
        wire = Mock()
        wire.chat.side_effect = lambda *args, **kwargs: next(replies)
        planner = RepairPlanner([step()])
        def agents(repo, gateway, config, log):
            return m.Agents(planner, m.ModelExecutor(h.Workflow(repo, wire, config, log)),
                            Forbidden('critic'), Forbidden('reviewer'))
        self.assertEqual(m.execute(self.store, agents=agents, gateway=self.gateway)['status'], 'COMPLETED')
        evidence = planner.calls[1][0]['recovery_evidence']
        self.assertEqual(evidence['classification'], 'KNOWN_CHECK_FAILED')
        self.assertEqual([a['action'] for a in evidence['attempted_actions']], ['write_file', 'run_command', 'done'])
        self.assertEqual(evidence['last_successful_observation']['kind'], 'command')
        self.assertEqual(evidence['last_successful_observation']['observation']['exit_code'], 1)
        self.assertEqual(self.rounds()[1]['verification'], 'passed')
        self.assertEqual(self.dirs(), ['0002.json'])

    def test_verifier_evidence_bound_replan_preserves_contract_and_no_false_trust(self):
        self.make(budgets=LOOSE)
        planner, executor = RepairPlanner([step()]), Executor([wrong(1), ADD_OK])
        self.assertEqual(self.run_loop(planner, executor)['status'], 'COMPLETED')
        failure = planner.calls[1][0]['recovery_evidence']
        self.assertEqual(failure['schema_version'], 1)
        self.assertEqual((failure['round'], failure['step_id'], failure['classification']), (1, 's1', 'VERIFICATION_FAILED'))
        self.assertFalse(failure['trusted_progress'])
        self.assertIsNone(failure['trusted_checkpoint'])
        self.assertTrue(failure['implementation_changed'])
        self.assertEqual(failure['changed_paths'], ['calc.py'])
        self.assertEqual(failure['commands'][0]['argv'], CHECK1)
        self.assertNotEqual(failure['commands'][0]['exit_code'], 0)
        self.assertIn('AssertionError', failure['commands'][0]['stderr'])
        self.assertLessEqual(len(json.dumps(failure)), 6500)
        first, second = (call[1] for call in executor.calls)
        self.assertEqual(first['task_contract_sha256'], second['task_contract_sha256'])
        self.assertEqual(first['trusted_state'], second['trusted_state'])
        self.assertEqual(second['workspace']['untrusted_paths'], ['calc.py'])
        self.assertNotIn('messages', second)
        self.assertNotEqual(executor.calls[0][0]['goal'], executor.calls[1][0]['goal'])
        self.assertEqual(executor.calls[1][0]['recovery_from'], {'round': 1, 'fingerprint': failure['fingerprint']})
        self.assertEqual([r['trusted'] for r in self.rounds()], [False, True])
        self.assertEqual(self.dirs(), ['0002.json'])

    def test_executor_failure_after_mutation_records_candidate_and_replans(self):
        self.make(budgets=LOOSE)
        class FailsAfterWrite(Executor):
            def execute(inner, proposal, context, repo):
                if not inner.calls:
                    inner.calls.append((proposal, copy.deepcopy(context)))
                    repo.write('calc.py', wrong(1))
                    raise m.ExecutorError('Same-path edits lack evidence', 'NO_PROGRESS')
                return super().execute(proposal, context, repo)
        planner = RepairPlanner([step()])
        self.assertEqual(self.run_loop(planner, FailsAfterWrite([ADD_OK]))['status'], 'COMPLETED')
        evidence = planner.calls[1][0]['recovery_evidence']
        self.assertEqual(evidence['classification'], 'NO_PROGRESS')
        self.assertTrue(evidence['implementation_changed'])
        self.assertEqual(evidence['changed_paths'], ['calc.py'])

    def test_same_goal_new_evidence_accepted_but_same_state_cosmetic_replay_refused(self):
        self.make(budgets={**LOOSE, 'max_rounds': 4})
        class Replay(Planner):
            def plan(inner, context, tier):
                value = super().plan(context, tier)
                if context.get('recovery_evidence'):
                    f = context['recovery_evidence']
                    value = dict(value, recovery_from={'round': f['round'], 'fingerprint': f['fingerprint']})
                return value
        executor = Executor([wrong(1), wrong(1)])
        planner = Replay([step(), step('renamed', goal=step()['goal']), step('renamed_again', goal=step()['goal']), step('renamed_yet_again', goal=step()['goal'])])
        self.run_loop(planner, executor)
        self.assertEqual(len(planner.calls), 4)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(self.rounds()[1]['reason'], 'VERIFICATION_FAILED')
        self.assertEqual(self.rounds()[2]['reason'], 'RECOVERY_REPLAN_REQUIRED')
        self.assertEqual(self.rounds()[3]['reason'], 'RECOVERY_REPLAN_REQUIRED')
        first = json.loads((self.store.directory/'rounds/0002/failure-0002.json').read_text())
        rejected = json.loads((self.store.directory/'rounds/0003/failure-0003.json').read_text())
        self.assertEqual(first['recovery_state_fingerprint'], rejected['recovery_state_fingerprint'])
        diagnostic = json.loads((self.store.directory/'rounds/0003/plan-executor-01.json').read_text())
        self.assertEqual(diagnostic['substantive_validation'], 'passed')
        self.assertEqual(diagnostic['replay_validation'], 'failed')
        self.assertIsNone(self.state()['last_verified_checkpoint'])

    def test_same_goal_with_new_failure_evidence_on_same_workspace_can_recover(self):
        self.make(budgets=LOOSE)
        class NewCause(Executor):
            def execute(inner, proposal, context, repo):
                if len(inner.calls) == 0:
                    inner.calls.append((proposal, copy.deepcopy(context)))
                    repo.write('calc.py', wrong(1))
                    raise m.ExecutorError('Observation requirement ignored', 'NO_PROGRESS')
                if len(inner.calls) == 1:
                    inner.calls.append((proposal, copy.deepcopy(context)))
                    raise m.ExecutorError('New targeted import failure observed', 'EXECUTOR_ERROR')
                return super().execute(proposal, context, repo)
        planner, executor = SameGoalPlanner([]), NewCause([ADD_OK])
        self.assertEqual(self.run_loop(planner, executor)['status'], 'COMPLETED')
        self.assertEqual(len(planner.calls), 3)
        self.assertEqual(len({m.step_fingerprint(c[0]) for c in executor.calls}), 1)
        self.assertNotEqual(self.rounds()[1]['recovery_attempt_identity'], self.rounds()[2]['recovery_attempt_identity'])
        self.assertEqual([r['stalled'] for r in self.rounds()[:2]], [False, False])
        self.assertEqual([r['trusted'] for r in self.rounds()], [False, False, True])
        self.assertEqual(self.dirs(), ['0003.json'])

    def test_same_goal_new_workspace_cannot_evade_retry_lineage(self):
        self.make(budgets={**LOOSE, 'max_step_retries': 1})
        planner, executor = SameGoalPlanner([]), Executor([wrong(1), wrong(2), ADD_OK])
        self.run_loop(planner, executor)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(self.state()['manager']['stop']['reason'], 'max_step_retries')
        self.assertEqual(len(self.state()['manager']['step_attempts']), 1)

    def test_cached_failed_step_without_fresh_binding_never_dispatches(self):
        self.make(budgets={**LOOSE, 'max_rounds': 2})
        planner, executor = Planner([step(), step()]), Executor([wrong(1)])
        self.run_loop(planner, executor)
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(self.rounds()[1]['reason'], 'INVALID_MANAGER_OUTPUT')
        self.assertIsNone(self.state()['last_verified_checkpoint'])

    def test_executor_timeout_becomes_evidence_for_fresh_planning(self):
        self.make(budgets=LOOSE)
        planner = RepairPlanner([step()])
        self.run_loop(planner, Executor([m.ExecutorError('Request timed out', 'EXECUTOR_TIMEOUT'), ADD_OK]))
        self.assertEqual(planner.calls[1][0]['recovery_evidence']['classification'], 'EXECUTOR_TIMEOUT')
        self.assertEqual(self.state()['status'], 'COMPLETED')

    def test_chat_transport_failure_records_safe_evidence_and_replans(self):
        self.make(budgets=LOOSE)
        planner = RepairPlanner([step()])
        class Transport(Executor):
            def execute(inner, proposal, context, repo):
                if not inner.calls:
                    inner.calls.append((proposal, copy.deepcopy(context)))
                    workflow = Mock()
                    workflow.implement.side_effect = h.ModelRequestError(400)
                    return m.ModelExecutor(workflow).execute(proposal, context, repo)
                return super().execute(proposal, context, repo)
        self.run_loop(planner, Transport([ADD_OK]))
        failure = planner.calls[1][0]['recovery_evidence']
        self.assertEqual(failure['classification'], 'EXECUTOR_REQUEST_FAILED')
        self.assertIn('HTTP 400', failure['detail'])
        self.assertFalse(failure['implementation_changed'])
        self.assertEqual(self.state()['status'], 'COMPLETED')

    def test_malformed_role_wire_output_replans_without_accepting_model_claims(self):
        self.make(budgets=LOOSE, reviewer=True)
        planner = RepairPlanner([h.ModelResponseError('Malformed chat response'), step(reviewer=True)])
        self.run_loop(planner, Executor([ADD_OK, ADD_OK_DOC]),
                      reviewer=Reviewer([h.ModelResponseError('Model returned empty or non-text content'), (True, 'ok')]))
        self.assertEqual([r['reason'] for r in self.rounds()], ['INVALID_MANAGER_OUTPUT', 'REVIEWER_MALFORMED', None])
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(self.dirs(), ['0003.json'])

    def test_critic_timeout_and_rejection_replan_without_lowering_risk_floor(self):
        self.make(budgets=LOOSE, critic=True)
        planner = RepairPlanner([step(risk='medium'), step(risk='low')])
        self.run_loop(planner, Executor([ADD_OK, ADD_OK_DOC]), Critic([TimeoutError(), clean_critic()]))
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(planner.calls[1][0]['recovery_evidence']['classification'], 'CRITIC_TIMEOUT')
        self.assertEqual(self.rounds()[1]['policy']['risk'], 'medium')
        self.assertEqual(self.rounds()[1]['policy']['critic'], 'run')

    def test_reviewer_timeout_replans_and_retains_all_three_model_gates(self):
        self.make(budgets=LOOSE, critic=True, reviewer=True, review_policy='three-model')
        planner = RepairPlanner([step()])
        self.run_loop(planner, Executor([ADD_OK, ADD_OK_DOC]), Critic([clean_critic()] * 2),
                      Reviewer([TimeoutError(), (True, 'approved')]))
        self.assertEqual(planner.calls[1][0]['recovery_evidence']['classification'], 'REVIEWER_TIMEOUT')
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(len(self.rounds()[1]['evidence']['verifier']), 2)
        self.assertEqual(self.rounds()[1]['policy']['reviewer'], 'required')
        self.assertEqual(self.dirs(), ['0002.json'])

    def test_collection_failure_extracts_targeted_check_and_preserves_acceptance(self):
        self.make(budgets={**LOOSE, 'max_rounds': 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        commands = [command_observation({'argv': ['python', '-m', 'pytest', '-q'], 'exit_code': 2,
                                        'stdout': 'ERROR collecting tests/test_calc.py\nSyntaxError: unterminated string',
                                        'stderr': '', 'duration_seconds': 0.2})]
        self.assertEqual(failed_tests(commands), ['tests/test_calc.py'])
        self.assertEqual(targeted_check(failed_tests(commands)), ['python', '-m', 'pytest', 'tests/test_calc.py', '-q'])
        self.assertEqual(self.state()['manager']['checks'][0]['argv'], CHECK1)

    def test_reviewer_malformed_response_is_untrusted_replan_not_crash(self):
        self.make(budgets=LOOSE, reviewer=True)
        planner = RepairPlanner([step(reviewer=True)])
        self.run_loop(planner, Executor([ADD_OK, ADD_OK_DOC]), reviewer=Reviewer([ValueError('unsafe model output'), (True, 'ok')]))
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(planner.calls[1][0]['recovery_evidence']['classification'], 'REVIEWER_MALFORMED')
        self.assertNotIn('unsafe model output', json.dumps(planner.calls[1][0]))

    def test_repair_cannot_drop_required_reviewer_on_retained_rejected_candidate(self):
        self.make(budgets=LOOSE, reviewer=True)
        planner = RepairPlanner([step(reviewer=True), step(reviewer=False)])
        reviewer = Reviewer([(False, 'Repair the candidate'), (True, 'accepted repair')])
        self.run_loop(planner, Executor([ADD_OK, ADD_OK_DOC]), reviewer=reviewer)
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(len(reviewer.calls), 2)
        self.assertEqual(self.rounds()[1]['policy']['reviewer'], 'required')

    def critic_context(self, context, proposal):
        store = self.reload()
        repo = h.Repository(self.path, store.state["options"]["config"], None)
        loop = m.ManagerLoop(store, None, repo, self.gateway, None, None)
        reference = store.state["evidence"]["verifier"][-1]
        context["critic_packet"] = loop.critic_packet(proposal, context, {"reference": reference}, repo.diff(), ["calc.py"])
        return context

    def test_review_adapters_use_controller_evidence_instead_of_executor_history(self):
        self.make(budgets={**LOOSE, 'max_rounds': 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        context = m.build_context(self.reload(), role='executor', step=step())
        context['recovery_evidence']['detail'] = 'OPAQUE_FAILURE_MARKER'
        self.critic_context(context, step())
        workflow = Mock()
        m.ModelCritic(workflow).critique(step(), context, 'current check passed', 'current diff')
        m.ModelReviewer(workflow).review(step(), context, 'current check passed', 'current diff', None)
        packet = workflow.critique.call_args.kwargs['packet']
        self.assertIn(self.state()['original_task'], json.dumps(packet))
        self.assertNotIn('OPAQUE_FAILURE_MARKER', json.dumps(packet))
        self.assertEqual(packet['recovery']['classification'], 'VERIFICATION_FAILED')
        self.assertNotIn('OPAQUE_FAILURE_MARKER', workflow.review.call_args.args[0])
        self.assertIn(self.state()['original_task'], workflow.review.call_args.args[0])

    def test_executor_receives_latest_binding_and_new_strategy_for_same_goal(self):
        self.make(budgets={**LOOSE, 'max_rounds': 1})
        self.run_loop(Planner([step()]), Executor([wrong(1)]))
        context = m.build_context(self.reload(), role='executor', step=step())
        failure = context['recovery_evidence']
        proposal = {**step(), 'rationale': 'NEW_STRATEGY: inspect signed result and run targeted negative check',
                    'recovery_from': {'round': failure['round'], 'fingerprint': failure['fingerprint']}}
        workflow = Mock()
        m.ModelExecutor(workflow).execute(proposal, context, None)
        prompt = workflow.implement.call_args.args[0]
        self.assertIn('Current episode: evidence-bound recovery', prompt)
        self.assertIn('Validated recovery_from', prompt)
        self.assertIn(json.dumps(proposal['recovery_from']), prompt)
        self.assertIn('NEW_STRATEGY', prompt)
        self.assertIn('untrusted proposal', prompt)
        self.assertIn('Stable task contract sha256: '+context['task_contract_sha256'], prompt)
        self.assertIs(workflow.implement.call_args.kwargs['episode_context'], context)
        self.critic_context(context, proposal)
        for adapter in (m.ModelCritic(workflow), m.ModelReviewer(workflow)):
            if isinstance(adapter, m.ModelCritic): adapter.critique(proposal, context, 'checks', 'diff')
            else: adapter.review(proposal, context, 'checks', 'diff', None)
        self.assertNotIn('NEW_STRATEGY', json.dumps(workflow.critique.call_args.kwargs['packet']))
        self.assertNotIn('NEW_STRATEGY', workflow.review.call_args.args[0])


class EvidenceBoundsTests(unittest.TestCase):
    def test_recovery_identity_separates_content_from_material_context(self):
        proposal = step()
        evidence = {'round': 1, 'fingerprint': 'f'*64, 'commands': [{'argv': CHECK, 'exit_code': 1,
            'stdout': '1 failed in 0.21s', 'stderr': 'AssertionError: expected 1', 'duration_seconds': 1}]}
        workspace = {'files': {'calc.py': {'sha256': 'a'}, 'other.py': {'sha256': 'b'}}}
        identity = m.recovery_attempt_identity(proposal, evidence, workspace, None)
        cosmetic = {**proposal, 'step_id': 'renamed', 'rationale': 'new words', 'recovery_from': {'round': 4, 'fingerprint': 'f'*64}}
        timed = {**evidence, 'round': 4, 'commands': [{**evidence['commands'][0], 'stdout': '1 failed in 9.84s', 'duration_seconds': 10}]}
        unrelated = {'files': {**workspace['files'], 'other.py': {'sha256': 'c'}}}
        self.assertEqual(identity, m.recovery_attempt_identity(cosmetic, timed, unrelated, None))
        tools = {'fingerprint': 'f'*64, 'tool_failures': [{'action': 'replace_text', 'path': 'calc.py', 'classification': 'anchor_not_found'}]}
        alias = {**tools, 'tool_failures': [{**tools['tool_failures'][0], 'path': './calc.py'}]}
        self.assertEqual(m.recovery_state_fingerprint(tools), m.recovery_state_fingerprint(alias))
        for changed, files, checkpoint in (({**evidence, 'fingerprint': 'e'*64}, workspace, None),
                ({**evidence, 'commands': [{**evidence['commands'][0], 'stderr': 'SyntaxError'}]}, workspace, None),
                (evidence, {'files': {**workspace['files'], 'calc.py': {'sha256': 'd'}}}, None),
                (evidence, workspace, {'sha256': 'verified', 'path': 'checkpoint.json'})):
            with self.subTest(changed=changed, checkpoint=checkpoint):
                self.assertNotEqual(identity, m.recovery_attempt_identity(proposal, changed, files, checkpoint))

    def test_live_unittest_failure_summary_is_not_a_failed_test_name(self):
        # Recorded real Qwen smoke output, 2026-10-01; this is verifier text, not model claims.
        commands = [{'exit_code': 1, 'stdout': '', 'stderr':
                     'FAIL: test_add (test_calc.AdditionTest.test_add)\n'
                     'AssertionError: -1 != 5\nRan 1 test in 0.000s\nFAILED (failures=1)\n'}]
        self.assertEqual(failed_tests(commands), ['test_add'])

    def test_http_error_preserves_status_without_backend_body_or_startup_reclassification(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        gateway = h.Gateway({'base_url': 'http://127.0.0.1:9292'}, h.EventLog(Path(temp.name)/'events.jsonl'))
        error = urllib.error.HTTPError('http://127.0.0.1:9292/v1/chat/completions', 400, 'bad', {}, io.BytesIO(b'OPAQUE_PROMPT'))
        with patch('urllib.request.urlopen', side_effect=error), self.assertRaises(h.ModelRequestError) as caught:
            gateway.request('POST', '/v1/chat/completions', {})
        self.assertEqual(caught.exception.status, 400)
        self.assertNotIn('OPAQUE_PROMPT', str(caught.exception))
        startup = urllib.error.HTTPError('http://127.0.0.1:9292/upstream/llm-code/health', 500, 'bad', {}, io.BytesIO(b'startup failed'))
        with patch('urllib.request.urlopen', side_effect=startup), self.assertRaises(RuntimeError) as caught:
            gateway.request('GET', '/upstream/llm-code/health')
        self.assertNotIsInstance(caught.exception, h.ModelRequestError)

    def test_large_failure_shrinks_valid_json_without_losing_binding(self):
        value = {'schema_version': 1, 'round': 3, 'fingerprint': 'f'*64, 'step_id': 'repair',
                 'trusted_progress': False, 'detail': 'x'*1500,
                 'commands': [command_observation({'argv': ['python'] + ['a'*200]*31, 'exit_code': 2,
                              'stdout': 'FAILED test_calc.py\n'+'x'*5000, 'stderr': 'y'*5000})]*3,
                 'attempted_actions': [{'action': 'write_file', 'path': 'a'*180, 'index': n} for n in range(16)],
                 'tool_failures': [{'path': 'a'*180, 'safe_reason': 'b'*240, 'classification': 'anchor_not_found'}]*8,
                 'changed_paths': ['a'*180]*20, 'failed_tests': ['test_calc.py::'+'n'*160]*10,
                 'last_successful_observation': {'kind': 'read_file', 'path': 'p'*180}}
        for limit in (6500, 2600):
            with self.subTest(limit=limit):
                result = shrink_failure(value, limit)
                self.assertLessEqual(len(json.dumps(result)), limit)
                self.assertEqual((result['round'], result['fingerprint']), (3, 'f'*64))
                self.assertTrue(result['evidence_truncated'])
                self.assertEqual(result['commands'][0]['exit_code'], 2)
        self.assertEqual(len(value['commands']), 3, 'reduction never mutates stored evidence')

    def test_output_bound_redaction_and_timeout_classification(self):
        result = command_observation({'argv': CHECK, 'exit_code': None, 'stdout': 'password=secret\n'+'x'*5000,
                                      'stderr': 'y'*5000, 'duration_seconds': 0.5})
        self.assertTrue(result['output_truncated'])
        self.assertLessEqual(len(result['stdout']), 800)
        self.assertIn('password=[redacted]', result['stdout'])
        self.assertNotIn('secret', result['stdout'])
        self.assertTrue(request_timed_out(urllib.error.URLError(TimeoutError())))
        self.assertFalse(request_timed_out(urllib.error.URLError('connection refused')))


if __name__ == '__main__':
    unittest.main()
