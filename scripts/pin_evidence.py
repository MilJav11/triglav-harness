"""Read-only sampling of Windows' Locked page bit in mapped memory."""
import ctypes as C
from ctypes import wintypes as W


def locked_pages(pid):
    class Region(C.Structure):
        _fields_ = [("base", C.c_void_p), ("allocation", C.c_void_p),
                    ("allocation_protect", W.DWORD), ("partition", W.WORD),
                    ("size", C.c_size_t), ("state", W.DWORD), ("protect", W.DWORD), ("type", W.DWORD)]
    class Page(C.Structure):
        _fields_ = [("address", C.c_void_p), ("flags", C.c_size_t)]
    k = C.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k.OpenProcess.restype = W.HANDLE
    k.CloseHandle.argtypes = [W.HANDLE]
    k.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(Region), C.c_size_t]
    k.VirtualQueryEx.restype = C.c_size_t
    ps = C.WinDLL("psapi", use_last_error=True)
    ps.QueryWorkingSetEx.argtypes = [W.HANDLE, C.c_void_p, W.DWORD]
    handle = k.OpenProcess(0x400, False, pid)
    if not handle:
        raise C.WinError(C.get_last_error())
    try:
        addresses, address = [], 0
        region = Region()
        while k.VirtualQueryEx(handle, address, C.byref(region), C.sizeof(region)):
            end = (region.base or 0) + region.size
            if end <= address:
                raise RuntimeError("Invalid virtual region")
            # One 4-KiB page at each 1-MiB interval in committed file mappings.
            if region.state == 0x1000 and region.type == 0x40000 and not region.protect & 0x101:
                addresses.extend(range(region.base, end, 2**20))
            address = end
        pages = (Page * len(addresses))(*(Page(a, 0) for a in addresses))
        if not ps.QueryWorkingSetEx(handle, pages, C.sizeof(pages)):
            raise C.WinError(C.get_last_error())
        locked = sum(bool(p.flags & 1 and p.flags & (1 << 22)) for p in pages)
        return dict(pid=pid, mapped_pages_sampled=len(pages),
                    valid_pages=sum(bool(p.flags & 1) for p in pages),
                    locked_pages_sampled=locked, sample_stride_bytes=2**20)
    finally:
        k.CloseHandle(handle)
