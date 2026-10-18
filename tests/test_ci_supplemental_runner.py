"""Inventory and synthetic evidence negatives; actual native pilot is separate."""
import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import runpy
import tempfile
import unittest
from unittest.mock import patch

from scripts import ci_supplemental as ci
from scripts import ci_verify_summary as summary


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ci.read_json(ci.MANIFEST)
        self.inventory = {k:list(v) for k,v in self.manifest["groups"].items() if k != "outside_scope"}
        self.inventory["all"] = sorted(i for group in self.manifest["groups"].values() for i in group)

    def refuse(self):
        with self.assertRaises(ci.CoverageError): ci.validate_sets(self.manifest,self.inventory)

    def test_real_source_discovery_and_pins_match(self):
        ci.load_manifest()

    def test_duplicate_id_is_refused(self):
        group = self.manifest["groups"]["supplemental"]
        group.insert(0,group[0]); self.refuse()

    def test_overlapping_group_is_refused(self):
        self.manifest["groups"]["outside_scope"] = sorted(self.manifest["groups"]["outside_scope"] +
                                                         self.manifest["groups"]["policy_proof"][:1])
        self.refuse()

    def test_missing_mandatory_policy_case_is_refused(self):
        self.manifest["groups"]["policy_proof"].pop(); self.refuse()

    def test_unknown_discovered_case_is_refused(self):
        self.inventory["all"].append("test_new.NewTests.test_unreviewed"); self.refuse()

    def test_guarded_source_selection_drift_is_refused(self):
        self.inventory["guarded"].pop(); self.refuse()

    def test_supervisor_lifecycle_cannot_move_to_supplemental(self):
        group = self.manifest["groups"]
        case = next(i for i in group["guarded"] if "SupervisorLifecycleTests" in i)
        group["supplemental"] = sorted(group["supplemental"]+[case]); self.refuse()

    def test_duplicate_shard_case_is_refused(self):
        shard = self.manifest["draft_shards"][0]
        shard["test_ids"][0] = self.manifest["draft_shards"][1]["test_ids"][0]
        shard["test_ids"].sort(); self.refuse()

    def test_missing_shard_case_is_refused(self):
        shard = self.manifest["draft_shards"][0]["test_ids"]
        old = shard.pop(); self.refuse(); shard.append(old)
        self.manifest["case_budgets_seconds"].pop(old); self.refuse()

    def test_stale_manifest_version_or_head_is_refused(self):
        for key in ("commit_binding","manifest_version"):
            with self.subTest(key=key):
                old = self.manifest[key]; self.manifest[key] = "stale"
                self.refuse(); self.manifest[key] = old
        with patch.object(ci.subprocess,"check_output",return_value="not-a-commit"):
            with self.assertRaises(ci.CoverageError): ci.checkout_head()
        with patch.object(ci.subprocess,"check_output",return_value="a"*40), patch.dict(os.environ,{"TRIGLAV_CI_EXPECTED_HEAD":"b"*40}):
            with self.assertRaises(ci.CoverageError): ci.checkout_head()

    def test_boolean_schema_version_is_refused(self):
        self.manifest["schema_version"] = True; self.refuse()

    def test_pilot_cannot_silently_drop_a_case(self):
        self.manifest["pilot_ids"].pop(); self.refuse()

    def test_source_digest_drift_is_refused(self):
        name = "scripts/ci_supplemental_policy.py"
        self.manifest["source_sha256"][name] = "0"*64
        with self.assertRaises(ci.CoverageError): ci.validate_sources(self.manifest)

    def test_unpinned_source_is_refused(self):
        self.manifest["source_sha256"].pop("scripts/ci_supplemental_policy.py")
        with self.assertRaises(ci.CoverageError): ci.validate_sources(self.manifest)

    def test_source_drift_is_refused_before_importing_discovery(self):
        self.manifest["source_sha256"]["scripts/ci_supplemental_policy.py"] = "0"*64
        with patch.object(ci,"read_json",return_value=self.manifest), patch.object(ci,"discover") as discovery:
            with self.assertRaises(ci.CoverageError): ci.load_manifest()
            discovery.assert_not_called()
        with tempfile.TemporaryDirectory(dir=ci.ROOT/"runs",prefix="guard-report-") as temp, \
                patch.object(ci,"discover",side_effect=AssertionError("Do not import tests before historical wrappers")):
            report = ci.GuardedReport(Path(temp)/"export","synthetic")
            self.assertEqual(len(report.ids),390)
            self.assertFalse((report.output/"guarded-seal.json").exists())

    def test_duplicate_or_nonfinite_json_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"bad.json"
            for raw in ('{"x":1,"x":2}', '{"x":NaN}'):
                with self.subTest(raw=raw):
                    path.write_text(raw)
                    with self.assertRaises(ci.CoverageError): ci.read_json(path)


