"""Restart a stalled bore transport after repeated pinned TLS health failures.

Only the transport is restarted. Desktop requests and result receipts remain
owned by the existing brokers and workers and are never submitted here.
"""
import argparse
import hashlib
import hmac
import signal
import socket
import ssl
import subprocess
import threading


def reachable(host,port,pin):
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname=False;context.verify_mode=ssl.CERT_NONE
    try:
        with socket.create_connection((host,port),8) as sock:
            with context.wrap_socket(sock,server_hostname=host) as secured:
                return hmac.compare_digest(hashlib.sha256(secured.getpeercert(True)).hexdigest(),pin)
    except (OSError,ValueError):return False


def supervise(command,host,port,pin,stop,launch=subprocess.Popen,probe=reachable):
    child=None
    try:
        while not stop.is_set():
            child=launch(command)
            failures=0
            while not stop.wait(20):
                if child.poll() is not None:break
                failures=0 if probe(host,port,pin) else failures+1
                if failures>=3:break
            if child.poll() is None:
                child.terminate()
                try:child.wait(timeout=5)
                except subprocess.TimeoutExpired:child.kill();child.wait()
            child=None
            if not stop.is_set():stop.wait(5)
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:child.wait(timeout=5)
            except subprocess.TimeoutExpired:child.kill();child.wait()


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',required=True);parser.add_argument('--remote',type=int,required=True)
    parser.add_argument('--pin',required=True);parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args();command=args.command
    if command and command[0]=='--':command=command[1:]
    if not command:parser.error('missing tunnel command')
    stop=threading.Event()
    for sig in (signal.SIGTERM,signal.SIGINT):signal.signal(sig,lambda *_:stop.set())
    supervise(command,args.host,args.remote,args.pin,stop)


if __name__=='__main__':main()
