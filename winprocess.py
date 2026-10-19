"""Read-only Windows process evidence (no third-party dependencies)."""
import ctypes as C
import os
import time
from pathlib import Path
from ctypes import wintypes as W


def boot_identity():
    """Boot GUID, never a hardware identifier; unavailable information fails closed."""
    if os.name != "nt":
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    class Boot(C.Structure):
        _fields_ = [("guid", C.c_ubyte * 16), ("firmware", W.DWORD), ("flags", C.c_ulonglong)]
    n = C.WinDLL("ntdll")
    n.NtQuerySystemInformation.argtypes = [W.ULONG, C.c_void_p, W.ULONG, C.c_void_p]
    n.NtQuerySystemInformation.restype = C.c_long
    value = Boot()
    status = n.NtQuerySystemInformation(90, C.byref(value), C.sizeof(value), None)
    if status < 0 or not any(value.guid):
        raise RuntimeError("Cannot establish Windows boot identity; recovery refused")
    return bytes(value.guid).hex()


def _kernel():
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes, k.OpenProcess.restype = [W.DWORD, W.BOOL, W.DWORD], W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.GetProcessTimes.argtypes = [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    k.CreateJobObjectW.argtypes, k.CreateJobObjectW.restype = [C.c_void_p, W.LPCWSTR], W.HANDLE
    k.OpenJobObjectW.argtypes, k.OpenJobObjectW.restype = [W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE
    k.SetInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD]
    k.QueryInformationJobObject.argtypes = [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.c_void_p]
    k.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]
    k.IsProcessInJob.argtypes = [W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)]
    k.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]
    k.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    return k


def _handle_identity(k, handle):
    times = [W.FILETIME() for _ in range(4)]
    if not k.GetProcessTimes(handle, *(C.byref(t) for t in times)):
        raise C.WinError(C.get_last_error())
    if times[1].dwHighDateTime or times[1].dwLowDateTime:
        return None
    name, length = C.create_unicode_buffer(32768), W.DWORD(32768)
    if not k.QueryFullProcessImageNameW(handle, 0, name, C.byref(length)):
        raise C.WinError(C.get_last_error())
    return {"created": str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime),
            "executable": os.path.normcase(str(Path(name.value).resolve()))}


def identity(pid):
    if os.name != "nt":
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return None
            return {"created": fields[19], "executable": str(Path(f"/proc/{pid}/exe").resolve(strict=True))}
        except FileNotFoundError:
            return None
    k = _kernel()
    handle = k.OpenProcess(0x1000, False, pid)
    if not handle:
        if C.get_last_error() == 87:
            return None
        raise C.WinError(C.get_last_error())
    try:
        return _handle_identity(k, handle)
    finally:
        k.CloseHandle(handle)


class OwnedJob:
    """Owner death kills Job members; the legacy broker also joins its own Job."""
    def __init__(self, name, *, assign_current=True):
        if os.name != "nt":
            raise RuntimeError("Durable verifier containment currently requires Windows")
        class Basic(C.Structure):
            _fields_ = [("process_time", C.c_longlong), ("job_time", C.c_longlong),
                        ("flags", W.DWORD), ("min_ws", C.c_size_t), ("max_ws", C.c_size_t),
                        ("active", W.DWORD), ("affinity", C.c_size_t),
                        ("priority", W.DWORD), ("scheduling", W.DWORD)]
        class Extended(C.Structure):
            _fields_ = [("basic", Basic), ("io", C.c_ulonglong * 6),
                        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
                        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]
        self.k = _kernel()
        self.handle = self.k.CreateJobObjectW(None, name)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        if C.get_last_error() == 183:
            self.k.CloseHandle(self.handle)
            raise RuntimeError("Ownership job already exists")
        info = Extended()
        info.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; no breakaway flags.
        if not self.k.SetInformationJobObject(self.handle, 9, C.byref(info), C.sizeof(info)):
            self.k.CloseHandle(self.handle)
            raise C.WinError(C.get_last_error())
        if not assign_current:
            return  # Opt-in controller owns this job, but must never join it.
        proc = self.k.OpenProcess(0x101, False, os.getpid())
        try:
            if not proc or not self.k.AssignProcessToJobObject(self.handle, proc):
                self.k.CloseHandle(self.handle)
                raise C.WinError(C.get_last_error())
        finally:
            if proc:
                self.k.CloseHandle(proc)

    def close(self):
        # The legacy broker is a member, so successful close terminates it too.
        self.k.CloseHandle(self.handle)


