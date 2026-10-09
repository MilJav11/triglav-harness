"""Real Qwen planning -> intentional failure -> real recovery planning -> focused repair.

Uses production planners/executor/Manager/verifier in a temporary Git repository.
All request/response pairs are synthetic, explicitly captured and bounded; cleanup
removes the target and unloads Qwen. Does not exercise critic/reviewer models.
"""
import hashlib
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import durable
import harness as h
import manager as m
import supervision

CHECK = ['python', '-m', 'unittest', 'test_calc']
SEED = 'def add(a, b):\n    return a - b\n'
TEST = ('import unittest\nfrom calc import add\n'
        'class AdditionTest(unittest.TestCase):\n'
        '    def test_add(self): self.assertEqual(add(2, 3), 5)\n')
TASK = ('Controlled recovery diagnostic: first create calc.py with exactly ' + repr(SEED)
        + ', run python -m unittest test_calc, and request done after the intentional failure. '
        'Do not repair during this seed step. The next fresh repair step must inspect calc.py, '
        'use replace_text to return a + b, rerun the targeted test and request done after PASS. '
        'Preserve test_calc.py. Only calc.py may change.')


def main():
    config = h.load_config(ROOT / 'config/harness.json')
    config['max_actions'] = 8  # Smaller than production; never enlarge a recovery budget.
    out = ROOT / 'runs' / ('manager-recovery-smoke-' + uuid.uuid4().hex[:12])
    out.mkdir()
    log = h.EventLog(out / 'preflight.jsonl', config=config)
    gateway = h.Gateway(config, log)
    if gateway.running() or h.servers():
        raise RuntimeError('Smoke requires an idle dedicated gateway and no native model server')
    wires = []
    request = gateway.request
    def capture(method, path, payload=None, timeout=10):
        result = request(method, path, payload, timeout)
        if path == '/v1/chat/completions':
            wire = json.dumps({'request': payload, 'response': result}, ensure_ascii=False, indent=2)
            if len(wire.encode('utf-8')) > 65536 or len(wires) >= 16:
                raise RuntimeError('Synthetic wire exceeds capture bound')
            name = f'wire-{len(wires)+1:02d}.json'
            (out / name).write_text(wire, encoding='utf-8')
            phase = 'manager_plan' if payload.get('response_format', {}).get('json_schema', {}).get('name') == 'manager_step' else 'implement'
            wires.append({'file': name, 'phase': phase, **h.executor_wire_diagnostic(result)})
        return result
    gateway.request = capture
    report = {'passed': False, 'evidence': str(out), 'planner': 'real Qwen initial and recovery planning',
              'three_model_exercised': False, 'target_removed': False}
    print('Evidence:', out, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='target-', dir=out) as target:
            path = Path(target).resolve()
            if not path.is_relative_to(out.resolve()):
                raise RuntimeError('Unsafe temporary target')
            (path / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
            (path / 'test_calc.py').write_text(TEST, encoding='utf-8')
            for argv in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'baseline: fixed addition test']):
                subprocess.run(['git', '-C', str(path), '-c', f'safe.directory={path}',
                                '-c', 'user.name=Recovery smoke', '-c', 'user.email=smoke@localhost', *argv],
                               check=True, capture_output=True)
            repo = h.Repository(path, config, log)
            store = durable.Store(out / 'state', 'recovery')
            m.create_run(store, repo, TASK, [CHECK], config, criteria=['Addition passes the pinned test'],
                         budgets={'max_rounds': 2}, allow=['calc.py'], forbid=['test_calc.py'])
            def agents(repo_, gateway_, config_, log_):
                gateway_.log = log_
                real = m.default_agents(repo_, gateway_, config_, log_)
                return real
            m.execute(store, agents=agents, gateway=gateway, progress=True)
            store.load()
            events = store.records
            actions = [r for r in events if r['event'] == 'agent_action']
            commands = [r for r in events if r['event'] == 'executor_observation' and r.get('kind') == 'command']
            rounds = store.state['manager']['rounds']
            if len(rounds) != 2 or not rounds[0]['step_reference']:
                raise RuntimeError('Live planning did not reach the two-round recovery lifecycle')
            first_failure = (store.directory / 'rounds/0001/failure-0001.json')
            evidence = json.loads(first_failure.read_text(encoding='utf-8')) if first_failure.exists() else None
            order = [(r['round_number'], r['action']) for r in actions]
            checks = [(r['round_number'], r['observation']['exit_code']) for r in commands]
            first_context = json.loads((store.directory / 'rounds/0001/context-planner.json').read_text())
            second_context = json.loads((store.directory / 'rounds/0002/context-planner.json').read_text())
            initial_step = store.read_evidence(rounds[0]['step_reference'])
            repair_step = store.read_evidence(rounds[1]['step_reference']) if rounds[1]['step_reference'] else None
            valid_repair = (repair_step is not None and evidence is not None
                and repair_step.get('recovery_from') == {'round': 1, 'fingerprint': evidence['fingerprint']}
                and m.step_fingerprint(initial_step) != m.step_fingerprint(repair_step)
                and first_context['task_contract_sha256'] == second_context['task_contract_sha256']
                and m.RISKS.index(repair_step['risk']) >= m.RISKS.index(rounds[0]['risk'])
                and (not second_context['planning_constraints']['reviewer_required'] or repair_step['needs_reviewer']))
            expected = (valid_repair and len(rounds) == 2 and rounds[0]['reason'] == 'KNOWN_CHECK_FAILED'
                        and rounds[0]['trusted'] is False and rounds[1]['trusted'] is True
                        and checks == [(1, 1), (2, 0)]
                        and [(n, a) for n, a in order if a in ('write_file', 'replace_text')]
                            == [(1, 'write_file'), (2, 'replace_text')]
                        and (2, 'read_file') in order and order[-1] == (2, 'done')
                        and evidence and evidence['commands'][0]['exit_code'] == 1
                        and (path / 'test_calc.py').read_text(encoding='utf-8') == TEST
                        and store.state['status'] == 'COMPLETED')
            checkpoint = store.read_evidence(store.state['last_verified_checkpoint']['reference']) if store.state['last_verified_checkpoint'] else None
            supervision.Supervisor(store).require_idle()
            report.update(passed=bool(expected), status=store.state['status'], actions=order,
                          targeted_checks=checks, wire=wires, live_calls=len(wires),
                          failure_evidence=evidence, independent_verification_checkpoint=checkpoint is not None,
                          repair_step=repair_step, stable_contract=first_context['task_contract_sha256'],
                          valid_evidence_bound_repair=bool(valid_repair),
                          planner_requests=sum(1 for w in wires if w['phase']=='manager_plan'),
                          source_sha256=hashlib.sha256((path / 'calc.py').read_bytes()).hexdigest(),
                          tests_preserved=True, supervisor_idle=True)
        report['target_removed'] = not path.exists()
    except Exception as exc:
        report['error_class'] = type(exc).__name__
        if 'store' in locals():
            report['status'] = store.state['status']
    finally:
        if 'path' in locals():
            report['target_removed'] = not path.exists()
        gateway.unload()
        report['cleanup'] = {'gateway_running': gateway.running(), 'servers': h.servers()}
        report['passed'] = bool(report['passed'] and report['target_removed'] and
                                not report['cleanup']['gateway_running'] and not report['cleanup']['servers'])
        (out / 'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
