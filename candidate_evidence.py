"""Controller-built bounded changed-code evidence shared by both semantic gates."""
from durable import DurableError, digest
from manager import path_permitted

MAX_DIFF_CHARS = 6000
MAX_CHANGED_FILES = 32
TRUNCATION_MARKER = '\n[TRUNCATED CANDIDATE DIFF: evidence incomplete; approval blocked]\n'


def build_candidate_evidence(state, repo, candidate, paths, sanitize, config):
    scope = state.get('manager', {}).get('scope', {'allowed_paths': [], 'forbidden_paths': []})
    allowed = set(scope.get('allowed_paths', []))
    forbidden = set(scope.get('forbidden_paths', []))
    for unit in state['work_units']['units'].values():
        allowed.update(unit['spec']['scope'].get('allowed_paths', []))
    effective = {'allowed_paths': sorted(allowed), 'forbidden_paths': sorted(forbidden)}
    if any(not path_permitted(path, effective) for path in paths):
        raise DurableError('Candidate evidence contains unauthorized changes')
    if len(paths) > MAX_CHANGED_FILES:
        raise DurableError('Too many changed files for bounded candidate evidence')
    if candidate['head'] != state['baseline']['head']:
        raise DurableError('Candidate HEAD changed; HEAD diff cannot represent baseline changes')
    cap = config.get('candidate_evidence', {}).get('max_diff_chars', MAX_DIFF_CHARS)
    if type(cap) is not int or not len(TRUNCATION_MARKER) < cap <= MAX_DIFF_CHARS:
        raise DurableError('Invalid candidate diff bound')
    # Repository.diff disables external diff/textconv and uses literal pathspecs.
    # New files carry source text; deleted files carry removed lines. Never fetch
    # arbitrary context files, and never use model-selected paths.
    raw = ''.join(repo.diff(only={path}) for path in sorted(paths))
    safe = sanitize(raw)  # Redact complete text BEFORE taking a bounded excerpt.
    reasons = []
    if paths and not raw.strip():
        reasons.append('changed files have no visible diff')
    if 'Binary contents not text-reviewed:' in raw or 'Binary file deleted:' in raw:
        reasons.append('binary contents unavailable for semantic review')
    if len(safe) > cap:
        keep = cap - len(TRUNCATION_MARKER)
        safe = safe[:keep].rsplit('\n', 1)[0] + TRUNCATION_MARKER
        reasons.append('diff exceeds bounded evidence budget')
    evidence = sanitize({
        'version': 1, 'changed_files': sorted(paths), 'complete': not reasons,
        'limitations': reasons, 'max_diff_chars': cap, 'raw_diff_digest': digest(raw),
        'candidate_snapshot_digest': digest(candidate),
    })
    evidence['digest'] = digest({'manifest': evidence, 'candidate_diff': safe})
    return evidence, safe


def require_complete_evidence(packet):
    if not packet.get('candidate_evidence', {}).get('complete'):
        raise DurableError('Incomplete candidate code evidence cannot establish trust')
