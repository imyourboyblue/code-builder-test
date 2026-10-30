#!/usr/bin/env python3
"""PatchBay moved to the "patchbay" folder next to this one. This file only starts it from there, so old
shortcuts keep working. Your settings and data move over by themselves the first time it starts."""
import os
import sys

new = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "patchbay", "patchbay.py")
os.chdir(os.path.dirname(new))
os.execv(sys.executable, [sys.executable, new] + sys.argv[1:])
