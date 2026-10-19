"""Exact coverage and sequential Windows trusted-fixture CI selections.

One pinned test entry runs per native Job-contained interpreter. Descendant
launches inside that reviewed test are trusted, NOT recursively policy guarded.
Dynamic fixture construction remains in its original test; no helper is rewritten.
The parent reuses one policy/hook. Per-test hooks die with the test interpreter.
"""
import argparse
from contextlib import ExitStack, contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/ci_manifest.json"
VERSION = "3.1F-2B3.1"
SUPPLEMENTAL_MODULES = (
    "test_supervision", "test_unittest_verifier", "test_verifier_invocations",
    "test_retry_eligibility", "test_policy_b_shadow", "test_repair_evidence",
    "test_repair_evidence_active", "test_repair_authorizations", "test_repair_grant_issuance",
    "test_repair_consumption", "test_supervised_offline_executor", "test_offline_repair_dispatch",
    "test_offline_repair_verification", "test_supervised_safe_qwen", "test_supervised_safe_qwen_gateway",
    "test_safe_qwen_recovery_identity", "test_safe_qwen_active_first", "test_safe_qwen_active_verification",
    "test_safe_qwen_active_eligibility", "test_safe_qwen_active_grant", "test_safe_qwen_active_allocation",
    "test_safe_qwen_active_dispatch", "test_safe_qwen_active_dispatch_crash",
    "test_safe_qwen_active_second_verification", "test_safe_qwen_active_second_verification_crash",
)
NATIVE_PILOT = tuple("test_supervision.NativeVerifierObservationTests." + name for name in (
    "test_suspended_creation_gates_execution_and_retains_identity",
    "test_timeout_is_observed_and_job_drained",
    "test_native_natural_exit_cleans_real_lingering_descendant",
    "test_atomic_job_membership_and_crash_at_native_creation_return",
))
CAPSULE = ("import sys\n"
           "sys.path.insert(0,sys.argv[1])\n"
           "from scripts.ci_supplemental import child_main\n"
           "raise SystemExit(child_main(*sys.argv[2:]))\n").encode("utf-8")


class CoverageError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise CoverageError(reason)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "Duplicate JSON key")
        result[key] = value
    return result


def read_json(path):
    with Path(path).open(encoding="utf-8") as stream:
        return json.load(stream, object_pairs_hook=_pairs,
                         parse_constant=lambda _: require(False, "Nonfinite JSON"))


def write_json(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item


def test_ids(loader, name):
    ids = [t.id() for t in flatten(loader.loadTestsFromName(name))]
    require(not loader.errors and not any("_FailedTest" in t for t in ids), "unittest load failure")
    require(len(ids) == len(set(ids)), "Duplicate discovered test IDs")
    return sorted(ids)


def discover():
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    from scripts import ci_offline
    loader = unittest.TestLoader()
    all_ids = [t.id() for t in flatten(loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT / "tests")))]
    require(not loader.errors and not any("_FailedTest" in t for t in all_ids), "Discovery failed")
    require(len(all_ids) == len(set(all_ids)), "Discovery produced duplicate IDs")
    core = []
    for name, count in ci_offline.MODULES.items():
        selected = test_ids(loader, name)
        require(len(selected) == count, "Guarded module count drift: " + name)
        core.extend(selected)
    require(len(core) == len(set(core)) == 390, "Guarded selection drift")
    supplemental = [i for module in SUPPLEMENTAL_MODULES for i in test_ids(loader, module) if i not in set(core)]
    policy = test_ids(loader, "test_ci_supplemental_policy")
    runner = test_ids(loader, "test_ci_supplemental_runner")
    return dict(all=sorted(all_ids), guarded=sorted(core), supplemental=sorted(supplemental),
                policy_proof=policy, runner_security=runner)


def exact_ids(ids):
    require(type(ids) is list and all(type(i) is str and i.startswith("test_") for i in ids), "Invalid ID list")
    require(ids == sorted(set(ids)), "Unsorted or duplicate IDs")
    return set(ids)


