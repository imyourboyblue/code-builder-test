"""
Other ways to reach a device's files besides FTP. Each one behaves like Python's ftplib.FTP for the few commands the
rest of PatchBay uses (mlsd, retrbinary, storbinary, mkd, rmd, delete, rename, size...), so every feature (Files,
scans, backups, edits with undo) works the same whichever way the device is reached.

  - SFTP (files over SSH): uses the Mac's own ssh and sftp tools, with the user's own login (a password, or the
    SSH keys in ~/.ssh). One connection is kept open for a few minutes so each step is quick. The device's identity
    (host key) is remembered the first time, in PatchBay's own list, and a change is reported instead of ignored.
  - SMB (Windows/Mac file sharing): the Mac connects the shared folder (like Finder's Connect to Server) into a
    folder inside data-cache, and PatchBay works on the files there.
  - A folder: any folder that's already on this Mac, for example a share you connected in Finder (/Volumes/...).

SSH devices can also run commands (only after the user confirms each one on a card). Built-in parts only.
"""

import ftplib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
DATA = HERE / "data-cache"
KNOWN_HOSTS = DATA / "ssh_known_hosts"
MOUNTS = DATA / "mounts"
PROTOCOLS = ("ftp", "sftp", "smb", "folder")
DEFAULT_PORTS = {"ftp": [21, 5000], "sftp": [22], "smb": [445], "folder": []}
LABELS = {"ftp": "FTP", "sftp": "SFTP (SSH)", "smb": "SMB file sharing", "folder": "a folder on this Mac"}

_LS = re.compile(r"^([\-dlcbps])[rwxsStT\-]{9}\S*\s+(?:\d+|\?)\s+\S+\s+\S+\s+(\d+)\s+(\w{3}\s+\d+\s+[\d:]+)\s+(.+)$")


def perm(msg):
    return ftplib.error_perm("550 " + (msg or "That didn't work.").strip())


# ---------------------------------------------------------------- SSH / SFTP

class SSHError(OSError):
    pass


