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
    """Broker holds the only long-lived handle; broker death kills its descendants."""
    def __init__(self, name):
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
        proc = self.k.OpenProcess(0x101, False, os.getpid())
        try:
            if not proc or not self.k.AssignProcessToJobObject(self.handle, proc):
                self.k.CloseHandle(self.handle)
                raise C.WinError(C.get_last_error())
        finally:
            if proc:
                self.k.CloseHandle(proc)

    def close(self):
        # Caller is a member: successful close terminates this broker too.
        self.k.CloseHandle(self.handle)


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


def hotpin_environment(pid):
    """Read only LLAMA_HOT_EXPERTS from a native x64 child PEB for validation."""
    if C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("Environment evidence requires native x64 Python")
    k = C.WinDLL("kernel32", use_last_error=True)
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
        return data.raw[:used.value]
    try:
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
        for value in data.decode("utf-16-le").split("\0"):
            if value.startswith("LLAMA_HOT_EXPERTS="):
                return value.split("=", 1)[1]
        return None
    finally:
        k.CloseHandle(handle)


def limit_working_set(pid, maximum_gb):
    """Apply and read back a process-only hard maximum, retaining HotPin's minimum."""
    if os.name != "nt":
        raise RuntimeError("Reviewer working-set limit requires Windows")
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.GetProcessWorkingSetSizeEx.argtypes = [W.HANDLE, C.POINTER(C.c_size_t), C.POINTER(C.c_size_t), C.POINTER(W.DWORD)]
    k.SetProcessWorkingSetSizeEx.argtypes = [W.HANDLE, C.c_size_t, C.c_size_t, W.DWORD]
    handle = k.OpenProcess(0x500, False, pid)  # QUERY_INFORMATION | SET_QUOTA
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
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
        if not flags.value & 4 or maximum.value > cap:
            raise RuntimeError("Windows did not enforce the requested working-set ceiling")
        return dict(pid=pid, minimum_gb=minimum.value / 2**30,
                    maximum_gb=maximum.value / 2**30, flags=flags.value)
    finally:
        k.CloseHandle(handle)