def validate_sets(manifest, inventory):
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1 and manifest["manifest_version"] == VERSION,
            "Unsupported manifest version")
    require(manifest["commit_binding"] == "immutable-checkout", "Unsupported commit binding")
    groups = manifest["groups"]
    require(set(groups) == {"guarded", "supplemental", "policy_proof", "runner_security", "outside_scope"},
            "Unknown or missing coverage groups")
    union = set()
    for name, ids in groups.items():
        selected = exact_ids(ids)
        require(not union & selected, "Overlapping coverage: " + name)
        union |= selected
        if name != "outside_scope":
            require(ids == inventory[name], "Exact source selection drift: " + name)
    require(union == set(inventory["all"]), "Missing or foreign discovered IDs")
    require([len(groups[n]) for n in ("guarded", "supplemental", "policy_proof")] == [390,848,21],
            "Historical or mandatory policy coverage count drift")
    require(len(groups["runner_security"]) == 37, "37 runner/security cases are mandatory")
    lifecycle = {i for i in groups["guarded"] if i.startswith("test_supervision.SupervisorLifecycleTests.")}
    require(len(lifecycle) == 25 and not lifecycle & set(groups["supplemental"]), "Lifecycle exclusion drift")
    shards = manifest["draft_shards"]
    require([s["shard"] for s in shards] == [str(i) for i in range(1,10)], "Nine draft shards required")
    combined = []
    for shard in shards:
        exact_ids(shard["test_ids"])
        require(shard["count"] == len(shard["test_ids"]), "Shard count mismatch")
        combined.extend(shard["test_ids"])
    require(len(combined) == len(set(combined)) == 848 and set(combined) == set(groups["supplemental"]),
            "Missing/duplicate/foreign shard IDs")
    require([s["count"] for s in shards] == [65,319,66,66,66,67,66,67,66], "Reviewed draft boundaries changed")
    require(manifest["pilot_ids"] == sorted(set(groups["policy_proof"]) | set(NATIVE_PILOT)), "Pilot selection drift")
    required = set(groups["supplemental"]+groups["policy_proof"]+groups["runner_security"])
    budgets = manifest["case_budgets_seconds"]
    require(set(budgets) == required and all(type(v) is int and 120 <= v <= 3600 for v in budgets.values()),
            "Missing or invalid reviewed execution budgets")
    require(set(manifest["entry_profiles"]) == required and
            all(v in {"closed-gateway","fake-http-gateway"} for v in manifest["entry_profiles"].values()),
            "Missing or unknown trusted entry profile")
    for name,ids in manifest["smoke_ids"].items():
        exact_ids(ids)
        require(ids and set(ids) <= set(selection_ids(manifest,name)),"Foreign smoke IDs")


def checkout_head():
    head = subprocess.check_output(["git", "rev-parse", "--verify", "HEAD^{commit}"], cwd=ROOT, text=True).strip()
    require(re.fullmatch("[0-9a-f]{40}",head),"Immutable Git commit required")
    expected = os.environ.get("TRIGLAV_CI_EXPECTED_HEAD")
    if os.environ.get("GITHUB_ACTIONS") == "true":
        require(expected == os.environ.get("GITHUB_SHA"),"Missing CI checkout binding")
    if expected:
        require(expected == head,"Foreign checkout commit")
    return head


def validate_sources(manifest):
    pins = manifest["source_sha256"]
    require(type(pins) is dict and pins, "Missing reviewed source pins")
    for name, digest in pins.items():
        path = Path(name)
        require(not path.is_absolute() and ".." not in path.parts and path.as_posix() == name,
                "Invalid source pin path")
        require(type(digest) is str and re.fullmatch("[0-9a-f]{64}", digest), "Invalid source digest")
        require(sha((ROOT/path).read_bytes()) == digest, "Reviewed source drift: " + name)
    source_files = {p.relative_to(ROOT).as_posix() for p in ROOT.glob("*.py")}
    source_files |= {p.relative_to(ROOT).as_posix() for base in (ROOT/"scripts",ROOT/"tests")
                     for p in base.rglob("*.py")}
    require(source_files <= set(pins), "Unpinned source module")


