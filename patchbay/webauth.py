"""
The password that protects the app when you open it from another computer
(like your laptop). The password itself is never saved, only a scrambled
fingerprint of it (PBKDF2), in access.json next to this file.

Only uses Python's built-in parts.
"""

import getpass
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

ITERATIONS = 300000
SESSION_DAYS = 30
COOKIE = "devagent_session"


class Access:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.failures = {}   # ip -> [count, blocked_until]

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _write(self, data):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)

    def has_password(self):
        return bool(self._read().get("hash"))

    def set_password(self, password):
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        # A new password logs out every computer that was signed in.
        self._write({"salt": salt.hex(), "hash": digest.hex(), "iterations": ITERATIONS, "sessions": {}})

    def ask_for_new_password(self):
        print("Choose a password for opening this app from other computers (like your laptop).")
        print("Use at least 8 characters. You won't see it as you type.")
        while True:
            a = getpass.getpass("New password: ")
            if len(a) < 8:
                print("That's shorter than 8 characters. Try again.")
                continue
            if getpass.getpass("Type it again: ") != a:
                print("Those didn't match. Try again.")
                continue
            self.set_password(a)
            print("Password saved.")
            return

    # ---- signing in

    def blocked_for(self, ip):
        count, until = self.failures.get(ip, [0, 0])
        return max(0, int(until - time.time()))

    def check_password(self, ip, password):
        """True if right. Too many wrong tries block that computer for a while."""
        if self.blocked_for(ip):
            return False
        data = self._read()
        if not data.get("hash"):
            return False
        digest = hashlib.pbkdf2_hmac("sha256", (password or "").encode(), bytes.fromhex(data["salt"]),
                                     int(data.get("iterations", ITERATIONS)))
        if hmac.compare_digest(digest.hex(), data["hash"]):
            self.failures.pop(ip, None)
            return True
        with self.lock:
            count = self.failures.get(ip, [0, 0])[0] + 1
            wait = 0 if count < 5 else min(3600, 30 * 2 ** (count - 5))
            self.failures[ip] = [count, time.time() + wait]
        time.sleep(1)
        return False

    def new_session(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            data = self._read()
            sessions = {k: v for k, v in data.get("sessions", {}).items() if v > time.time()}
            sessions[hashlib.sha256(token.encode()).hexdigest()] = time.time() + SESSION_DAYS * 86400
            data["sessions"] = sessions
            self._write(data)
        return token

    def session_ok(self, token):
        if not token:
            return False
        key = hashlib.sha256(token.encode()).hexdigest()
        return self._read().get("sessions", {}).get(key, 0) > time.time()

    def end_session(self, token):
        if not token:
            return
        with self.lock:
            data = self._read()
            data.get("sessions", {}).pop(hashlib.sha256(token.encode()).hexdigest(), None)
            self._write(data)
