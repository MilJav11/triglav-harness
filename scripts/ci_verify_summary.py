"""Fail-closed pilot evidence verification. Never grants full qualification."""
import argparse
import math
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))
from scripts.ci_supplemental import (CAPSULE, CoverageError, MANIFEST, binding, load_manifest,
                                     read_json, require, sha, selection_ids)


def verify_records(manifest,digest,run,records,children,intents,lifecycle):
    def bound(artifact,base):
        return type(artifact) is dict and all(type(artifact.get(k)) is type(v) and artifact.get(k) == v for k,v in base.items())
    base = binding(manifest,digest)
    require(bound(run,base),"Stale run binding")
    scope,selection = run["scope"],run["selection"]
    require(scope in {"pilot","selection","smoke"} and (scope != "pilot" or selection == "pilot"),"Wrong scope")
    ids = selection_ids(manifest,selection,scope == "smoke")
    require(run.get("expected_ids") == ids,"Wrong expected selection IDs")
    run_id = run["run_id"]
    require(type(run_id) is str and re.fullmatch("[0-9a-f]{32}",run_id),"Invalid run ID")
    base["run_id"] = run_id
    require(type(run["batch"]) is str and re.fullmatch("[A-Za-z0-9._-]{1,80}",run["batch"]),"Invalid batch")
    base.update(batch=run["batch"],selection=selection,scope=scope)
    count = len(ids)
    expected = set(range(count))
    require(set(records) == set(children) == set(intents) == expected,"Missing or duplicate/unexpected artifacts")
    seen, identities, jobs = set(),set(),set()
    for index,test_id in enumerate(ids):
        case, child, intent = records[index],children[index]["value"],intents[index]
        for artifact in (case,child,intent):
            require(bound(artifact,base),"Stale or foreign per-case binding")
            require(artifact.get("test_id") == test_id,"Foreign, duplicate or unreported test ID")
        require(test_id not in seen,"Duplicate case")
        seen.add(test_id)
        require(case.get("status") == child.get("status") == "PASS" and case.get("error") is None,"Case did not pass")
        require(type(child.get("tests_run")) is int and child["tests_run"] == 1 and child.get("events") == ["PASS"],
                "Skipped, failed, extra or unreported child tests")
        seconds = child.get("seconds")
        require(type(seconds) in (int,float) and math.isfinite(seconds) and seconds >= 0,"Invalid case duration")
        require(type(child.get("audit_hooks_installed")) is int and 0 <= child["audit_hooks_installed"] <= 1,
                "Per-case hook lifecycle violated")
        require(case.get("child_sha256") == children[index]["sha256"],"Child receipt hash mismatch")
        native = case["native"]
        require(type(native["pid"]) is int and native["pid"] > 0,"Invalid native PID")
        identity = native["identity"]
        require(set(identity) == {"created","executable"} and type(identity["created"]) is str
                and identity["created"].isdigit()
                and identity["executable"] == os.path.normcase(run["executable"]),"Invalid original native identity")
        key = (native["pid"],identity["created"])
        require(key not in identities and native["job"] not in jobs,"Reused native execution")
        require(re.fullmatch(r"Local\\ci-pilot-[0-9a-f]{32}",native["job"]),"Foreign native Job")
        identities.add(key); jobs.add(native["job"])
        require(type(native["exit_code"]) is int and native["exit_code"] == 0
                and native["cleanup"] == "observed_empty" and native["suspended_before_resume"] is True
                and native["job_member"] is True,
                "Incomplete exit, suspension or cleanup evidence")
        require(native["budget_seconds"] == manifest["case_budgets_seconds"][test_id] and seconds <= native["budget_seconds"],
                "Execution budget mismatch/exhaustion")
        argv = intent["argv"]
        require(native["argv"] == argv and len(argv) == 15 and argv[:3] == [run["executable"],"-I","-B"]
                and argv[6] == test_id and argv[8:10] == [digest,run_id]
                and argv[11:] == [base["head"],selection,run["batch"],scope],"Native argv binding drift")
        require(intent["helper_sha256"] == sha(CAPSULE),"Wrong trusted entry source")
        admission = case["admission"]
        require(admission.get("decision") == "ALLOW" and admission.get("boundary") == "native"
                and admission.get("profile") == "isolated_helper" and admission.get("argv") == argv
                and type(admission.get("launch_number")) is int and admission["launch_number"] == 1,"Missing closed launch admission")
        require(admission["sources"].get(argv[3]) == sha(CAPSULE),"Unpinned trusted helper")
        require(admission["sources"].get(argv[5]) == digest,"Unpinned manifest")
        require(all(admission["sources"].get(str(Path(run["checkout_root"])/p)) == h for p,h in manifest["source_sha256"].items()),
                "Missing reviewed test/import source pins")
    require(bound(lifecycle,base),"Lifecycle binding mismatch")
    require(type(lifecycle.get("parent_policy_instances")) is type(lifecycle.get("parent_audit_hooks")) is int
            and lifecycle["parent_policy_instances"] == lifecycle["parent_audit_hooks"] == 1
            and lifecycle.get("per_case_process") is True and lifecycle.get("recursive_guards") is False
            and lifecycle.get("os_network_isolation") is False,"Unbounded or misreported hook/security lifecycle")
    return dict(**base,status="PILOT_PASS" if scope == "pilot" else "SMOKE_PASS" if scope == "smoke" else "SELECTION_PASS",
                complete=True,distinct_passed=count,failed=0,skipped=0,verified_ids=ids,
                full_phase3_1_qualified=False,recursive_guards=False,os_network_isolation=False)


