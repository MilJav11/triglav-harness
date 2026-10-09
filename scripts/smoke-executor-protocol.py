"""One real Qwen action through production extraction, parsing, validation and dispatch.

Synthetic prompt/target only; raw wire is bounded and retained for explicit protocol QA.
No autonomous task, review, verification claim or trusted checkpoint is created.
"""
import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import harness


def main():
    out=ROOT/'runs'/('executor-protocol-smoke-'+uuid.uuid4().hex[:12])
    out.mkdir()
    config=harness.load_config(ROOT/'config/harness.json')
    config['max_actions']=1  # Stop after the real action, independently of model claims.
    log=harness.EventLog(out/'events.jsonl',progress=True,config=config)
    gateway=harness.Gateway(config,log)
    if gateway.running() or harness.servers():
        raise RuntimeError('Protocol smoke requires the idle dedicated gateway and no native model server')
    original=gateway.request
    def capture(method,path,payload=None,timeout=10):
        result=original(method,path,payload,timeout)
        if path=='/v1/chat/completions':
            wire=json.dumps({'request':payload,'response':result},indent=2,ensure_ascii=False)
            if len(wire.encode('utf-8'))>65536:
                raise RuntimeError('Synthetic smoke wire exceeds bounded capture limit')
            (out/'wire.json').write_text(wire,encoding='utf-8')
        return result
    gateway.request=capture
    class MarkerRepository(harness.Repository):
        def write(self,path,content):
            if path!='marker.txt':
                raise ValueError('Smoke allows only marker.txt')
            super().write(path,content)
    result={'passed':False,'evidence':str(out),'live_calls':0,'trusted_checkpoint':False}
    print('Evidence:',out,flush=True)
    try:
        with tempfile.TemporaryDirectory(prefix='target-',dir=out) as target:
            target_path=Path(target).resolve()
            if not target_path.is_relative_to(out.resolve()):
                raise RuntimeError('Temporary target is outside the smoke evidence directory')
            subprocess.run(['git','init','-q',str(target_path)],check=True)
            repo=MarkerRepository(target_path,config,log)
            try:
                harness.Workflow(repo,gateway,config,log).implement(
                    'Create marker.txt containing exactly PROTOCOL_OK followed by a newline. '
                    'This is a tiny executor protocol probe. Only marker.txt may be written.')
            except RuntimeError as exc:
                if str(exc)!='Agent exceeded max_actions without finishing':
                    raise
                result['stop']='controller_one_action_budget'
            records=[json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()]
            result['live_calls']=sum(r['event']=='model_request' for r in records)
            actions=[r['action'] for r in records if r['event']=='agent_action']
            if result['live_calls']!=1 or actions!=['write_file']:
                raise RuntimeError('One real canonical write_file must be parsed and dispatched')
            if repo.read('marker.txt')!='PROTOCOL_OK\n':
                raise RuntimeError('Dispatched marker does not match the synthetic task')
            result.update(passed=True,action='write_file',content='PROTOCOL_OK\\n')
        result['temporary_target_removed']=not target_path.exists()
    finally:
        gateway.unload()
        result['cleanup']={'running':gateway.running(),'servers':harness.servers()}
        result['passed']=result['passed'] and not result['cleanup']['running'] and not result['cleanup']['servers']
        (out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2),flush=True)
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
