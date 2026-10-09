"""Ownership, boot, drift, and WAL safety without model inference or timing luck."""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import durable
import environment
import supervision as s
import winprocess
from test_durable import fixture, FakeGateway
from harness import EventLog


IDENTITY = {"created": "123456", "executable": os.path.normcase(str(Path(sys.executable).resolve()))}


class MemoryStore:
    def __init__(self):
        self.state = {"run_id": "test-run", "supervision": s.initial()}
        self.mutex = threading.RLock()
        self.events = []

    def commit(self, **fields):
        self.state.update(copy.deepcopy(fields))
        s.validate(self.state["supervision"], "test-run")
        self.events.append(fields["supervision_event"]["event"])


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.log = EventLog(Path(temporary.name) / "events.jsonl")
        self.store = MemoryStore()
        self.clock = Mock(return_value=100)
        self.boot = Mock(return_value="boot-a")
        self.inspect = Mock(return_value=IDENTITY)
        self.sup = s.Supervisor(self.store, self.clock, self.boot, self.inspect)
        self.child_id = self.sup.register("verification_command", sys.executable, ["hidden argument"])

    def start(self):
        self.sup.started(self.child_id, 42)
        return self.sup.child(self.child_id)

    def test_registration_has_full_ownership_and_no_raw_command(self):
        child = self.sup.child(self.child_id)
        self.assertEqual(child["state"], "STARTING")
        self.assertEqual(child["run_id"], "test-run")
        self.assertEqual(child["boot"], "boot-a")
        self.assertEqual(child["controller_pid"], os.getpid())
        self.assertEqual(len(child["token"]), 32)
        self.assertNotIn("hidden argument", json.dumps(child))
        self.assertEqual(self.store.events, ["process_registered"])

    def test_pid_alone_is_not_ownership(self):
        child = self.sup.child(self.child_id)
        child["pid"] = 42
        self.assertEqual(self.sup.observe(child)["state"], "UNKNOWN")
        self.inspect.assert_not_called()

    def test_verified_process_is_observable(self):
        self.assertEqual(self.sup.observe(self.start())["state"], "RUNNING")

    def test_pid_reuse_detected(self):
        child = self.start()
        self.inspect.return_value = {**IDENTITY, "created": "different"}
        self.assertEqual(self.sup.observe(child)["reason"], "PID_REUSED")

    def test_executable_mismatch_detected(self):
        self.inspect.return_value = {**IDENTITY, "executable": "other.exe"}
        with self.assertRaisesRegex(RuntimeError, "executable"):
            self.start()

    def test_unrelated_pid_never_killed(self):
        self.start()
        self.inspect.return_value = {**IDENTITY, "created": "different"}
        with patch("winprocess.terminate_owned_job") as kill, self.assertRaises(durable.DurableError):
            self.sup.terminate(self.child_id)
        kill.assert_not_called()

    def test_natural_exit_recorded(self):
        self.start()
        self.sup.exited(self.child_id, 0)
        self.assertEqual(self.sup.child(self.child_id)["termination"], "natural")
        self.assertEqual(self.sup.child(self.child_id)["exit_code"], 0)

    def test_forced_exit_recorded_after_stopping(self):
        self.start()
        with patch("winprocess.terminate_owned_job") as kill:
            self.sup.terminate(self.child_id)
        kill.assert_called_once()
        self.assertEqual(self.store.events[-2:], ["process_stopping", "process_exit"])
        self.assertEqual(self.sup.child(self.child_id)["termination"], "forced")

    def test_death_during_cleanup_leaves_stopping(self):
        self.start()
        with patch("winprocess.terminate_owned_job", side_effect=RuntimeError), self.assertRaises(RuntimeError):
            self.sup.terminate(self.child_id)
        self.assertEqual(self.sup.reconcile()[0]["state"], "STOPPING")

    def test_death_before_start_record_leaves_unknown(self):
        with self.assertRaisesRegex(durable.DurableError, "UNKNOWN"):
            self.sup.require_idle()

    def test_launch_failure_is_known_exit(self):
        self.sup.exited(self.child_id, None, "launch_failed")
        self.sup.require_idle()
        self.assertEqual(self.sup.child(self.child_id)["state"], "EXITED")

    def test_live_verifier_blocks_duplicate(self):
        self.start()
        with self.assertRaisesRegex(durable.DurableError, "RUNNING"):
            self.sup.require_idle()

    def test_child_exit_while_down_is_lost_not_success(self):
        self.start()
        self.inspect.return_value = None
        self.sup.require_idle()
        child = self.sup.child(self.child_id)
        self.assertEqual(child["state"], "LOST")
        self.assertIsNone(child["exit_code"])

    def test_reboot_invalidates_live_assumption_without_inspection(self):
        self.start()
        self.boot.return_value = "boot-b"
        self.inspect.reset_mock()
        self.sup.require_idle()
        self.inspect.assert_not_called()
        self.assertEqual(self.sup.child(self.child_id)["reason"], "LOST_AFTER_REBOOT")

    def test_reboot_never_signals_old_pid(self):
        self.start()
        self.boot.return_value = "boot-b"
        with patch("winprocess.terminate_owned_job") as kill, self.assertRaises(durable.DurableError):
            self.sup.terminate(self.child_id)
        kill.assert_not_called()

    def test_inspection_denied_fails_safe(self):
        self.start()
        self.inspect.side_effect = OSError("access denied")
        with self.assertRaisesRegex(durable.DurableError, "UNKNOWN"):
            self.sup.require_idle()

    def test_stale_heartbeat(self):
        c = {"boot": "a", "heartbeat": 10}
        self.assertEqual(s.controller_status(c, "a", lambda: 56)["heartbeat"], "stale")

    def test_fresh_heartbeat(self):
        self.assertEqual(s.controller_status({"boot": "a", "heartbeat": 10}, "a", lambda: 20)["heartbeat"], "fresh")

    def test_clock_rollback_is_stale(self):
        self.assertEqual(s.controller_status({"boot": "a", "heartbeat": 10}, "a", lambda: 9)["heartbeat"], "stale")

    def test_heartbeat_changes_no_progress(self):
        self.start()
        self.sup.heartbeat()
        self.assertNotIn("verified_progress", self.store.state)
        self.assertEqual(self.store.events[-2:], ["controller_heartbeat", "process_heartbeat"])

    def test_heartbeat_never_revives_exit(self):
        self.start()
        self.sup.exited(self.child_id, 0)
        self.sup.heartbeat()
        self.assertEqual(self.sup.child(self.child_id)["state"], "EXITED")

    def test_corrupt_state_rejected(self):
        for field, value in (("identity", {}), ("state", "SUCCESS"), ("pid", -1), ("token", ""), ("boot", None)):
            data = copy.deepcopy(self.store.state["supervision"])
            data["children"][self.child_id][field] = value
            with self.subTest(field=field), self.assertRaises(durable.DurableError):
                s.validate(data, "test-run")

    def test_model_server_cannot_be_job_killed(self):
        self.start()
        self.store.state["supervision"]["children"][self.child_id]["containment"] = "delegated"
        with patch("winprocess.terminate_owned_job") as kill, self.assertRaises(durable.DurableError):
            self.sup.terminate(self.child_id)
        kill.assert_not_called()

    def test_unowned_model_unload_refused(self):
        with self.assertRaisesRegex(durable.DurableError, "ownership not proven"):
            self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 567}])

    def test_empty_gateway_cannot_clear_uncertain_model_startup(self):
        self.sup.register("model_server", sys.executable, [], "delegated")
        with self.assertRaisesRegex(durable.DurableError, "empty gateway does not prove it resolved"):
            self.sup.guard_model_unload([], [])

    def ambiguous_model_launch(self):
        child_id = self.sup.register("model_server", sys.executable, [], "delegated")
        child = self.sup.child(child_id)
        child.update(parent_pid=5, parent_identity=IDENTITY, alias="llm-code")
        self.sup.change("model_launch_intent", child)
        with self.assertRaises(RuntimeError):
            self.sup.model_observed(child_id, [])
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")
        return child_id

    def test_ambiguous_launch_with_empty_gateway_blocks_next_model_launch(self):
        import harness
        self.ambiguous_model_launch()
        gateway = harness.Gateway({"roles": {"code": "llm-code"}}, self.log)
        gateway.supervisor = self.sup
        registered = len(self.store.state["supervision"]["children"])
        with patch.object(harness.Gateway, "request", return_value=[]) as request, \
             patch("harness.servers", return_value=[]), \
             patch("harness.available_ram_gb", return_value=999), \
             self.assertRaisesRegex(durable.DurableError, "empty gateway does not prove it resolved"):
            gateway.switch("code")
        self.assertFalse([c for c in request.call_args_list if "/upstream/" in str(c)])
        self.assertEqual(len(self.store.state["supervision"]["children"]), registered)
        self.assertIsNone(gateway.active_role)

    def test_ambiguous_launch_blocks_model_registration_directly(self):
        self.ambiguous_model_launch()
        with patch.object(self.sup, "register") as register, \
             self.assertRaisesRegex(durable.DurableError, "ambiguous"):
            self.sup.model_starting("code")
        register.assert_not_called()

    def test_ambiguous_launch_blocks_unload_even_when_another_model_is_owned(self):
        self.ambiguous_model_launch()
        self.model()
        with self.assertRaisesRegex(durable.DurableError, "ambiguous"):
            self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])

    def test_ambiguous_launch_survives_reconcile_and_resume(self):
        self.sup.exited(self.child_id, None, "launch_failed")
        self.sup.require_idle()
        ambiguous = self.ambiguous_model_launch()
        self.sup.reconcile()
        with self.assertRaisesRegex(durable.DurableError, ambiguous + " UNKNOWN"):
            self.sup.require_idle()

    def test_ambiguous_model_launch_stays_untrusted(self):
        child = self.sup.child(self.child_id)
        child.update(parent_pid=5, parent_identity=IDENTITY)
        self.sup.change("model_launch_intent", child)
        with self.assertRaises(RuntimeError):
            self.sup.model_observed(self.child_id, [])
        self.assertEqual(self.sup.child(self.child_id)["state"], "UNKNOWN")

    def model(self):
        child_id = self.sup.register("model_server", sys.executable, [], "delegated")
        child = self.sup.child(child_id)
        child.update(parent_pid=5, parent_identity=IDENTITY, alias="llm-code")
        self.sup.change("model_launch_intent", child)
        self.sup.model_observed(child_id, [{"pid": 42, "parent_pid": 5}])
        return child_id

    def test_verified_model_shutdown_records_forced_exit(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.assertEqual(self.sup.child(child_id)["state"], "STOPPING")
        self.inspect.return_value = None
        self.sup.models_unloaded()
        self.assertEqual(self.sup.child(child_id)["state"], "EXITED")
        self.assertEqual(self.sup.child(child_id)["termination"], "forced")

    def test_gateway_parent_reuse_blocks_unload(self):
        self.model()
        self.inspect.side_effect = lambda pid: {**IDENTITY, "created": "reused"} if pid == 5 else IDENTITY
        with self.assertRaises(durable.DurableError):
            self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])

    def gone(self, pid=42):
        original = self.inspect.return_value
        self.inspect.side_effect = lambda value: None if value == pid else original

    def test_heartbeat_during_intentional_stop_cannot_steal_terminal_state(self):
        child_id = self.model()
        self.assertEqual(self.sup.child(child_id)["state"], "RUNNING")
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.assertEqual(self.sup.child(child_id)["state"], "STOPPING")
        self.gone()
        self.sup.heartbeat()  # the race: lands after the native exit, before models_unloaded()
        child = self.sup.child(child_id)
        self.assertEqual((child["state"], child["termination"]), ("STOPPING", "forced"))
        self.assertNotIn(child["state"], {"LOST", "EXITED"})
        self.sup.heartbeat()
        self.assertEqual(self.sup.child(child_id)["state"], "STOPPING")
        self.sup.models_unloaded()
        child = self.sup.child(child_id)
        self.assertEqual((child["state"], child["termination"], child["reason"]), ("EXITED", "forced", "exit_observed"))
        self.assertEqual(self.store.events[-1], "process_exit")

    def test_stopping_is_not_exited_by_pid_disappearance_alone(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.gone()
        for _ in range(3):
            self.sup.heartbeat()
        self.assertEqual(self.sup.child(child_id)["state"], "STOPPING")
        self.assertIsNone(self.sup.child(child_id)["exit_code"])
        self.assertNotIn("process_exit", self.store.events)

    def test_unload_reconciliation_does_not_record_exit_while_process_alive(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.sup.heartbeat()
        with self.assertRaisesRegex(RuntimeError, "not been observed"):
            self.sup.models_unloaded()
        self.assertEqual(self.sup.child(child_id)["state"], "STOPPING")

    def test_gateway_unload_with_heartbeat_race_ends_exited_forced(self):
        import harness
        child_id = self.model()
        state = {"gone": False, "beats": 0}
        self.inspect.side_effect = lambda pid: None if pid == 42 and state["gone"] else IDENTITY

        def request(method, path, payload=None, timeout=10):
            if path == "/running":
                return [] if state["gone"] else [{"model": "llm-code", "state": "ready"}]
            state["gone"] = True  # /api/models/unload: model exits, then a heartbeat fires before finalization
            self.sup.heartbeat()
            state["beats"] += 1
            return {}
        gateway = harness.Gateway({"shutdown_timeout_seconds": 5, "min_available_ram_gb": 1}, self.log)
        gateway.supervisor = self.sup
        with patch.object(harness.Gateway, "request", side_effect=request), \
             patch("harness.servers", side_effect=lambda: [] if state["gone"] else [{"pid": 42}]), \
             patch("harness.available_ram_gb", return_value=999):
            gateway.unload()
        self.assertEqual(state["beats"], 1)
        child = self.sup.child(child_id)
        self.assertEqual((child["state"], child["termination"]), ("EXITED", "forced"))
        self.sup.exited(self.child_id, None, "launch_failed")  # unrelated setUp child
        self.sup.require_idle()

    def test_gateway_unload_that_never_empties_never_records_exit(self):
        import harness
        child_id = self.model()
        state = {"gone": False}
        self.inspect.side_effect = lambda pid: None if pid == 42 and state["gone"] else IDENTITY

        def request(method, path, payload=None, timeout=10):
            if path == "/running":
                return [{"model": "llm-code", "state": "ready"}]  # gateway never reports empty
            state["gone"] = True  # native PID disappears, but emptiness is never verified
            return {}
        gateway = harness.Gateway({"shutdown_timeout_seconds": 0.2, "min_available_ram_gb": 1}, self.log)
        gateway.supervisor = self.sup
        with patch.object(harness.Gateway, "request", side_effect=request), \
             patch("harness.servers", return_value=[{"pid": 42}]), \
             patch("harness.available_ram_gb", return_value=999), self.assertRaises(TimeoutError):
            gateway.unload()
        self.assertNotEqual(self.sup.child(child_id)["state"], "EXITED")

    def test_unexpected_disappearance_from_running_still_becomes_lost(self):
        child_id = self.model()
        self.gone()
        self.sup.heartbeat()
        child = self.sup.child(child_id)
        self.assertEqual((child["state"], child["reason"]), ("LOST", "exit_unobserved"))
        self.assertIsNone(child["exit_code"])

    def test_orphaned_stopping_model_is_still_lost_for_non_heartbeat_observers(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.gone()  # controller died mid-unload; resume/status/require_idle must not stay blocked forever
        self.sup.exited(self.child_id, None, "launch_failed")  # unrelated setUp child
        self.sup.require_idle()
        self.assertEqual(self.sup.child(child_id)["state"], "LOST")

    def test_stopping_pid_reuse_unchanged_under_heartbeat(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.inspect.side_effect = lambda pid: {**IDENTITY, "created": "reused"} if pid == 42 else IDENTITY
        self.sup.heartbeat()
        child = self.sup.child(child_id)
        self.assertEqual((child["state"], child["reason"]), ("STALE", "PID_REUSED"))
        self.sup.models_unloaded()  # never relabels a reused PID as this model's exit
        self.assertEqual(self.sup.child(child_id)["state"], "STALE")
        self.assertNotIn("process_exit", self.store.events)

    def test_stopping_reboot_unchanged_under_heartbeat(self):
        child_id = self.model()
        self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 42}])
        self.boot.return_value = "boot-b"
        self.inspect.reset_mock()
        observed = self.sup.observe(self.sup.child(child_id), live_unload=True)
        self.assertEqual((observed["state"], observed["reason"]), ("LOST", "LOST_AFTER_REBOOT"))
        self.inspect.assert_not_called()

    def test_non_model_stopping_is_not_exempt(self):
        self.start()
        child = self.sup.child(self.child_id)
        child.update(state="STOPPING", termination="forced")
        self.sup.change("test_setup", child)
        self.gone()
        self.sup.heartbeat()
        self.assertEqual(self.sup.child(self.child_id)["state"], "LOST")

    def test_model_still_alive_cannot_record_exit(self):
        child_id = self.model()
        with self.assertRaises(RuntimeError):
            self.sup.models_unloaded()
        self.assertEqual(self.sup.child(child_id)["state"], "RUNNING")

    def test_native_cleanup_rechecks_identity_before_signaling(self):
        child = self.start()
        kernel = Mock()
        kernel.OpenProcess.return_value = 123
        with patch("winprocess._kernel", return_value=kernel), \
             patch("winprocess._handle_identity", return_value={**IDENTITY, "created": "reused"}), \
             self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            winprocess.terminate_owned_job(child, boot=lambda: "boot-a")
        kernel.TerminateJobObject.assert_not_called()
        kernel.CloseHandle.assert_called_once_with(123)


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "harness.py").write_text("# harness")
        self.config = {"roles": {"code": "llm-code"}, "review_profile": "safe"}
        self.before = self.capture()

    def capture(self):
        real_run = environment.subprocess.run
        def run(command, **kwargs):
            if isinstance(command, list) and command[0] == "git":
                return Mock(returncode=0, stdout=b"test-harness-head")
            return real_run(command, **kwargs)
        with patch("environment.subprocess.run", side_effect=run):
            return environment.capture(self.config, self.root, [["git", "status"]], root=self.root)

    def drift(self, **fields):
        after = {**copy.deepcopy(self.before), **fields}
        after["sha256"] = s.checksum({k: v for k, v in after.items() if k != "sha256"})
        return environment.compare(self.before, after)

    def test_fingerprint_deterministic(self):
        self.assertEqual(self.before, self.capture())

    def rendered_fixture(self):
        self.config["review_profiles"] = {}
        for name in ("config", "runs", "runtime", "models", "tools/llama-swap/bin"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for name in ("first.exe", "rendered.exe"):
            (self.root / "runtime" / name).write_bytes(b"runtime")
        (self.root / "models/model.gguf").write_bytes(b"model")
        (self.root / "tools/llama-swap/bin/llama-swap.exe").write_bytes(b"swap")
        (self.root / "runs/swap-profile.json").write_text("{}")
        for file, binary in (("config/llama-swap.yaml", "first.exe"), ("runs/swap-profile.yaml", "rendered.exe")):
            (self.root / file).write_text(f'"{(self.root / "runtime" / binary).as_posix()}" --model "{(self.root / "models/model.gguf").as_posix()}"')

    def test_rendered_runtime_is_strongly_fingerprinted(self):
        self.rendered_fixture()
        before = self.capture()
        (self.root / "runtime/rendered.exe").write_bytes(b"replaced-runtime")
        drift = environment.compare(before, self.capture())
        self.assertEqual(drift["decision"], "UNSAFE")
        self.assertTrue(any("rendered.exe" in c["field"] for c in drift["changes"]))

    def test_model_file_metadata_drift_is_detected(self):
        self.rendered_fixture()
        before = self.capture()
        (self.root / "models/model.gguf").write_bytes(b"changed-model")
        self.assertEqual(environment.compare(before, self.capture())["decision"], "REVALIDATION_REQUIRED")

    def test_missing_harness_head_fails_closed(self):
        with patch("environment.subprocess.run", return_value=Mock(returncode=1)), self.assertRaisesRegex(RuntimeError, "Git HEAD"):
            environment.capture(self.config, self.root, [["git", "status"]], root=self.root)

    def test_small_config_changed(self):
        path = self.root / "critical.json"
        path.write_text("one")
        old = environment.file_identity(path)
        path.write_text("two")
        self.assertNotEqual(old["sha256"], environment.file_identity(path)["sha256"])

    def test_large_critical_file_is_strongly_fingerprinted(self):
        path = self.root / "large-critical.bin"
        with path.open("wb") as stream:
            stream.seek(256 * 1024**2)
            stream.write(b"\0")

        first = environment.file_identity(path)
        second = environment.file_identity(path)

        self.assertNotIn("error", first)
        self.assertEqual(first["size"], 256 * 1024**2 + 1)
        self.assertEqual(first["sha256"], second["sha256"])

    def test_runtime_replaced_unsafe(self):
        runtimes = copy.deepcopy(self.before["runtime"])
        runtimes["python"]["sha256"] = "replaced"
        self.assertEqual(self.drift(runtime=runtimes)["decision"], "UNSAFE")

    def test_model_metadata_changed_requires_revalidation(self):
        self.assertEqual(self.drift(models={"m": {"path": "model.gguf", "size": 123, "mtime_ns": 3}})["decision"], "REVALIDATION_REQUIRED")

    def test_model_hashing_is_not_used(self):
        path = self.root / "model.gguf"
        path.write_bytes(b"data")
        with patch("hashlib.file_digest", side_effect=AssertionError):
            self.assertEqual(environment.file_identity(path, strong=False)["size"], 4)

    def test_timestamp_only_strong_file_informational(self):
        runtimes = copy.deepcopy(self.before["runtime"])
        runtimes["python"]["mtime_ns"] += 1
        self.assertEqual(self.drift(runtime=runtimes)["decision"], "INFORMATIONAL")

    def test_unavailable_runtime_unsafe_even_unchanged(self):
        runtime = {"python": {"error": "unavailable"}}
        self.assertEqual(self.drift(runtime=runtime)["decision"], "UNSAFE")

    def test_profile_drift_requires_revalidation(self):
        self.assertEqual(self.drift(profile="performance")["decision"], "REVALIDATION_REQUIRED")

    def test_python_patch_requires_revalidation(self):
        version = self.before["python_version"].split(".")
        version[2] = str(int(version[2]) + 1)
        self.assertEqual(self.drift(python_version=".".join(version))["decision"], "REVALIDATION_REQUIRED")

    def test_repository_drift_unsafe(self):
        self.assertEqual(self.drift(repository="other")["decision"], "UNSAFE")

    def test_semantic_configuration_unsafe(self):
        config = {**self.config, "max_actions": 100000}
        self.assertEqual(self.drift(config=config, config_sha256=s.checksum(config))["decision"], "UNSAFE")

    def test_corrupt_environment_rejected(self):
        self.before["sha256"] = "invalid"
        with self.assertRaisesRegex(durable.DurableError, "environment"):
            environment.validate(self.before)


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, _ = fixture(self.root)
        self.sup = s.Supervisor(self.store, clock=lambda: 100, boot=lambda: "a", inspect=lambda pid: IDENTITY)

    def reload(self):
        store = durable.Store(self.root / "runs", "test-run")
        store.load()
        return store

    def args(self, **kwargs):
        return argparse.Namespace(command="resume", run_id="test-run", quiet=True,
                                  revalidate_unverified=True, **kwargs)

    def test_registration_and_ledger_consistent(self):
        child_id = self.sup.register("helper", sys.executable, ["a"])
        loaded = self.reload()
        self.assertEqual(loaded.state["supervision"], self.store.state["supervision"])
        self.assertEqual(loaded.records[-1]["transition"]["event"], "process_registered")
        self.assertEqual(loaded.records[-1]["transition"]["child_id"], child_id)

    def test_heartbeat_crash_replayed_from_wal(self):
        with patch("durable.atomic_write", side_effect=OSError), self.assertRaises(OSError):
            self.sup.heartbeat()
        self.assertEqual(self.reload().state["supervision"]["controller"]["heartbeat"], 100)

    def test_corrupt_supervision_cache_fails_safe(self):
        path = self.store.directory / "state.json"
        value = json.loads(path.read_text())
        value["supervision"]["version"] = 999
        path.write_text(json.dumps(value))
        with self.assertRaises(durable.DurableError):
            self.reload()

    def test_drift_revalidation_is_not_silent(self):
        current = copy.deepcopy(self.store.state["environment"])
        current["profile"] = "changed"
        current["sha256"] = s.checksum({k: v for k, v in current.items() if k != "sha256"})
        with patch("durable.current_fingerprint", return_value=current), patch("durable.execute") as execute:
            with self.assertRaisesRegex(durable.DurableError, "revalidation required"):
                durable.cli(self.args(), self.root)
        execute.assert_not_called()

    def test_unsafe_drift_never_executes(self):
        current = copy.deepcopy(self.store.state["environment"])
        current["harness_head"] = "different"
        current["sha256"] = s.checksum({k: v for k, v in current.items() if k != "sha256"})
        with patch("durable.current_fingerprint", return_value=current), patch("durable.execute") as execute:
            with self.assertRaisesRegex(durable.DurableError, "automatic continuation refused"):
                durable.cli(self.args(revalidate_environment=True), self.root)
        execute.assert_not_called()

    def test_revalidation_flag_selects_gates_without_implementation(self):
        current = copy.deepcopy(self.store.state["environment"])
        current["profile"] = "changed"
        current["sha256"] = s.checksum({k: v for k, v in current.items() if k != "sha256"})
        with patch("durable.current_fingerprint", return_value=current), patch("durable.execute") as execute:
            durable.cli(self.args(revalidate_environment=True), self.root)
        self.assertTrue(execute.call_args.kwargs["revalidate"])
        self.assertTrue(self.reload().state["environment_revalidation_pending"])

    def test_interrupted_revalidation_cannot_reconstruct_old_checkpoint(self):
        self.store.commit(environment_revalidation_pending=True)
        with patch("durable.execute") as execute, self.assertRaisesRegex(durable.DurableError, "interrupted environment"):
            durable.cli(self.args(), self.root)
        execute.assert_not_called()

    def test_resume_live_child_never_starts_gateway(self):
        sup = s.Supervisor(self.store)
        # Use the actual executable path that Supervisor.inspect() would see for the current process
        import winprocess
        actual_executable = winprocess.identity(os.getpid())['executable']
        child_id = sup.register("helper", actual_executable, [])
        sup.started(child_id, os.getpid())
        with patch("durable.Gateway") as gateway, self.assertRaisesRegex(durable.DurableError, "RUNNING"):
            durable.cli(self.args(), self.root)
        gateway.assert_not_called()

    def test_gate_not_released_if_identity_commit_fails(self):
        with patch("supervision.subprocess.Popen", return_value=Mock(pid=42)), \
             patch.object(self.sup, "started", side_effect=OSError), self.assertRaises(OSError):
            s.run_command(self.sup, [sys.executable], self.root, 1, 100)
        self.assertEqual(list(self.store.directory.glob("processes/*/go.json")), [])

    def test_lost_later_work_preserves_trusted_checkpoint(self):
        with patch("durable.Gateway", FakeGateway):
            durable.execute(self.store)
        trusted = copy.deepcopy(self.store.state["last_verified_checkpoint"])
        sup = s.Supervisor(self.store, boot=lambda: "old", inspect=lambda pid: IDENTITY)
        child_id = sup.register("helper", sys.executable, [])
        sup.started(child_id, 42)
        with patch("winprocess.terminate_owned_job") as kill:
            sup.boot = lambda: "new"
            sup.require_idle()
        kill.assert_not_called()
        self.assertEqual(self.reload().state["last_verified_checkpoint"], trusted)

    def test_verifier_group_natural_exit_cleans_lingering_descendant(self):
        path = self.root / "descendant.txt"
        command = [sys.executable, "-c", "import subprocess,sys,pathlib; "
                   "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                   "pathlib.Path(sys.argv[1]).write_text(str(p.pid))", str(path)]
        result = s.run_command(s.Supervisor(self.store), command, self.root, 10, 100)
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(winprocess.identity(int(path.read_text())))
        record = next(iter(self.store.state["supervision"]["children"].values()))
        self.assertEqual(record["termination"], "natural")

    def test_verifier_timeout_is_forced_and_group_empty(self):
        result = s.run_command(s.Supervisor(self.store), [sys.executable, "-c", "import time; time.sleep(60)"],
                               self.root, 0.1, 100)
        self.assertIsNone(result["exit_code"])
        record = next(iter(self.store.state["supervision"]["children"].values()))
        self.assertEqual(record["termination"], "forced")
        self.assertIsNone(winprocess.identity(record["pid"]))

    def test_verifier_launch_failure_cannot_pass(self):
        result = s.run_command(s.Supervisor(self.store), [str(self.root / "missing.exe")], self.root, 10, 100)
        self.assertIsNone(result["exit_code"])
        record = next(iter(self.store.state["supervision"]["children"].values()))
        self.assertEqual(record["termination"], "launch_failed")


class ModelRecoveryTests(unittest.TestCase):
    """Explicit operator resolution of UNKNOWN model children; real WAL-backed store, no inference."""
    REASON = "Inspected gateway and task manager; no llama-server exists"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.store, _ = fixture(self.root)
        self.clock = Mock(return_value=200)
        self.sup = s.Supervisor(self.store, clock=self.clock)
        kill = patch("winprocess.terminate_owned_job", side_effect=AssertionError("process signaled"))
        self.kill = kill.start()
        self.addCleanup(kill.stop)

    def unknown_model(self, purpose="model_server"):
        child_id = self.sup.register(purpose, sys.executable, [], "delegated")
        self.sup.reconcile()
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")
        return child_id

    def resolve(self, child_id, run_id="test-run", confirmed=True, running=(), processes=(), reason=None,
                resolution="model_not_running"):
        return self.sup.resolve_model(run_id, child_id, resolution, self.REASON if reason is None else reason, confirmed,
                                      list(running), list(processes))

    def reload(self):
        store = durable.Store(self.root / "runs", "test-run")
        store.load()
        return store

    def cli_args(self, child_id, **kwargs):
        values = dict(command="recover-model", run_id="test-run", child_id=child_id, quiet=True,
                      resolution="model_not_running", reason=self.REASON, confirm=True)
        return argparse.Namespace(**{**values, **kwargs})

    def cli(self, child_id, running=(), processes=(), **kwargs):
        gateway = Mock()
        gateway.return_value.running.return_value = list(running)
        with patch("durable.Gateway", gateway), patch("durable.winprocess.servers", return_value=list(processes)):
            return durable.cli(self.cli_args(child_id, **kwargs), self.root)

    def test_unknown_model_blocks_automatic_continuation(self):
        self.unknown_model()
        args = argparse.Namespace(command="resume", run_id="test-run", quiet=True, revalidate_unverified=True,
                                  revalidate_environment=False)
        with patch("durable.Gateway") as gateway, patch("durable.execute") as execute, \
             self.assertRaisesRegex(durable.DurableError, "UNKNOWN.*recover-model"):
            durable.cli(args, self.root)
        gateway.assert_not_called()
        execute.assert_not_called()
        self.assertEqual(self.reload().state["supervision"]["children"].popitem()[1]["state"], "UNKNOWN")

    def test_no_confirmation_refused_and_changes_nothing(self):
        child_id = self.unknown_model()
        before = self.store.state["revision"]
        with self.assertRaisesRegex(durable.DurableError, "--confirm"):
            self.resolve(child_id, confirmed=False)
        with self.assertRaisesRegex(durable.DurableError, "--confirm"):
            self.cli(child_id, confirm=False)
        self.assertEqual(self.reload().state["revision"], before)

    def test_wrong_run_id_refused(self):
        child_id = self.unknown_model()
        with self.assertRaisesRegex(durable.DurableError, "run ID"):
            self.resolve(child_id, run_id="other-run")
        with self.assertRaises(durable.DurableError):
            self.cli(child_id, run_id="other-run")
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")

    def test_wrong_child_id_refused(self):
        child_id = self.unknown_model()
        with self.assertRaisesRegex(durable.DurableError, "not recorded"):
            self.resolve("model_server-" + "0" * 12)
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")

    def test_non_model_child_refused(self):
        for purpose in ("helper", "verification_command"):
            with self.subTest(purpose=purpose):
                child_id = self.unknown_model(purpose)
                with self.assertRaisesRegex(durable.DurableError, "only to delegated model_server"):
                    self.resolve(child_id)
                self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")

    def test_non_eligible_lifecycle_states_refused(self):
        for state, reason in ("EXITED", "exit_observed"), ("LOST", "exit_unobserved"), ("STALE", "PID_REUSED"):
            with self.subTest(state=state):
                child_id = self.sup.register("model_server", sys.executable, [], "delegated")
                child = self.sup.child(child_id)
                child.update(state=state, reason=reason)
                self.sup.change("test_setup", child)
                with self.assertRaisesRegex(durable.DurableError, state):
                    self.resolve(child_id)
                self.assertEqual(self.sup.child(child_id)["state"], state)

    def test_unknown_with_other_reason_refused(self):
        child_id = self.unknown_model()
        child = self.sup.child(child_id)
        child.update(reason="inspection_denied")
        with patch.object(self.sup, "reconcile", return_value=[]), patch.object(self.sup, "child", return_value=child):
            with self.assertRaisesRegex(durable.DurableError, "not eligible"):
                self.resolve(child_id)

    def live_owned_model(self):
        # Use the actual executable path that Supervisor.inspect() would see for the current process
        import winprocess
        actual_executable = winprocess.identity(os.getpid())['executable']
        child_id = self.sup.register("model_server", actual_executable, [], "delegated")
        self.sup.started(child_id, os.getpid())
        self.assertEqual(self.sup.child(child_id)["state"], "RUNNING")
        return child_id

    def test_verified_live_owned_model_is_not_cleared(self):
        child_id = self.live_owned_model()
        with self.assertRaisesRegex(durable.DurableError, "RUNNING"):
            self.resolve(child_id)
        with self.assertRaisesRegex(durable.DurableError, "RUNNING"):
            self.cli(child_id)
        self.assertEqual(self.reload().state["supervision"]["children"][child_id]["state"], "RUNNING")

    def test_pid_match_alone_cannot_authorize(self):
        child_id = self.sup.register("model_server", sys.executable, [], "delegated")
        child = self.sup.child(child_id)
        child.update(pid=os.getpid())
        self.sup.change("test_setup", child)
        for kwargs in ({"processes": [{"pid": os.getpid()}]}, {"running": [{"model": "llm-code"}]}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(durable.DurableError, "ownership cannot be attributed"):
                self.resolve(child_id, **kwargs)
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")
        self.kill.assert_not_called()

    def test_unrelated_live_server_blocks_even_for_different_pid(self):
        child_id = self.unknown_model()
        with self.assertRaisesRegex(durable.DurableError, "ownership cannot be attributed"):
            self.resolve(child_id, processes=[{"pid": 987654}])
        with self.assertRaisesRegex(durable.DurableError, "ownership cannot be attributed"):
            self.cli(child_id, processes=[{"pid": 987654}])

    def test_pid_reuse_remains_safe(self):
        child_id = self.live_owned_model()
        reused = s.Supervisor(self.store, clock=self.clock, inspect=lambda pid: {**IDENTITY, "created": "reused"})
        with self.assertRaisesRegex(durable.DurableError, "STALE"):
            reused.resolve_model("test-run", child_id, "model_not_running", self.REASON, True, [], [])
        self.assertEqual(self.reload().state["supervision"]["children"][child_id]["reason"], "PID_REUSED")
        self.kill.assert_not_called()

    def test_success_preserves_unknown_evidence_and_appends_audit(self):
        child_id = self.unknown_model()
        ledger = [dict(r) for r in self.store.records]
        self.clock.return_value = 300
        result = self.cli(child_id)
        self.assertEqual((result["state"], result["resolution"]), ("RESOLVED", "model_not_running"))
        loaded = self.reload()
        self.assertEqual(loaded.records[:len(ledger)], ledger)
        history = [r["state"]["supervision"]["children"][child_id]["state"]
                   for r in loaded.records if r["event"] == "state_committed"
                   and child_id in r["state"]["supervision"]["children"]]
        self.assertEqual(history, ["STARTING", "UNKNOWN", "RESOLVED"])
        audit = loaded.records[-1]
        self.assertEqual(audit["transition"], {"event": "operator_model_resolution", "child_id": child_id,
            "detail": {"resolution": "model_not_running", "reason": self.REASON,
                       "prior_state": "UNKNOWN", "prior_reason": "launch_identity_unproven"}})
        record = loaded.state["supervision"]["children"][child_id]
        self.assertEqual(record["resolution"]["version"], 1)
        self.assertEqual(record["resolution"]["prior_reason"], "launch_identity_unproven")
        self.assertEqual(record["token"], self.sup.child(child_id)["token"])
        self.assertNotIn(os.environ.get("USERNAME", "<none>"), json.dumps(record["resolution"]).replace(self.REASON, ""))

    def test_success_clears_only_intended_block(self):
        first, second = self.unknown_model(), self.unknown_model()

        self.resolve(first)
        self.assertEqual(self.sup.child(first)["state"], "RESOLVED")
        self.assertEqual(self.sup.child(second)["state"], "UNKNOWN")
        with self.assertRaisesRegex(durable.DurableError, "ambiguous"):
            self.sup.require_no_ambiguous_model()
        with self.assertRaisesRegex(durable.DurableError, second):
            self.sup.require_idle()
        self.resolve(second)
        self.sup.require_no_ambiguous_model()
        self.sup.require_idle()

    def test_resolution_does_not_authorize_unowned_unload_or_launch_shortcuts(self):
        child_id = self.unknown_model()
        self.resolve(child_id)
        with self.assertRaisesRegex(durable.DurableError, "ownership not proven"):
            self.sup.guard_model_unload([{"model": "llm-code"}], [{"pid": 1}])
        self.assertEqual(self.sup.reconcile()[0]["state"], "RESOLVED")
        self.kill.assert_not_called()

    def test_trusted_checkpoint_and_progress_unchanged(self):
        with patch("durable.Gateway", FakeGateway):
            durable.execute(self.store)
        trusted = copy.deepcopy((self.store.state["last_verified_checkpoint"], self.store.state["verified_progress"],
                                 self.store.state["status"], self.store.state["current_git_head"]))
        child_id = self.unknown_model()
        self.cli(child_id)
        state = self.reload().state
        self.assertEqual((state["last_verified_checkpoint"], state["verified_progress"], state["status"],
                          state["current_git_head"]), trusted)

    def test_unverified_work_remains_unverified(self):
        self.store.commit("INTERRUPTED", unverified_work={"observed": {"head": "x"}, "recorded": ["edit"]})
        child_id = self.unknown_model()
        before = copy.deepcopy({k: self.store.state[k] for k in ("unverified_work", "status", "verified_progress",
                                "last_verified_checkpoint", "remaining_work", "round_number")})
        self.cli(child_id)
        state = self.reload().state
        self.assertEqual({k: state[k] for k in before}, before)
        self.assertIsNone(state["last_verified_checkpoint"])

    def test_repeat_is_refused_cleanly_without_new_history(self):
        child_id = self.unknown_model()
        self.resolve(child_id)
        before = (self.store.state["revision"], len(self.store.records))
        with self.assertRaisesRegex(durable.DurableError, "already operator-resolved"):
            self.resolve(child_id)
        with self.assertRaisesRegex(durable.DurableError, "already operator-resolved"):
            self.cli(child_id)
        loaded = self.reload()
        self.assertEqual((loaded.state["revision"], len(loaded.records)), before)

    def test_reason_and_resolution_are_bounded(self):
        child_id = self.unknown_model()
        for reason in ("", " padded ", "x" * 201, "line\nbreak", "\x00nul"):
            with self.subTest(reason=reason), self.assertRaisesRegex(durable.DurableError, "reason"):
                self.resolve(child_id, reason=reason)
        with self.assertRaisesRegex(durable.DurableError, "Unknown recovery resolution"):
            self.resolve(child_id, resolution="assume_safe")
        self.assertEqual(self.sup.child(child_id)["state"], "UNKNOWN")

    def test_unreachable_gateway_refuses_recovery(self):
        child_id = self.unknown_model()
        gateway = Mock()
        gateway.return_value.running.side_effect = OSError("refused")
        with patch("durable.Gateway", gateway), patch("durable.winprocess.servers", return_value=[]), \
             self.assertRaisesRegex(durable.DurableError, "Gateway state cannot be verified"):
            durable.cli(self.cli_args(child_id), self.root)
        self.assertEqual(self.reload().state["supervision"]["children"][child_id]["state"], "UNKNOWN")

    def test_resolution_is_not_an_automatic_resume_side_effect(self):
        child_id = self.unknown_model()
        args = argparse.Namespace(command="resume", run_id="test-run", quiet=True, revalidate_unverified=True,
                                  revalidate_environment=False)
        for _ in range(2):
            with patch("durable.execute"), self.assertRaisesRegex(durable.DurableError, "UNKNOWN"):
                durable.cli(args, self.root)
        self.assertEqual(self.reload().state["supervision"]["children"][child_id]["state"], "UNKNOWN")

    def test_corrupt_resolution_state_fails_safe(self):
        child_id = self.unknown_model()
        self.resolve(child_id)
        good = copy.deepcopy(self.store.state["supervision"])
        for field, value in (("resolution", None), ("resolution", {"version": 2}), ("state", "UNKNOWN")):
            data = copy.deepcopy(good)
            data["children"][child_id][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(durable.DurableError):
                s.validate(data, "test-run")
        data = copy.deepcopy(good)
        data["children"][child_id]["resolution"]["reason"] = "bad\nreason"
        with self.assertRaises(durable.DurableError):
            s.validate(data, "test-run")
        path = self.store.directory / "state.json"
        value = json.loads(path.read_text())
        value["supervision"]["children"][child_id]["resolution"]["resolution"] = "assume_safe"
        path.write_text(json.dumps(value))
        with self.assertRaises(durable.DurableError):
            self.cli(child_id)

    def test_cli_parser_requires_explicit_arguments_and_defaults_unconfirmed(self):
        import harness
        with patch("durable.cli", return_value={"ok": True}) as cli:
            self.assertEqual(harness.main(["recover-model", "--run-id", "r", "--child-id", "c",
                                           "--resolution", "model_not_running", "--reason", "why"]), 0)
        self.assertFalse(cli.call_args.args[0].confirm)
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            harness.main(["recover-model", "--run-id", "r", "--child-id", "c", "--reason", "why"])
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            harness.main(["recover-model", "--run-id", "r", "--child-id", "c",
                          "--resolution", "assume_safe", "--reason", "why"])


if __name__ == "__main__":
    unittest.main()
