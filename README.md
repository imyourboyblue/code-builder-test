# PatchBay (code-builder-test)

PatchBay is an AI helper for the devices on your home network. It runs on your Mac with a local AI (Ollama), and
you use it in its own Mac app window or in a browser.

Everything lives in [`patchbay/`](patchbay/). Start with its [README](patchbay/README.md), which has the
step-by-step setup.

```
cd ~/Documents/Patchbay/patchbay
python3 patchbay.py
```

What it does: finds crash damage in device files, fixes things with your OK, rebuilds homebrew apps from source for
your firmware, and looks after your devices with backups, health checks, security checks and a file manager.

It only uses Python's built-in parts, so there's nothing to install besides Python and Ollama. Your settings,
passwords, backups and data are never committed (see `.gitignore`).

If you used PatchBay before it was renamed, start it once from Terminal with `cd patchbay && python3 patchbay.py`.
That moves your old settings and data over and points the Dock app at the new folder.
