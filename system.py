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
        self.c_temp = wintypes.HANDLE()
        if self.pdh.PdhAddEnglishCounterW(self.query, r"\Thermal Zone Information(*)\Temperature", 0,
                                          ctypes.byref(self.c_temp)) != 0:
            self.c_temp = None
        self.pdh.PdhCollectQueryData(self.query)

        class Value(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", Value)]

        self.Item = Item

    def _values(self, counter):
        ct = self.ct
        size, count = ct.c_ulong(0), ct.c_ulong(0)
        fmt = 0x00000200 | 0x00008000   # PDH_FMT_DOUBLE | PDH_FMT_NOCAP100
        self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ct.byref(size), ct.byref(count), None)
        if size.value == 0:
            return []
        buf = (ct.c_byte * size.value)()
        if self.pdh.PdhGetFormattedCounterArrayW(counter, fmt, ct.byref(size), ct.byref(count), buf) != 0:
            return []
        items = ct.cast(buf, ct.POINTER(self.Item))
        return [items[i].FmtValue.doubleValue for i in range(count.value) if items[i].FmtValue.CStatus == 0]

    def cpu_temp(self):
        """Hottest ACPI thermal zone in deg C (Windows reports Kelvin); None when the PC has no thermal zone."""
        if not self.c_temp:
            return None
        c = [k - 273.15 for k in self._values(self.c_temp) if k > 0]
        c = [x for x in c if 5 < x < 125]
        return max(c) if c else None

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
    out = subprocess.run([exe, "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,name",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True,
                         timeout=3, creationflags=flags).stdout.strip().splitlines()
    if not out:
        return None
    util, used, total, temp, name = [x.strip() for x in out[0].split(",", 4)]
    num = lambda x: float(x) if x.replace(".", "", 1).isdigit() else None
    return num(util), num(used), num(total), name, num(temp)


class SystemMonitor:
    def __init__(self):
        self.stats = {"cpu": None, "ram": None, "ram_used_gb": None, "ram_total_gb": None,
                      "gpu": None, "gpu_mem_mb": None, "gpu_name": None, "cpu_temp": None, "gpu_temp": None, "app_cpu": None, "app_ram_mb": None, "app_children_cpu": None, "top_other": [], "app_parts": [],
                      "net_up_mbps": None, "net_down_mbps": None}
        self._proc = psutil.Process()
        self._pdh = None
        self._running = False
        self._net = None
        self._tick = 0

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

    def _top(self, s):
        """What else uses the CPU: Playout's own helper processes (its screen, web CG browsers, ffmpeg) and the
        busiest other programs, so the health page can say where the CPU goes."""
        n = psutil.cpu_count() or 1
        mine = {c.pid for c in self._proc.children(recursive=True)}
        seen, kids, other = getattr(self, "_seen", {}), 0.0, {}
        parts = self._threads(n)
        fresh = {}
        for p in psutil.process_iter(["name"]):
            if p.pid in (0, self._proc.pid):
                continue
            q = seen.get(p.pid)
            if q is None or q.name() != p.info["name"]:
                q = p
                q.cpu_percent(None)            # first look: starts counting
                fresh[p.pid] = q
                continue
            fresh[p.pid] = q
            try:
                c = q.cpu_percent(None) / n
            except psutil.Error:
                continue
            if p.pid in mine:
                kids += c
                nm = (p.info["name"] or "?").removesuffix(".exe").lower()
                nm = "scoreboard / web CG browser" if nm in ("msedge", "chrome", "chromium") else nm
                nm = "GPU engine (mixer, scoreboard, encoders)" if nm == "pgpu" else nm
                parts[nm] = parts.get(nm, 0.0) + c
            elif c >= 1 and (p.info["name"] or "").lower() not in ("system idle process", "idle"):
                nm = (p.info["name"] or "?").removesuffix(".exe")
                other[nm] = other.get(nm, 0.0) + c
        self._seen = fresh
        s["app_children_cpu"] = round(kids)
        s["app_parts"] = [[k, round(v)] for k, v in sorted(parts.items(), key=lambda x: -x[1]) if v >= 2][:6]
        s["top_other"] = [[k, round(v)] for k, v in sorted(other.items(), key=lambda x: -x[1])[:4]]

    def _threads(self, n):
        """CPU of each of Playout's own threads (by name: program output, decode <clip>, stream encoder...),
        as % of the whole CPU. Threads Python did not start (codec and graphics driver workers) are grouped."""
        now = time.time()
        names = {t.native_id: t.name for t in threading.enumerate() if t.native_id}
        cur = {t.id: t.user_time + t.system_time for t in self._proc.threads()}
        last, t0 = getattr(self, "_tlast", ({}, now))
        self._tlast = (cur, now)
        dt = now - t0
        out = {}
        if dt <= 0:
            return out
        for tid, used in cur.items():
            if tid not in last:
                continue
            c = (used - last[tid]) / dt * 100 / n
            nm = names.get(tid, "codec / driver workers")
            nm = "decode clips" if nm.startswith("decode ") else nm
            out[nm] = out.get(nm, 0.0) + c
        return out

    def _sample(self):
        vm = psutil.virtual_memory()
        s = self.stats
        s["cpu"] = round(psutil.cpu_percent(None))
        s["ram"] = round(vm.percent)
        s["ram_used_gb"] = round(vm.used / 1e9, 1)
        s["ram_total_gb"] = round(vm.total / 1e9, 1)
        s["app_cpu"] = round(self._proc.cpu_percent(None) / psutil.cpu_count())
        try:
            self._top(s)
        except Exception:
            pass
        s["app_ram_mb"] = round(self._proc.memory_info().rss / 1048576)
        n, now = psutil.net_io_counters(), time.time()
        if self._net:
            sent, recv, t = self._net
            dt_ = max(0.1, now - t)
            s["net_up_mbps"] = round((n.bytes_sent - sent) * 8 / dt_ / 1e6, 2)
            s["net_down_mbps"] = round((n.bytes_recv - recv) * 8 / dt_ / 1e6, 2)
        self._net = (n.bytes_sent, n.bytes_recv, now)
        self._tick += 1
        nv = None
        if self._tick % 3 == 1:            # temperatures change slowly: every 6 s is enough
            try:
                nv = _nvidia_smi()
                s["gpu_temp"] = round(nv[4]) if nv and nv[4] is not None else None
            except Exception:
                pass
        if self._pdh:
            try:
                t = self._pdh.cpu_temp()
                s["cpu_temp"] = round(t) if t is not None else None
            except Exception:
                pass
            try:
                util, mem = self._pdh.read()
                if util is not None:
                    s["gpu"] = round(util)
                    s["gpu_mem_mb"] = round(mem) if mem else None
                    return
            except Exception:
                pass
        try:
            nv = nv or _nvidia_smi()
            if nv and nv[0] is not None:
                s["gpu"], s["gpu_mem_mb"], s["gpu_name"] = round(nv[0]), round(nv[1] or 0), nv[3]
        except Exception:
            pass
