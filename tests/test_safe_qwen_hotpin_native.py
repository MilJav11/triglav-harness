"""Native enforcement regressions with fake processes/handles; no model/native mutations."""
import contextlib
import copy
import ctypes as C
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

import durable
import harness
import manager
import safe_executor_binding as binding
import supervision
import terminal_run as launch
import winprocess
import test_safe_qwen_hotpin_policy as contract
from critic_executor import ScriptedCritic


class FakeModelHost:
    def __init__(self, cfg):
        self.cfg = cfg
        self.live = []
        self.identities = {12345: {"created": "parent-created", "executable": os.path.normcase(
            str(Path(cfg["safe_qwen_gateway"]["binary"]).resolve()))},
            os.getpid(): {"created": "controller-created", "executable": os.path.normcase(sys.executable)}}
        self.ready = []
        self.calls = []
        self.next_pid = 4000
        self.replies = iter([])

    def inspect(self, pid):
        return copy.deepcopy(self.identities.get(pid))

    def start(self, role):
        self.next_pid += 1
        pid = self.next_pid
        executable = os.path.normcase(str(Path(self.cfg["safe_qwen_runtime"][role]["binary"]).resolve()))
        self.identities[pid] = {"created": str(pid * 10), "executable": executable}
        self.live = [{"pid": pid, "parent_pid": 12345, "path": executable}]
        self.ready = [{"model": self.cfg["roles"][role], "state": "ready"}]

    def request(self, method, path, payload=None, timeout=10):
        self.calls.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path.startswith("/upstream/"):
            alias = unquote(path[len("/upstream/"):].removesuffix("/health"))
            role = next(r for r, a in self.cfg["roles"].items() if a == alias)
            self.start(role)
            return None
        if (method, path) == ("POST", "/api/models/unload"):
            for process in self.live:
                self.identities.pop(process["pid"], None)
            self.live = []
            self.ready = []
            return None
        if (method, path) == ("POST", "/v1/chat/completions"):
            answer = next(self.replies)
            return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": answer}}]}
        raise AssertionError((method, path))


