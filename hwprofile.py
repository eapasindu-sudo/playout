"""This PC's hardware (CPU, RAM, graphics card) and the settings Playout picks for it.

Playout runs on very different PCs: a 4-core office PC with 8 GB, a gaming PC, a workstation. At start this
looks at the PC once and sizes what depends on it, so a small PC is not overloaded and a big one is used:

  - memory for looping video CGs (and how much each one may keep) from the RAM
  - decoded frames held ahead for each clip from the RAM and the CPU
  - the x264 preset for recording when the PC has no GPU encoder
  - pictures per second taken from web CGs (scoreboards) in the Edge / Chrome fallback

GPU work (encoders, decoding, mixing) was already chosen per PC ("auto": NVIDIA, Intel or AMD, else the CPU);
the profile reports what was found. Any of these can be fixed in config.json ("hw_profile": "light" / "standard"
/ "strong" picks a level; "cg_cache_mb", "clip_buffer" set a value); "auto" (the default) means this PC decides.
"""
import logging
import os
import platform
import subprocess
import sys

LEVELS = {
    #             CG cache share of RAM, its limits (MB),  frames ahead, x264 preset, web CG pictures / s
    "light":    {"cache_share": 0.05, "cache_min": 250, "cache_max": 500, "buffer": 6, "preset": "superfast", "web_rate": 8},
    "standard": {"cache_share": 0.07, "cache_min": 400, "cache_max": 1200, "buffer": 8, "preset": "veryfast", "web_rate": 10},
    "strong":   {"cache_share": 0.08, "cache_min": 800, "cache_max": 2500, "buffer": 12, "preset": "faster", "web_rate": 15},
}

PROFILE = {}      # filled by apply(): what was found and what was picked (Health page, diagnostics)


def _cpu_name():
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    if sys.platform == "darwin":
        try:
            return subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True,
                                  timeout=3).stdout.strip()
        except Exception:
            pass
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "CPU"


def _gpus():
    """Names of the graphics cards (Windows: from the display adapters; elsewhere best effort)."""
    names = []
    if sys.platform == "win32":
        try:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  "(Get-CimInstance Win32_VideoController).Name"], capture_output=True, text=True,
                                 timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            names = [n.strip() for n in out.splitlines() if n.strip()]
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            out = subprocess.run(["system_profiler", "SPDisplaysDataType"], capture_output=True, text=True, timeout=8).stdout
            names = [ln.split(":", 1)[1].strip() for ln in out.splitlines() if "Chipset Model" in ln]
        except Exception:
            pass
    return [n for n in names if "Microsoft Basic" not in n and "Remote Display" not in n]


def detect():
    import psutil
    ram = psutil.virtual_memory().total / 2 ** 30
    return {"cpu": _cpu_name(), "cores": psutil.cpu_count(logical=False) or os.cpu_count() or 1,
            "threads": psutil.cpu_count() or os.cpu_count() or 1, "ram_gb": round(ram, 1), "gpus": _gpus(),
            "os": f"{platform.system()} {platform.release()}"}


def level_for(hw):
    """light: 4 threads or fewer, or 8 GB or less; strong: 12+ threads and 24 GB+; else standard."""
    if hw["threads"] <= 4 or hw["ram_gb"] < 9:
        return "light"
    if hw["threads"] >= 12 and hw["ram_gb"] >= 24:
        return "strong"
    return "standard"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def apply(cfg):
    """Detect this PC and set the module defaults. Call once at start, before clips / CGs open."""
    import cg
    import media
    try:
        hw = detect()
    except Exception as e:
        logging.warning("hardware profile: %s", e)
        hw = {"cpu": "?", "cores": os.cpu_count() or 1, "threads": os.cpu_count() or 1, "ram_gb": 8.0, "gpus": [], "os": ""}
    want = str(cfg.get("hw_profile", "auto")).lower()
    lvl = want if want in LEVELS else level_for(hw)
    L = LEVELS[lvl]
    cache = _num(cfg.get("cg_cache_mb"))
    if cache is None:
        cache = min(L["cache_max"], max(L["cache_min"], hw["ram_gb"] * 1024 * L["cache_share"]))
    cg.CACHE_BUDGET = int(cache) * 1024 * 1024
    cg.CGLayer.CACHE_LIMIT = min(cg.CACHE_BUDGET, max(cg.CGLayer.CACHE_LIMIT, cg.CACHE_BUDGET // 2))
    buf = _num(cfg.get("clip_buffer"))
    media.ClipPlayer.VIDEO_BUFFER = int(buf) if buf and buf >= 2 else L["buffer"]
    PROFILE.clear()
    PROFILE.update(hw, level=lvl, chosen=want if want in LEVELS else "auto", cg_cache_mb=int(cache),
                   clip_buffer=media.ClipPlayer.VIDEO_BUFFER, x264_preset=L["preset"], web_rate=L["web_rate"])
    logging.info("This PC: %s, %d cores / %d threads, %.0f GB RAM, %s -> %s profile (CG memory %d MB, %d frames ahead)",
                 hw["cpu"], hw["cores"], hw["threads"], hw["ram_gb"], ", ".join(hw["gpus"]) or "graphics ?",
                 lvl, int(cache), media.ClipPlayer.VIDEO_BUFFER)
    return PROFILE


def x264_preset(default="veryfast"):
    """The CPU encoder preset for this PC (used when streams / recording run on x264)."""
    return PROFILE.get("x264_preset") or default


def web_rate(default=10):
    return PROFILE.get("web_rate") or default
