#!/usr/bin/env python3
"""
PatchBay: your devices, an AI chat, files, backups and builds, in your web browser.

    python3 patchbay.py           only this Mac can open it
    python3 patchbay.py --lan     your laptop can open it too (needs a password)

    python3 patchbay.py scan      the command line tools (scan, backup, restore, history, undo ...)
"""
import os
import sys
import time

STARTED = time.time()


def _recover(error):
    """PatchBay couldn't start: if a change PatchBay made to itself caused it, put the old version back (once)."""
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
    import migrate
    migrate.run()        # after the rename: move settings and data from the old names (does nothing later)
except Exception as e:
    print("Couldn't check the old folders: %s" % e, file=sys.stderr)

CHECKER_COMMANDS = {"test", "browse", "scan", "explain", "restore", "history", "undo", "backup", "menu"}
if __name__ == "__main__" and len(sys.argv) > 1 and (sys.argv[1] in CHECKER_COMMANDS or
                                                     sys.argv[1] in ("--device", "--config")):
    import core            # python3 patchbay.py scan, backup, restore ... (the command line tools)
    sys.exit(core.main(sys.argv[1:]))

try:
    import server
except Exception as e:
    if not _recover(e):
        raise

if __name__ == "__main__":
    try:
        server.main()
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as e:
        if not _recover(e):
            raise
