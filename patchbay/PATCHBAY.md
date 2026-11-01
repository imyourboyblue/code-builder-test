# Rules for working on PatchBay

PatchBay reads this file before every question about changing its own code. Any project can have one:
put a PATCHBAY.md in the project's folder with its build command, style and what not to touch.

## Build and check
- There is no build step. Python files must compile (the change card checks this).
- After a change to a .py file, offer propose_patchbay_restart. Changes to web/app.html only need a page reload.

## Style
- Python standard library only (no pip installs). Must work on Python 3.9 and newer.
- Small find/replace edits that match the code around them. No big rewrites.
- Words the user sees are plain and short. Write "PatchBay" with a capital B.
- web/app.html is one file: HTML, CSS and JavaScript together. Keep it that way.

## Never change without the user clearly asking (and then say what it changes)
- Confirm cards, typed device names, backups and Undo.
- What may go to Claude or the web: never the user's files, code, calendar, names or addresses.
- The AI address must stay on this Mac or the home network.
- Network scans: own network only, no password guessing, no attacks.
- config.json, passwords and anything in data-cache.