def load_manifest(path=MANIFEST):
    digest = sha(Path(path).read_bytes())
    manifest = read_json(path)
    head = checkout_head()
    require(re.fullmatch("[0-9a-f]{40}",head),"Invalid checkout commit")
    validate_sources(manifest)
    validate_sets(manifest, discover())
    require(sha(Path(path).read_bytes()) == digest,"Manifest changed during validation")
    require(checkout_head() == head,"Checkout changed during validation")
    return manifest, digest


def binding(manifest, digest):
    return dict(schema_version=1, manifest_version=manifest["manifest_version"],
                manifest_sha256=digest, head=checkout_head())


def selection_ids(manifest,name,smoke=False):
    if name == "pilot":
        ids = manifest["pilot_ids"]
    elif name in {"policy","runner","guarded"}:
        ids = manifest["groups"][{"policy":"policy_proof","runner":"runner_security","guarded":"guarded"}[name]]
    else:
        matches = [s["test_ids"] for s in manifest["draft_shards"] if name == "shard-"+s["shard"]]
        require(len(matches) == 1,"Unknown selection")
        ids = matches[0]
    return manifest["smoke_ids"][name] if smoke else ids


def offline_environment(temp):
    os.environ.update(TEMP=str(temp), TMP=str(temp), PYTHONDONTWRITEBYTECODE="1", PYTHONUTF8="1",
                      PYTHONPATH=str(ROOT)+os.pathsep+str(ROOT/"tests"),
                      GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                      GIT_CONFIG_NOSYSTEM="1", GIT_ATTR_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="3")
    for index, (key,value) in enumerate((("core.hooksPath",os.devnull),("commit.gpgSign","false"),("protocol.allow","never"))):
        os.environ[f"GIT_CONFIG_KEY_{index}"] = key
        os.environ[f"GIT_CONFIG_VALUE_{index}"] = value
    tempfile.tempdir = str(temp)


ASYNC_SELF_PIPE_TEST = ("test_unittest_verifier.TestUnittestVerifierAdapter."
                        "test_async_teardown_failure_returns_execution_error")


def _deny_child_network(*args, **kwargs):
    raise RuntimeError("Pilot forbids real parent networking and unmocked Gateway operations")


@contextmanager
def _prepared_asyncio_loop(test_id):
    """Prepare only the reviewed Windows fixture's internal self-pipe.

    No connect exemption exists: construction precedes the case, and the
    unchanged network denial covers its setup, body, teardown and loop close.
    This is trusted-fixture compatibility, not hostile-code isolation.
    """
    if os.name != "nt" or test_id != ASYNC_SELF_PIPE_TEST:
        yield
        return
    import asyncio
    import threading
    owner = (os.getpid(), threading.get_ident())
    loop = asyncio.ProactorEventLoop()
    active, used = True, False
    def take_loop():
        nonlocal used
        require(active and not used and (os.getpid(), threading.get_ident()) == owner
                and not loop.is_closed(), "Prepared asyncio loop is unavailable")
        used = True
        return loop
    try:
        # Windows ConnectEx/WSAConnect bypass socket.socket.connect. The
        # prepared loop receives no authority to open another connection.
        with patch.object(loop._proactor, "connect", _deny_child_network), \
                patch.object(asyncio.events, "new_event_loop", take_loop):
            yield
            require(used, "Approved asyncio loop was not consumed")
    finally:
        active = False
        if not loop.is_closed():
            loop.close()


@contextmanager
def _child_network_guard(profile):
    """The existing child denials; no address or caller-based exception."""
    require(profile in {"fake-http-gateway", "closed-gateway"}, "Unknown child profile")
    import harness
    with ExitStack() as stack:
        stack.enter_context(patch.object(socket.socket, "connect", _deny_child_network))
        stack.enter_context(patch.object(socket.socket, "connect_ex", _deny_child_network))
        stack.enter_context(patch.object(socket, "create_connection", _deny_child_network))
        methods = (("request",) if profile == "fake-http-gateway"
                   else ("request", "switch", "unload", "running"))
        for method in methods:
            stack.enter_context(patch.object(harness.Gateway, method, _deny_child_network))
        yield


