"""Strict production planner contract, correction budget and trust regressions."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import harness as h
import manager as m
import planner_protocol as pp
from test_manager import ManagerCase, Planner, Executor, Reviewer, Forbidden, step, wrong, ADD_OK, CHECK1, LOOSE

CHECKS = [{'id': 'check-1', 'argv': CHECK1}]
SCOPE = {'allowed_paths': ['calc.py'], 'forbidden_paths': ['test_add.py']}


def failure():
    return {'round': 1, 'fingerprint': 'f' * 64, 'step_id': 's1',
            'step_fingerprint': m.step_fingerprint(step())}


def repair():
    return {**step('repair', goal='Inspect calc.py and repair observed addition assertion'),
            'recovery_from': {'round': 1, 'fingerprint': 'f' * 64}}


def parse(value, **kwargs):
    return m.parse_step(value, checks=CHECKS, proven=[], run_scope=SCOPE, **kwargs)


class PlannerContractTests(unittest.TestCase):
    def reject(self, value, classification, **kwargs):
        with self.assertRaises(m.StepError) as caught:
            parse(value, **kwargs)
        self.assertEqual(caught.exception.classification, classification)

    def test_initial_and_recovery_plans_pass_same_canonical_schema(self):
        for value, evidence in ((step(), None), (repair(), failure())):
            with self.subTest(recovery=bool(evidence)):
                parsed = parse(json.dumps(value), recovery=evidence)
                pp.validate_shape(parsed, pp.schema({'checks': CHECKS, 'recovery_evidence': evidence}))

    def test_missing_binding_field_set_failure_has_precise_new_diagnostic(self):
        self.reject(step(), 'missing_recovery_from', recovery=failure())

    def test_bad_recovery_binding_and_malformed_binding_are_explicit(self):
        for binding, classification in (({'round': 1, 'fingerprint': 'e'*64}, 'wrong_recovery_fingerprint'),
                                        ({'round': 2, 'fingerprint': 'f'*64}, 'wrong_recovery_round'),
                                        ({'round': True, 'fingerprint': 'f'*64}, 'invalid_field_type'),
                                        (None, 'invalid_field_type')):
            self.reject({**repair(), 'recovery_from': binding}, classification, recovery=failure())

    def test_same_goal_with_exact_binding_is_schema_valid_but_rename_keeps_content_fingerprint(self):
        for name in ('s1', 'renamed'):
            value = {**step(name, goal='  IMPLEMENT via S1 '), 'rationale': 'different rationale',
                     'recovery_from': repair()['recovery_from']}
            parsed = parse(value, recovery=failure())
            self.assertEqual(m.step_fingerprint(parsed), failure()['step_fingerprint'])

    def test_risk_floor_and_required_reviewer_are_strict(self):
        self.reject(repair(), 'risk_floor_violation', recovery=failure(), risk_floor='medium')
        self.reject(repair(), 'reviewer_floor_violation', recovery=failure(), reviewer_required=True)
        parse({**repair(), 'risk': 'medium', 'needs_reviewer': True}, recovery=failure(),
              risk_floor='medium', reviewer_required=True)

    def test_json_is_not_salvaged_from_wrappers_reasoning_nested_or_trailing_text(self):
        for raw in ('```json\n'+json.dumps(step())+'\n```', '<think>secret</think>'+json.dumps(step()),
                    json.dumps(step())+' trailing', '{"nested":'+json.dumps(step())+'}',
                    '{"goal":"x", "goal":"y"}', '{"goal":NaN}', '{'):
            with self.subTest(raw=raw), self.assertRaises(m.StepError):
                parse(raw)

    def test_truncated_even_complete_json_is_refused_before_schema(self):
        self.reject(h.PlannerResponse(json.dumps(step()), {'finish_reason': 'length'}), 'truncated_response')
        self.reject(h.PlannerResponse(json.dumps(step()), {'finish_reason': 'tool_calls'}), 'incomplete_wire_response')

    def test_prompt_wire_parser_share_exact_dynamic_schema_and_safe_metadata(self):
        class Gateway(h.Gateway):
            def switch(self, role): pass
            def check_active_ram(self): pass
            def request(self, method, path, payload=None, timeout=10):
                self.payload = payload
                return {'choices': [{'finish_reason': 'stop', 'message': {
                    'content': json.dumps(repair()), 'reasoning_content': 'SECRET_REASONING'}}],
                    'usage': {'prompt_tokens': 321, 'completion_tokens': 123, 'secret': 'SECRET_USAGE'}}
        context = {'checks': CHECKS, 'recovery_evidence': failure()}
        with tempfile.TemporaryDirectory() as target:
            log = h.EventLog(Path(target)/'events.jsonl')
            gateway = Gateway({'roles': {'code': 'llm-code'}, 'request_timeout_seconds': 10}, log)
            raw = m.ModelPlanner(gateway).plan(context, 'executor')
            expected = pp.schema(context)
            self.assertEqual(gateway.payload['response_format']['json_schema']['schema'], expected)
            self.assertEqual(json.loads(gateway.payload['messages'][0]['content'].split('Canonical output schema: ')[1]), expected)
            self.assertEqual(raw.diagnostic['schema_sha256'], pp.schema_hash(expected))
            self.assertEqual(raw.diagnostic['finish_reason'], 'stop')
            self.assertEqual(raw.diagnostic['completion_tokens'], 123)
            parse(raw, recovery=failure())
            records = log.path.read_text()
            self.assertNotIn('SECRET_', records)
            self.assertNotIn('executor_response', records)
            self.assertIn('planner_response', records)

    def test_structural_preview_has_only_known_fields_and_counts(self):
        raw = {**step(), 'SECRET_KEY': 'SECRET_VALUE'}
        diagnostic = pp.structural_diagnostic(raw)
        self.assertEqual(diagnostic['unexpected_field_count'], 1)
        self.assertNotIn('SECRET', json.dumps(diagnostic))


class PlannerCorrectionTests(ManagerCase):
    def correcting(self, values):
        planner = Planner(values)
        planner.supports_correction = True
        return planner

    def test_one_correction_succeeds_inside_same_round_with_fresh_context(self):
        self.make(budgets={**LOOSE, 'max_rounds': 1})
        planner = self.correcting([{'SECRET_KEY': 'SECRET_BODY'}, step()])
        executor = Executor([ADD_OK])
        self.run_loop(planner, executor)
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(len(planner.calls), 2)
        first, second = [c[0] for c in planner.calls]
        self.assertEqual(first['task_contract_sha256'], second['task_contract_sha256'])
        self.assertEqual(second['plan_correction']['classification'], 'missing_fields')
        self.assertNotIn('SECRET', json.dumps(second))
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(list((self.store.directory/'rounds/0001').glob('plan-executor-*.json'))), 2)

    def test_repeated_invalid_plans_stop_at_two_calls_per_round_without_trust(self):
        self.make(budgets={**LOOSE, 'max_rounds': 2})
        planner = self.correcting([{}]*4)
        self.run_loop(planner, Forbidden('executor'))
        self.assertEqual(len(planner.calls), 4)
        self.assertEqual(self.state()['status'], 'BUDGET_EXHAUSTED')
        self.assertEqual(self.state()['verified_progress'], [])
        self.assertIsNone(self.state()['last_verified_checkpoint'])
        self.assertTrue(all(r['implementation']=='not_started' for r in self.rounds()))

    def test_bad_recovery_binding_corrected_from_exact_failure_no_new_round(self):
        self.make(budgets={**LOOSE, 'max_rounds': 2})
        class Correcting(Planner):
            supports_correction = True
            def plan(inner, context, tier):
                inner.calls.append((copy.deepcopy(context), tier))
                if 'recovery_evidence' not in context: return step()
                f = context['recovery_evidence']
                value = step('repair', goal='Inspect and fix the observed addition assertion')
                if context.get('plan_correction'):
                    value['recovery_from'] = {'round': f['round'], 'fingerprint': f['fingerprint']}
                return value
        planner = Correcting([])
        executor = Executor([wrong(1), ADD_OK])
        self.run_loop(planner, executor)
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(len(planner.calls), 3)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual([r['trusted'] for r in self.rounds()], [False, True])
        self.assertNotEqual(planner.calls[1][0]['recovery_evidence']['commands'][0]['exit_code'], 0)
        self.assertEqual(planner.calls[2][0]['plan_correction']['classification'], 'missing_recovery_from')

    def test_correction_cannot_bypass_invocation_budget(self):
        self.make(budgets={**LOOSE, 'max_model_invocations': {'code': 1}})
        planner = self.correcting([{}, step()])
        self.run_loop(planner, Forbidden('executor'))
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(self.state()['manager']['stop']['reason'], 'max_model_invocations:code')
        self.assertEqual(self.state()['verified_progress'], [])

    def test_senior_correction_retains_high_risk_before_any_executor_dispatch(self):
        self.make(budgets={**LOOSE, 'max_rounds': 1}, reviewer=True)
        planner = self.correcting([step(risk='high'), step(risk='low'), step(risk='high')])
        executor = Executor([ADD_OK])
        self.run_loop(planner, executor, reviewer=Reviewer([(True, 'approved')]))
        self.assertEqual(self.state()['status'], 'COMPLETED')
        self.assertEqual(planner.tiers(), ['executor', 'reviewer', 'reviewer'])
        self.assertEqual(planner.calls[2][0]['plan_correction']['classification'], 'risk_floor_violation')
        self.assertEqual(planner.calls[2][0]['planning_constraints']['risk_floor'], 'high')
        self.assertEqual(self.rounds()[0]['risk'], 'high')
        self.assertEqual(len(executor.calls), 1)
