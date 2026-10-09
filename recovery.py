"""Bounded observations and fresh-episode convergence rules; never trust model claims."""
import ast
import hashlib
import json
import math
import os
import posixpath
import re
import urllib.error

FAILURE_EVIDENCE_VERSION = 1
FAILURE_EVIDENCE_CHARS = 6500
MUTATIONS_WITHOUT_OBSERVATION = 2
MAX_FORCED_OBSERVATIONS = 1


def executor_progress_policy():
    return (f"At most {MUTATIONS_WITHOUT_OBSERVATION} successful same-path mutations may occur without a useful observation. "
            f"There is only {MAX_FORCED_OBSERVATIONS} forced-observation recovery opportunity per episode: a third mutation "
            "is blocked with observation_required; your very next action must read that path, inspect a relevant git diff, "
            "or run a relevant deterministic check. Ignoring it stops NO_PROGRESS. After significant code changes prefer "
            "read/diff/targeted test before more edits. Prefer the smallest relevant test during repair. "
            "A failed replace_text anchor requires a fresh read before any edit of that path. "
            "Prefer focused replace_text over rewriting an existing whole file. Heartbeats and file listings are not observations.")


def request_timed_out(exc):
    return isinstance(exc, TimeoutError) or (isinstance(exc, urllib.error.URLError)
                                             and isinstance(exc.reason, TimeoutError))


def path_key(path):
    """One progress identity for tool aliases; filesystem confinement stays in Repository."""
    if path is None:
        return None
    value = posixpath.normpath(path.replace('\\', '/'))
    return value.casefold() if os.name == 'nt' else value


def bounded_text(value, limit=800):
    text = value if isinstance(value, str) else ''
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', text)
    if re.search(r'</?(?:think|analysis|reasoning)\b', text, re.I):
        return '[omitted: reasoning markers]'
    text = ''.join(c if c.isprintable() or c in '\n\t' else ' ' for c in text)
    text = re.sub(r'(?i)\b(api[_-]?key|password|secret|access_token)\s*[:=]\s*[^\s,;]+', r'\1=[redacted]', text)
    if len(text) <= limit:
        return text
    marker='\n[truncated]\n'
    n=(limit-len(marker))//2
    return text[:n]+marker+text[-n:]


def command_observation(result, limit=800):
    def metric(value):
        return max(0, value) if type(value) in (int,float) and math.isfinite(value) else None
    return {'argv':[bounded_text(x,200) for x in result.get('argv',[])[:32]],
            'exit_code':result.get('exit_code') if type(result.get('exit_code')) is int else None,
            'stdout':bounded_text(result.get('stdout'),limit), 'stderr':bounded_text(result.get('stderr'),limit),
            'output_truncated':bool(result.get('output_truncated')) or any(
                len(result.get(k,''))>limit for k in ('stdout','stderr')),
            'duration_seconds':metric(result.get('duration_seconds'))}


def failed_tests(commands):
    names=[]
    for c in commands:
        if c.get('exit_code')==0:
            continue
        text=c.get('stdout','')+'\n'+c.get('stderr','')
        for match in re.finditer(r'(?:ERROR collecting |(?:FAILED|ERROR) |(?:FAIL|ERROR): )([^\s\n]+)',text):
            name=match.group(1)[:200]
            # unittest's final "FAILED (failures=1)" is a summary, not a test identity.
            if not name.startswith('(') and name not in names:
                names.append(name)
    return names[:10]


def targeted_check(names):
    for name in names:
        path=name.split('::')[0].replace('\\','/')
        if re.fullmatch(r'[A-Za-z0-9_./-]+\.py',path) and not path.startswith('/') and '..' not in path.split('/'):
            return ['python','-m','pytest',path,'-q']
    return None