class _StartupInfoW(C.Structure):
    _fields_ = [("cb", W.DWORD), ("lpReserved", W.LPWSTR), ("lpDesktop", W.LPWSTR),
                ("lpTitle", W.LPWSTR), ("dwX", W.DWORD), ("dwY", W.DWORD),
                ("dwXSize", W.DWORD), ("dwYSize", W.DWORD), ("dwXCountChars", W.DWORD),
                ("dwYCountChars", W.DWORD), ("dwFillAttribute", W.DWORD), ("dwFlags", W.DWORD),
                ("wShowWindow", W.WORD), ("cbReserved2", W.WORD),
                ("lpReserved2", C.POINTER(W.BYTE)), ("hStdInput", W.HANDLE),
                ("hStdOutput", W.HANDLE), ("hStdError", W.HANDLE)]


class _StartupInfoExW(C.Structure):
    _fields_ = [("StartupInfo", _StartupInfoW), ("lpAttributeList", C.c_void_p)]


class _ProcessInformation(C.Structure):
    _fields_ = [("hProcess", W.HANDLE), ("hThread", W.HANDLE),
                ("dwProcessId", W.DWORD), ("dwThreadId", W.DWORD)]


_PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000d
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000


def _create_verifier_in_job(k, command, cwd, inherited, job_handle):
    """Create suspended in the Job: never return a child awaiting assignment.

    CPython 3.12's _winapi wrapper only supports HANDLE_LIST, so use the
    Windows STARTUPINFOEX contract directly for JOB_LIST as well. No fallback.
    """
    import subprocess
    k.InitializeProcThreadAttributeList.argtypes = [C.c_void_p, W.DWORD, W.DWORD, C.POINTER(C.c_size_t)]
    k.InitializeProcThreadAttributeList.restype = W.BOOL
    k.UpdateProcThreadAttribute.argtypes = [C.c_void_p, W.DWORD, C.c_size_t, C.c_void_p,
                                          C.c_size_t, C.c_void_p, C.POINTER(C.c_size_t)]
    k.UpdateProcThreadAttribute.restype = W.BOOL
    k.DeleteProcThreadAttributeList.argtypes, k.DeleteProcThreadAttributeList.restype = [C.c_void_p], None
    k.CreateProcessW.argtypes = [W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL,
                                W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(_StartupInfoW),
                                C.POINTER(_ProcessInformation)]
    k.CreateProcessW.restype = W.BOOL
    size = C.c_size_t()
    C.set_last_error(0)
    if (k.InitializeProcThreadAttributeList(None, 2, 0, C.byref(size)) or
            C.get_last_error() != 122 or not size.value):  # ERROR_INSUFFICIENT_BUFFER expected.
        raise RuntimeError("Cannot size creation-time verifier attributes")
    attributes = C.create_string_buffer(size.value)
    initialized = False
    try:
        if not k.InitializeProcThreadAttributeList(attributes, 2, 0, C.byref(size)):
            raise C.WinError(C.get_last_error())
        initialized = True
        jobs = (W.HANDLE * 1)(job_handle)
        handles = (W.HANDLE * len(inherited))(*inherited)
        # Attribute values and buffers remain alive through creation and deletion.
        for attribute, value in ((_PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
                                 (_PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles)):
            if not k.UpdateProcThreadAttribute(attributes, 0, attribute, C.byref(value),
                                               C.sizeof(value), None, None):
                raise C.WinError(C.get_last_error())
        startup = _StartupInfoExW()
        startup.StartupInfo.cb = C.sizeof(startup)
        startup.StartupInfo.dwFlags = 0x100  # STARTF_USESTDHANDLES
        startup.StartupInfo.hStdInput, startup.StartupInfo.hStdOutput, startup.StartupInfo.hStdError = inherited
        startup.lpAttributeList = C.addressof(attributes)
        information = _ProcessInformation()
        command_line = C.create_unicode_buffer(subprocess.list2cmdline(command))
        if not k.CreateProcessW(str(Path(command[0]).resolve()), command_line, None, None, True,
                                _EXTENDED_STARTUPINFO_PRESENT | 0x00000004 | 0x08000000,
                                None, str(cwd), C.byref(startup.StartupInfo), C.byref(information)):
            raise C.WinError(C.get_last_error())
        return information.hProcess, information.hThread, information.dwProcessId
    finally:
        if initialized:
            k.DeleteProcThreadAttributeList(attributes)


class ControllerVerifier:
    """Direct creation provenance: retain process/thread/job handles before execution.

    No PID attachment or worker receipt is involved. This is lifecycle containment,
    not an isolation boundary against hostile processes running as the same user.
    """
    def __init__(self, job_name):
        self.job = OwnedJob(job_name, assign_current=False)
        self.k = self.job.k
        self.process = self.thread = None
        self.pid = self.identity = None
        self.k.ResumeThread.argtypes, self.k.ResumeThread.restype = [W.HANDLE], W.DWORD
        self.k.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]

    def launch(self, command, cwd, streams):
        import _winapi
        import msvcrt
        if self.process is not None:
            raise RuntimeError("Verifier process already created")
        if (not isinstance(command, list) or not command or
                any(not isinstance(arg, str) or "\0" in arg for arg in command) or
                not Path(command[0]).is_absolute() or len(streams) != 3):
            raise ValueError("Native verifier requires absolute executable, argv and three standard streams")
        # Explicit handle list: never inherit the job/process/thread handles.
        inherited = []
        try:
            try:
                current = _winapi.GetCurrentProcess()
                for stream in streams:
                    inherited.append(_winapi.DuplicateHandle(
                        current, msvcrt.get_osfhandle(stream.fileno()), current,
                        0, True, _winapi.DUPLICATE_SAME_ACCESS))
                self.process, self.thread, self.pid = _create_verifier_in_job(
                    self.k, command, cwd, inherited, self.job.handle)
            finally:
                for handle in inherited:
                    _winapi.CloseHandle(handle)
            self.identity = _handle_identity(self.k, self.process)
            if (self.identity is None or self.identity["executable"] !=
                    os.path.normcase(str(Path(command[0]).resolve()))):
                raise RuntimeError("Created verifier executable identity mismatch")
            self.verify(self.identity)
        except BaseException:
            if self.process is not None:
                _winapi.TerminateProcess(self.process, 137)
                _winapi.WaitForSingleObject(self.process, 10000)
            raise

    def verify(self, expected_identity):
        member = W.BOOL()
        if (_handle_identity(self.k, self.process) != expected_identity
                or not self.k.IsProcessInJob(self.process, self.job.handle, C.byref(member))
                or not member.value):
            raise RuntimeError("Retained verifier identity/job membership mismatch")

    def resume(self, expected_identity):
        self.verify(expected_identity)
        previous = self.k.ResumeThread(self.thread)
        if previous != 1:
            raise RuntimeError("Verifier primary thread was not suspended exactly once")

    def wait(self, timeout):
        status = self.k.WaitForSingleObject(self.process, max(0, min(int(timeout * 1000), 0xfffffffe)))
        if status == 258:
            return None
        if status != 0:
            raise RuntimeError("Native verifier wait failed")
        code = W.DWORD()
        if not self.k.GetExitCodeProcess(self.process, C.byref(code)):
            raise C.WinError(C.get_last_error())
        return code.value  # Includes 259: a signaled handle distinguishes STILL_ACTIVE.

    def terminate_job(self):
        if not self.k.TerminateJobObject(self.job.handle, 137):
            raise C.WinError(C.get_last_error())

    def drain(self, timeout=10):
        _wait_job_empty(self.k, self.job.handle, timeout)

    def close(self):
        # KILL_ON_JOB_CLOSE also covers exceptions and controller death.
        import _winapi
        try:
            if self.process is not None and self.wait(0) is None:
                # Also covers an exception before suspended-process Job assignment.
                _winapi.TerminateProcess(self.process, 137)
                if _winapi.WaitForSingleObject(self.process, 10000) != 0:
                    raise RuntimeError("Verifier handle did not signal during close")
        finally:
            if self.job is not None:
                self.job.close()
                self.job = None
            for name in ("thread", "process"):
                handle = getattr(self, name)
                if handle is not None:
                    self.k.CloseHandle(handle)
                    setattr(self, name, None)