class HotPinNativeTests(unittest.TestCase):
    def setUp(self):
        case = contract.HotPinPolicyTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        self.fixture = case.fixture
        self.expert = case.expert
        self.fixture.cfg["memory_recovery_timeout_seconds"] = 0
        command = '\n'.join(f'  {alias}:\n    cmd: >-\n      "{(self.fixture.resource / "server.bin").as_posix()}" --model "{(self.fixture.resource / "fixture-model.bin").as_posix()}"'
                            for alias in self.fixture.cfg["roles"].values())
        (self.fixture.resource / "gateway.json").write_text('models:\n' + command + '\n')
        self.worker, self.cfg = self.fixture.prepared(critic=True, reviewer=True)
        self.host = FakeModelHost(self.cfg)
        self.sup = supervision.Supervisor(self.fixture.store, boot=lambda: "fake-boot", inspect=self.host.inspect)
        self.gateway = harness.Gateway(self.cfg, self.fixture.repo.log)
        self.gateway.supervisor = self.sup
        # Exercise production _switch/_unload even under the global CI Gateway guard.
        self.gateway.switch = lambda role: self.gateway._switch(role)
        self.gateway.unload = lambda: self.gateway._unload()
        self.gateway.running = lambda: copy.deepcopy(self.host.ready)
        self.gateway.request = Mock(side_effect=self.host.request)
        self.ram = Mock(return_value=50)
        self.environment = Mock(return_value=str(self.expert.resolve()))
        self.limit = Mock(side_effect=lambda pid, cap, expected_identity=None: {
            "pid": pid, "minimum_gb": 8, "maximum_gb": cap, "flags": 7})
        stack = self.enterContext(contextlib.ExitStack())
        stack.enter_context(patch("harness.available_ram_gb", self.ram))
        stack.enter_context(patch("harness.servers", side_effect=lambda: copy.deepcopy(self.host.live)))
        stack.enter_context(patch("winprocess.servers", side_effect=lambda: copy.deepcopy(self.host.live)))
        stack.enter_context(patch("winprocess.hotpin_environment", self.environment))
        stack.enter_context(patch("winprocess.limit_working_set", self.limit))

    def load(self):
        self.gateway._switch("review")
        return self.sup.child(self.gateway.active_model_child_id)

    def inference_calls(self):
        return [c for c in self.host.calls if c[1] == "/v1/chat/completions"]

    def failed_chat(self):
        with self.assertRaises((RuntimeError, ValueError, OSError, durable.DurableError)):
            self.gateway.chat("review", [], "review")
        self.assertEqual(self.inference_calls(), [])

    def test_owned_reviewer_passes_before_inference(self):
        self.host.replies = iter(['{"decision":"APPROVE","findings":[],"summary":"ok"}'])
        child = self.load()
        self.environment.assert_called_with(child["pid"], expected_identity=child["identity"])
        self.limit.assert_called_with(child["pid"], 32, expected_identity=child["identity"])
        self.assertEqual(self.inference_calls(), [])
        self.gateway.chat("review", [], "review")
        self.assertEqual(len(self.inference_calls()), 1)
        self.assertEqual(self.limit.call_count, 2)

    def test_cached_role_revalidates_environment_and_cap(self):
        self.load()
        self.environment.return_value = None
        self.failed_chat()
        self.assertEqual(self.limit.call_count, 1)

    def test_cached_role_rejects_failed_cap(self):
        self.load()
        self.limit.side_effect = RuntimeError("cap unavailable")
        self.failed_chat()

    def test_ttl_reload_gets_new_owned_child_and_reapplies_cap(self):
        first = self.load()
        self.host.request("POST", "/api/models/unload", {})
        second = self.load()
        self.assertNotEqual(first["child_id"], second["child_id"])
        self.assertNotEqual(first["pid"], second["pid"])
        self.assertEqual(self.limit.call_count, 2)
        self.limit.assert_called_with(second["pid"], 32, expected_identity=second["identity"])

    def test_ttl_reload_rechecks_native_gate(self):
        self.load()
        self.host.request("POST", "/api/models/unload", {})
        self.environment.return_value = None
        self.failed_chat()

    def test_44_gib_preflight_blocks_low_ram_before_startup(self):
        self.ram.return_value = 43.99
        self.failed_chat()
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.fixture.store.state["supervision"]["children"], {})

    def test_unavailable_or_nonfinite_ram_is_refused(self):
        for value in (None, float("nan"), float("inf")):
            self.ram.return_value = value
            self.failed_chat()
        self.assertEqual(self.host.calls, [])

    def test_wrong_pid_is_rejected(self):
        self.load()
        self.host.live[0]["pid"] += 77
        self.failed_chat()

    def test_reused_pid_is_rejected(self):
        child = self.load()
        self.host.identities[child["pid"]]["created"] = "reused"
        self.failed_chat()

    def test_wrong_executable_is_rejected(self):
        child = self.load()
        self.host.identities[child["pid"]]["executable"] = "unrelated.exe"
        self.failed_chat()

    def test_incorrect_parent_lineage_is_rejected(self):
        self.load()
        self.host.live[0]["parent_pid"] = 9876
        self.failed_chat()

    def test_reused_gateway_parent_is_rejected(self):
        self.load()
        self.host.identities[12345]["created"] = "reused-parent"
        self.failed_chat()

    def test_additional_model_process_is_rejected(self):
        self.load()
        self.host.live.append({"pid": 9999, "parent_pid": 12345, "path": self.host.live[0]["path"]})
        self.failed_chat()

    def test_wrong_inherited_expert_is_rejected(self):
        other = self.fixture.resource / "other.txt"
        other.write_bytes(self.expert.read_bytes())
        self.environment.return_value = str(other.resolve())
        self.failed_chat()
        self.limit.assert_not_called()

    def test_changed_expert_bytes_are_rejected_on_cached_role(self):
        self.load()
        self.expert.write_bytes(b"changed fake experts\n")
        self.failed_chat()
        self.assertEqual(self.limit.call_count, 1)

    def test_cap_exception_is_terminal_before_inference(self):
        self.limit.side_effect = OSError("SET_QUOTA denied")
        self.failed_chat()
        self.assertIsNotNone(self.gateway.active_model_child_id)

    def test_missing_or_ineffective_cap_receipts_are_rejected(self):
        self.limit.side_effect = None
        for receipt in (None, {}, {"pid": 4001, "flags": 0, "maximum_gb": 32},
                        {"pid": 4001, "flags": 12, "maximum_gb": 32},
                        {"pid": 4001, "flags": 4, "maximum_gb": 33},
                        {"pid": 4001, "flags": 4, "maximum_gb": float("nan")}):
            self.limit.return_value = receipt
            self.failed_chat()

    def test_executor_key_mismatch_cannot_fall_back_to_legacy(self):
        self.gateway.config = copy.deepcopy(self.cfg)
        self.gateway.config["executor"] = "custom"
        self.failed_chat()
        self.assertEqual(self.host.calls, [])

    def test_policy_gate_does_not_depend_on_display_metadata(self):
        self.fixture.cfg["model_metadata"][self.cfg["roles"]["review"]]["runtime"] = "llama.cpp"
        self.fixture.store = durable.Store(self.fixture.root / "runs", "metadata-case")
        self.worker, self.cfg = self.fixture.prepared(critic=True, reviewer=True)
        self.gateway.config = self.cfg
        self.host.cfg = self.cfg
        self.sup.store = self.fixture.store
        self.environment.return_value = None
        self.failed_chat()
        self.environment.assert_called_once()

    def test_changed_pinned_metadata_is_rejected(self):
        self.cfg["model_metadata"][self.cfg["roles"]["review"]]["runtime"] = "llama.cpp"
        self.failed_chat()
        self.assertEqual(self.host.calls, [])

    def test_actual_worker_rejects_divergent_gateway_configuration(self):
        self.gateway.config = copy.deepcopy(self.cfg)
        self.gateway.config["executor"] = "custom"
        self.worker.bind_gateway(self.gateway)
        repo = type("Repo", (), {"root": self.fixture.repo.root, "store": self.fixture.store})()
        with self.assertRaises(durable.DurableError):
            self.worker.execute(launch.fixed_unit("x", self.cfg, self.worker, ["check-1"]), {}, repo)
        self.assertEqual(self.host.calls, [])

    def test_missing_launch_child_binding_is_rejected(self):
        self.load()
        self.gateway.active_model_child_id = None
        self.failed_chat()

    def test_owned_cleanup_remains_authorized_after_cap_failure(self):
        self.limit.side_effect = RuntimeError("cap failed")
        self.failed_chat()
        child_id = self.gateway.active_model_child_id
        self.gateway._unload()
        self.assertEqual(self.sup.child(child_id)["state"], "EXITED")
        self.assertEqual(self.host.live, [])
        self.assertIsNone(self.gateway.active_model_child_id)

    def test_cleanup_refuses_reused_process_identity(self):
        child = self.load()
        self.host.identities[child["pid"]]["created"] = "reused"
        count = len(self.host.calls)
        with self.assertRaises(durable.DurableError):
            self.gateway._unload()
        self.assertEqual(len(self.host.calls), count)
        self.assertTrue(self.host.live)

    def run_chain(self, fail=False):
        self.host.replies = iter(self.fixture.replies() + [json.dumps({
            "decision": "APPROVE", "findings": [], "summary": "offline fake approval"})])
        if fail:
            self.limit.side_effect = RuntimeError("cap readback failed")
        def factory(repo, gateway, cfg, log):
            agents = launch.agents_factory(self.worker)(repo, gateway, cfg, log)
            agents.critic = ScriptedCritic("clean")
            return agents
        with patch.object(manager, "Supervisor", return_value=self.sup):
            return manager.execute(self.fixture.store, agents=factory, gateway=self.gateway)

    def test_real_controller_chain_accepts_only_after_native_gates(self):
        result = self.run_chain()
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNotNone(self.fixture.store.state["last_verified_checkpoint"])
        reviews = [c for c in self.inference_calls() if c[2]["model"] == self.cfg["roles"]["review"]]
        self.assertEqual(len(reviews), 1)
        self.assertEqual(self.limit.call_count, 2)

    def test_failed_enforcement_cannot_infer_or_create_trusted_checkpoint(self):
        result = self.run_chain(fail=True)
        self.assertNotEqual(result["status"], "COMPLETED")
        self.assertIsNone(self.fixture.store.state["last_verified_checkpoint"])
        self.assertEqual([c for c in self.inference_calls() if c[2]["model"] == self.cfg["roles"]["review"]], [])
        self.assertEqual(self.host.live, [])

    def test_role_alias_collision_is_rejected_before_dispatch(self):
        self.fixture.cfg["roles"]["code"] = self.fixture.cfg["roles"]["review"]
        with self.assertRaisesRegex(ValueError, "distinct model aliases"):
            self.fixture.configure(critic=True, reviewer=True)

    def test_identity_changed_during_enforcement_is_rejected(self):
        def change(pid, cap, expected_identity=None):
            self.host.identities[pid]["created"] = "reused-after-cap"
            return {"pid": pid, "flags": 4, "maximum_gb": 32}
        self.limit.side_effect = change
        self.failed_chat()

    def test_cached_binary_drift_is_rejected(self):
        self.load()
        (self.fixture.resource / "server.bin").write_bytes(b"changed fake executable")
        self.failed_chat()


    def test_exact_44_gib_startup_threshold_passes(self):
        self.ram.return_value = 44
        self.load()
        self.limit.assert_called_once()

    def test_cached_role_uses_active_ram_floor_not_startup_floor(self):
        self.load()
        self.ram.return_value = 7
        self.gateway._switch("review")
        self.assertEqual(self.limit.call_count, 2)

    def test_missing_inherited_expert_refuses_cold_inference(self):
        self.environment.return_value = None
        self.failed_chat()
        self.limit.assert_not_called()

    def test_ambiguous_supervisor_launch_refuses_cached_inference(self):
        self.load()
        self.sup.register("model_server", self.cfg["safe_qwen_runtime"]["review"]["binary"], {}, "delegated")
        self.failed_chat()
        self.assertEqual(self.limit.call_count, 1)

    def test_supervisor_executor_key_mismatch_cannot_select_legacy_launch(self):
        self.fixture.store.state["options"]["config"]["executor"] = "custom"
        with self.assertRaises(ValueError):
            self.sup.model_starting("review")
        self.assertEqual(self.host.calls, [])

    def test_actual_worker_executor_key_mismatch_is_rejected(self):
        self.worker.config = copy.deepcopy(self.cfg)
        self.worker.config["executor"] = "custom"
        self.worker.bind_gateway(self.gateway)
        repo = type("Repo", (), {"root": self.fixture.repo.root, "store": self.fixture.store})()
        with self.assertRaises(ValueError):
            self.worker.execute(launch.fixed_unit("x", self.cfg, self.worker, ["check-1"]), {}, repo)
        self.assertEqual(self.host.calls, [])

    def test_generic_reviewer_gateway_skips_hotpin_native_functions(self):
        del self.fixture.cfg["safe_qwen_reviewer_policy"]
        self.fixture.cfg["model_metadata"][self.cfg["roles"]["review"]]["runtime"] = "llama.cpp"
        self.fixture.store = durable.Store(self.fixture.root / "runs", "generic-case")
        self.worker, self.cfg = self.fixture.prepared(critic=True, reviewer=True)
        self.gateway.config = self.cfg
        self.host.cfg = self.cfg
        self.sup.store = self.fixture.store
        self.load()
        self.environment.assert_not_called()
        self.limit.assert_not_called()

    def test_legacy_profile_route_retains_existing_cap_calls(self):
        legacy = {k: v for k, v in self.cfg.items() if not k.startswith("safe_qwen_")}
        legacy.update(executor="custom", review_profile="review-safe", review_profiles={"review-safe": {
            "hot_experts": str(self.expert.resolve()), "min_available_ram_gb": 44, "max_working_set_gb": 32}})
        self.gateway.config = legacy
        self.gateway.supervisor = None
        self.gateway.unload = Mock()
        with patch("pathlib.Path.read_text", return_value='{"profile":"review-safe"}'), \
             patch("harness.hotpin_environment", return_value=str(self.expert.resolve())) as env, \
             patch("harness.limit_working_set", return_value={"pid": 4001, "maximum_gb": 32, "flags": 4}) as limit:
            self.gateway._switch("review")
        env.assert_called_once_with(4001)
        limit.assert_called_once_with(4001, 32)
        self.environment.assert_not_called()


