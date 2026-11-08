"""Persistent TLS broker. New devices come online without disturbing active requests."""
import http.server
import json
import os
import signal
import ssl
import subprocess
import sys
import threading
from pathlib import Path

from relay import State, handler
from .core import Bridge
from .discovery import serve


def start_listener(root, config):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(root / "server.crt"), str(root / "server.key"))
    # Claim the listener before running recovery on its journal.
    server = http.server.ThreadingHTTPServer(("0.0.0.0", config["port"]), http.server.BaseHTTPRequestHandler)
    try:
        state = State(config["admin"], config["device"], root / "journal")
        server.RequestHandlerClass = handler(state)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server
    except BaseException:
        server.server_close()
        raise


def update_listeners(bridge, listeners):
    for device in bridge.devices():
        if device["id"] in listeners:
            continue
        root = Path(device["runtime"])
        try:
            config = json.loads((root / "config.json").read_text())
            listeners[device["id"]] = (start_listener(root, config), config)
        except (KeyError, ValueError, OSError) as error:
            print(f"Device {device['id']} cannot start: {type(error).__name__}", file=sys.stderr, flush=True)


def main():
    bridge = Bridge()
    stop = threading.Event()
    listeners = {}
    snapshot = []
    threading.Thread(target=serve, args=(lambda: snapshot, stop), daemon=True).start()
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: stop.set())
    awake = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        while not stop.is_set():
            update_listeners(bridge, listeners)
            snapshot = [config for _, config in listeners.values()]
            stop.wait(3)
    finally:
        awake.terminate()
        awake.wait(timeout=3)
        for server, _ in listeners.values():
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    main()
