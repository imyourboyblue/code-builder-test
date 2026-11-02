"""
The private builder: turn an idea into code with Claude, without Claude seeing who you are or what it's for.

  1. compiler.py  The AI on this Mac (Ollama) rewrites the idea as a plain, generic software spec.
  2. sanitizer.py Checks the spec for anything private (your block list, PatchBay's own device names, IPs,
                  paths, emails, keys...). Any hit stops everything.
  3. coder.py     Sends only that spec to Claude, as one fresh request (no chat, no memory, no paths).
                  Claude answers with the files.
  pipeline.py     The steps above, shared by PatchBay's chat and the command line (run.py).

Python's standard library only. Nothing is sent anywhere until the spec has passed the checks and you said yes.
"""