class FakeKernel:
    def __init__(self, executable):
        self.executable = str(executable)
        self.created = 100
        self.exited = False
        self.handle = 101
        self.minimum = 8 * 2**30
        self.maximum = 64 * 2**30
        self.flags = 3
        self.ws_reads = 0
        self.bad_readback = None
        self.partial = False
        self.env = 'LLAMA_HOT_EXPERTS=C:/fake/experts.txt\0\0'.encode('utf-16-le')
        self.OpenProcess = Mock(return_value=self.handle)
        self.CloseHandle = Mock(return_value=1)
        self.GetProcessTimes = Mock(side_effect=self.times)
        self.QueryFullProcessImageNameW = Mock(side_effect=self.image)
        self.ReadProcessMemory = Mock(side_effect=self.read)
        self.GetProcessWorkingSetSizeEx = Mock(side_effect=self.get_ws)
        self.SetProcessWorkingSetSizeEx = Mock(side_effect=self.set_ws)

    def times(self, handle, created, exited, kernel, user):
        assert handle == self.handle
        created._obj.dwLowDateTime = self.created
        exited._obj.dwLowDateTime = int(self.exited)
        return 1

    def image(self, handle, flags, buffer, length):
        assert handle == self.handle
        buffer.value = self.executable
        length._obj.value = len(self.executable)
        return 1

    def read(self, handle, address, buffer, count, used):
        assert handle == self.handle
        if address == 0x1020:
            data = (0x2000).to_bytes(8, 'little')
        elif address == 0x2080:
            data = (0x3000).to_bytes(8, 'little')
        else:
            offset = address - 0x3000
            data = self.env[offset:offset + count]
        C.memmove(buffer, data, len(data))
        used._obj.value = len(data) - int(self.partial)
        return 1

    def get_ws(self, handle, minimum, maximum, flags):
        assert handle == self.handle
        self.ws_reads += 1
        low, high, bits = self.minimum, self.maximum, self.flags
        if self.ws_reads > 1 and self.bad_readback is not None:
            low, high, bits = self.bad_readback
        minimum._obj.value = low
        maximum._obj.value = high
        flags._obj.value = bits
        return 1

    def set_ws(self, handle, minimum, maximum, flags):
        assert handle == self.handle
        self.minimum, self.maximum, self.flags = minimum, maximum, flags
        return 1


