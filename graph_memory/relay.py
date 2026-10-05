"""Console entry point. A stdio `serve` is relayed to the running MCP server's session
socket when MEMORY_SESSION_SOCKET names one, so a `docker exec` client costs a small
relay process instead of a second copy of the engine. Standard library only: the
engine is imported only when the relay cannot be used.
"""

import json
import os
import socket
import sys
import threading
import time

ENV = "MEMORY_SESSION_SOCKET"


def relayable(argv):
    """(namespace, read_only) for the plain stdio `serve` forms, else None. Anything
    else, including options this parser does not know, goes to the full CLI."""
    namespace = os.environ.get("MEMORY_NAMESPACE", "personal")
    rest = list(argv)
    while rest and rest[0] != "serve":
        option = rest.pop(0)
        if option == "--namespace" and rest:
            namespace = rest.pop(0)
        elif option.startswith("--namespace="):
            namespace = option.split("=", 1)[1]
        elif option != "--debug":
            return None
    if not rest:
        return None
    read_only, rest = False, rest[1:]
    while rest:
        option = rest.pop(0)
        if option == "--read-only":
            read_only = True
        elif option == "--transport" and rest and rest[0] == "stdio":
            rest.pop(0)
        elif option != "--transport=stdio":
            return None
    return namespace, read_only


def connect(path, namespace, read_only):
    """A session the server accepted, or None to serve in this process instead."""
    deadline = time.monotonic() + 10
    while True:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.settimeout(max(deadline - time.monotonic(), 0.1))
            client.connect(path)
            break
        except BlockingIOError:
            # A full accept backlog: the server is there, so wait for it rather than
            # loading a second engine, which is the cost this relay exists to avoid.
            client.close()
            if time.monotonic() > deadline:
                return None
            time.sleep(0.05)
        except OSError:
            client.close()
            return None
    try:
        client.sendall(json.dumps({"namespace": namespace, "read_only": read_only}).encode())
        client.sendall(b"\n")
        reply = b""
        while not reply.endswith(b"\n") and len(reply) < 1024:
            chunk = client.recv(1024 - len(reply))
            if not chunk:
                break
            reply += chunk
        if json.loads(reply) != {"ok": True}:
            raise ValueError("session refused")
        client.settimeout(None)
        return client
    except (OSError, ValueError):
        client.close()
        return None


def pump(client):
    # File descriptors, not sys streams: PYTHONUNBUFFERED changes what those wrap.
    stdin, stdout = sys.stdin.fileno(), sys.stdout.fileno()

    def upstream():
        try:
            while chunk := os.read(stdin, 65536):
                client.sendall(chunk)
        except OSError:
            pass
        # EOF from the client: the server finishes what it has, answers, and closes.
        try:
            client.shutdown(socket.SHUT_WR)
        except OSError:
            pass

    threading.Thread(target=upstream, daemon=True).start()
    try:
        while chunk := client.recv(65536):
            view = memoryview(chunk)
            while view:
                view = view[os.write(stdout, view) :]
    except OSError:
        pass


def main():
    path = os.environ.get(ENV)
    session = relayable(sys.argv[1:]) if path else None
    client = session and connect(path, *session)
    if client is None:
        from .cli import main as cli

        return cli()
    try:
        pump(client)
    finally:
        client.close()


if __name__ == "__main__":
    main()