def child_main(manifest_path, test_id, receipt_path, expected_sha, run_id, temp, expected_head, selection, batch, scope):
    """Pinned trusted test entry; not a recursive child-process guard."""
    offline_environment(Path(temp))
    manifest, digest = load_manifest(Path(manifest_path))
    require(digest == expected_sha and checkout_head() == expected_head and
            test_id in selection_ids(manifest,selection,scope == "smoke"), "Foreign child launch binding")
    require(re.fullmatch("[0-9a-f]{32}",run_id), "Invalid run identity")
    base = dict(**binding(manifest,digest),run_id=run_id,test_id=test_id,selection=selection,batch=batch,scope=scope)
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromName(test_id)
    require(not loader.errors and [t.id() for t in flatten(suite)] == [test_id], "Child ID drift")
    events = []
    class Result(unittest.TextTestResult):
        def addSuccess(self,test): super().addSuccess(test); events.append("PASS")
        def addFailure(self,test,err): super().addFailure(test,err); events.append("FAIL")
        def addError(self,test,err): super().addError(test,err); events.append("ERROR")
        def addSkip(self,test,reason): super().addSkip(test,reason); events.append("SKIP")
        def addExpectedFailure(self,test,err): super().addExpectedFailure(test,err); events.append("XFAIL")
        def addUnexpectedSuccess(self,test): super().addUnexpectedSuccess(test); events.append("XPASS")
        def addSubTest(self,test,subtest,err):
            super().addSubTest(test,subtest,err)
            if err: events.append("FAIL")
    hooks = []
    original_hook = sys.addaudithook
    def counted(hook):
        original_hook(hook)
        hooks.append(type(getattr(hook,"__self__",None)).__name__)
    started = time.monotonic()
    with _prepared_asyncio_loop(test_id):
        with patch.object(sys,"addaudithook",counted), \
                _child_network_guard(manifest["entry_profiles"][test_id]):
            result = unittest.TextTestRunner(verbosity=2,resultclass=Result).run(suite)
    validate_sources(manifest)
    require(sha(Path(manifest_path).read_bytes()) == expected_sha, "Manifest changed during child")
    status = "PASS" if events == ["PASS"] and result.testsRun == 1 and result.wasSuccessful() else "ERROR"
    write_json(receipt_path,dict(**base,status=status,tests_run=result.testsRun,events=events,
                  seconds=time.monotonic()-started,audit_hooks_installed=len(hooks)))
    return 0 if status == "PASS" else 1


def run_pilot(output, manifest_path=MANIFEST):
    return run_selection("pilot",output,manifest_path=manifest_path)


