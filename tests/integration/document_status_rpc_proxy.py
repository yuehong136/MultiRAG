"""Transparent real Thrift traffic, with one explicitly controlled Update delay."""

import re
import select
import socket
import threading


class StatusRPCProxy:
    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self.stop = threading.Event()
        self.entered, self.release = threading.Event(), threading.Event()
        self.arm = False
        self.lock = threading.Lock()
        self.events: list[tuple[int, str]] = []
        self.active: set[int] = set()
        self.workers: list[threading.Thread] = []
        self.thread = threading.Thread(target=self.accept, daemon=True)
        self.thread.start()

    def accept(self) -> None:
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            identifier = len(self.workers) + 1
            with self.lock:
                self.active.add(identifier)
            worker = threading.Thread(target=self.relay, args=(client, identifier), daemon=True)
            self.workers.append(worker)
            worker.start()

    def relay(self, client: socket.socket, identifier: int) -> None:
        try:
            with client, socket.create_connection(("127.0.0.1", self.target_port), timeout=10) as upstream:
                upstream.settimeout(None)
                while not self.stop.is_set():
                    ready, _, _ = select.select([client, upstream], [], [], 0.1)
                    for source in ready:
                        data = source.recv(65536)
                        if not data:
                            return
                        target = upstream if source is client else client
                        if source is client:
                            methods = re.findall(rb"\x80\x01\x00\x01\x00\x00\x00[\x01-\x7f]([A-Za-z]+)", data)
                            for method in methods:
                                name = method.decode()
                                with self.lock:
                                    self.events.append((identifier, name))
                                    delayed = self.arm and name == "Update"
                                    if delayed:
                                        self.arm = False
                                if delayed:
                                    self.entered.set()
                                    assert self.release.wait(30), "controlled real RPC delay timed out"
                        target.sendall(data)
        finally:
            with self.lock:
                self.active.discard(identifier)

    def close(self) -> None:
        self.stop.set()
        self.release.set()
        self.listener.close()
        self.thread.join(5)
        for worker in self.workers:
            worker.join(5)
        assert not self.thread.is_alive() and not any(worker.is_alive() for worker in self.workers)
        assert not self.active
