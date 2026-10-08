"""CPU / RAM / GPU usage for the status bar, sampled in the background every 2 s."""
import shutil
import subprocess
import sys
import threading
import time

import psutil


class _PdhGpu:
    """Windows GPU load from performance counters (works for NVIDIA, AMD and Intel)."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.pdh = ctypes.windll.pdh
        self.query = wintypes.HANDLE()
        if self.pdh.PdhOpenQueryW(None, 0, ctypes.byref(self.query)) != 0:
            raise OSError("PdhOpenQuery failed")
        self.c_util = wintypes.HANDLE()
        self.c_mem = wintypes.HANDLE()
        self.pdh.PdhAddEnglishCounterW(self.query, r"\GPU Engine(*engtype_3D)\Utilization Percentage", 0,
                                       ctypes.byref(self.c_util))
        self.pdh.PdhAddEnglishCounterW(self.query, r"\GPU Adapter Memory(*)\Dedicated Usage", 0,
                                       ctypes.byref(self.c_mem))
        self.pdh.PdhCollectQueryData(self.query)

        class Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", Value)]

        self.Item = Item

    def _sum(self, counter):
        ct = self.ct
        size, count = ct.c_ulong(0), ct.c_ulong(0)
        fmt = 0x00000200 | 0x00008000   # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ct.byref(size), ct.byref(count), None)
        if size.value == 0:
            return None
        buf = (ct.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ct.byref(size), ct.byref(count), buf) != 0:
            return None
        items = ct.cast(buf, ct.POINTER(self.Item))
        return sum(items[i].FmtValue.doubleValue for i in range(count.value) if items[i].FmtValue.CStatus == 0)

    def read(self):
        self.pdh.PdhCollectQueryData(self.query)
        util = self._sum(self.c_util)
        mem = self._sum(self.c_mem)
        return (min(100.0, util) if util is not None else None,
                mem / 1048576 if mem else None)


def _nvidia_smi():
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    out = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used,memory.total,name",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True,
                         timeout=3, creationflags=flags).stdout.strip().splitlines()
    if not out:
        return None
    util, used, total, name = [x.strip() for x in out[0].split(",", 3)]
    return float(util), float(used), float(total), name


class SystemMonitor:
    def __init__(self):
        self.stats = {"cpu": None, "ram": None, "ram_used_gb": None, "ram_total_gb": None,
                      "gpu": None, "gpu_mem_mb": None, "gpu_name": None, "app_cpu": None, "app_ram_mb": None}
        self._proc = psutil.Process()
        self._pdh = None
        self._running = False

    def start(self):
        self._running = True
        if sys.platform == "win32":
            try:
                self._pdh = _PdhGpu()
            except Exception:
                self._pdh = None
        psutil.cpu_percent(None)
        self._proc.cpu_percent(None)
        threading.Thread(target=self._run, daemon=True, name="system monitor").start()

    def stop(self):
        self._running = False

    def _run(self):
        while self._running:
            time.sleep(2)
            try:
                self._sample()
            except Exception:
                pass

    def _sample(self):
        vm = psutil.virtual_memory()
        s = self.stats
        s["cpu"] = round(psutil.cpu_percent(None))
        s["ram"] = round(vm.percent)
        s["ram_used_gb"] = round(vm.used / 1e9, 1)
        s["ram_total_gb"] = round(vm.total / 1e9, 1)
        s["app_cpu"] = round(self._proc.cpu_percent(None) / psutil.cpu_count())
        s["app_ram_mb"] = round(self._proc.memory_info().rss / 1048576)
        if self._pdh:
            try:
                util, mem = self._pdh.read()
                if util is not None:
                    s["gpu"] = round(util)
                    s["gpu_mem_mb"] = round(mem) if mem else None
                    return
            except Exception:
                pass
        try:
            nv = _nvidia_smi()
            if nv:
                s["gpu"], s["gpu_mem_mb"], s["gpu_name"] = round(nv[0]), round(nv[1]), nv[3]
        except Exception:
            pass