def verify_directory(directory,manifest,digest):
    directory = Path(directory)
    require((directory/"run.json").is_file(),"Missing run header")
    run = read_json(directory/"run.json")
    if run["scope"] == "guarded":
        return verify_guarded(directory,manifest,digest,run)
    count = len(selection_ids(manifest,run["selection"],run["scope"] == "smoke"))
    for prefix,suffix in (("case","json"),("child","json"),("intent","json"),("stdout","log"),("stderr","log")):
        require({p.name for p in directory.glob(prefix+"-*"+"."+suffix)} ==
                {f"{prefix}-{i:03}.{suffix}" for i in range(count)},"Missing or unexpected "+prefix+" artifacts")
    records,children,intents = {},{},{}
    for i in range(count):
        records[i] = read_json(directory/f"case-{i:03}.json")
        path = directory/f"child-{i:03}.json"
        children[i] = dict(value=read_json(path),sha256=sha(path.read_bytes()))
        intents[i] = read_json(directory/f"intent-{i:03}.json")
    return verify_records(manifest,digest,run,records,children,intents,
                          read_json(directory/"lifecycle.json"))


def verify_guarded(directory,manifest,digest,run):
    base = binding(manifest,digest)
    require(all(type(run.get(k)) is type(v) and run.get(k) == v for k,v in base.items()),"Foreign guarded binding")
    ids = manifest["groups"]["guarded"]
    require(run["selection"] == "guarded" and run["expected_ids"] == ids,"Wrong guarded selection")
    require(re.fullmatch("[0-9a-f]{32}",run["run_id"]) and re.fullmatch("[A-Za-z0-9._-]{1,80}",run["batch"]),"Invalid guarded identity")
    base.update(run_id=run["run_id"],batch=run["batch"],selection="guarded",scope="guarded")
    directory = Path(directory)
    require({p.name for p in directory.glob("case-*.json")} == {f"case-{i:03}.json" for i in range(390)},"Incomplete guarded cases")
    for i,test_id in enumerate(ids):
        case = read_json(directory/f"case-{i:03}.json")
        require(all(type(case.get(k)) is type(v) and case.get(k) == v for k,v in base.items()) and
                case.get("test_id") == test_id and case.get("events") == ["PASS"] and case.get("status") == "PASS",
                "Failed/skipped/stale/duplicate guarded case")
        require(type(case["seconds"]) in (int,float) and math.isfinite(case["seconds"]) and case["seconds"] >= 0,"Bad guarded time")
    seal = read_json(directory/"guarded-seal.json")
    require(all(type(seal.get(k)) is type(v) and seal.get(k) == v for k,v in base.items()) and
            seal.get("cli_help") is True and seal.get("complete") is True and
            type(seal.get("guard_denials")) is int and seal["guard_denials"] == 0 and
            type(seal.get("head_probes")) is int and seal["head_probes"] >= 1,"Missing guarded CLI/HEAD/guard seal")
    return dict(**base,complete=True,status="GUARDED_PASS",verified_ids=ids,distinct_passed=390,
                failed=0,skipped=0,full_phase3_1_qualified=False)


