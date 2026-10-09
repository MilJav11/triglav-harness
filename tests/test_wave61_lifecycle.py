"""Wave 6.1: real lifecycle APIs with deterministic transport/process observations."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import durable
import harness
import manager
import supervision
import work_units
from live_work_unit_executor import LiveWorkUnitExecutor
from work_unit_scheduler import WorkUnitScheduler
from test_live_work_unit_executor import fixture, make_spec, verifier_registry, FakeRunner


class Wave61LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, self.config = fixture(self.root, 'wave61')
        self.config['startup_timeout_seconds'] = 3
        self.registry = verifier_registry()
        self.log = harness.EventLog(self.root / 'lifecycle.jsonl')
        self.native = {os.getpid(): {'created': 'controller', 'executable': str(Path(sys.executable).resolve())}}
        self.sup = supervision.Supervisor(self.store, boot=lambda: 'boot-test', inspect=lambda pid: self.native.get(pid))
        self.processes = []
        self.running_models = []
        self.requests = []
        self.loaded = []
        self.control = self.root / 'control'
        (self.control / 'runs').mkdir(parents=True)
        binary = self.control / 'tools/llama-swap/bin/llama-swap.exe'
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b'test gateway identity')
        (self.control / 'runs/swap.pid').write_text('101')
        (self.control / 'runs/swap-profile.yaml').write_text(''.join(
            f'  {alias}:\n    cmd: >-\n      "{Path(sys.executable).as_posix()}" --role {role}\n'
            for role, alias in self.config['roles'].items()))
        self.native[101] = {'created': 'gateway', 'executable': str(binary.resolve())}
        self.gateway = harness.Gateway(self.config, self.log)
        self.gateway.supervisor = self.sup
        self.gateway.request = self.transport

    def transport(self, method, path, payload=None, **kwargs):
        self.requests.append((method, path))
        if path == '/running':
            return copy.deepcopy(self.running_models)
        if path == '/api/models/unload':
            for process in self.processes:
                self.native.pop(process['pid'], None)
            self.processes.clear()
            self.running_models.clear()
            return None
        if path.startswith('/upstream/'):
            self.assertFalse(self.processes, 'A second model must never start while one is live')
            alias = path.split('/')[2]
            pid = 9000 + len(self.loaded)
            self.loaded.append(alias)
            self.native[pid] = {'created': f'model-{pid}', 'executable': str(Path(sys.executable).resolve())}
            self.processes.append({'pid': pid, 'parent_pid': 101})
            self.running_models.append({'model': alias, 'state': 'ready'})
            return {'status': 'ok'}
        raise AssertionError((method, path))

    def create(self, worker):
        manager.create_run(self.store, self.repo, 'Implement multiply',
            [['python', '-m', 'unittest', 'test_calculator.py']], self.config,
            criteria=['Multiply works'], allow=['calculator.py'], work_units=[make_spec()],
            verifier_registry=self.registry, executor=worker)

    def owned_start(self, role):
        with patch('environment.ROOT', self.control):
            return self.real_start(role)

    def runner(self, cmd, cwd, timeout, **kwargs):
        self.assertEqual(self.running_models, [{'model': self.config['roles']['code'], 'state': 'ready'}])
        models = [c for c in self.store.state['supervision']['children'].values()
                  if c['purpose'] == 'model_server' and c['state'] == 'RUNNING']
        self.assertEqual(len(models), 1)
        self.assertEqual(models[0]['pid'], self.processes[0]['pid'])
        (Path(cwd) / 'calculator.py').write_text('def multiply(a, b):\n    return a * b\n')
        return dict(exit_code=0, stdout='', stderr='', duration_seconds=0.1, timed_out=False,
                    cleanup_invoked=False, launch_failed=False)

    def test_manager_live_worker_owned_cleanup_allows_critic_transition(self):
        worker = LiveWorkUnitExecutor(self.config, runner=self.runner, gateway=self.gateway, log=self.log)
        self.create(worker)
        self.real_start = self.sup.model_starting
        with patch('harness.servers', side_effect=lambda: copy.deepcopy(self.processes)), \
             patch('harness.available_ram_gb', return_value=100), \
             patch.object(self.sup, 'model_starting', side_effect=self.owned_start), self.sup:
            # Reproduce the previous operator prewarm and Manager.begin_session unload.
            self.gateway.switch('code')
            loop = manager.ManagerLoop(self.store, self.sup, self.repo, self.gateway,
                manager.Agents(None, worker, None, None), self.log)
            loop.run()
            self.assertEqual(self.store.state['work_units']['units']['unit-1']['status'], 'UNIT_VERIFIED')
            self.assertEqual(self.store.state['manager']['milestone_status'], 'MILESTONE_READY')
            self.assertFalse(self.running_models, 'Worker-owned model must be cleaned before critic transition')
            self.sup.require_idle()
            self.gateway.switch('critic')
            self.assertEqual(self.running_models[0]['model'], 'llm-critic')
            self.gateway.unload()
            self.sup.require_idle()
        self.assertIsNone(self.sup.failure)
        self.assertEqual(self.loaded, ['llm-code', 'llm-code', 'llm-critic'])
        reloaded = durable.Store(self.root / 'runs', 'wave61')
        reloaded.load()
        work_units.validate_section(reloaded.state['work_units'])

    def test_foreign_model_is_not_adopted_or_unloaded(self):
        worker = LiveWorkUnitExecutor(self.config, runner=self.runner, log=self.log)
        self.create(worker)
        self.processes.append({'pid': 4444, 'parent_pid': 101})
        self.native[4444] = {'created': 'foreign', 'executable': str(Path(sys.executable).resolve())}
        self.running_models.append({'model': 'llm-code', 'state': 'ready'})
        with patch('harness.servers', side_effect=lambda: copy.deepcopy(self.processes)):
            with self.assertRaisesRegex(durable.DurableError, 'ownership not proven'):
                self.gateway.unload()
        self.assertNotIn(('POST', '/api/models/unload'), self.requests)
        self.assertEqual(self.processes[0]['pid'], 4444)

    def test_crashed_launch_reservation_still_blocks_cleanup(self):
        worker = LiveWorkUnitExecutor(self.config, runner=FakeRunner())
        self.create(worker)
        self.sup.register('model_server', sys.executable, [], 'delegated')
        with patch('harness.servers', return_value=[]):
            with self.assertRaisesRegex(durable.DurableError, 'empty gateway does not prove it resolved'):
                self.gateway.unload()
        self.assertNotIn(('POST', '/api/models/unload'), self.requests)

    def test_live_worker_does_not_adopt_foreign_model(self):
        worker = LiveWorkUnitExecutor(self.config, runner=FakeRunner(), gateway=self.gateway)
        self.create(worker)
        self.processes.append({'pid': 4444, 'parent_pid': 101})
        self.native[4444] = {'created': 'foreign', 'executable': str(Path(sys.executable).resolve())}
        self.running_models.append({'model': 'llm-code', 'state': 'ready'})
        with patch('harness.servers', side_effect=lambda: copy.deepcopy(self.processes)):
            with self.assertRaises(manager.ExecutorError) as error:
                worker.execute(make_spec(), {'attempt_number': 1}, self.repo)
        self.assertEqual(error.exception.reason, 'CLEANUP_FAILED')
        self.assertFalse(worker.runner.calls)
        self.assertNotIn(('POST', '/api/models/unload'), self.requests)
        self.assertFalse(self.store.state['supervision']['children'])

    def test_production_worker_requires_supervised_gateway_before_launch(self):
        gateway = harness.Gateway(self.config, self.log)
        worker = LiveWorkUnitExecutor(self.config, gateway=gateway)
        self.create(worker)
        with patch.object(gateway, 'switch') as switch, patch('live_work_unit_executor.subprocess.Popen') as launch:
            with self.assertRaises(manager.ExecutorError) as error:
                worker.execute(make_spec(), {'attempt_number': 1}, self.repo)
        self.assertEqual(error.exception.reason, 'MODEL_OWNERSHIP_REQUIRED')
        switch.assert_not_called()
        launch.assert_not_called()

    def test_heartbeat_between_transition_and_seal_reaches_verified_and_reloads(self):
        worker = LiveWorkUnitExecutor(self.config, runner=FakeRunner(side_effect=lambda *a, **k:
            self.write_candidate(a[1])))
        self.create(worker)
        failures, observed = [], []
        original = work_units.seal_unit_state
        def seal(unit):
            if unit.get('status') in ('VALIDATED', 'READY', 'EXECUTING', 'VERIFYING', 'UNIT_VERIFIED'):
                def pulse():
                    try:
                        self.sup.heartbeat()
                        observed.append(self.store.state['work_units']['units']['unit-1']['status'])
                    except BaseException as exc:
                        failures.append(exc)
                thread = threading.Thread(target=pulse)
                thread.start()
                thread.join(5)
                self.assertFalse(thread.is_alive(), 'Heartbeat must not wait on a long-running worker')
            return original(unit)
        with self.sup, patch.object(work_units, 'seal_unit_state', side_effect=seal):
            scheduled = WorkUnitScheduler(self.store, self.repo, verifier_registry=self.registry,
                supervisor=self.sup, agents=manager.Agents(None, worker, None, None), log=self.log)
            result = scheduled.run_sequence()
        self.assertEqual(result['status'], 'MILESTONE_READY')
        self.assertTrue(observed)
        self.assertEqual(failures, [])
        self.assertIsNone(self.sup.failure)
        self.assertEqual(self.store.state['work_units']['units']['unit-1']['status'], 'UNIT_VERIFIED')
        loaded = durable.Store(self.root / 'runs', 'wave61')
        loaded.load()
        work_units.validate_section(loaded.state['work_units'])
        self.assertEqual(loaded.state['work_units'], self.store.state['work_units'])

    def write_candidate(self, cwd):
        (Path(cwd) / 'calculator.py').write_text('def multiply(a, b):\n    return a * b\n')
        return dict(exit_code=0, stdout='', stderr='', duration_seconds=0.1,
                    timed_out=False, launch_failed=False)

    def test_commit_does_not_publish_caller_owned_workunit_alias(self):
        self.create(LiveWorkUnitExecutor(self.config, runner=FakeRunner()))
        section = copy.deepcopy(self.store.state['work_units'])
        self.store.commit(work_units=section)
        committed = copy.deepcopy(self.store.state['work_units'])
        section['units']['unit-1']['updated_at'] = 'unsealed local edit'
        self.sup.heartbeat()
        self.assertEqual(self.store.state['work_units'], committed)
        for record in self.store.records:
            if record['event'] == 'state_committed':
                work_units.validate_section(record['state']['work_units'])

    def test_queued_heartbeat_preserves_newer_workunit_commit(self):
        self.create(LiveWorkUnitExecutor(self.config, runner=FakeRunner()))
        queued, failures = threading.Event(), []
        def pulse():
            queued.set()
            try:
                self.sup.heartbeat()
            except BaseException as exc:
                failures.append(exc)
        with self.store.mutex:
            thread = threading.Thread(target=pulse)
            thread.start()
            self.assertTrue(queued.wait(5))
            section = copy.deepcopy(self.store.state['work_units'])
            unit = section['units']['unit-1']
            work_units.transition_unit(unit, 'VALIDATED')
            work_units.seal_unit_state(unit)
            work_units._reseal_section(section)
            self.store.commit(work_units=section)
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.store.state['work_units'], section)
        loaded = durable.Store(self.root / 'runs', 'wave61')
        loaded.load()
        self.assertEqual(loaded.state['work_units'], section)

    def test_corrupt_workunit_commit_and_heartbeat_still_fail_closed(self):
        self.create(LiveWorkUnitExecutor(self.config, runner=FakeRunner()))
        corrupt = copy.deepcopy(self.store.state['work_units'])
        corrupt['units']['unit-1']['updated_at'] = 'tampered'
        revision = self.store.state['revision']
        with self.assertRaisesRegex(work_units.WorkUnitError, 'checksum mismatch'):
            self.store.commit(work_units=corrupt)
        self.assertEqual(self.store.state['revision'], revision)
        self.sup.heartbeat()
        self.store.state['work_units'] = corrupt
        with self.assertRaisesRegex(work_units.WorkUnitError, 'checksum mismatch'):
            self.sup.heartbeat()


if __name__ == '__main__':
    unittest.main()
