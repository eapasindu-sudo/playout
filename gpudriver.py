"""Graphics card driver check (Windows): is a real driver installed for the graphics card, and if not,
install one from Windows Update (the driver Microsoft publishes for that card), or open the maker's
driver page.

Without a driver Windows runs the card as "Microsoft Basic Display Adapter": no GPU video decoding,
no GPU encoding (NVENC / Quick Sync / AMF) and no GPU CG blending, so the CPU does everything.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

VENDORS = {
    "10DE": ("NVIDIA", "https://www.nvidia.com/Download/index.aspx"),
    "1002": ("AMD", "https://www.amd.com/en/support/download/drivers.html"),
    "1022": ("AMD", "https://www.amd.com/en/support/download/drivers.html"),
    "8086": ("Intel", "https://www.intel.com/content/www/us/en/support/detect.html"),
}
BASIC = ("microsoft basic display", "microsoft basic render", "standard vga")

STATE = {"checked": False, "cards": [], "missing": False, "install": "", "install_log": ""}
_lock = threading.Lock()
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ps(script, timeout=30):
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=_NO_WINDOW)
    return r.stdout.strip()


def check():
    """Look at the graphics cards of this PC. Returns STATE."""
    if sys.platform != "win32":
        STATE.update(checked=True, cards=[], missing=False)
        return STATE
    cards = []
    try:
        out = _ps("Get-CimInstance Win32_VideoController | Select-Object Name,PNPDeviceID,DriverVersion,"
                  "ConfigManagerErrorCode | ConvertTo-Json -Compress")
        rows = json.loads(out) if out else []
        rows = rows if isinstance(rows, list) else [rows]
        for r in rows:
            name = r.get("Name") or ""
            pnp = (r.get("PNPDeviceID") or "").upper()
            ven = pnp.split("VEN_", 1)[1][:4] if "VEN_" in pnp else ""
            if ven == "1414" or "remote display" in name.lower():   # Microsoft remote / virtual adapters
                continue
            no_driver = any(b in name.lower() for b in BASIC) or (r.get("ConfigManagerErrorCode") or 0) == 28
            maker, url = VENDORS.get(ven, ("", ""))
            cards.append({"name": name, "vendor": maker, "driver": r.get("DriverVersion") or "",
                          "ok": not no_driver, "url": url})
    except Exception as e:
        STATE["error"] = str(e)[:200]
    real = [c for c in cards if c["ok"]]
    STATE.update(checked=True, cards=cards, missing=bool(cards) and not real)
    return STATE


# Windows Update Agent (COM): find and install display drivers offered for this PC. Needs administrator
# rights, so it runs in its own elevated PowerShell (Windows asks "allow changes?" once).
_WU = r"""
$log = '{log}'
function Say($t) {{ Add-Content -Path $log -Value $t }}
try {{
  Say 'Looking for graphics drivers on Windows Update...'
  $s = New-Object -ComObject Microsoft.Update.Session
  $r = $s.CreateUpdateSearcher().Search("IsInstalled=0 and Type='Driver'")
  $todo = New-Object -ComObject Microsoft.Update.UpdateColl
  foreach ($u in $r.Updates) {{ if ($u.DriverClass -eq 'Display') {{ $todo.Add($u) | Out-Null; Say ('Found: ' + $u.Title) }} }}
  if ($todo.Count -eq 0) {{ Say 'NONE'; exit 0 }}
  $d = $s.CreateUpdateDownloader(); $d.Updates = $todo; Say 'Downloading...'; $d.Download() | Out-Null
  $i = $s.CreateUpdateInstaller(); $i.Updates = $todo; Say 'Installing...'
  $res = $i.Install()
  if ($res.ResultCode -eq 2 -or $res.ResultCode -eq 3) {{ Say ('DONE' + $(if ($res.RebootRequired) {{ ' REBOOT' }} else {{ '' }})) }}
  else {{ Say ('FAILED code ' + $res.ResultCode) }}
}} catch {{ Say ('FAILED ' + $_.Exception.Message) }}
"""


def install():
    """Start installing a graphics driver from Windows Update (in the background). Returns STATE."""
    if sys.platform != "win32":
        STATE.update(install="failed", install_log="Only on Windows")
        return STATE
    with _lock:
        if STATE["install"] == "running":
            return STATE
        STATE.update(install="running", install_log="Starting (allow the Windows question)...")
    d = tempfile.mkdtemp(prefix="playout-gpu-")
    log = os.path.join(d, "log.txt")
    ps1 = os.path.join(d, "driver.ps1")
    with open(ps1, "w", encoding="utf-8") as f:
        f.write(_WU.format(log=log.replace("'", "''")))
    threading.Thread(target=_run_install, args=(ps1, log), daemon=True, name="gpu driver").start()
    return STATE


def _run_install(ps1, log):
    try:
        arg = f"-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File \"{ps1}\""
        # Start-Process -Verb RunAs: Windows asks the user to allow it (administrator rights)
        _ps(f"Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '{arg}'", timeout=3600)
    except Exception as e:
        STATE.update(install="failed", install_log=f"Could not start: {e}"[:300])
        return
    text = ""
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            text = f.read().strip()
    except OSError:
        pass
    last = text.splitlines()[-1] if text else ""
    if last.startswith("DONE"):
        STATE.update(install="done", install_log=text + ("\nRestart the PC to finish." if "REBOOT" in last else ""))
    elif last == "NONE":
        STATE.update(install="none", install_log="Windows Update has no graphics driver for this PC: download it from the card maker's page.")
    elif not text:
        STATE.update(install="failed", install_log="Not allowed (the Windows question was answered No) or it did not start.")
    else:
        STATE.update(install="failed", install_log=text)
    time.sleep(1)
    check()
