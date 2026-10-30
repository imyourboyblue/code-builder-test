#!/usr/bin/env python3
"""
Patchbay: your devices, an AI chat, files, backups and builds, in your web browser.

    python3 patchbay.py           only this Mac can open it
    python3 patchbay.py --lan     your laptop can open it too (needs a password)

(This just starts gd1chat.py, so the old command still works too.)
"""
import os
import sys
import time

STARTED = time.time()


def _recover(error):
    """Patchbay couldn't start: if a change Patchbay made to itself caused it, put the old version back (once)."""
    if os.environ.get("PATCHBAY_RECOVERED") or time.time() - STARTED > 90:
        return False
    os.environ["PATCHBAY_RECOVERED"] = "1"
    try:
        import selfedit
        import traceback
        where = {f.filename for f in traceback.extract_tb(error.__traceback__)}
        if getattr(error, "filename", None):
            where.add(error.filename)   # a SyntaxError names the broken file
        msg = selfedit.recover("%s: %s" % (type(error).__name__, error), where)
    except Exception:
        msg = None
    if not msg:
        return False
    print(msg, file=sys.stderr)
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)   # start again with the version from before


try:
    import gd1chat
except Exception as e:
    if not _recover(e):
        raise

if __name__ == "__main__":
    try:
        gd1chat.main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        if not _recover(e):
            raise