def shrink_failure(value, limit=FAILURE_EVIDENCE_CHARS):
    """Keep the canonical object, never clip serialized JSON or its evidence binding."""
    result=dict(value)
    for key in ('attempted_actions','tool_failures','commands','changed_paths','failed_tests'):
        result[key]=list(value.get(key,[]))
    while len(json.dumps(result,ensure_ascii=False))>limit:
        if len(result['attempted_actions'])>2:
            result['attempted_actions']=result['attempted_actions'][1:]
        elif len(result['tool_failures'])>2:
            result['tool_failures']=result['tool_failures'][1:]
        elif len(result['commands'])>1:
            result['commands']=result['commands'][-1:]
        elif any(len(c.get(k,''))>180 for c in result['commands'] for k in ('stdout','stderr')):
            result['commands']=[{**c,'stdout':bounded_text(c.get('stdout'),180),'stderr':bounded_text(c.get('stderr'),180),
                                'output_truncated':True} for c in result['commands']]
        elif len(result['changed_paths'])>5:
            result['changed_paths']=result['changed_paths'][:5]
        elif len(result.get('detail',''))>150:
            result['detail']=bounded_text(result['detail'],150)
        elif len(result['failed_tests'])>2:
            result['failed_tests']=result['failed_tests'][-2:]
        elif len(result['attempted_actions'])>1:
            result['attempted_actions']=result['attempted_actions'][-1:]
        elif len(result['tool_failures'])>1:
            result['tool_failures']=result['tool_failures'][-1:]
        elif len(result['changed_paths'])>1:
            result['changed_paths']=result['changed_paths'][:1]
        elif any(len(c.get('argv',[]))>8 or any(len(a)>80 for a in c.get('argv',[])) for c in result['commands']):
            result['commands']=[{**c, 'argv':[bounded_text(a,80) for a in c['argv'][:8]],
                                'argv_truncated':True} for c in result['commands']]
        elif result.get('last_successful_observation') is not None:
            result['last_successful_observation']=None
        elif len(result['failed_tests'])>1:
            result['failed_tests']=result['failed_tests'][-1:]
        elif any(len(c.get(k,''))>80 for c in result['commands'] for k in ('stdout','stderr')):
            result['commands']=[{**c,'stdout':bounded_text(c.get('stdout'),80),'stderr':bounded_text(c.get('stderr'),80),
                                'output_truncated':True} for c in result['commands']]
        elif any(len(c.get('argv',[]))>4 or any(len(a)>40 for a in c.get('argv',[])) for c in result['commands']):
            result['commands']=[{**c,'argv':[bounded_text(a,40) for a in c['argv'][:4]],
                                'argv_truncated':True} for c in result['commands']]
        elif any(len(t.get('safe_reason',''))>80 for t in result['tool_failures']):
            result['tool_failures']=[{**t,'safe_reason':bounded_text(t.get('safe_reason'),80)} for t in result['tool_failures']]
        else:
            raise ValueError('Failure evidence exceeds its structural bound')
        result['evidence_truncated']=True
    return result


class EpisodeStop(RuntimeError):
    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason,self.detail=reason,detail


class RecoveryActionError(ValueError):
    def __init__(self, classification, message):
        super().__init__(message)
        self.classification=classification


class ObservationRequired(RecoveryActionError):
    def __init__(self, path):
        super().__init__('observation_required',
                         f'Before another mutation of {path}, perform a useful observation: read_file of this path, '
                         'a relevant deterministic check, or git diff. The blocked mutation was not executed.')
        self.path = path


def check_paths(argv, repo):
    """Static test/import paths for repeat-check relevance; no imported code is executed."""
    paths=set()
    if argv[0].lower().removesuffix('.exe') == 'git' and len(argv)>1 and argv[1]=='diff':
        return {path_key(p) for p in argv[argv.index('--')+1:]} if '--' in argv else None
    is_python=any(x in argv for x in ('pytest','unittest','compileall'))
    if not is_python:
        return None  # Non-Python/full checks conservatively depend on all mutations.
    for arg in argv:
        path=arg.split('::')[0].replace('\\','/')
        if not path.startswith('-') and path.endswith('.py'):
            paths.add(path)
    if 'unittest' in argv:
        index=argv.index('unittest')
        for arg in argv[index+1:]:
            if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*',arg):
                paths.add(arg.replace('.','/')+'.py')
    if not paths:
        return None
    for path in list(paths)[:10]:
        try:
            tree=ast.parse(repo.read(path))
        except (ValueError,OSError,SyntaxError,AssertionError):
            continue
        for node in ast.walk(tree):
            modules=([n.name for n in node.names] if isinstance(node,ast.Import) else
                     [node.module] if isinstance(node,ast.ImportFrom) and node.module else [])
            for module in modules[:20]:
                paths.add(module.replace('.','/')+'.py')
    return {path_key(p) for p in paths}