class WindowsHandleTests(unittest.TestCase):
    def setUp(self):
        self.kernel = FakeKernel(Path(__file__).resolve())
        self.expected = {"created": "100", "executable": os.path.normcase(str(Path(__file__).resolve()))}
        self.ntdll = Mock()
        def info(handle, kind, basic, size, length):
            self.assertEqual(handle, self.kernel.handle)
            basic[1] = 0x1000
            return 0
        self.ntdll.NtQueryInformationProcess = Mock(side_effect=info)
        self.enterContext(patch.object(winprocess, "_kernel", return_value=self.kernel))
        self.enterContext(patch.object(winprocess.C, "WinDLL", return_value=self.ntdll))

    def test_environment_identity_and_peb_reads_use_same_retained_handle(self):
        self.assertEqual(winprocess.hotpin_environment(77, self.expected), 'C:/fake/experts.txt')
        self.kernel.OpenProcess.assert_called_once_with(0x410, False, 77)
        self.assertEqual(self.kernel.GetProcessTimes.call_count, 2)
        self.assertTrue(all(c.args[0] == self.kernel.handle for c in self.kernel.ReadProcessMemory.call_args_list))
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_environment_reused_pid_is_rejected_before_peb_read(self):
        self.kernel.created = 101
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.ReadProcessMemory.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_missing_environment_is_returned_as_missing(self):
        self.kernel.env = 'OTHER=value\0\0'.encode('utf-16-le')
        self.assertIsNone(winprocess.hotpin_environment(77, self.expected))

    def test_partial_memory_reads_are_rejected(self):
        self.kernel.partial = True
        with self.assertRaisesRegex(RuntimeError, "Incomplete"):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_ambiguous_environment_is_rejected(self):
        self.kernel.env = 'LLAMA_HOT_EXPERTS=a\0llama_hot_experts=b\0\0'.encode('utf-16-le')
        with self.assertRaisesRegex(RuntimeError, "Ambiguous"):
            winprocess.hotpin_environment(77, self.expected)

    def test_failed_environment_query_is_rejected_and_handle_closed(self):
        self.ntdll.NtQueryInformationProcess.side_effect = None
        self.ntdll.NtQueryInformationProcess.return_value = -1
        with self.assertRaisesRegex(RuntimeError, "NtQueryInformationProcess"):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_failed_environment_read_is_rejected_and_handle_closed(self):
        self.kernel.ReadProcessMemory.side_effect = None
        self.kernel.ReadProcessMemory.return_value = 0
        with self.assertRaises(OSError):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_exited_process_identity_is_rejected(self):
        self.kernel.exited = True
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.ReadProcessMemory.assert_not_called()

    def test_cap_identity_set_and_readback_use_same_handle(self):
        result = winprocess.limit_working_set(77, 32, self.expected)
        self.assertEqual(result["maximum_gb"], 32)
        self.assertEqual(result["minimum_gb"], 8)
        self.assertEqual(result["flags"], 7)
        self.kernel.SetProcessWorkingSetSizeEx.assert_called_once_with(self.kernel.handle, 8 * 2**30, 32 * 2**30, 7)
        self.assertEqual(self.kernel.GetProcessTimes.call_count, 2)
        self.assertEqual(self.kernel.GetProcessWorkingSetSizeEx.call_count, 2)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_quota_reused_pid_is_rejected_before_set(self):
        self.kernel.created = 101
        with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
            winprocess.limit_working_set(77, 32, self.expected)
        self.kernel.SetProcessWorkingSetSizeEx.assert_not_called()
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_failed_quota_apis_are_terminal(self):
        for name in ("OpenProcess", "GetProcessWorkingSetSizeEx", "SetProcessWorkingSetSizeEx"):
            with self.subTest(api=name), patch.object(self.kernel, name, return_value=0):
                self.kernel.ws_reads = 0
                with self.assertRaises(OSError):
                    winprocess.limit_working_set(77, 32, self.expected)

    def test_ineffective_hard_limit_readback_is_rejected(self):
        for maximum, flags in ((33 * 2**30, 4), (32 * 2**30, 0), (32 * 2**30, 12), (0, 4)):
            self.kernel.ws_reads = 0
            self.kernel.bad_readback = (8 * 2**30, maximum, flags)
            with self.subTest(maximum=maximum, flags=flags), self.assertRaisesRegex(RuntimeError, "did not enforce"):
                winprocess.limit_working_set(77, 32, self.expected)

    def test_existing_minimum_is_never_lowered(self):
        self.kernel.minimum = 33 * 2**30
        with self.assertRaisesRegex(ValueError, "below HotPin"):
            winprocess.limit_working_set(77, 32, self.expected)
        self.kernel.SetProcessWorkingSetSizeEx.assert_not_called()

    def test_legacy_callers_without_expected_identity_remain_supported(self):
        self.assertEqual(winprocess.hotpin_environment(77), 'C:/fake/experts.txt')
        self.assertEqual(winprocess.limit_working_set(77, 32)["maximum_gb"], 32)
        self.kernel.GetProcessTimes.assert_not_called()


    def test_quota_identity_changed_after_set_is_rejected_on_same_handle(self):
        def changed(handle, minimum, maximum, flags):
            self.kernel.set_ws(handle, minimum, maximum, flags)
            self.kernel.created = 101
            return 1
        self.kernel.SetProcessWorkingSetSizeEx.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            winprocess.limit_working_set(77, 32, self.expected)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_environment_identity_changed_during_read_is_rejected(self):
        def changed(handle, address, buffer, count, used):
            result = self.kernel.read(handle, address, buffer, count, used)
            if address >= 0x3000:
                self.kernel.created = 101
            return result
        self.kernel.ReadProcessMemory.side_effect = changed
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            winprocess.hotpin_environment(77, self.expected)
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)

    def test_failed_cap_readback_api_is_terminal(self):
        def failed(handle, minimum, maximum, flags):
            if self.kernel.ws_reads:
                return 0
            return self.kernel.get_ws(handle, minimum, maximum, flags)
        self.kernel.GetProcessWorkingSetSizeEx.side_effect = failed
        with self.assertRaises(OSError):
            winprocess.limit_working_set(77, 32, self.expected)
        self.kernel.SetProcessWorkingSetSizeEx.assert_called_once()
        self.kernel.CloseHandle.assert_called_once_with(self.kernel.handle)