def run_selection(selection, output, *, manifest_path=MANIFEST, batch=None, smoke=False):
    started = time.monotonic()
    require(os.name == "nt", "Real Windows Jobs required; no skipped pilot")
    manifest, digest = load_manifest(manifest_path)
    require(selection != "guarded","Guarded cases must use scripts/ci_offline.py")
    ids = selection_ids(manifest,selection,smoke)
    scope = "smoke" if smoke else ("pilot" if selection == "pilot" else "selection")
    batch = batch or "local-"+uuid.uuid4().hex
    require(re.fullmatch("[A-Za-z0-9._-]{1,80}",batch),"Invalid qualification batch")
    output = Path(output).resolve()
    require(output.is_relative_to(ROOT/"runs"), "Pilot output must be under this checkout's runs/")
    output.parent.mkdir(parents=True,exist_ok=True)
    output.mkdir()  # Never adopt old results or resume a crashed pilot.
    run_id = uuid.uuid4().hex
    base = dict(**binding(manifest,digest),run_id=run_id,selection=selection,batch=batch,scope=scope)
    write_json(output/"run.json",dict(**base,expected_ids=ids,executable=str(Path(sys.executable).resolve()),
                  checkout_root=str(ROOT),python=sys.version,platform=platform.platform(),pid=os.getpid()))
    scratch = ROOT/"runs"/"f2p"
    scratch.mkdir(exist_ok=True)
    from scripts.ci_supplemental_policy import SupplementalPolicy
    import winprocess
    policy = SupplementalPolicy(ROOT,scratch/run_id[:8])
    hooks = []
    real_hook = sys.addaudithook
    def counted(hook):
        require(getattr(hook,"__self__",None) is policy,"Unexpected parent audit hook")
        real_hook(hook); hooks.append(hook)
    for index,test_id in enumerate(ids):
        validate_sources(manifest)
        require(sha(Path(manifest_path).read_bytes()) == digest,"Manifest drift before launch")
        require(checkout_head() == base["head"],"HEAD drift before launch")
        fixture = policy.new_fixture()
        temp = fixture.root/"t"; temp.mkdir()
        helper = fixture.root/"case.py"; helper.write_bytes(CAPSULE)
        receipt = output/f"child-{index:03}.json"
        command = [str(policy.python),"-I","-B",str(helper),str(ROOT),str(Path(manifest_path).absolute()),
                   test_id,str(receipt),digest,run_id,str(temp),base["head"],selection,batch,scope]
        pins = {ROOT/p:h for p,h in manifest["source_sha256"].items()}
        pins.update({helper:sha(CAPSULE),Path(manifest_path).absolute():digest})
        policy.register(fixture,profile="isolated_helper",boundary="native",argv=command,cwd=fixture.root,source_pins=pins)
        case = dict(**base,test_id=test_id,status="ERROR",child_sha256=None,native=None,error=None)
        write_json(output/f"intent-{index:03}.json",dict(**base,test_id=test_id,argv=command,helper_sha256=sha(CAPSULE)))
        job_name = "Local\\ci-pilot-"+uuid.uuid4().hex
        native = winprocess.ControllerVerifier(job_name)
        try:
            with open(os.devnull,"rb") as inp, (output/f"stdout-{index:03}.log").open("xb") as out, \
                    (output/f"stderr-{index:03}.log").open("xb") as err, patch.object(sys,"addaudithook",counted), policy.guard():
                native.launch(command,fixture.root,(inp,out,err))
                import ctypes
                member = winprocess.W.BOOL()
                require(native.k.IsProcessInJob(native.process,native.job.handle,ctypes.byref(member)) and member.value,
                        "Native test is outside its Job")
                identity = dict(native.identity)
                require(native.wait(0) is None,"Test was not suspended before resume")
                native.verify(identity)
                native.resume(identity)
                code = native.wait(manifest["case_budgets_seconds"][test_id])
                native.terminate_job()
                native.drain(10)
                observed = native.wait(10)
                require(code is not None and observed == code,"Case deadline/exit evidence failure")
                require(winprocess._handle_identity(native.k,native.process) is None,"Original test process remains live")
                case["native"] = dict(pid=native.pid,identity=identity,job=job_name,argv=command,
                    exit_code=code,cleanup="observed_empty",suspended_before_resume=True,job_member=True,
                    budget_seconds=manifest["case_budgets_seconds"][test_id])
            child = read_json(receipt)
            case["child_sha256"] = sha(receipt.read_bytes())
            require(all(child.get(k) == v for k,v in dict(**base,test_id=test_id).items()),"Child binding mismatch")
            require(code == 0 and child["status"] == "PASS","Child case did not pass")
            case["status"] = "PASS"
        except Exception as exc:
            case["error"] = repr(exc)
        finally:
            try:
                native.terminate_job(); native.drain(10)
            except Exception as exc:
                case["status"] = "ERROR"; case["error"] = "Cleanup failure: " + repr(exc)
            finally:
                native.close()
        case["admission"] = policy.events[-1] if policy.events else None
        write_json(output/f"case-{index:03}.json",case)
        print(json.dumps(dict(test_id=test_id,status=case["status"])),flush=True)
        if case["status"] != "PASS": break
    write_json(output/"lifecycle.json",dict(**base,parent_policy_instances=1,parent_audit_hooks=len(hooks),
                  per_case_process=True,recursive_guards=False,os_network_isolation=False))
    validate_sources(manifest)
    require(checkout_head() == base["head"],"HEAD drift after selection")
    from scripts.ci_verify_summary import verify_directory
    try:
        summary = verify_directory(output,manifest,digest)
    except (CoverageError, OSError, KeyError, TypeError) as exc:
        summary = dict(**base,complete=False,full_phase3_1_qualified=False,error=str(exc))
    summary["seconds"] = time.monotonic()-started
    write_json(output/"summary.json",summary)
    return summary