class EpisodeProgress:
    """Per-call state only. Successful tool dispatch is not automatically semantic progress."""
    def __init__(self, inspect_existing=False):
        self.inspect_existing=inspect_existing
        self.observed=set();self.need_read=set();self.mutations={};self.unobserved={}
        self.failure_counts={};self.commands={};self.changed=False;self.check=None
        self.file_list = None
        self.observation_required = set()
        self.forced_observations = 0

    @staticmethod
    def relevant(path, paths):
        return paths is None or any(path == p or path.startswith(p.rstrip('/')+'/') for p in paths)

    def before_action(self, action, repo):
        if not self.observation_required:
            return
        path = next(iter(self.observation_required))
        kind = action['action']
        useful = kind == 'read_file' and path_key(action['path']) == path
        if kind == 'run_command':
            argv = action['argv']
            git = argv[0].lower().removesuffix('.exe') == 'git'
            useful = (not git or (len(argv)>1 and argv[1]=='diff')) and self.relevant(path, check_paths(argv, repo))
        if not useful:
            raise EpisodeStop('NO_PROGRESS','Required same-path observation was ignored; fresh recovery plan required')

    def before_mutation(self,path,source):
        path=path_key(path)
        if self.observation_required:
            raise EpisodeStop('NO_PROGRESS','Required same-path observation was ignored; fresh recovery plan required')
        if path in self.need_read:
            raise EpisodeStop('NO_PROGRESS','A failed anchor requires a fresh read before another mutation')
        if self.inspect_existing and source is not None and path not in self.observed:
            raise RecoveryActionError('inspection_required','Read the current file before editing this untrusted/stale candidate')
        if self.unobserved.get(path,0)>=MUTATIONS_WITHOUT_OBSERVATION:
            if self.forced_observations >= MAX_FORCED_OBSERVATIONS:
                raise EpisodeStop('NO_PROGRESS','Forced-observation opportunity exhausted; fresh recovery plan required')
            self.observation_required.add(path)
            self.forced_observations += 1
            raise ObservationRequired(path)

    def mutation(self,path,before,after):
        path=path_key(path)
        if before==after:
            raise RecoveryActionError('no_change_edit','The write made no observable content change; inspect before retrying')
        self.changed=True
        self.mutations[path]=self.mutations.get(path,0)+1
        self.unobserved[path]=self.unobserved.get(path,0)+1
        self.need_read.discard(path)

    def observe_file(self,path):
        path=path_key(path)
        self.observed.add(path);self.need_read.discard(path);self.unobserved[path]=0
        self.observation_required.discard(path)

    def observe_files(self,files):
        digest=hashlib.sha256(json.dumps(sorted(files)).encode()).hexdigest()
        signature=(digest,tuple(sorted(self.mutations.items())))
        if self.file_list==signature:
            raise EpisodeStop('NO_PROGRESS','File listing repeats unchanged repository evidence')
        self.file_list=signature
        return digest

    def failed(self,kind,path,classification,state=''):
        path=path_key(path)
        key=(kind,path,classification,state)
        self.failure_counts[key]=self.failure_counts.get(key,0)+1
        if len(self.failure_counts)>64:
            self.failure_counts.pop(next(iter(self.failure_counts)))
        if self.failure_counts[key]>=2:
            raise EpisodeStop('NO_PROGRESS','Repeated tool failure against unchanged evidence; fresh recovery plan required')
        if kind=='replace_text':
            self.need_read.add(path)

    def before_command(self,argv,repo):
        key=tuple(argv);paths=check_paths(argv,repo)
        # Static imports plus exact test files; unrelated mutations do not reset a failed check.
        revision=tuple(sorted((p,n) for p,n in self.mutations.items() if paths is None or p in paths))
        if key in self.commands and self.commands[key]['revision']==revision:
            raise EpisodeStop('NO_PROGRESS','Identical command already observed without a relevant mutation')
        return key,revision,paths

    def command(self,token,result):
        key,revision,paths=token
        self.commands[key]={'revision':revision,'exit_code':result.get('exit_code')}
        check=not key[0].lower().removesuffix('.exe')=='git'
        if check:
            self.check={'exit_code':result.get('exit_code'),'mutations':dict(self.mutations),
                        'paths':paths,
                        'observation':command_observation(result)}
        useful=result.get('exit_code') is not None and (check or (len(key)>1 and key[1]=='diff'))
        if useful:
            for p in self.unobserved:
                if self.relevant(p, paths):
                    self.unobserved[p]=0
                    self.observation_required.discard(p)
        if self.observation_required:
            raise EpisodeStop('NO_PROGRESS','Required observation did not complete; fresh recovery plan required')

    def done(self):
        relevant = self.check and self.check['paths']
        revision = {p:n for p,n in self.mutations.items() if relevant is None or p in relevant}
        previous = {p:n for p,n in (self.check or {}).get('mutations',{}).items() if relevant is None or p in relevant}
        if self.changed and self.check and self.check['exit_code']!=0 and previous==revision:
            raise EpisodeStop('KNOWN_CHECK_FAILED','Done refused: the most recent check after the last mutation failed')
