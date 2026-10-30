"""
Makes secure web connections (https) work with the python.org version of Python on a Mac.

That Python comes without the list of trusted certificates, so every https request fails with
"CERTIFICATE_VERIFY_FAILED". Its own fix is to run "Install Certificates.command" once. To not depend on that,
Patchbay uses, in order: Python's own list if it has one, the certifi package if it's installed, or the
certificates the Mac itself trusts (read with the built-in `security` tool and saved to a file next to this one).
Certificates are always checked; this never turns checking off.
"""

import os
import ssl
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAC_BUNDLE = HERE / "data-cache" / "mac-certificates.pem"
MAC_KEYCHAINS = ["/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"]
HELP = ("This Mac's Python can't check secure (https) connections. Fix it once: open Finder > Applications > the "
        "Python 3 folder, and double-click \"Install Certificates.command\". Then restart Patchbay. If it still "
        "happens, a security app or network filter may be inspecting secure connections.")
_source = None


def _count(ctx):
    try:
        return ctx.cert_store_stats().get("x509_ca", 0)
    except (AttributeError, ssl.SSLError):
        return 0


def _mac_bundle():
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["/usr/bin/security", "find-certificate", "-a", "-p"] + MAC_KEYCHAINS,
                             capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "BEGIN CERTIFICATE" not in out:
        return None
    MAC_BUNDLE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAC_BUNDLE.with_suffix(".tmp")
    tmp.write_text(out)
    tmp.replace(MAC_BUNDLE)
    return str(MAC_BUNDLE)


def context():
    """A verifying SSL context that has trusted certificates loaded."""
    global _source
    ctx = ssl.create_default_context()
    if _count(ctx):
        _source = _source or "python"
        return ctx
    try:
        import certifi   # only if it happens to be installed
        ctx = ssl.create_default_context(cafile=certifi.where())
        if _count(ctx):
            _source = "certifi"
            return ctx
    except (ImportError, OSError, ssl.SSLError):
        pass
    bundle = str(MAC_BUNDLE) if MAC_BUNDLE.exists() else _mac_bundle()
    if bundle:
        try:
            ctx = ssl.create_default_context(cafile=bundle)
            _source = "mac"
            return ctx
        except (OSError, ssl.SSLError):
            pass
    _source = "none"
    return ssl.create_default_context()


def install():
    """Make every https request in Patchbay use context(), but only when Python's own list is missing."""
    global _source
    if _count(ssl.create_default_context()):
        _source = "python"
        return "python"
    ctx = context()
    ssl._create_default_https_context = lambda *a, **k: ctx   # still verifies certificates and host names
    return _source


def explain(error):
    """A clearer message for certificate errors."""
    text = str(error)
    if "CERTIFICATE_VERIFY_FAILED" in text:
        return HELP
    return text


if os.environ.get("PATCHBAY_NO_TLSFIX") != "1":
    install()
