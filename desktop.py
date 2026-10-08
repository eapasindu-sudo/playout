"""Playout desktop app: the control screen in its own window (no browser).

Playout.exe runs this. The web server still listens on the network, so a tablet or
second PC can open http://PLAYOUT-PC:8080 as a remote control.

    Playout.exe              normal start, opens the Playout window
    Playout.exe --headless   no window (used by the build test)
"""
import os
import socket
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


def main():
    import app as server

    port = int(server.cfg.get("web_port", 8080))
    if port_in_use(port):
        message_box("Playout", f"Playout is already running (port {port} is in use).\n\n"
                               "Close the other Playout window first.")
        return 1

    server.start_backend()
    threading.Thread(target=server.serve, kwargs={"port": port}, daemon=True, name="web").start()
    if not wait_for_server(port):
        message_box("Playout", "The control screen did not start. See Documents\\Playout\\logs\\playout.log")
        server.shutdown()
        return 1
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

    webview.create_window(
        "Playout", f"http://127.0.0.1:{port}/",
        width=1600, height=950, min_size=(1100, 700),
        background_color="#0c0d0f", confirm_close=True,
    )
    webview.start()       # returns when the window is closed
    server.shutdown()
    return 0


if __name__ == "__main__":
    code = main()
    os._exit(code or 0)   # stop any stuck background threads
