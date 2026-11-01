"""
The Stop button: cut off the AI's connection at once.

A request to the AI (Ollama or Claude) can sit for a long time before any words come back: while the model loads,
while it reads a long conversation, or for answers that aren't streamed. Checking a "stop" flag between words can't
help then. So the chat's requests go through urlopen() here, which remembers each connection's socket, and abort()
closes them from the Stop button. Ollama notices the closed connection and stops generating.
"""

import http.client
import socket
import threading
import urllib.request

_LOCK = threading.Lock()
_SOCKS = {}          # thread id -> sockets that thread opened through urlopen()


def _track(sock):
    if sock is None:
        return
    with _LOCK:
        _SOCKS.setdefault(threading.get_ident(), set()).add(sock)


class _Conn(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _track(self.sock)


class _SConn(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _track(self.sock)


class _Http(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_Conn, req)


class _Https(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_SConn, req, context=self._context)


_OPENER = urllib.request.build_opener(_Http, _Https)


def urlopen(req, timeout=None):
    """Like urllib.request.urlopen, but abort() can cut it off from another thread."""
    return _OPENER.open(req, timeout=timeout)


def abort(thread_id):
    """Close every connection that thread opened. Its waiting read ends with an error straight away."""
    with _LOCK:
        socks = _SOCKS.pop(thread_id, set())
    for s in socks:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    return len(socks)


def forget(thread_id=None):
    """Drop the list for a thread whose work is finished (the connections are already closed)."""
    with _LOCK:
        _SOCKS.pop(thread_id or threading.get_ident(), None)
