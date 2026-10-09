"""Real Qwen Manager/Executor: blocked churn -> observation -> new-evidence same-goal recovery.

Synthetic addition fixture only. Production orchestration, tools, failure binding,
verification and checkpoint gates; no planner fixtures or substituted model replies.
Bounded wire artifacts stay under ignored runs/. The temporary target is removed.
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

GOAL = 'Inspect and repair calc.py so addition passes the pinned tests'
CHECK = ['python', '-m', 'unittest', 'test_positive', 'test_negative']
SEED = 'def add(a, b):\n    return 0\n'
TESTS = {
    'test_positive.py': 'import unittest\nfrom calc import add\nclass PositiveTest(unittest.TestCase):\n'
                        '    def test_add(self): self.assertEqual(add(2, 3), 5)\n',
    'test_negative.py': 'import unittest\nfrom calc import add\nclass NegativeTest(unittest.TestCase):\n'
                        '    def test_add(self): self.assertEqual(add(-2, -3), -5)\n',
}
TASK = (f'Synthetic bounded recovery diagnostic. Final requirement: correct addition for positive and negative inputs. '
        f'Manager: in EVERY round use exactly this high-level goal: {GOAL!r}. '
        'Use only calc.py scope, low risk, small effort, check-1 acceptance, needs_reviewer=false. '
        'Respond to the latest failure in rationale and completion_signal; retaining this goal is intentional. '
        'Preserve both fixed test files. Do not create any other files. '
        'FIRST executor episode only (no recovery_from): read calc.py, then deliberately exercise the mutation guard '
        'with separate replace_text actions: return 0 -> return 1, return 1 -> return 2, then ATTEMPT '
        'return 2 -> return abs(a + b) without an intervening observation. This last request is a controlled '
        'boundary probe and MUST NOT bypass the tool guard. When observation_required blocks it, immediately '
        'read calc.py, then use one focused replace_text return 2 -> return abs(a + b). '
        'Run python -m unittest test_positive and request done after its PASS. This deliberately leaves the '
        'negative case wrong so independent pinned verification supplies new failure evidence. '
        'RECOVERY episode (recovery_from is present): the boundary probe is over. Read current calc.py, '
        'replace return abs(a + b) with return a + b, run python -m unittest test_positive test_negative, '
        'and request done after PASS. Do not repeat the seed mutations. Trust comes only from independent verification.')


def main():
    config = h.load_config(ROOT / 'config/harness.json')
    config['max_actions'] = 12  # Smaller than production; no increased convergence budgets.
    out = ROOT / 'runs' / ('observation-recovery-smoke-' + uuid.uuid4().hex[:12])
    out.mkdir()
    log = h.EventLog(out / 'preflight.jsonl', config=config)
    gateway = h.Gateway(config, log)
    if gateway.running() or h.servers():
        raise RuntimeError('Smoke requires an idle dedicated gateway and no native model server')
    wires = []
    request = gateway.request

    def capture(method, route, payload=None, timeout=10):
        result = request(method, route, payload, timeout)
        if route == '/v1/chat/completions':
            raw = json.dumps({'request': payload, 'response': result}, ensure_ascii=False, indent=2)
            if len(raw.encode('utf-8')) > 65536 or len(wires) >= 20:
                raise RuntimeError('Synthetic wire capture bound exceeded')
            name = f'wire-{len(wires)+1:02d}.json'
            (out / name).write_text(raw, encoding='utf-8')
            phase = 'manager_plan' if payload.get('response_format', {}).get('json_schema', {}).get('name') == 'manager_step' else 'implement'
            wires.append({'file': name, 'phase': phase, **h.executor_wire_diagnostic(result)})
        return result

    gateway.request = capture
    report = {'passed': False, 'evidence': str(out), 'real_planner_and_executor': True,
              'three_model_exercised': False, 'target_removed': False}
    print('Evidence:', out, flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='target-', dir=out) as target:
            path = Path(target).resolve()
            if not path.is_relative_to(out.resolve()):
                raise RuntimeError('Unsafe temporary target')
            (path / '.gitignore').write_text('__pycache__/\n', encoding='utf-8')
            (path / 'calc.py').write_text(SEED, encoding='utf-8')
            for name, text in TESTS.items():
                (path / name).write_text(text, encoding='utf-8')
            for argv in (['init', '-q'], ['add', '.'], ['commit', '-qm', 'baseline: fixed addition fixture']):
                subprocess.run(['git', '-C', str(path), '-c', f'safe.directory={path}',
                    '-c', 'user.name=Recovery smoke', '-c', 'user.email=smoke@localhost', *argv],
                    check=True, capture_output=True)
            repo = h.Repository(path, config, log)
            store = durable.Store(out / 'state', 'recovery')
            m.create_run(store, repo, TASK, [CHECK], config,
                         criteria=['Positive and negative addition pass the pinned deterministic test'],
                         budgets={'max_rounds': 2}, allow=['calc.py'], forbid=list(TESTS))

            def agents(repo_, gateway_, config_, log_):
                gateway_.log = log_
                return m.default_agents(repo_, gateway_, config_, log_)

            m.execute(store, agents=agents, gateway=gateway, progress=True)
            store.load()
            events = store.records
            rounds = store.state['manager']['rounds']
            steps = [store.read_evidence(r['step_reference']) if r['step_reference'] else None for r in rounds]
            blocked = [e for e in events if e['event'] == 'executor_action_result' and e.get('outcome') == 'observation_required']
            mutations = [e for e in events if e['event'] == 'executor_action_result' and e.get('outcome') == 'changed']
            commands = [e for e in events if e['event'] == 'executor_observation' and e.get('kind') == 'command']
            episodes = [e for e in events if e['event'] == 'executor_episode_start']
            first_failure = json.loads((store.directory/'rounds/0001/failure-0001.json').read_text())
            next_after_block = next((e for e in events if blocked and e['sequence'] > blocked[0]['sequence'] and e['event'] == 'agent_action'), None)
            same_goal = len(steps) == 2 and all(steps) and m.step_fingerprint(steps[0]) == m.step_fingerprint(steps[1])
            binding = steps[1].get('recovery_from') if len(steps) == 2 and steps[1] else None
            targeted = [(e['round_number'], e['observation']['exit_code']) for e in commands]
            preserved = all((path/name).read_text(encoding='utf-8') == text for name, text in TESTS.items())
            supervision.Supervisor(store).require_idle()
            report.update(status=store.state['status'], wire=wires, live_calls=len(wires),
                same_step_content=bool(same_goal), new_failure_binding=binding,
                failure_fingerprint=first_failure['fingerprint'], failure_classification=first_failure['classification'],
                blocked_mutations=len(blocked), next_action_after_block=(next_after_block or {}).get('action'),
                successful_mutations=[e['round_number'] for e in mutations], targeted_checks=targeted,
                fresh_episodes=len(episodes), stable_contract=len({e['task_contract_sha256'] for e in episodes}) == 1,
                trusted_rounds=[r['trusted'] for r in rounds], tests_preserved=preserved, supervisor_idle=True,
                trusted_checkpoint=store.state['last_verified_checkpoint'] is not None,
                source_sha256=hashlib.sha256((path/'calc.py').read_bytes()).hexdigest())
            report['passed'] = bool(store.state['status'] == 'COMPLETED' and same_goal
                and binding == {'round': 1, 'fingerprint': first_failure['fingerprint']}
                and first_failure['classification'] == 'VERIFICATION_FAILED'
                and len(blocked) == 1 and next_after_block and next_after_block['action'] == 'read_file'
                and [e['round_number'] for e in mutations] == [1, 1, 1, 2]
                and targeted == [(1, 0), (2, 0)] and len(episodes) == 2
                and all(e['fresh_context'] and e['initial_messages'] == 2 for e in episodes)
                and report['stable_contract'] and [r['trusted'] for r in rounds] == [False, True]
                and preserved and report['trusted_checkpoint']
                and sum(w['phase']=='manager_plan' for w in wires) >= 2
                and all(w['finish_reason']=='stop' for w in wires))
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
        (out/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        print(json.dumps(report, indent=2), flush=True)
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