def terminate_owned_job(record, boot=boot_identity):
    """Retain handles through verification and signaling: no PID check/kill race."""
    if os.name != "nt" or record["boot"] != boot():
        raise RuntimeError("Refusing to signal a process from another boot")
    k = _kernel()
    process = k.OpenProcess(0x1000 | 0x100000, False, record["pid"])
    job = None
    try:
        if not process or _handle_identity(k, process) != record["identity"]:
            raise RuntimeError("Refusing termination: process identity mismatch")
        job = k.OpenJobObjectW(0x0008 | 0x0004, False, record["job"])
        member = W.BOOL()
        if not job or not k.IsProcessInJob(process, job, C.byref(member)) or not member.value:
            raise RuntimeError("Refusing termination: job membership unproven")
        if record["boot"] != boot() or not k.TerminateJobObject(job, 137):
            raise RuntimeError("Owned job termination failed")
        if k.WaitForSingleObject(process, 10000) != 0:
            raise RuntimeError("Owned broker exit not observed")
        _wait_job_empty(k, job)
    finally:
        if job:
            k.CloseHandle(job)
        if process:
            k.CloseHandle(process)


def _wait_job_empty(k, job, timeout=10):
    class Accounting(C.Structure):
        _fields_ = [("times", C.c_longlong * 4), ("faults", W.DWORD),
                    ("total", W.DWORD), ("active", W.DWORD), ("terminated", W.DWORD)]
    deadline = time.monotonic() + timeout
    while True:
        value = Accounting()
        if not k.QueryInformationJobObject(job, 1, C.byref(value), C.sizeof(value), None):
            raise C.WinError(C.get_last_error())
        if value.active == 0:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Owned job still has live descendants; verification refused")
        time.sleep(0.05)


