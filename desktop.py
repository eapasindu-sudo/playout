"""Playout desktop app: the control screen in its own window (no browser).

Playout.exe runs this. The web server still listens on the network, so a tablet or
second PC can open http://PLAYOUT-PC:8080 as a remote control.

    Playout.exe              normal start: a small launcher starts the Playout window and
                             starts it again if it ever crashes or freezes (it then carries on
                             with the item that was on air)
    Playout.exe --app        the Playout window itself, without the launcher
    Playout.exe --headless   no window (used by the build test)
    Playout.exe --composer   CG Composer (separate CG preparation window)
    Playout.exe --atem       ATEM & Cameras (ATEM switcher + Sony camera control window)
"""
import os
import socket
import subprocess
import sys
import threading
import time

import certs  # HTTPS certificates from the Windows store (update checks on every PC)
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
EXIT_RESTART = 12              # launcher: start again at once (Settings > Output format)


def supervise():
    """Launcher: runs the Playout window as a child process and restarts it after a crash."""
    args = [sys.executable, "--app"] + [a for a in sys.argv[1:] if a != "--app"]
    recent = []
    while True:
        started = time.time()
        # crashed twice within a minute: start without putting the on-air item back (it may be the cause)
        quick = [t for t in recent if started - t < 60]
        code = subprocess.call(args + (["--no-resume"] if len(quick) >= 2 else []))
        if code == EXIT_RESTART:
            time.sleep(1)
            continue
        if code in (0, EXIT_ALREADY_RUNNING, EXIT_START_FAILED):
            return code
        now = time.time()
        print(f"launcher: Playout stopped unexpectedly (exit code {code}) after {now - started:.0f}s - restarting")
        recent = [t for t in recent if now - t < 600] + [now]
        if len(recent) > 5:
            message_box("Endora Playout", "Playout stopped unexpectedly 6 times in 10 minutes and was not started again.\n\n"
                                   "See Documents\\Playout\\logs\\playout.log")
            return code
        time.sleep(2)


def _above_normal():
    """Playout runs a little above other programs, so a busy PC slows them, not the program
    output, the stream or the recording. (The scoreboard browser is started below normal.)"""
    if sys.platform != "win32":
        return
    try:
        import psutil
        psutil.Process().nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def main():
    if "--composer" in sys.argv:
        import composer
        return composer.main()
    if "--atem" in sys.argv:
        import atemwin
        return atemwin.main()

    if paths.FROZEN and not ({"--app", "--headless", "--no-launcher"} & set(sys.argv)):
        return supervise()

    _above_normal()
    if "--headless" in sys.argv:
        r = start_server()
        if isinstance(r, int):
            return r
        server, port = r
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        server.shutdown()
        return 0

    import webview
    import splash
    import winicon

    winicon.own_taskbar_button("Endora.Playout")
    # the loading screen shows at once; the engine starts behind it, then the control screen opens
    w, h = splash.SIZE
    x = y = None
    try:                      # in the middle of the main monitor
        sc = webview.screens[0]
        x = getattr(sc, "x", 0) + max(0, (sc.width - w) // 2)
        y = getattr(sc, "y", 0) + max(0, (sc.height - h) // 2)
    except Exception:
        pass
    loading = webview.create_window("Endora Playout loading", html=splash.html("playout"), width=w, height=h,
                                    x=x, y=y, frameless=True, resizable=False, on_top=True,
                                    background_color="#0c0c0e")
    result = {"code": 0, "server": None}

    def boot():
        try:
            _boot()
        except Exception:
            import traceback
            traceback.print_exc()
            message_box("Endora Playout", "Playout did not start. See Documents\\Playout\\logs\\playout.log")
            result["code"] = EXIT_START_FAILED
            loading.destroy()

    def _boot():
        t0 = time.time()
        r = start_server(lambda text: splash.status(loading, text))
        if isinstance(r, int):
            result["code"] = r
            loading.destroy()
            return
        server, port = r
        result["server"] = server
        splash.status(loading, "Opening the control screen...")
        time.sleep(max(0.0, 1.5 - (time.time() - t0)))      # let the logo be seen
        open_control_screen(webview, server, port)
        loading.destroy()

    webview.start(boot)       # returns when every window is closed
    if result["server"] is not None:
        result["server"].shutdown()
    return result["code"]


def start_server(say=print):
    """Engine + web server. (server module, port), or an exit code when it cannot start."""
    say("Loading the engine...")
    import app as server

    port = int(server.cfg.get("web_port", 8080))
    if port_in_use(port):
        message_box("Endora Playout", f"Playout is already running (port {port} is in use).\n\n"
                                      "Close the other Playout window first.")
        return EXIT_ALREADY_RUNNING

    say("Starting playout, NDI and the playlist...")
    server.start_backend()
    say("Starting the control screen...")
    threading.Thread(target=server.serve, kwargs={"port": port}, daemon=True, name="web").start()
    if not wait_for_server(port):
        message_box("Endora Playout", "The control screen did not start. See Documents\\Playout\\logs\\playout.log")
        server.shutdown()
        return EXIT_START_FAILED

    def install_update():
        """The update installer is running: close cleanly (exit 0, so the launcher does not restart us)."""
        server.shutdown()
        os._exit(0)

    server.updater.on_install = install_update

    def restart():
        """Settings > Output format: close and let the launcher start Playout again."""
        server.shutdown()
        os._exit(EXIT_RESTART)

    if paths.FROZEN and "--app" in sys.argv:
        server.restart_hook = restart
    print(f"Control screen on http://localhost:{port}")
    return server, port


def open_control_screen(webview, server, port):
    main_win = webview.create_window(
        "Endora Playout", f"http://127.0.0.1:{port}/",
        width=1600, height=950, min_size=(1100, 700), maximized=True,
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
        win = webview.create_window("Endora Playout output", f"http://127.0.0.1:{port}/output?w={sc.width}",
                                    screen=sc, fullscreen=True, frameless=True, background_color="#000000")
        win.events.closed += lambda: out.update(win=None) if out["win"] is win else None
        out["win"] = win

    server.fullscreen_hook = fullscreen

    def pick_folder(start=""):
        """Windows folder picker (Settings > Recording > Browse)."""
        try:
            kind = getattr(webview, "FOLDER_DIALOG", None) or webview.FileDialog.FOLDER
            r = main_win.create_file_dialog(kind, directory=start if start and os.path.isdir(start) else "")
        except Exception:
            return None
        return (r[0] if isinstance(r, (list, tuple)) else r) if r else None

    server.folder_dialog_hook = pick_folder
    main_win.events.closed += lambda: fullscreen(False)
    import winicon
    main_win.events.shown += lambda: threading.Timer(0.5, winicon.set_icon, ("Endora Playout", "endora-playout.ico")).start()
    return main_win


if __name__ == "__main__":
    code = main()
    os._exit((code or 0) & 0x7FFFFFFF)   # stop any stuck background threads (a Windows crash code is too big for exit)
