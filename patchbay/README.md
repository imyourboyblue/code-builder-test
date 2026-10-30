# PatchBay

PatchBay is an AI helper for the devices on your home network: consoles like the Switch and PS5, Linux boxes, NAS
boxes, TVs and your own homemade devices. It runs on your Mac. You use it in its own Mac app window, or in a browser on
the Mac or on your laptop.

What it does:
- **Finds crash damage** in a device's files: files cut short, broken headers, zero-byte padding, empty files, broken
  JSON or XML, damaged databases, leftover temp files, and files that suddenly shrank.
- **Fixes things for you**, with your OK: puts back good files, edits settings, rebuilds homebrew apps from source
  for your firmware, and installs them.
- **Looks after your devices**: backups, a health list, security checks, a homebrew app library, and a file manager.

The AI runs on your Mac (with Ollama), so your files stay home. You can also let it ask Claude (Anthropic's cloud AI)
for help when it's stuck, using your own API key.

## Contents

- [Setting up](#setting-up)
- [Starting PatchBay](#starting-patchbay)
- [The home screen](#the-home-screen)
- [A device's page](#a-devices-page)
- [Chat](#chat)
- [Scans and Needs attention](#scans-and-needs-attention)
- [Files](#files)
- [Backups](#backups)
- [Apps and homebrew updates](#apps-and-homebrew-updates)
- [Building apps from source](#building-apps-from-source)
- [AI models and Claude](#ai-models-and-claude)
- [Setup, updates and looks](#setup-updates-and-looks)
- [Clear data](#clear-data)
- [Using it from your laptop](#using-it-from-your-laptop)
- [Safety](#safety)
- [Where things are saved](#where-things-are-saved)
- [Settings](#settings)

## Setting up

You do this once.

1. Open **Terminal** (press Cmd+Space, type Terminal, press Enter).
2. Type `python3 --version` and press Enter. If your Mac offers to install developer tools, click **Install**.
3. Get PatchBay. It lives in the `patchbay` folder of the download:
   ```
   cd ~/Documents/Patchbay/patchbay
   ```
4. Make your settings file:
   ```
   cp config.example.json config.json
   ```
   You don't need to edit it. You add devices from the home screen.
5. Install [Ollama](https://ollama.com) (the AI that runs on your Mac), open it, then get the two models PatchBay
   uses:
   ```
   ollama pull qwen3.6:35b-a3b
   ollama pull qwen3.8:27b
   ```

Nothing else needs installing. PatchBay only uses what comes with Python. Build tools for homebrew (devkitPro) are
installed by PatchBay itself when a job needs them.

## Starting PatchBay

The first time, start it from Terminal:
```
cd ~/Documents/Patchbay/patchbay
python3 patchbay.py
```

Then make it a real Mac app: click **🩺 Setup** at the top of the page and press **Make the PatchBay app**. It goes
into your Applications folder, with the pirate skull icon. Drag it onto the Dock. From then on, click it to start
PatchBay in its own window. Quitting the app stops PatchBay. Building the app takes about a minute and needs Apple's
free command line tools. If they're missing, Setup shows an **Install Apple's tools** button.

In Setup you can also tick **Start PatchBay by itself when I log in**.

## The home screen

- **Device tiles**: every device you added, with its picture and firmware. Ones that answer right now are at the top
  with a green dot. Click a tile to open the device. Hover and click **Picture** to give it a photo.
- **Add a device**: give it a short name and its address. PatchBay checks it can connect, then remembers it. It
  connects with FTP, SFTP/SSH, SMB file sharing, or a folder on the Mac, whichever the device offers.
- **🔎 Find devices on my network**: lists what's on your network (TVs, consoles, printers, computers), what each
  probably is, and how to connect to it. **Add as a device** fills in the details.
- **🛡 Security check** (on a found device): checks open doors, known security problems for its software version, and
  risky settings, then gives a plain report with fixes. It only looks. It never guesses passwords or tries to break in,
  and only works on your own network.
- **Health**: devices without a recent backup, files that need attention, security problems, and writing left on. It
  checks when the home screen opens or when you press **Check health**, never on a timer.
- **Home chat**: ask about any device, or ask for a change to PatchBay itself. Drag its edge to make it wider or
  narrower.

## A device's page

The tabs across the top:
- **Chat**: talk to the AI about this device.
- **Files**: the Mac's files and the device's files side by side.
- **Backups**: full and partial backups.
- **Apps**: the homebrew apps on the device, with updates.
- **Needs attention**: files a scan found problems in. A red number shows how many.

Also at the top: **🗑 Clear data**, **🕘 Activity** (everything PatchBay did, with **Undo**) and **🩺 Setup**.

## Chat

Ask in your own words, for example:
- "Check it for crash damage"
- "What's in the /data folder?"
- "Update hb app store"
- "Change the flush setting in app.conf to always"
- "Undo the last change"

How it works:
- **It reads freely, and changes only with your OK.** Before anything on a device changes, you see a card with the
  exact change, side by side before and after. You type the device's name to confirm, and the old file is backed up
  first.
- **One plan, one button.** For bigger jobs (installs, downloads, builds, copying to the device), it first works
  everything out by reading. Then it shows one plan card with the numbered steps, the device files it will change, any
  commands it will run, and what could go wrong. Press Confirm once and it does every step. It never shows a second
  card while one is waiting.
- **No guessing.** It looks things up before it acts: the program's GitHub page, README, build files and releases.
  It checks that a download address or package exists before offering it, and says where each fact came from. If it
  can't confirm something, it asks you.
- **Stop** works any time, also in the middle of a scan.
- **It shows its progress**: a bar with the current step, how many steps so far, and the time.
- **Past chats** are saved. **New chat** starts fresh. A new big job starts a new chat by itself.
- **Drop files in** (or click 📎): logs, text files, .nro files and pictures.
- **⭐ Saved prompts**: keep prompts you use often and run them with one click. Write `{device}` where the device's
  name should go. Long pasted messages fold up with **Show all**.

If a step keeps failing, it stops after 2 tries (or 8 steps without progress), shows what it tried, and lets you
choose **Ask Claude**, **Try another way** or **Stop here**.

## Scans and Needs attention

A scan only reads the device. It downloads each file, checks it, and deletes it from the Mac again. **No copies are
kept** of files that are fine. PatchBay keeps only a tiny fingerprint of each one (size and checksum), so the next
scan can tell what changed.

While it scans, each line shows the time left, like "about 8 to 12 min left". PatchBay works this out from how
long each file really takes on that device (a small cost per file, plus its size divided by the speed). The range
gets narrower as it goes and follows the device if it slows down. The next scan starts from the last scan's numbers.

Files with problems are kept on the Mac and listed on the device's **Needs attention** tab, with what's wrong. You
decide what happens to each:
- **Keep as is**: it's fine for you. That exact version won't be flagged again. If it changes later, it's checked
  again.
- **Repair**: the chat finds a good version first (one of your backups, or the program's own release), then shows
  one plan to put it back. If it can't find one, it tells you.
- **Delete from device**: the usual card where you type the device's name.

Since scans don't keep spare copies, **make a backup** (Backups tab) when you want something to restore from. Scan
copies made before this change can be removed in 🩺 Setup with **Delete old scan copies**. That keeps a good copy of
any file that's on Needs attention.

## Files

The Files tab shows the Mac on the left and the device on the right (or just one side, with the buttons at the top).
- Drag files between the sides, or drop files from Finder onto the device side. Copies show in a queue with
  progress, **Pause**, **Resume** and **Cancel**.
- Double-click a file to **preview** it: text, pictures, a hex view, or an .nro's details and damage checks. Text
  files can be **edited**. You see before and after side by side before you save.
- Right-click for **Rename**, **Download**, **Delete**, **Back up**, **Compare** and **Ask AI about this**.
- **🔍 Search** finds files by name on the device. **☆** bookmarks a folder.
- Anything that changes the device shows a list of exactly what will happen, and needs the device's name typed.
  Deleting a folder or 20 or more files also needs an **I understand** tick.

## Backups

1. On the Backups tab, click **Change…** and pick where backups go: a folder, an external drive or a network share.
2. Click **Back up now**. You see the progress and time left. It keeps going if you close the page.
3. **Stop** any time. **Resume** carries on where it left off.

Each backup is a dated folder with the device's files laid out as on the device. Unchanged files aren't copied
again, so each new backup only takes the space of what changed. You can also back up just some files (select them
in Files and click **Back up**), give backups labels, **Check** that a backup isn't damaged, keep only the newest N,
zip them, and save a second copy somewhere else. To put files back, open a backup, tick what you want and press
**Put back**.

When you approve a plan that changes files on the device, PatchBay backs up those folders first ("Before: ..." in
the list).

## Apps and homebrew updates

Open a Switch (or a PS4 or PS5 with a homebrew FTP server), go to the **Apps** tab and click **Read the apps**. You
get every app with its icon, name and version, damaged ones flagged, and the background programs (sysmodules).
**Check for updates** looks up each app's newest release on GitHub (only the project name is sent). **Update**
starts the guided update in the chat.

To update an app so it works with your firmware, just ask, for example "update NX-Shell". PatchBay finds the app,
reads your firmware and Atmosphère versions, and looks up the newest releases. Then it shows one plan: download the
source, install any build libraries it needs, build, and install the new file on the console (backing up the old
one). It knows where well-known homebrew lives (HB App Store, NX-Shell, ftpd, Goldleaf, Atmosphère, hekate and
more), and searches GitHub for anything else.

**What firmware is it on?** Each device's tile shows its firmware once known. For the Switch, PatchBay can put a tiny
helper app on the console, **PatchBay system info**. You open it once from the Homebrew Menu, and it reports the
exact versions.

## Building apps from source

PatchBay works on an app's source code on your Mac, in `~/Patchbay-projects/`, and never sends your code anywhere.
- Before building a console app, it reads the project's build files and installs **every devkitPro library the
  project needs in one go**. It uses devkitPro's real package names, for example `switch-freetype`.
- Builds find devkitPro's and Homebrew's tools by themselves, including the right library paths for console builds.
  You never need to type commands or set anything up in Terminal.
- Installs bring up your Mac's own password box. PatchBay never sees that password.
- Code changes show as cards with the exact difference, and the old file is backed up first.

The same works for other systems (3DS, Wii U, Vita, PSP, PS4, Linux handhelds and your own devices). For anything
new, the AI researches it and saves what it learned, so next time it already knows.

## AI models and Claude

**The AI model menu** (above the chat) shows what each model is for, not its version:
- **Automatic** (recommended): uses the fast model (Everyday help) for questions and lookups, and switches to the
  stronger one (Hard problems and building) for builds, fixes, updates, plans, or after a step fails. The progress
  bar shows which one it picked and why.
- **Second opinion** (Gemma 4 from Google, about 18 GB): when the usual models fail the same step twice, Automatic
  asks this one, since a model from a different maker often sees the problem differently. Download it with one
  button in 🩺 Setup, or in Terminal with `ollama pull gemma4:26b`.
- Or pick a model yourself, for example **Everyday help** or **Hard problems and building**. A short line under the
  menu says what it's good for.

The words are set in `model_labels.py`, if you want to change them.

**☁ Claude** (the button above the chat) lets PatchBay ask Claude when its own AI is stuck:
- The button shows a badge: green **connected**, red **key rejected**, or grey **not connected**. Checking the key
  is free.
- Paste your API key (make one at https://platform.claude.com/settings/keys). It's kept in the Mac's Keychain.
- Pick when to use it: **Off**, **When stuck**, **Plan + when stuck**, or **Always** (costs the most). Pick the
  model: **Hardest problems** (Claude Opus) or **Everyday help, cheaper** (Claude Sonnet).
- Set a monthly spending limit.

PatchBay only asks Claude after its own AI has done at least 3 lookups online and still can't work it out. It sends
the list of what it already tried. Before anything is sent, your user name, home folder, IP addresses, device names,
emails and passwords are swapped for stand-ins like `<device-1>`. Unless you tick **Send automatically**, a card
shows exactly what will be sent and you press **Send**. **See what was sent** lists everything that left the Mac.

## Setup, updates and looks

**🩺 Setup** checks that everything works:
- Ollama (the AI) and which models Automatic uses
- secure connections and the internet
- the build tools and your devices
- free space and saved passwords
- old scan copies
- Claude

Anything not right gets a **Fix** button. **Copy error report** copies versions and recent errors, never passwords
or keys, so you can paste it to get help.

**Updates**: Setup says when a newer PatchBay is out. Click **Update and restart**. Your settings, devices, backups
and chats are kept. If you changed a PatchBay file yourself, Setup shows the change and offers **Save my changes and
update**, which keeps a copy of your change first.

**Look**: pick a theme (**Match my Mac**, **Pirate**, **Mac Tahoe** or **Dark neon glass**) and the spacing
(**Comfortable** or **Compact**). The Mac app and your laptop's browser use the same choice.

**Changing PatchBay itself**: ask the home chat ("add a Back up all button"). You see the exact code change on a
card first. It's checked for mistakes, and the old file is backed up, with Undo in 🕘 Activity. If PatchBay can't
start after a change, it puts the old version back by itself. If the page itself breaks, open
http://127.0.0.1:8765/fix for a plain repair page.

## Clear data

**🗑 Clear data** (at the top of a device's page) deletes what PatchBay keeps on the Mac for that device, to start
over or to get rid of a device. Nothing on the device itself is touched. You see each item with its size and tick
what goes:
- backups
- scan results (fingerprints and the Needs attention files)
- change history and Undo
- chats
- device info and picture

Two choices always start unticked, so you decide each time:
- **Saved password**: removes it from the Keychain.
- **Remove the device from PatchBay**: takes it off the home screen.

Type the device's name to confirm. To do this for every device at once, use 🩺 Setup → **Clear everything…** and
type "Clear everything".

## Using it from your laptop

There are two ways. Pick the one that fits.

**A. The laptop opens PatchBay on your Mac (nothing to install on the laptop).** The Mac does all the work.

1. On the Mac, open **🩺 Setup → Laptop access**, choose a laptop password and press **Turn on**. PatchBay
   restarts and shows the address for your laptop, with a Copy button. (From Terminal instead:
   `python3 patchbay.py --set-password`, then `python3 patchbay.py --lan`. If the Dock app is already running, use
   Setup instead, because only one copy runs at a time.)
2. If the Mac asks whether Python may accept incoming connections, click **Allow**. Keep the Mac awake
   (System Settings > Energy > Prevent automatic sleeping when the display is off).
3. On the laptop, on the same network, open that address and sign in. You stay signed in for 30 days.

After 5 wrong passwords, that computer has to wait before trying again. Only use this on your home network.

**B. PatchBay runs on the laptop, and the AI runs on your desktop.** The laptop keeps its own devices, files and
builds; only the AI questions go to the desktop.

1. Install PatchBay on the laptop like on the Mac (see Setting up). The laptop doesn't need Ollama or any models.
2. On the desktop, open Ollama's Settings and turn on **Expose Ollama to the network**, then quit and reopen
   Ollama. Click **Allow** if it asks about incoming connections. Keep the desktop awake.
3. On the laptop, open **🩺 Setup → Where the AI runs**, type the desktop's address (like `192.168.50.10` or
   `Mac-Studio.local`) and press **Test and use**. The model list then shows the desktop's models, and a small
   label next to **AI model** says where the AI is running. **Use this computer** switches back.

Ollama has no password, so anyone on your Wi-Fi could use it. That's fine at home, but never forward port 11434 on
your router. PatchBay refuses internet addresses for the AI, so your files never leave your home network.

## Safety

- **Writing to a device is off** until you turn it on. Every change needs the device's name typed, is backed up
  first, and is uploaded under a temporary name and only swapped in once it's complete.
- **Risky commands** (like deleting folders, formatting, force-stopping programs, `sudo`) are never run by
  themselves. They're marked **⚠ Risky** in plain words and need an **I understand** tick.
- **Passwords and the Claude key** are kept in the Mac's Keychain (Keychain Access, search "PatchBay"). The first
  time, macOS may ask to allow it: choose **Always Allow**.
- **The AI is local.** PatchBay only talks to Ollama on your own Mac. Web lookups send only general things like a
  program's name, a version or an error message. Your files and code stay on the Mac.
- **Your network only.** Network finding and security checks only look at your own network, only when you ask, and
  never guess passwords or try to break in.
- **Run by hand.** Nothing runs on a schedule.
- **🕘 Activity** lists everything PatchBay did, with **Undo** where possible.

## Where things are saved

- **Device data**: `~/PatchBay-data/<device name>/` (set with `data_folder`). This holds the scan fingerprints
  and reports, the Needs attention files, change history, chats, and the device's picture.
- **Backups**: wherever you chose on the Backups tab (default `~/Patchbay-backups/<device name>/`).
- **Projects** (source code): `~/Patchbay-projects/`.
- **Device settings**: `config.json` for your first device, and one file per device in the `devices` folder.
- **PatchBay's own data** (themes, saved prompts, caches): `data-cache/` inside the PatchBay folder.

None of your settings, passwords, backups or data are uploaded to GitHub.

## Settings

You rarely need to edit these by hand. Ask the chat instead ("only scan /data and /etc", "turn on writing"). It shows
the old and new values on a card first.

| Setting | What it means |
| --- | --- |
| `host`, `ports` | The device's address, and the ports to try |
| `username` / `password` | Leave as anonymous for an FTP server with no login |
| `remote_folders` | Which folders a scan checks, e.g. `["/data", "/etc"]` |
| `include` / `exclude` | File patterns to check or skip, e.g. `["*.db", "*.json"]` |
| `max_file_mb` | Skip files bigger than this when scanning |
| `allow_writes` | Must be on for any change to the device |
| `ollama_model` | `auto` for Automatic, or one model's name |
| `about` | One sentence about the device, so the AI knows what it is |
| `backup_folder` | Where backups go (set on the Backups tab) |
| `lan_access` | `true` always lets the laptop in |

The AI model and data folder are shared by all devices and come from `config.json`.