class SummaryTests(unittest.TestCase):
    def setUp(self):
        self.manifest = ci.read_json(ci.MANIFEST)
        self.digest = ci.sha(ci.MANIFEST.read_bytes())
        self.base = dict(**ci.binding(self.manifest,self.digest),run_id="f"*32,scope="pilot",selection="pilot",batch="synthetic")
        self.run = dict(**self.base,expected_ids=self.manifest["pilot_ids"],executable=str(Path(sys.executable).resolve()),checkout_root=str(ci.ROOT))
        self.lifecycle = dict(**self.base,parent_policy_instances=1,parent_audit_hooks=1,
                              per_case_process=True,recursive_guards=False,os_network_isolation=False)
        self.records,self.children,self.intents = {},{},{}
        for i,test_id in enumerate(self.manifest["pilot_ids"]):
            helper = ci.ROOT/"runs"/"synthetic"/str(i)/"case.py"
            command = [str(Path(sys.executable).resolve()),"-I","-B",str(helper),str(ci.ROOT),str(ci.MANIFEST),
                       test_id,"synthetic-receipt.json",self.digest,"f"*32,"synthetic-temp",self.base["head"],"pilot","synthetic","pilot"]
            child = dict(**self.base,test_id=test_id,status="PASS",tests_run=1,events=["PASS"],seconds=1.0,audit_hooks_installed=1)
            self.children[i] = dict(value=child,sha256=ci.sha(json.dumps(child).encode()))
            admission = dict(decision="ALLOW",boundary="native",profile="isolated_helper",argv=command,
                launch_number=1,sources={**{str(ci.ROOT/p):h for p,h in self.manifest["source_sha256"].items()},
                                        str(helper):ci.sha(ci.CAPSULE),str(ci.MANIFEST):self.digest})
            native = dict(pid=i+100,identity=dict(created=str(i+1000),executable=os.path.normcase(str(Path(sys.executable).resolve()))),
                          job="Local\\ci-pilot-"+format(i,"032x"),argv=command,exit_code=0,cleanup="observed_empty",
                          suspended_before_resume=True,job_member=True,budget_seconds=self.manifest["case_budgets_seconds"][test_id])
            self.records[i] = dict(**self.base,test_id=test_id,status="PASS",error=None,native=native,
                                   admission=admission,child_sha256=self.children[i]["sha256"])
            self.intents[i] = dict(**self.base,test_id=test_id,argv=command,helper_sha256=ci.sha(ci.CAPSULE))

    def verify(self):
        return summary.verify_records(self.manifest,self.digest,self.run,self.records,self.children,self.intents,self.lifecycle)

    def refuse(self):
        with self.assertRaises(ci.CoverageError): self.verify()

    def test_complete_synthetic_evidence_is_only_pilot_pass(self):
        result = self.verify()
        self.assertTrue(result["complete"])
        self.assertEqual(result["distinct_passed"],25)
        self.assertFalse(result["full_phase3_1_qualified"])
        full = summary.combine(self.manifest,self.digest,"synthetic",self.parts())
        self.assertEqual(full["distinct_passed"],1296)
        self.assertTrue(full["full_phase3_1_qualified"])

    def parts(self):
        return {name:dict(**{k:v for k,v in self.base.items() if k not in {"selection","scope"}},selection=name,
                         scope="guarded" if name == "guarded" else "selection",complete=True,
                         status="GUARDED_PASS" if name == "guarded" else "SELECTION_PASS",failed=0,skipped=0,
                         full_phase3_1_qualified=False,distinct_passed=len(ci.selection_ids(self.manifest,name)),
                         verified_ids=list(ci.selection_ids(self.manifest,name)))
                for name in ["guarded","policy","runner",*("shard-"+str(i) for i in range(1,10))]}

    def test_missing_case_child_or_intent_is_refused(self):
        for records in (self.records,self.children,self.intents):
            old = records.pop(0); self.refuse(); records[0] = old
        for missing in self.parts():
            parts = self.parts(); parts.pop(missing)
            with self.assertRaises(ci.CoverageError): summary.combine(self.manifest,self.digest,"synthetic",parts)

    def test_unexpected_artifact_is_refused(self):
        self.records[1000] = copy.deepcopy(self.records[0]); self.refuse()

    def test_duplicate_case_id_is_refused(self):
        self.records[1]["test_id"] = self.records[0]["test_id"]; self.refuse()
        parts = self.parts(); parts["shard-1"]["verified_ids"][0] = parts["shard-2"]["verified_ids"][0]
        with self.assertRaises(ci.CoverageError): summary.combine(self.manifest,self.digest,"synthetic",parts)

    def test_skip_failure_error_xfail_and_unreported_child_are_refused(self):
        for events in (["SKIP"],["FAIL"],["ERROR"],["XFAIL"],["XPASS"],[],["PASS","PASS"],["FAIL","PASS"]):
            with self.subTest(events=events):
                self.children[0]["value"]["events"] = events; self.refuse()

    def test_zero_or_extra_test_count_is_refused(self):
        for count in (0,2,True):
            self.children[0]["value"]["tests_run"] = count; self.refuse()

    def test_stale_run_head_manifest_and_nonce_are_refused(self):
        for key in ("head","manifest_version","manifest_sha256","run_id"):
            old = self.records[0][key]; self.records[0][key] = "stale"
            self.refuse(); self.records[0][key] = old
        for key,value in (("head","0"*40),("batch","foreign"),("complete",False),("scope","smoke"),
                          ("status","FAILED"),("skipped",1),("failed",1)):
            parts = self.parts(); parts["runner"][key] = value
            with self.assertRaises(ci.CoverageError): summary.combine(self.manifest,self.digest,"synthetic",parts)

    def test_boolean_schema_binding_is_refused(self):
        self.run["schema_version"] = True; self.refuse()

    def test_falsified_expected_selection_is_refused(self):
        self.run["expected_ids"] = self.run["expected_ids"][:-1]; self.refuse()

    def test_failure_or_unobserved_native_cleanup_is_refused(self):
        native = self.records[0]["native"]
        for key,value in (("exit_code",1),("exit_code",True),("cleanup","unknown"),("suspended_before_resume",False),("job_member",False)):
            old = native[key]; native[key] = value; self.refuse(); native[key] = old

    def test_original_identity_cannot_be_replaced_by_bare_pid(self):
        self.records[0]["native"]["identity"]["created"] = ""; self.refuse()

    def test_reused_native_execution_is_refused(self):
        self.records[1]["native"]["job"] = self.records[0]["native"]["job"]; self.refuse()

    def test_argv_drift_is_refused(self):
        self.records[0]["native"]["argv"] = ["unapproved.exe"]; self.refuse()

    def test_missing_admission_or_wrong_fixture_hash_is_refused(self):
        admission = self.records[0]["admission"]
        admission["decision"] = "DENY"; self.refuse(); admission["decision"] = "ALLOW"
        admission["sources"][self.intents[0]["argv"][3]] = "0"*64; self.refuse()

    def test_missing_dependency_pin_is_refused(self):
        self.records[0]["admission"]["sources"].pop(str(ci.ROOT/"scripts/ci_supplemental_policy.py")); self.refuse()

    def test_corrupt_child_receipt_hash_is_refused(self):
        self.records[0]["child_sha256"] = "0"*64; self.refuse()

    def test_accumulating_parent_or_child_audit_hooks_is_refused(self):
        for value in (2,True):
            self.lifecycle["parent_audit_hooks"] = value; self.refuse()
        self.lifecycle["parent_audit_hooks"] = 1
        self.children[0]["value"]["audit_hooks_installed"] = 2; self.refuse()

    def test_false_recursive_or_os_isolation_claim_is_refused(self):
        for key in ("recursive_guards","os_network_isolation"):
            self.lifecycle[key] = True; self.refuse(); self.lifecycle[key] = False

    def test_nonfinite_or_negative_duration_is_refused(self):
        for value in (float("nan"),float("inf"),-1,True):
            self.children[0]["value"]["seconds"] = value; self.refuse()

    def test_directory_without_required_artifacts_is_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaises(ci.CoverageError): summary.verify_directory(temp,self.manifest,self.digest)

    def test_cli_launch_failure_seals_only_incomplete_summary(self):
        import winprocess
        with tempfile.TemporaryDirectory(dir=ci.ROOT/"runs",prefix="ci-neg-") as temp:
            output = Path(temp)/"pilot"
            with patch.object(winprocess,"ControllerVerifier") as factory, \
                    patch.object(sys,"argv",["ci_supplemental.py","pilot","--output",str(output)]), \
                    redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as stopped:
                factory.return_value.launch.side_effect = OSError("pre-creation test fault")
                runpy.run_path(str(ci.ROOT/"scripts/ci_supplemental.py"),run_name="__main__")
            self.assertEqual(stopped.exception.code,1)
            result = ci.read_json(output/"summary.json")
            self.assertFalse(result["complete"])
            self.assertFalse(result["full_phase3_1_qualified"])
            self.assertEqual(len(list(output.glob("case-*.json"))),1)
            self.assertEqual(ci.read_json(output/"case-000.json")["status"],"ERROR")
            factory.assert_called_once()
