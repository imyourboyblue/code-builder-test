# code-builder-test

A local AI agent for checking and fixing code and files on devices on your home network.

Everything lives in [`gd1-checker/`](gd1-checker/). Start with its [README](gd1-checker/README.md), which has step-by-step setup for the Mac Studio and the laptop.

What it does:
- **Devices:** a home screen with a picture for each saved device that's on the network right now.
- **Chat:** a local AI (Ollama, on your Mac) that checks a device for crash damage, reads and explains files, and suggests fixes. Nothing is written to a device until you type its name to confirm.
- **Files:** drag and drop between the Mac and the device over FTP, with backups of anything replaced or deleted.
- **Backups:** full backups of a device to a folder you choose. A stopped backup carries on where it left off.
- **.nro programs:** damage checks, fixes from good copies, and updates by editing the source code and rebuilding.
- **Laptop access:** use it from your laptop on the same network while the Mac Studio does the work (password protected).

It only uses Python's built-in parts, so there is nothing to install besides Python and Ollama.

Your `config.json`, device settings, password file and backups are never committed (see `.gitignore`).
