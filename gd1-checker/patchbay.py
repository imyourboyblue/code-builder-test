#!/usr/bin/env python3
"""
Patchbay: your devices, an AI chat, files, backups and builds, in your web browser.

    python3 patchbay.py           only this Mac can open it
    python3 patchbay.py --lan     your laptop can open it too (needs a password)

(This just starts gd1chat.py, so the old command still works too.)
"""
import gd1chat

if __name__ == "__main__":
    gd1chat.main()