class SFTPClient:
    """ftplib.FTP-like access to a device over SFTP, using the Mac's own ssh/sftp."""

    def __init__(self, cfg, port, timeout=20):
        if not shutil.which("sftp") or not shutil.which("ssh"):
            raise SSHError("This computer has no ssh/sftp tools.")
        self.cfg, self.port, self.timeout = cfg, int(port), int(timeout)
        self.user = (cfg.get("username") or "").strip()
        if self.user in ("", "anonymous"):
            self.user = os.environ.get("USER") or "root"
        pw = cfg.get("password") or ""
        self.password = "" if pw == "anonymous@" else pw
        self.target = "%s@%s" % (self.user, cfg["host"])
        DATA.mkdir(parents=True, exist_ok=True)
        self._askpass = None
        self.welcome = ""

    # -- plumbing

    def _opts(self):
        o = ["-o", "StrictHostKeyChecking=accept-new", "-o", "UserKnownHostsFile=%s" % KNOWN_HOSTS,
             "-o", "ConnectTimeout=%d" % max(3, self.timeout), "-o", "ServerAliveInterval=15",
             "-o", "ControlMaster=auto", "-o", "ControlPath=%s" % self._control_path(), "-o", "ControlPersist=180",
             "-o", "LogLevel=ERROR"]
        if self.password:
            o += ["-o", "PreferredAuthentications=password,keyboard-interactive,publickey",
                  "-o", "NumberOfPasswordPrompts=1"]
        else:
            o += ["-o", "BatchMode=yes"]
        return o

    def _control_path(self):
        # Must be short (unix socket paths are limited to ~100 characters), so it lives in /tmp.
        base = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()
        # The login is part of the name, so a changed password logs in again instead of reusing an old connection.
        import hashlib
        tag = hashlib.sha256(("%s\0%s" % (self.user, self.password)).encode()).hexdigest()[:8]
        return os.path.join(base, "pb-%s-%%C" % tag)

    def _env(self):
        env = dict(os.environ)
        if self.password:
            if not self._askpass:
                fd, path = tempfile.mkstemp(prefix="pb-askpass-", dir=str(DATA))
                with os.fdopen(fd, "w") as f:
                    f.write('#!/bin/sh\nprintf "%s\\n" "$PATCHBAY_SSH_PASSWORD"\n')
                os.chmod(path, 0o700)
                self._askpass = path
            env.update(SSH_ASKPASS=self._askpass, SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY", ":0"),
                       PATCHBAY_SSH_PASSWORD=self.password)
        return env

    def _explain(self, err):
        e = (err or "").strip()
        low = e.lower()
        if "remote host identification has changed" in low or "host key verification failed" in low:
            return SSHError("The device's identity (SSH host key) changed since PatchBay first connected. That "
                            "happens after a reset or reinstall, but can also mean something is pretending to be it. "
                            "If you reset it, remove its line from %s and try again." % KNOWN_HOSTS)
        if "permission denied (" in low or "authentication failed" in low or "too many authentication" in low:
            return ftplib.error_perm("530 The SSH login was refused. Check the username and password (or SSH key).")
        if "connection refused" in low:
            return ConnectionRefusedError("Nothing is accepting SSH on port %d." % self.port)
        if "timed out" in low or "no route to host" in low or "could not resolve" in low or \
                "connection closed" in low or "broken pipe" in low or "connection reset" in low:
            return SSHError(e.splitlines()[-1] if e else "The connection dropped.")
        return None

    def _ensure_master(self):
        """Open (or reuse) the kept-open SSH connection. Logging in happens here, once, not on every step:
        sftp's batch mode can't type a password, but it can ride on this connection."""
        base = ["ssh", "-p", str(self.port)] + self._opts()
        try:
            if subprocess.run(base + ["-O", "check", self.target], capture_output=True, timeout=10,
                              stdin=subprocess.DEVNULL).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        fd, errfile = tempfile.mkstemp(prefix="pb-ssh-err-", dir=str(DATA))
        try:
            with os.fdopen(fd, "w+") as err:
                try:
                    # -f: go to the background once logged in; output goes to a file so we don't wait on it.
                    r = subprocess.run(base + ["-f", "-N", "-o", "ControlMaster=yes", self.target], stdout=subprocess.DEVNULL,
                                       stderr=err, stdin=subprocess.DEVNULL, env=self._env(),
                                       timeout=max(20, self.timeout * 2))
                except subprocess.TimeoutExpired:
                    raise SSHError("The SSH login took too long.")
                err.seek(0)
                text = err.read()
        finally:
            try:
                os.unlink(errfile)
            except OSError:
                pass
        if r.returncode:
            known = self._explain(text)
            if known:
                raise known
            raise SSHError((text.strip().splitlines() or ["SSH didn't connect."])[-1])

    def _sftp(self, commands, timeout=None):
        """Run sftp batch commands. Returns stdout. Raises perm() for file problems, OSError for connection ones."""
        self._ensure_master()
        script = "\n".join(commands) + "\n"
        try:
            r = subprocess.run(["sftp", "-q", "-b", "-", "-P", str(self.port)] + self._opts() + [self.target],
                               input=script, capture_output=True, text=True, env=self._env(),
                               timeout=timeout or max(60, self.timeout * 3))
        except subprocess.TimeoutExpired:
            raise SSHError("SFTP took too long to answer.")
        if r.returncode:
            err = (r.stderr or "") + "\n" + (r.stdout or "")
            known = self._explain(r.stderr)
            if known:
                raise known
            lines = [x for x in err.strip().splitlines() if x.strip() and not x.startswith("sftp>")]
            raise perm(lines[-1] if lines else "SFTP said no.")
        return r.stdout

    def run_command(self, command, timeout=120):
        """Run a command on the device over SSH. Returns (exit code, output)."""
        self._ensure_master()
        try:
            r = subprocess.run(["ssh", "-p", str(self.port)] + self._opts() + [self.target, "--", command],
                               capture_output=True, text=True, env=self._env(), timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return 124, "The command was still running after %d seconds, so PatchBay stopped waiting." % timeout
        if r.returncode == 255:
            known = self._explain(r.stderr)
            if known:
                raise known
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    @staticmethod
    def q(path):
        return '"%s"' % str(path).replace("\\", "\\\\").replace('"', '\\"')

    # -- ftplib-like

    def connect(self):
        out = self._sftp(["pwd"])
        m = re.search(r"Remote working directory:\s*(.+)", out)
        self.home = m.group(1).strip() if m else "/"
        self.welcome = "SFTP to %s (home folder %s)" % (self.target, self.home)
        return self.welcome

    def getwelcome(self):
        return self.welcome

    def set_pasv(self, *_):
        pass

    @staticmethod
    def qglob(path):
        # Inside double quotes sftp already treats * ? [ as plain characters.
        return SFTPClient.q(path)

    def _ls(self, d):
        out = self._sftp(["ls -la %s" % self.qglob(d)])
        rows = []
        for line in out.splitlines():
            m = _LS.match(line.strip())
            if not m:
                continue
            t, size, when, name = m.groups()
            name = name.rsplit("/", 1)[-1]   # sftp shows the whole path
            rows.append((t, int(size), when, name))
        return rows

    def mlsd(self, path="", facts=()):
        rows = self._ls(path or ".")
        for t, size, when, name in rows:
            if name in (".", ".."):
                continue
            kind = {"d": "dir", "-": "file"}.get(t, "link" if t == "l" else "other")
            yield name, {"type": kind, "size": str(size), "modify": when}

    def retrlines(self, cmd, callback=print):
        d = cmd[5:].strip() if cmd.upper().startswith("LIST") else "."
        out = self._sftp(["ls -la %s" % self.qglob(d or ".")])
        for line in out.splitlines():
            if _LS.match(line.strip()):
                callback(line.strip())
        return "226 Done"

    def retrbinary(self, cmd, callback, blocksize=65536, rest=None):
        remote = cmd[5:] if cmd.upper().startswith("RETR ") else cmd
        fd, tmp = tempfile.mkstemp(prefix="pb-get-", dir=str(DATA))
        os.close(fd)
        try:
            self._sftp(["get %s %s" % (self.q(remote), self.q(tmp))], timeout=24 * 3600)
            with open(tmp, "rb") as f:
                while True:
                    chunk = f.read(blocksize)
                    if not chunk:
                        break
                    callback(chunk)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return "226 Done"

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        remote = cmd[5:] if cmd.upper().startswith("STOR ") else cmd
        fd, tmp = tempfile.mkstemp(prefix="pb-put-", dir=str(DATA))
        try:
            with os.fdopen(fd, "wb") as f:
                while True:
                    chunk = fp.read(blocksize)
                    if not chunk:
                        break
                    f.write(chunk)
                    if callback:
                        callback(chunk)
            os.chmod(tmp, 0o644)   # a new file on the device gets normal permissions, not the temp file's
            self._sftp(["put %s %s" % (self.q(tmp), self.q(remote))], timeout=24 * 3600)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        return "226 Done"

    def size(self, path):
        rows = [r for r in self._ls(path) if r[0] == "-"]
        if len(rows) != 1:
            raise perm("Not a file: %s" % path)
        return rows[0][1]

    def mkd(self, path):
        self._sftp(["mkdir %s" % self.q(path)])
        return path

    def rmd(self, path):
        self._sftp(["rmdir %s" % self.q(path)])
        return "250 Done"

    def delete(self, path):
        self._sftp(["rm %s" % self.q(path)])
        return "250 Done"

    def rename(self, old, new):
        self._sftp(["rename %s %s" % (self.q(old), self.q(new))])
        return "250 Done"

    def voidcmd(self, cmd):
        return "200 OK"

    def sendcmd(self, cmd):
        c = cmd.strip().upper()
        if c == "SYST":
            code, out = self.run_command("uname -a", timeout=15)
            if code == 0 and out:
                return "215 " + out.splitlines()[0]
            return "215 SFTP"
        raise ftplib.error_perm("502 Not available over SFTP")

    def quit(self):
        self.close()

    def close(self):
        if self._askpass:
            try:
                os.unlink(self._askpass)
            except OSError:
                pass
            self._askpass = None

    def disconnect(self):
        """Close the kept-open SSH connection too."""
        try:
            subprocess.run(["ssh", "-O", "exit", "-o", "ControlPath=%s" % self._control_path(), "-p", str(self.port),
                            self.target], capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            pass


# ---------------------------------------------------------------- a folder on this Mac (and SMB shares mounted into one)

class FolderClient:
    """ftplib.FTP-like access to a folder on this computer. Device paths like /Music/a.mp3 are inside that folder."""

    def __init__(self, root, label=None):
        self.root = Path(root)
        self.welcome = "Folder %s" % (label or root)

    def connect(self):
        if not self.root.is_dir():
            raise ConnectionRefusedError("The folder %s isn't there (is the share connected?)." % self.root)
        return self.welcome

    def getwelcome(self):
        return self.welcome

    def set_pasv(self, *_):
        pass

    def _p(self, path):
        rel = PurePosixPath("/" + str(path or "").lstrip("/"))
        parts = [p for p in rel.parts[1:] if p not in ("", ".")]
        if ".." in parts:
            raise perm("Paths can't go above the shared folder.")
        return self.root.joinpath(*parts) if parts else self.root

    def _wrap(self, fn, *a):
        try:
            return fn(*a)
        except FileNotFoundError as e:
            raise perm("No such file or folder: %s" % (e.filename or ""))
        except (PermissionError, IsADirectoryError, NotADirectoryError, FileExistsError) as e:
            raise perm(str(e))
        except OSError as e:
            if e.errno in (5, 6, 50, 57, 60, 64, 65):   # I/O error / network gone (the share dropped)
                raise
            raise perm(str(e))

    def mlsd(self, path="", facts=()):
        d = self._p(path)
        entries = self._wrap(lambda: list(os.scandir(d)))
        for e in entries:
            try:
                if e.is_symlink():
                    kind = "link"
                elif e.is_dir():
                    kind = "dir"
                elif e.is_file():
                    kind = "file"
                else:
                    kind = "other"
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            yield e.name, {"type": kind, "size": str(st.st_size),
                           "modify": time.strftime("%Y%m%d%H%M%S", time.gmtime(st.st_mtime))}

    def retrlines(self, cmd, callback=print):
        d = cmd[5:].strip() if cmd.upper().startswith("LIST") else ""
        for name, f in self.mlsd(d):
            t = "d" if f["type"] == "dir" else "-"
            callback("%srw-r--r-- 1 user user %s Jan 1 00:00 %s" % (t, f["size"], name))
        return "226 Done"

    def retrbinary(self, cmd, callback, blocksize=65536, rest=None):
        remote = cmd[5:] if cmd.upper().startswith("RETR ") else cmd
        f = self._wrap(open, self._p(remote), "rb")
        with f:
            while True:
                chunk = f.read(blocksize)
                if not chunk:
                    break
                callback(chunk)
        return "226 Done"

    def storbinary(self, cmd, fp, blocksize=65536, callback=None, rest=None):
        remote = cmd[5:] if cmd.upper().startswith("STOR ") else cmd
        f = self._wrap(open, self._p(remote), "wb")
        with f:
            while True:
                chunk = fp.read(blocksize)
                if not chunk:
                    break
                f.write(chunk)
                if callback:
                    callback(chunk)
        return "226 Done"

    def size(self, path):
        p = self._p(path)
        if not p.is_file():
            raise perm("Not a file: %s" % path)
        return p.stat().st_size

    def mkd(self, path):
        self._wrap(os.mkdir, self._p(path))
        return path

    def rmd(self, path):
        self._wrap(os.rmdir, self._p(path))
        return "250 Done"

    def delete(self, path):
        p = self._p(path)
        if p.is_dir():
            raise perm("That's a folder: %s" % path)
        self._wrap(os.remove, p)
        return "250 Done"

    def rename(self, old, new):
        self._wrap(os.rename, self._p(old), self._p(new))
        return "250 Done"

    def voidcmd(self, cmd):
        return "200 OK"

    def sendcmd(self, cmd):
        if cmd.strip().upper() == "SYST":
            return "215 " + self.welcome
        raise ftplib.error_perm("502 Not available for a folder")

    def quit(self):
        pass

    def close(self):
        pass


def smb_mount(cfg, port=445, timeout=20):
    """Connect an SMB share into data-cache/mounts/<device> (macOS) and return that folder."""
    share = (cfg.get("share") or "").strip().strip("/")
    if not share:
        raise ftplib.error_perm("530 Which shared folder? Add its name (the share) in the device settings.")
    point = MOUNTS / re.sub(r"[^A-Za-z0-9_-]", "_", cfg["device_name"])
    if os.path.ismount(str(point)):
        return point
    if sys.platform != "darwin":
        raise SSHError("Connecting SMB shares by itself only works on a Mac. Connect the share yourself and add it "
                       "as a folder instead.")
    point.mkdir(parents=True, exist_ok=True)
    user = (cfg.get("username") or "").strip()
    pw = cfg.get("password") or ""
    if user in ("", "anonymous", "guest"):
        auth = "guest:@"
    else:
        auth = "%s:%s@" % (urllib.parse.quote(user, safe=""), urllib.parse.quote("" if pw == "anonymous@" else pw, safe=""))
    url = "//%s%s%s/%s" % (auth, cfg["host"], "" if int(port) == 445 else ":%d" % int(port),
                           urllib.parse.quote(share, safe=""))
    try:
        r = subprocess.run(["/sbin/mount_smbfs", "-N", url, str(point)], capture_output=True, text=True,
                           timeout=max(20, timeout * 2), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise SSHError("The file sharing connection took too long.")
    if r.returncode:
        err = (r.stderr or r.stdout or "").strip()
        low = err.lower()
        if "authentication" in low or "permission denied" in low or "-1073741715" in err:
            raise ftplib.error_perm("530 The file sharing login was refused. Check the username and password.")
        if "no route" in low or "timed out" in low or "connection refused" in low or "server connection failed" in low:
            raise ConnectionRefusedError("The device didn't accept a file sharing connection: %s" % err)
        if "share" in low and ("not found" in low or "does not exist" in low):
            raise ftplib.error_perm("550 There's no shared folder called %s on it." % share)
        raise SSHError("Couldn't connect the shared folder: %s" % (err.splitlines() or ["unknown error"])[-1])
    return point


def smb_unmount(cfg):
    point = MOUNTS / re.sub(r"[^A-Za-z0-9_-]", "_", cfg["device_name"])
    if os.path.ismount(str(point)):
        subprocess.run(["/sbin/umount", str(point)], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)


def open_client(cfg, port):
    """An ftplib.FTP-like, logged-in client for this device's protocol (not FTP itself)."""
    proto = cfg.get("protocol", "ftp")
    if proto == "sftp":
        c = SFTPClient(cfg, port, cfg.get("timeout_seconds", 20))
        c.connect()
        return c
    if proto == "smb":
        c = FolderClient(smb_mount(cfg, port, cfg.get("timeout_seconds", 20)),
                         "SMB share %s on %s" % (cfg.get("share"), cfg["host"]))
        c.connect()
        return c
    if proto == "folder":
        c = FolderClient(os.path.expanduser(cfg.get("folder") or ""), cfg.get("folder"))
        c.connect()
        return c
    raise ValueError("Unknown protocol %s" % proto)
