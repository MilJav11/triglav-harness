"""V0.1.2 regressions for controller-built changed-code review evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import candidate_evidence as code
import critic_executor as ce
import durable
import manager
import reviewer_executor as re
import work_unit_scheduler as wus
from test_reviewer_wave6 import setup_test_fixture, make_spec, build_verifier_registry


class TestCandidateEvidence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo, self.store, self.config = setup_test_fixture(self.root)
        self.config['critic']['max_input_chars'] = 16000
        self.config['reviewer']['max_input_chars'] = 20000
        self.registry = build_verifier_registry()

    def ready(self, writes=None, cap=None):
        if cap is not None:
            self.config['candidate_evidence'] = {'max_diff_chars': cap}
        writes = writes or {'calculator.py': 'def multiply(a, b):\n    return a * b\n'}
        spec = make_spec(paths=sorted(writes))
        manager.create_run(self.store, self.repo, 'Implement multiplication with no minimum value',
                           [['python', '-m', 'unittest', 'test_calculator.py']], self.config,
                           criteria=['Multiply works'], work_units=[spec],
                           verifier_registry=self.registry, critic=True, reviewer=True)
        worker = wus.ScriptedExecutor()
        worker.set_behavior('unit-1', {'writes': writes})
        scheduler = wus.WorkUnitScheduler(self.store, self.repo, verifier_registry=self.registry,
            agents=manager.Agents(None, worker, None, None),
            parent_scope={'allowed_paths': sorted(writes), 'forbidden_paths': []})
        self.assertEqual(scheduler.run_sequence()['status'], 'MILESTONE_READY')

    def critic(self, adapter=None):
        return ce.CriticExecutor(self.store, self.repo, self.config,
                                critic_adapter=adapter or ce.ScriptedCritic('clean')).execute()

    def reviewer(self, adapter=None):
        return re.ReviewerExecutor(self.store, self.repo, self.config,
                                  reviewer_adapter=adapter or re.ScriptedReviewer('approve')).execute()

    def test_both_packets_contain_exact_changed_code_and_critic_concern(self):
        self.ready({'calculator.py': 'def multiply(a, b):\n    return a * b\n',
                    'discounts.py': 'def discount(subtotal):\n    return subtotal // 10 if subtotal >= 100 else 0\n'})
        before = wus.capture_fs_snapshot(self.repo.root)
        critic_packet = ce.build_critic_packet(self.store, self.repo, self.config)
        finding = {'finding_id': 'edge', 'severity': 'MAJOR', 'title': 'Concern',
                   'description': 'Check small values', 'confidence': 'HIGH', 'evidence_refs': ['candidate_diff']}
        result = self.critic(lambda packet: json.dumps({'findings': [finding], 'summary': 'Concern'}))
        reviewer_packet = re.build_reviewer_packet(self.store, self.repo, self.config)
        for packet, size, limit in ((critic_packet, ce._input_size, 16000),
                                    (reviewer_packet, re._input_size, 20000)):
            self.assertIn('return a * b', packet['candidate_diff'])
            self.assertIn('subtotal >= 100', packet['candidate_diff'])
            self.assertEqual(packet['candidate_evidence']['changed_files'], ['calculator.py', 'discounts.py'])
            self.assertTrue(packet['candidate_evidence']['complete'])
            self.assertLessEqual(size(packet), limit)
            self.assertEqual(packet['candidate_binding']['candidate_evidence_digest'],
                             packet['candidate_evidence']['digest'])
        self.assertIn('candidate_evidence.py', self.store.state['environment']['source'])
        self.assertEqual(critic_packet['candidate_diff'], reviewer_packet['candidate_diff'])
        self.assertEqual(critic_packet['candidate_evidence'], reviewer_packet['candidate_evidence'])
        self.assertEqual(critic_packet['candidate_binding'], reviewer_packet['candidate_binding'])
        self.assertEqual(reviewer_packet['critic_evidence']['findings'], result['findings'])
        self.reviewer()
        self.assertEqual(before, wus.capture_fs_snapshot(self.repo.root))

    def test_changed_diff_invalidates_both_caches_without_snapshot_change(self):
        self.ready()
        first = self.critic()
        previous = self.reviewer()
        original = self.repo.diff
        with patch.object(self.repo, 'diff', side_effect=lambda **kw: original(**kw) + '\n+changed evidence\n'):
            self.assertTrue(ce.is_critic_stale(self.store, self.repo, self.config))
            self.assertTrue(re.is_reviewer_stale(self.store, self.repo, self.config))
            critic_adapter, reviewer_adapter = ce.ScriptedCritic('clean'), re.ScriptedReviewer('approve')
            second = self.critic(critic_adapter)
            current = self.reviewer(reviewer_adapter)
            self.assertNotEqual(first['candidate_evidence_digest'], second['candidate_evidence_digest'])
            self.assertNotEqual(previous['candidate_evidence_digest'], current['candidate_evidence_digest'])
            self.assertEqual(len(critic_adapter.calls), 1)
            self.assertEqual(len(reviewer_adapter.calls), 1)

    def test_explicit_truncation_cannot_approve_or_checkpoint(self):
        self.ready(cap=100)
        packet = ce.build_critic_packet(self.store, self.repo, self.config)
        self.assertIn('[TRUNCATED CANDIDATE DIFF:', packet['candidate_diff'])
        self.assertLessEqual(len(packet['candidate_diff']), 100)
        self.assertFalse(packet['candidate_evidence']['complete'])
        self.critic()
        reviewer_packet = re.build_reviewer_packet(self.store, self.repo, self.config)
        self.assertEqual(packet['candidate_evidence'], reviewer_packet['candidate_evidence'])
        self.assertEqual(self.reviewer()['decision'], 'INCONCLUSIVE')
        with self.assertRaises(re.ReviewerExecutionError):
            re.execute_final_verification(self.store, self.repo, self.config)
        self.assertIsNone(self.store.state.get('last_verified_checkpoint'))

    def test_unseen_truncated_tail_changes_digest(self):
        self.ready(cap=100)
        original = self.repo.diff
        with patch.object(self.repo, 'diff', side_effect=lambda **kw: original(**kw) + 'A' * 1000):
            first = ce.build_critic_packet(self.store, self.repo, self.config)
        with patch.object(self.repo, 'diff', side_effect=lambda **kw: original(**kw) + 'B' * 1000):
            second = ce.build_critic_packet(self.store, self.repo, self.config)
        self.assertEqual(first['candidate_diff'], second['candidate_diff'])
        self.assertNotEqual(first['candidate_evidence']['digest'], second['candidate_evidence']['digest'])

    def test_redaction_precedes_excerpt_and_digest_matches_delivered_evidence(self):
        self.ready()
        original = self.repo.diff
        secrets = '\n+API_KEY=SYNTHETIC_KEY\n+password="SYNTHETIC_PASSWORD"\n+Authorization: Bearer SYNTHETIC_BEARER\n+sk-proj-12345678901234567890\n'
        with patch.object(self.repo, 'diff', side_effect=lambda **kw: original(**kw) + secrets):
            self.critic()
            for packet in (ce.build_critic_packet(self.store, self.repo, self.config),
                           re.build_reviewer_packet(self.store, self.repo, self.config)):
                text = json.dumps(packet)
                for secret in ('SYNTHETIC_KEY', 'SYNTHETIC_PASSWORD', 'SYNTHETIC_BEARER', 'sk-proj-12345678901234567890'):
                    self.assertNotIn(secret, text)
                manifest = dict(packet['candidate_evidence'])
                saved_digest = manifest.pop('digest')
                self.assertEqual(saved_digest, durable.digest({'manifest': manifest, 'candidate_diff': packet['candidate_diff']}))
        self.assertEqual(ce.sanitize_evidence(secrets), ce.sanitize_evidence(ce.sanitize_evidence(secrets)))
        name = 'API_KEY=SYNTHETIC_FILENAME.py'
        state = copy.deepcopy(self.store.state)
        state['manager']['scope']['allowed_paths'].append(name)
        with patch.object(self.repo, 'diff', return_value='+' + name + '\n'):
            manifest, diff = code.build_candidate_evidence(state, self.repo, durable.snapshot(self.repo),
                                                          [name], ce.sanitize_evidence, self.config)
        delivered = ce.sanitize_evidence({'manifest': manifest, 'candidate_diff': diff})
        saved_digest = delivered['manifest'].pop('digest')
        self.assertNotIn('SYNTHETIC_FILENAME', json.dumps(delivered))
        self.assertEqual(saved_digest, durable.digest(delivered))


    def test_small_packet_budget_refuses_model_invocation(self):
        self.ready()
        cfg = copy.deepcopy(self.config)
        cfg['critic']['max_input_chars'] = 100
        adapter = ce.ScriptedCritic('clean')
        with self.assertRaises(ce.CriticExecutionError):
            ce.CriticExecutor(self.store, self.repo, cfg, critic_adapter=adapter).execute()
        self.assertFalse(adapter.calls)
        self.critic()
        cfg['reviewer']['max_input_chars'] = 100
        adapter = re.ScriptedReviewer('approve')
        with self.assertRaises(re.ReviewerExecutionError):
            re.ReviewerExecutor(self.store, self.repo, cfg, reviewer_adapter=adapter).execute()
        self.assertFalse(adapter.calls)

    def test_checkpoint_binds_both_reviews_final_verifier_and_candidate_evidence(self):
        self.ready()
        critic = self.critic()
        reviewer = self.reviewer()
        final = re.execute_final_verification(self.store, self.repo, self.config)
        checkpoint = re.create_trusted_checkpoint(self.store, self.repo, final, self.config)['checkpoint']
        self.assertEqual(critic['candidate_evidence_digest'], reviewer['candidate_evidence_digest'])
        self.assertEqual(checkpoint['candidate_evidence_digest'], reviewer['candidate_evidence_digest'])
        self.assertEqual(final['evidence']['candidate_evidence_digest'], reviewer['candidate_evidence_digest'])
        durable.validate_checkpoint_evidence(self.store, checkpoint)
        altered = copy.deepcopy(checkpoint)
        altered['candidate_evidence_digest'] = '0' * 64
        with self.assertRaises(durable.DurableError):
            durable.validate_checkpoint_evidence(self.store, altered)
        altered = copy.deepcopy(checkpoint)
        altered['snapshot']['head'] = 'different'
        with self.assertRaises(durable.DurableError):
            durable.validate_checkpoint_evidence(self.store, altered)

    def test_unauthorized_changes_are_never_included(self):
        self.ready()
        candidate = durable.snapshot(self.repo)
        with self.assertRaises(durable.DurableError):
            code.build_candidate_evidence(self.store.state, self.repo, candidate,
                                          ['test_calculator.py'], ce.sanitize_evidence, self.config)

    def test_deleted_file_diff_is_visible_and_binary_evidence_blocks_trust(self):
        self.ready()
        candidate = durable.snapshot(self.repo)
        with patch.object(self.repo, 'diff', return_value='--- a/calculator.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-def multiply(a,b): pass\n'):
            manifest, diff = code.build_candidate_evidence(self.store.state, self.repo, candidate,
                                      ['calculator.py'], ce.sanitize_evidence, self.config)
            self.assertTrue(manifest['complete'])
            self.assertIn('-def multiply', diff)
        with patch.object(self.repo, 'diff', return_value='Binary contents not text-reviewed: calculator.py'):
            manifest, _ = code.build_candidate_evidence(self.store.state, self.repo, candidate,
                                      ['calculator.py'], ce.sanitize_evidence, self.config)
            self.assertFalse(manifest['complete'])

if __name__ == '__main__':
    unittest.main()
