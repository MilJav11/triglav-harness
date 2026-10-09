"""Real Gateway protocol and CriticExecutor evidence with deterministic wire replies."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import critic
import critic_executor as ce
import durable
import harness
import test_critic_wave5 as _wave5
from test_critic_wave5 import setup_test_fixture, build_verifier_registry

CLEAN = json.dumps(dict(findings=[], uncertainties=[], evidence_reviewed=['verifier passed'], summary='No concrete defect found.'))


class TestCriticCompletion(unittest.TestCase):
    _create_milestone_ready_run = _wave5.TestCriticWave5._create_milestone_ready_run

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.registry = build_verifier_registry()
        self.store = self._create_milestone_ready_run('completion')
        self.gateway = harness.Gateway(self.config, harness.EventLog(self.root / 'wire.jsonl'))
        self.switch = self.enterContext(patch.object(self.gateway, 'switch'))
        self.unload = self.enterContext(patch.object(self.gateway, 'unload'))
        self.enterContext(patch.object(self.gateway, 'check_active_ram'))

    def run_wire(self, reason, content=CLEAN, tokens=120):
        wire = dict(choices=[dict(finish_reason=reason, message=dict(content=content))],
                    usage=dict(prompt_tokens=1200, completion_tokens=tokens, total_tokens=1200 + tokens))
        with patch.object(self.gateway, 'request', return_value=wire) as request:
            result = ce.CriticExecutor(self.store, self.repo, self.config, gateway=self.gateway).execute()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(self.store.read_evidence(result['artifact_ref'])['response_metadata'], result['response_metadata'])
        self.assertEqual(result['response_metadata']['finish_reason'], reason)
        record = next(iter(self.store.state['manager']['critic_executions'].values()))
        self.assertEqual(record['attempts'][-1]['response_metadata'], result['response_metadata'])
        self.assertEqual(record['attempts'][-1]['status'], 'finished')
        self.assertEqual(result['attempt_count'], 1)
        self.assertGreaterEqual(self.unload.call_count, 1)
        loaded = durable.Store(self.root / 'runs', 'completion')
        loaded.load()
        self.assertEqual(loaded.state['manager']['critic_review']['response_metadata'], result['response_metadata'])
        self.assertFalse(loaded.state['verified_progress'])
        self.assertFalse(list((loaded.directory / 'checkpoints').glob('*.json')))
        return result, request.call_args.args[2]

    def test_stop_valid_response_accepted_and_durable(self):
        result, _ = self.run_wire('stop')
        self.assertEqual(result['status'], ce.CRITIC_CLEAN)
        self.assertEqual(result['response_metadata']['completion_tokens'], 120)

    def test_length_incomplete_response_fails_closed_with_reason(self):
        result, _ = self.run_wire('length', '{"findings":[', 1536)
        self.assertEqual(result['status'], ce.CRITIC_FAILED)
        self.assertIn('length', result['raw_diagnostics'])
        self.assertEqual(result['response_metadata']['completion_tokens'], 1536)

    def test_unknown_nonstop_reason_preserved_exactly(self):
        result, _ = self.run_wire('runtime_custom_stop')
        self.assertEqual(result['status'], ce.CRITIC_FAILED)

    def test_stop_truncated_json_fails_closed(self):
        result, _ = self.run_wire('stop', '{"findings":[],"summary":')
        self.assertEqual(result['status'], ce.CRITIC_UNPARSEABLE)

    def test_stop_malformed_schema_fails_closed(self):
        result, _ = self.run_wire('stop', '{"findings":[],"summary":"x","verified":true}')
        self.assertEqual(result['status'], ce.CRITIC_UNPARSEABLE)

    def test_empty_content_retains_length_metadata(self):
        result, _ = self.run_wire('length', '', 1536)
        self.assertEqual(result['status'], ce.CRITIC_FAILED)

    def test_nonstop_complete_json_still_rejected(self):
        result, _ = self.run_wire('length')
        self.assertEqual(result['status'], ce.CRITIC_FAILED)

    def test_clean_response_budget_and_concise_contract(self):
        result, payload = self.run_wire('stop')
        self.assertEqual(payload['max_tokens'], critic.CRITIC_MAX_OUTPUT_TOKENS)
        self.assertEqual(payload['max_tokens'], 1536)
        self.assertLess(result['response_metadata']['completion_tokens'], payload['max_tokens'])
        self.assertLess(len(CLEAN.encode('utf-8')), payload['max_tokens'])
        self.assertIn('at most two findings', payload['messages'][0]['content'])
        self.assertIn('120 characters', payload['messages'][0]['content'])
        self.assertEqual(payload['response_format']['json_schema']['schema'], critic.CRITIC_SCHEMA)

    def test_transport_retry_remains_bounded_and_metadata_per_attempt(self):
        wire = dict(choices=[dict(finish_reason='stop', message=dict(content=CLEAN))])
        with patch.object(self.gateway, 'request', side_effect=[ConnectionError('reset'), wire]) as request:
            result = ce.CriticExecutor(self.store, self.repo, self.config, gateway=self.gateway).execute()
        self.assertEqual(result['status'], ce.CRITIC_CLEAN)
        self.assertEqual(request.call_count, 2)
        record = next(iter(self.store.state['manager']['critic_executions'].values()))
        self.assertEqual([a['status'] for a in record['attempts']], ['retryable', 'finished'])
        self.assertFalse(record['attempts'][0]['response_metadata']['response_received'])
        self.assertEqual(record['attempts'][1]['response_metadata']['finish_reason'], 'stop')
        artifact = self.store.read_evidence(result['artifact_ref'])
        self.assertEqual(artifact['attempts'], record['attempts'])

    def test_cleanup_failure_preserves_completion_reason(self):
        self.unload.side_effect = TimeoutError('cleanup failed')
        result, _ = self.run_wire('length', '{"findings":[', 1536)
        self.assertEqual(result['parse_status'], 'cleanup_failed')

    def test_cleanup_failure_after_valid_response_preserves_stop(self):
        self.unload.side_effect = TimeoutError('cleanup failed')
        result, _ = self.run_wire('stop')
        self.assertEqual(result['parse_status'], 'cleanup_failed')
        self.assertEqual(result['status'], ce.CRITIC_FAILED)

    def test_missing_finish_reason_rejected_and_identified(self):
        wire = dict(choices=[dict(message=dict(content=CLEAN))])
        with patch.object(self.gateway, 'request', return_value=wire) as request:
            result = ce.CriticExecutor(self.store, self.repo, self.config, gateway=self.gateway).execute()
        self.assertEqual(request.call_count, 1)
        self.assertEqual(result['status'], ce.CRITIC_FAILED)
        self.assertIsNone(result['response_metadata']['finish_reason'])
        self.assertFalse(result['response_metadata']['finish_reason_present'])
        self.assertEqual(self.store.read_evidence(result['artifact_ref'])['response_metadata'], result['response_metadata'])