def wait_job_empty(name):
    k = _kernel()
    job = k.OpenJobObjectW(0x0004, False, name)
    if not job:
        if C.get_last_error() == 2:  # Last handle closed and job destroyed.
            return
        raise C.WinError(C.get_last_error())
    try:
        _wait_job_empty(k, job)
    finally:
        k.CloseHandle(job)


def servers():
    """Enumerate every llama-server so a stale/unmanaged child fails closed."""
    if os.name != "nt":
        return []
    k = C.WinDLL("kernel32", use_last_error=True)
    class Entry(C.Structure):
        _fields_ = [("size", W.DWORD), ("usage", W.DWORD), ("pid", W.DWORD),
                    ("heap", C.c_size_t), ("module", W.DWORD), ("threads", W.DWORD),
                    ("parent", W.DWORD), ("priority", W.LONG), ("flags", W.DWORD),
                    ("exe", W.WCHAR * 260)]
    class Memory(C.Structure):
        _fields_ = [("cb", W.DWORD), ("faults", W.DWORD)] + [
            (n, C.c_size_t) for n in ("peak_ws", "ws", "peak_pool_p", "pool_p",
                                    "peak_pool_np", "pool_np", "pagefile", "peak_pagefile", "private")]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Process32FirstW.argtypes = k.Process32NextW.argtypes = [W.HANDLE, C.POINTER(Entry)]
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
    ps = C.WinDLL("psapi", use_last_error=True)
    ps.GetProcessMemoryInfo.argtypes = [W.HANDLE, C.POINTER(Memory), W.DWORD]
    snap = k.CreateToolhelp32Snapshot(2, 0)
    if snap == W.HANDLE(-1).value:
        raise C.WinError(C.get_last_error())
    result = []
    try:
        entry = Entry(size=C.sizeof(Entry))
        ok = k.Process32FirstW(snap, C.byref(entry))
        while ok:
            if entry.exe.lower() == "llama-server.exe":
                record = {"pid": entry.pid, "parent_pid": entry.parent}
                handle = k.OpenProcess(0x410, False, entry.pid)
                if handle:
                    try:
                        name, length = C.create_unicode_buffer(32768), W.DWORD(32768)
                        if k.QueryFullProcessImageNameW(handle, 0, name, C.byref(length)):
                            record["path"] = name.value
                        memory = Memory(cb=C.sizeof(Memory))
                        if ps.GetProcessMemoryInfo(handle, C.byref(memory), C.sizeof(memory)):
                            record.update(working_set_gb=memory.ws / 2**30, private_gb=memory.private / 2**30)
                    finally:
                        k.CloseHandle(handle)
                result.append(record)
            ok = k.Process32NextW(snap, C.byref(entry))
        if C.get_last_error() != 18:  # ERROR_NO_MORE_FILES
            raise C.WinError(C.get_last_error())
    finally:
        k.CloseHandle(snap)
    return result