class GuardedReport:
    """Optional evidence only: the existing guarded runner still owns execution."""
    def __init__(self,output,batch):
        # Do not discover/import tests here: historical fixture wrappers must be
        # installed by ci_offline before tests capture fixture function aliases.
        self.digest = sha(MANIFEST.read_bytes())
        self.manifest = read_json(MANIFEST)
        require(self.manifest["manifest_version"] == VERSION and self.manifest["commit_binding"] == "immutable-checkout",
                "Wrong guarded manifest contract")
        validate_sources(self.manifest)
        require(re.fullmatch("[A-Za-z0-9._-]{1,80}",batch or ""),"Explicit guarded CI batch required")
        self.output = Path(output).absolute()
        require(self.output.is_relative_to(ROOT/"runs"),"Guarded evidence must be checkout-local")
        self.output.mkdir(parents=True)
        self.ids = self.manifest["groups"]["guarded"]
        require(len(exact_ids(self.ids)) == 390,"Exact guarded IDs required")
        self.base = dict(**binding(self.manifest,self.digest),run_id=uuid.uuid4().hex,
                         scope="guarded",selection="guarded",batch=batch)
        write_json(self.output/"run.json",dict(**self.base,expected_ids=self.ids,source="scripts/ci_offline.py"))
        self.seen = set()
        owner = self
        class Result(unittest.TextTestResult):
            def startTest(self,test):
                self.started = time.monotonic(); self.events = []
                super().startTest(test)
            def addSuccess(self,test): super().addSuccess(test); self.events.append("PASS")
            def addFailure(self,test,err): super().addFailure(test,err); self.events.append("FAIL")
            def addError(self,test,err): super().addError(test,err); self.events.append("ERROR")
            def addSkip(self,test,reason): super().addSkip(test,reason); self.events.append("SKIP")
            def addExpectedFailure(self,test,err): super().addExpectedFailure(test,err); self.events.append("XFAIL")
            def addUnexpectedSuccess(self,test): super().addUnexpectedSuccess(test); self.events.append("XPASS")
            def addSubTest(self,test,subtest,err):
                super().addSubTest(test,subtest,err)
                if err: self.events.append("FAIL")
            def stopTest(self,test):
                super().stopTest(test)
                test_id = test.id()
                require(test_id in owner.ids and test_id not in owner.seen,"Duplicate/foreign guarded case")
                owner.seen.add(test_id)
                write_json(owner.output/f"case-{owner.ids.index(test_id):03}.json",dict(**owner.base,test_id=test_id,
                    events=self.events,status="PASS" if self.events == ["PASS"] else "ERROR",seconds=time.monotonic()-self.started))
        self.result_class = Result

    def seal(self,head,head_probes):
        require(head == self.base["head"] and self.seen == set(self.ids),"Guarded run incomplete")
        validate_sources(self.manifest)
        require(sha(MANIFEST.read_bytes()) == self.digest,"Manifest drift during guarded run")
        write_json(self.output/"guarded-seal.json",dict(**self.base,complete=True,cli_help=True,
                   head_probes=head_probes,guard_denials=0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("check","pilot","selection"))
    parser.add_argument("--output",type=Path)
    parser.add_argument("--selection")
    parser.add_argument("--batch")
    parser.add_argument("--smoke",action="store_true")
    args = parser.parse_args()
    try:
        if args.mode == "check":
            manifest,digest = load_manifest()
            print(json.dumps(dict(manifest_sha256=digest,groups={k:len(v) for k,v in manifest["groups"].items()},
                                  pilot=len(manifest["pilot_ids"]),full_phase3_1_qualified=False)))
        else:
            require(args.output is not None,"A fresh explicit --output directory is required")
            require(args.mode == "pilot" or args.selection is not None,"Explicit selection required")
            summary = (run_pilot(args.output) if args.mode == "pilot" else
                       run_selection(args.selection,args.output,batch=args.batch,smoke=args.smoke))
            print(json.dumps(summary))
            return 0 if summary["complete"] else 1
    except (CoverageError, OSError, KeyError, TypeError) as exc:
        print("BLOCKED: "+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.path.insert(0,str(ROOT))
    # Share exception/class identities with the summary verifier and test imports.
    from scripts.ci_supplemental import main as canonical_main
    raise SystemExit(canonical_main())
