# PatchBay system info (helper app)

A tiny Switch homebrew app. When you open it from the Homebrew Menu, it writes the
console's exact firmware and Atmosphère versions to
`/switch/patchbay-sysinfo/sysinfo.txt`, then closes. PatchBay reads that file over FTP.
It only reads system information and changes nothing else.

PatchBay builds and installs it for you (ask the chat "find my firmware").
To build it by hand: `make` (needs devkitPro with switch-dev).