def hotpin_environment(pid, expected_identity=None):
    """Read only LLAMA_HOT_EXPERTS from a native x64 child PEB for validation."""
    if os.name != "nt" or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Environment evidence requires native x64 Windows Python")
    k = _kernel()
    n = C.WinDLL("ntdll")
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)]
    n.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.c_void_p]
    handle = k.OpenProcess(0x410, False, pid)
    if not handle:
        raise C.WinError(C.get_last_error())
    def read(addr, count):
        data, used = C.create_string_buffer(count), C.c_size_t()
        if not k.ReadProcessMemory(handle, addr, data, count, C.byref(used)):
            raise C.WinError(C.get_last_error())
        if used.value != count:
            raise RuntimeError("Incomplete HotPin environment read")
        return data.raw
    try:
        if expected_identity is not None and _handle_identity(k, handle) != expected_identity:
            raise RuntimeError("HotPin environment process identity mismatch")
        basic = (C.c_ulonglong * 6)()
        status = n.NtQueryInformationProcess(handle, 0, basic, C.sizeof(basic), None)
        if status:
            raise RuntimeError(f"NtQueryInformationProcess: {status}")
        params = int.from_bytes(read(basic[1] + 0x20, 8), "little")
        env = int.from_bytes(read(params + 0x80, 8), "little")
        # Read one UTF-16 unit at a time to stop before the allocation boundary.
        data = bytearray()
        for offset in range(0, 262144, 2):
            data.extend(read(env + offset, 2))
            if len(data) >= 4 and data[-4:] == b"\0\0\0\0":
                break
        else:
            raise RuntimeError("Unterminated HotPin environment")
        values = [value.split("=", 1)[1] for value in data.decode("utf-16-le").split("\0")
                  if value.upper().startswith("LLAMA_HOT_EXPERTS=")]
        if len(values) > 1:
            raise RuntimeError("Ambiguous HotPin expert environment")
        if expected_identity is not None and _handle_identity(k, handle) != expected_identity:
            raise RuntimeError("HotPin environment process identity changed")
        return values[0] if values else None
    finally:
        k.CloseHandle(handle)


def limit_working_set(pid, maximum_gb, expected_identity=None):
    """Apply and read back a process-only hard maximum, retaining HotPin's minimum."""
    if os.name != "nt":
        raise RuntimeError("Reviewer working-set limit requires Windows")
    k = _kernel()
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.GetProcessWorkingSetSizeEx.argtypes = [W.HANDLE, C.POINTER(C.c_size_t), C.POINTER(C.c_size_t), C.POINTER(W.DWORD)]
    k.SetProcessWorkingSetSizeEx.argtypes = [W.HANDLE, C.c_size_t, C.c_size_t, W.DWORD]
    handle = k.OpenProcess(0x500, False, pid)  # QUERY_INFORMATION | SET_QUOTA
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        if expected_identity is not None and _handle_identity(k, handle) != expected_identity:
            raise RuntimeError("Working-set process identity mismatch")
        minimum, maximum, flags = C.c_size_t(), C.c_size_t(), W.DWORD()
        if not k.GetProcessWorkingSetSizeEx(handle, C.byref(minimum), C.byref(maximum), C.byref(flags)):
            raise C.WinError(C.get_last_error())
        cap = int(maximum_gb * 2**30)
        if cap < minimum.value:
            raise ValueError("Working-set ceiling is below HotPin's existing minimum")
        if not k.SetProcessWorkingSetSizeEx(handle, minimum.value, cap, (flags.value & ~8) | 4):
            raise C.WinError(C.get_last_error())
        if not k.GetProcessWorkingSetSizeEx(handle, C.byref(minimum), C.byref(maximum), C.byref(flags)):
            raise C.WinError(C.get_last_error())
        if (not flags.value & 4 or flags.value & 8 or not 0 < maximum.value <= cap
                or maximum.value < minimum.value):
            raise RuntimeError("Windows did not enforce the requested working-set ceiling")
        if expected_identity is not None and _handle_identity(k, handle) != expected_identity:
            raise RuntimeError("Working-set process identity changed")
        return dict(pid=pid, minimum_gb=minimum.value / 2**30,
                    maximum_gb=maximum.value / 2**30, flags=flags.value)
    finally:
        k.CloseHandle(handle)