def combine(manifest,digest,batch,parts):
    expected = {"guarded","policy","runner",*("shard-"+str(i) for i in range(1,10))}
    require(set(parts) == expected,"Missing, extra or duplicate qualification components")
    base = binding(manifest,digest)
    all_ids = []
    for name,part in parts.items():
        require(all(type(part.get(k)) is type(v) and part.get(k) == v for k,v in base.items()) and
                part.get("batch") == batch and part.get("selection") == name and part.get("complete") is True and
                part.get("scope") == ("guarded" if name == "guarded" else "selection"),"Foreign/stale/incomplete component")
        require(part.get("verified_ids") == selection_ids(manifest,name),"Missing or duplicate component IDs")
        require(part.get("status") == ("GUARDED_PASS" if name == "guarded" else "SELECTION_PASS") and
                part.get("distinct_passed") == len(part["verified_ids"]) and part.get("failed") == 0 and
                part.get("skipped") == 0 and part.get("full_phase3_1_qualified") is False,"Failed or misreported component")
        all_ids.extend(part["verified_ids"])
    required = {i for k,v in manifest["groups"].items() if k != "outside_scope" for i in v}
    require(len(all_ids) == len(set(all_ids)) == 1296 and set(all_ids) == required,"Incomplete full coverage")
    return dict(**base,batch=batch,status="FULL_PHASE3_1_QUALIFIED",complete=True,distinct_passed=1296,
                full_phase3_1_qualified=True,groups={k:len(v) for k,v in manifest["groups"].items() if k != "outside_scope"})


def verify_full(directory,manifest,digest,batch):
    directory = Path(directory)
    names = ["guarded","policy","runner",*("shard-"+str(i) for i in range(1,10))]
    require({p.name for p in directory.iterdir()} == {batch+"-"+n for n in names},"Missing/foreign artifact directories")
    parts = {n:verify_directory(directory/(batch+"-"+n),manifest,digest) for n in names}
    return combine(manifest,digest,batch,parts)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory",type=Path)
    parser.add_argument("--full",action="store_true")
    parser.add_argument("--batch")
    parser.add_argument("--output",type=Path)
    args = parser.parse_args()
    import json
    try:
        manifest,digest = load_manifest(MANIFEST)
        if args.full:
            require(args.batch is not None,"Full qualification requires the workflow batch")
            require(os.environ.get("TRIGLAV_CI_UPSTREAM_OK","true") == "true","An upstream job failed")
        result = (verify_full(args.directory,manifest,digest,args.batch) if args.full else
                  verify_directory(args.directory,manifest,digest))
        if args.output:
            from scripts.ci_supplemental import write_json
            write_json(args.output,result)
        print(json.dumps({k:v for k,v in result.items() if k != "verified_ids"}))
    except (CoverageError,OSError,KeyError,TypeError) as exc:
        print(json.dumps(dict(complete=False,full_phase3_1_qualified=False,error=str(exc))))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
