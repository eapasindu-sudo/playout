"""Playout desktop app: the control screen in its own window (no browser).

Playout.exe runs this. The web server still listens on the network, so a tablet or
second PC can open http://PLAYOUT-PC:8080 as a remote control.

    Playout.exe              normal start: a small launcher starts the Playout window and
                             starts it again if it ever crashes or freezes (it then carries on
                             with the item that was on air)
    Playout.exe --app        the Playout window itself, without the launcher
    Playout.exe --headless   no window (used by the build test)
    Playout.exe --composer   CG Composer (separate CG preparation window)
"""
import os
import socket
import subprocess
import sys
import threading
import time

import paths

HOME = paths.user_home()
if paths.FROZEN:
    # windowed exe has no console: send prints and errors to a log file
    os.makedirs(os.path.join(HOME, "logs"), exist_ok=True)
    _log = open(os.path.join(HOME, "logs", "playout.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = _log
    print(f"\n==== Playout start {time.strftime('%Y-%m-%d %H:%M:%S')} ====")


def message_box(title, text):
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    else:
        print(f"{title}: {text}")


def port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_server(port, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if port_in_use(port):
            return True
        time.sleep(0.2)
    return False


EXIT_ALREADY_RUNNING = 10      # launcher: do not restart for these
EXIT_START_FAILED = 11


def supervise():
    """Launcher: runs the Playout window as a child process and restarts it after a crash."""
    args = [sys.executable, "--app"] + [a for a in sys.argv[1:] if a != "--app"]
    recent = []
    while True:
        started = time.time()
        code = subprocess.call(args)
        if code in (0, EXIT_ALREADY_RUNNING, EXIT_START_FAILED):
            return code
        now = time.time()
        print(f"launcher: Playout stopped unexpectedly (exit code {code}) after {now - started:.0f}s - restarting")
        recent = [t for t in recent if now - t < 600] + [now]
        if len(recent) > 5:
            message_box("Playout", "Playout stopped unexpectedly 6 times in 10 minutes and was not started again.\n\n"
                                   "See Documents\\Playout\\logs\\playout.log")
            return code
        time.sleep(2)


def main():
    if "--composer" in sys.argv:
        import composer
        return composer.main()

    if paths.FROZEN and not ({"--app", "--headless", "--no-launcher"} & set(sys.argv)):
        return supervise()

    import app as server

    port = int(server.cfg.get("web_port", 8080))
    if port_in_use(port):
        message_box("Playout", f"Playout is already running (port {port} is in use).\n\n"
                               "Close the other Playout window first.")
        return EXIT_ALREADY_RUNNING

    server.start_backend()
    threading.Thread(target=server.serve, kwargs={"port": port}, daemon=True, name="web").start()
    if not wait_for_server(port):
        message_box("Playout", "The control screen did not start. See Documents\\Playout\\logs\\playout.log")
        server.shutdown()
        return EXIT_START_FAILED

    def install_update():
        """The update installer is running: close cleanly (exit 0, so the launcher does not restart us)."""
        server.shutdown()
        os._exit(0)

    server.updater.on_install = install_update
    print(f"Control screen on http://localhost:{port}")

    if "--headless" in sys.argv:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    import webview

    main_win = webview.create_window(
        "Playout", f"http://127.0.0.1:{port}/",
        width=1600, height=950, min_size=(1100, 700),
        background_color="#0c0d0f", confirm_close=True,
    )
    out = {"win": None}

    def fullscreen(on, screen=None):
        """Program output, full screen, on a display (default: the last one, normally the second monitor)."""
        if out["win"] is not None:
            try:
                out["win"].destroy()
            except Exception:
                pass
            out["win"] = None
        if not on:
            return
        screens = webview.screens
        idx = int(screen) if screen not in (None, "") else len(screens) - 1
        sc = screens[max(0, min(idx, len(screens) - 1))]
        win = webview.create_window("Playout output", f"http://127.0.0.1:{port}/output?w={sc.width}",
                                    screen=sc, fullscreen=True, frameless=True, background_color="#000000")
        win.events.closed += lambda: out.update(win=None) if out["win"] is win else None
        out["win"] = win

    server.fullscreen_hook = fullscreen
    main_win.events.closed += lambda: fullscreen(False)
    webview.start()       # returns when the window is closed
    server.shutdown()
    return 0


if __name__ == "__main__":
    code = main()
    os._exit(code or 0)   # stop any stuck background threads
