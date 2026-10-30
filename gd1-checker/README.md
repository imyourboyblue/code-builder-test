# Patchbay

Patchbay is a local AI agent for the devices on your home network. It runs on your Mac Studio and you use it in a web browser, there or on your laptop. Start it with `python3 patchbay.py` (the old `python3 gd1chat.py` still works).

It began as the GD1 checker, which finds files on GD1 that were damaged when an app froze partway through a save. It looks for:

- files cut short (truncated)
- broken headers (the start of the file is wrong)
- null padding (zero bytes where data should be)
- empty or all-zero files
- broken JSON or XML, and damaged SQLite databases
- leftover temp and journal files from an interrupted save
- files that shrank a lot since the last scan

Scanning only reads from GD1. Every scan copies GD1's files to a dated snapshot on your Mac. Then, if a file is damaged, you can put a good copy back with one command. Your Mac backs up the current file first, so you can always undo.

## One-time setup

1. Open **Terminal** (press Cmd+Space, type Terminal, press Enter).
2. Check that Python is there by typing `python3 --version` and pressing Enter. If your Mac offers to install developer tools, click Install.
3. Put the `gd1-checker` folder somewhere easy, like your Documents folder. Then go into it:
   ```
   cd ~/Documents/gd1-checker
   ```
4. Make your settings file:
   ```
   cp config.example.json config.json
   open -e config.json
   ```
5. In the file that opens, change `"host"` to GD1's IP address. If GD1's FTP server uses another port, add it to `"ports"`, for example `[21, 5000, 2121]`. The script tries each port in order. Save and close the file.
6. Optional: to have the AI explain the results, install [Ollama](https://ollama.com). Then run this once:
   ```
   ollama pull qwen3.6:35b-a3b
   ```

There is nothing else to install. The script only uses parts of Python that come built in.

## Easy start, and checking everything works

The first time, start Patchbay from Terminal (above). After that you have two shortcuts:

- **A Patchbay app you can double-click.** In Terminal run `python3 patchbay.py --install-app` once, or click **🩺 Setup** at the top of the page and press **Make the Patchbay app**. It appears in your Applications folder; drag it onto the Dock. Double-clicking it opens Patchbay (and just opens the page if it's already running). It's a real Mac app: Patchbay opens in its own window (with its own icon, menus, copy/paste and downloads), and quitting the app stops Patchbay if the app started it. Building it takes about a minute and needs Apple's free command line tools; without them you get a simpler app that opens Patchbay in your browser. After an update, the app rebuilds itself the next time Patchbay starts. The icon's source is `macapp/icon.svg.html`, the window code is `macapp/main.swift`.
- **Start on its own when the Mac turns on.** The same **🩺 Setup** panel has a tick-box for this (`--install-app` turns it on too). It runs Patchbay quietly in the background; open the page whenever you want it.

### Is everything working? (🩺 Setup)

Click **🩺 Setup** at the top of the page for a quick check of Ollama (the AI), secure connections, the build tools, the internet, and your devices. Anything not right shows a **Fix** button where Patchbay can put it right itself (start Ollama, load the Mac's certificates, install the Switch build tools). If something ever goes wrong, **Copy error report** copies the versions, these checks and the recent errors (never your passwords or keys) so you can paste it to get help.

## The Patchbay app

Start it on the Mac Studio, in Terminal, from inside the `gd1-checker` folder:

```
python3 patchbay.py
```

Your browser opens the **Devices** screen. Leave the Terminal window open while you use it. To stop, click on Terminal and press Ctrl+C.

### Find devices on your network

On the Devices screen, click **🔎 Find devices on my network** (or ask the chat "what's on my network?"). In about 20 seconds Patchbay lists the TVs, consoles, computers, printers and other devices on your own network: their address, any name they announce, the maker, what each probably is, and the normal ways to connect to it (FTP, SSH, file sharing, a TV's developer mode and so on).

The list stays on the Devices screen (also after a restart) until you press **Look again**. Names and models come from what devices say about themselves: Bonjour, UPnP, the router's names, Windows names, the PlayStation and Roku/Chromecast discovery, and the title of a device's own web page. The maker comes from the public list of hardware-address makers, downloaded once into data-cache. Phones and laptops that use a private Wi-Fi address don't reveal their maker.

- **Add as a device** appears for anything with an FTP server, with the address and port filled in.
- **How do I connect to it?** asks the chat, which explains the normal way in for that kind of device and, if it needs turning on first (like a TV's developer mode), looks up that model's steps and walks you through them.

It only looks at your own network, only when you ask, and only through the doors a device openly offers with your own login. It never tries passwords or tries to break in. On a newer Mac, the first time you'll be asked whether Python may find devices on your local network: click **Allow**.


### Security check for your own devices

In the "Found on your network" list, open a device and click **🛡 Security check**, tick that it's your device, and
start it (or ask the chat "is my router secure?"). Patchbay checks which doors are open, what software answers and
whether that version has known security problems (looked up in the public US vulnerability database, sending only the
product name and version), the secure-page (TLS) setup, and risky settings such as Telnet, Android debugging, open
databases, or FTP that lets anyone in. You get a plain-language report, worst first, with how to fix each item, and
**Help me fix this** walks you through it in the chat. It only looks: no break-in attempts and no password guessing
(the only login it tries is FTP's public "anonymous" one). It only checks private addresses on your own network.


### Updating

Open **🩺 Setup**. Under **Updates** it says whether a newer Patchbay is on GitHub and lists what changed; click
**Update and restart**. (The Setup button says "update ready" when there is one.) Your settings, devices, backups and
chats aren't touched. If you edited a Patchbay file by hand, it tells you which one and doesn't update.


### SFTP, SSH, file sharing (SMB) and folders

Besides FTP, **Add a device** can reach a device's files with:

- **SFTP / SSH**: Linux boxes, a Raspberry Pi, Macs, most NAS boxes. Uses your login on the device, or the SSH keys
  already on this Mac if you leave the password empty. The first time, Patchbay remembers the device's identity (in
  data-cache/ssh_known_hosts) and warns you if it ever changes. SSH devices can also run commands: the chat suggests
  one on a card, and it only runs after you type the device's name.
- **SMB file sharing**: Windows PCs, NAS boxes, shared folders. Give the shared folder's name (the share); Patchbay
  connects it like Finder's "Connect to Server".
- **A folder on this Mac**: any folder, for example a share you already connected in Finder (under /Volumes).

Files, scans, backups, edits with undo and the rest work the same whichever way a device is reached. In "Found on
your network", **Add as a device** picks the right way for you.


### Activity log with Undo

Click **🕘 Activity** at the top. It lists everything Patchbay did, newest first, for all devices or one: files
changed, put back, renamed or deleted on a device, source code edits, settings changes, commands run on SSH devices,
and backups. Changes that can be undone have an **Undo** button. The current version is backed up first and the
undo is written down too, so you can undo an undo. Undoing a change on a device needs you to type its name.
Commands run on a device can't be undone by Patchbay.


### Health

The Devices screen shows a **Health** list: devices that have never had a full backup or whose newest one is over two
weeks old, damaged files from the last scan, serious problems from the last security check, writing left on, and
(with **Check health**, for SSH devices) disks over 90% full. Each one has a button that opens the right place or asks
the chat. It only checks when the Devices screen opens or you press Check health, never on a timer.


### Apps (homebrew library)

Open a Switch (or a PS4/PS5 with a homebrew FTP server) and click the **Apps** tab, then **Read the apps**. Patchbay lists every app in /switch with its icon,
name, version and author (read from the file itself), flags damaged ones, and lists the background programs
(sysmodules). **Check for updates** looks up each app's newest release on GitHub (only the project name is sent);
**Update** hands it to the chat's guided update, which backs up the current file first. If Patchbay doesn't know an
app's GitHub project, click **set it**. The first read downloads each app once; after that only new or changed ones.

### Device pictures

- Every device you've added gets a round picture. The ones that answer on the network right now are at the top, with a green dot. Click one to open it.
- Devices that don't answer are shown faded below, with when they were last seen. The list checks again every 15 seconds, or press **Check again**.
- To add a device, click **Add a device** and type a short name (like `GD2`) and its IP address. The Mac logs in to its FTP server to check it works, then remembers it. Its settings are saved in the `devices` folder (for example `devices/GD2.json`). GD1 stays in `config.json`.
- To give a device a real picture, hover over its tile and click **Picture**, then pick any photo.

### Chat

Each device has a **Chat** tab. Things you can say:
- "Check it for crash damage"
- "What's in the /data folder?"
- "Show me settings.json and tell me if anything looks wrong"
- "Put back a good copy of game.db"
- "Change the flush setting in app.conf to always"
- "Undo the last change"

The AI can look at anything on the device without asking. To change a file, it shows you a card with the exact change, and nothing is written until you type the device's name (like `GD1`) and press **Confirm**.

- **Live replies and Stop:** the answer appears as the AI writes it. While it works, the Send button turns into **Stop**.
- **Steps in plain words:** each thing it did shows as one line, like "Looked in /switch: 3 folders and 12 files". Click a line to see the details.
- **Past chats:** every chat is saved on the Mac Studio by itself. **Past chats** at the top lists them, to reopen or delete. **New chat** starts a fresh one.
- **Add files:** drop files onto the chat, or click 📎. Logs and text files, .nro files and pictures all work. Pictures need a model that can see images (like qwen2.5vl).
- **Quick buttons** above the message box for common jobs.
- **AI model** picker at the top: switch between your fast model and your deeper one at any time.
- **Plan first:** for a big job, the AI shows a numbered plan. Approve it once (typing the device's name if it changes files on the device) and it does all the steps without stopping. The folders it will change on the device are backed up first.
- **Copy** buttons on every message and code block, and links you can click.

### .nro programs and source code

The AI understands .nro files (Switch homebrew programs):

- **Check:** "Check /switch/mygame/mygame.nro" shows its name, author, version and sizes, and looks for crash damage: a cut-off file, a wiped or broken header, missing sections, code that's all zero bytes, or a damaged icon or name block. Scans check every .nro too.
- **Fix damage:** "Put back a good copy of mygame.nro" finds the newest undamaged copy in your scans or full backups, and puts it back after you confirm.
- **Change its details:** "Rename mygame.nro to My Game, version 2.0" or "Use ~/Pictures/icon.jpg as its icon".

An .nro's program code is compiled, so it can't be edited directly. To change what a program does, for example to make it work with newer firmware, the AI works on the program's **source code** on the Mac Studio, then rebuilds it:

1. Tell it where the source is, for example: "The source for mygame is in ~/code/mygame. It builds with make, makes mygame.nro, and goes to /switch/mygame/mygame.nro on GD1." It saves that as a project.
2. Ask for the change: "Update mygame so it works with the newest firmware." It reads and searches the code, and can look up public release notes (like libnx's) on the web. Your code is never sent anywhere.
3. Each code change shows up as a card with the exact difference. Click **Save the change**. The old version of the file is backed up first (in `~/GD1-checker-data/GD1/project-backups/`).
4. It asks to build. Click **Run the build** to run the build command (like `make`) in the project folder. The AI reads the build output and fixes any errors the same way.
5. When the build works, it offers to install the new .nro on the device. As always, you type the device's name to confirm, and the device's current file is backed up first.

To build Switch homebrew, the Mac Studio needs devkitPro. You don't have to set it up by hand: the AI installs it and any packages a project needs (see below). Builds always find devkitPro's tools and Homebrew's tools (like cmake) by themselves, so you never need to `export` anything or edit shell files. Each build's last 80 lines of output show up in the chat (open, so it's easy to read or copy), with the first error line in the heading.

### Update a homebrew app for your firmware (like Chiaki)

In a device's chat, click **Update a homebrew app so it works with my firmware**, or just ask in your own words, for example "Update FTPD light so it works with my firmware". You don't need to know file names or versions: the AI finds them on the device. It then does the whole job, telling you in one line what it's doing:

1. It finds the app on the device from your words (apps in `/switch`, and background programs in `/atmosphere/contents`), reads the firmware and Atmosphère versions, and looks up the newest releases of Atmosphère, libnx and the app. If a newer release of the app already supports your firmware, it tells you first.
2. It checks the build tools on the Mac Studio. If something is missing or old, it shows an **Install** card listing the packages (for example switch-dev and switch-zlib from devkitPro, or cmake from Homebrew). Click **Install**. For devkitPro your Mac shows its own password box (the password you log in with); Patchbay never sees the password. If devkitPro isn't on the Mac at all, the same card installs it first from devkitPro's official installer. Homebrew itself is the one thing you'd install by hand, from https://brew.sh.
3. It downloads the app's source code into `~/Patchbay-projects/`, works out the build steps from the project's real build files (Patchbay already knows the right steps for ftpd), runs simple setup steps, builds, reads the errors, fixes the code and builds again until it works. These steps happen on the Mac by themselves (every changed code file is backed up first). To get a card for each one again, ask "turn off automatic Mac steps".
4. It installs the new file on the console. You type the device's name on a card, and the old file is backed up first. Then test it on the console (restart it for a background program).

Only general things (version numbers, library names, error messages) are looked up on the web. Your code and files are never sent anywhere.

### Ask Claude for help

The AI on your Mac does the work, but it can get stuck or guess wrong. Patchbay can ask Claude (Anthropic's cloud AI) instead, using your own API key. Click **☁ Claude** above the chat:

1. Paste your API key (make one at https://platform.claude.com/settings/keys). It's saved only on this Mac, in `claude.json`, which is never uploaded to GitHub.
2. Pick when to use Claude:
   - **Off:** only the AI on your Mac.
   - **When stuck:** if the same step fails twice, Patchbay asks Claude what to do.
   - **Plan + when stuck** (the default): Claude also writes the plan for big jobs, like updating an app. Your Mac still does every step.
   - **Always:** Claude answers everything. It's sent automatically and costs the most.
3. Pick the model: Claude Opus 5.5 (best) or Claude Sonnet 5.5 (about half the price).
4. Set a monthly spending limit. When it's reached, nothing more is sent until you raise it.

Before anything is sent, your Mac user name, home folder, IP addresses, device names, the Mac's name, email addresses and passwords are swapped for stand-ins like `<device-1>` or `<ip-1>`. Unless you tick **Send automatically**, a card shows exactly what will be sent and you press **Send to Claude**. **See what was sent** lists every request that left the Mac. The button shows what this session cost so far. A question usually costs a few cents, and a big job in Always mode about $1 to $3.

If the same step fails twice, or it takes 8 steps without getting further, it stops and shows what it tried, with buttons: **Ask Claude**, **Try another way** or **Stop here**. The AI also never asks you to type build commands in Terminal. If it tries, Patchbay sends it back to do the step itself, and an exact step that already failed can't be retried unchanged. Starting a new job like "update ftpd classic" in an old chat starts a fresh chat on its own (the old one is kept under **Past chats**).

### Homebrew on any device

The same works for homebrew on other systems: 3DS, Wii, Wii U, PS Vita, PSP, PS4, Linux handhelds, or your own devices. Patchbay knows the basics of these (where apps live, file types, build tools). For anything it doesn't know, the AI researches it on the web with general questions, then saves what it learned on the Mac (in `<data folder>/_platforms/`) so next time it already knows.

### What firmware is it running?

Each device's picture shows its firmware or operating system once it's known, for example `FreeBSD 14.1-RELEASE` or `Switch 19.0.1, Atmosphère 1.8.0`. New devices are identified automatically. For others, click **Identify firmware** on the tile, or ask in the chat "What firmware is GD1 running?".

It works on any device. It reads version files (like `/etc/os-release`, `freebsd-version` or Android's `build.prop`), the FTP server's greeting, folder names, and the greetings of the device's other services (SSH, web pages). It only reads. If that isn't enough, the AI searches the web for the most telling clue (only the clue, such as a version string) and saves the answer.

**Switch:** the console doesn't keep its firmware number in a normal file on the SD card, so over FTP Patchbay reads Atmosphère's crash and error reports (the newest one wins). Those can be old, so for exact, current versions Patchbay can put a tiny helper app on the console, **Patchbay system info**. Ask "what firmware is my Switch on?" and confirm the card. Then open the app once from the Homebrew Menu: it saves the firmware and Atmosphère versions to `/switch/patchbay-sysinfo/sysinfo.txt` and closes. Patchbay notices by itself and carries on. Run it again after you update the console. The app only reads system information. Its source is in `helper/patchbay-sysinfo/`.

### Change settings by asking

You can change any setting in plain words, for example:
- "Search 192.168.50.203 port 8080": it checks what answers there, then either adds it as a new device or points this device there.
- "Only scan /data and /etc"
- "Use the qwen3.8:27b model"
- "Turn on writing for GD1"

It shows a card with every old and new value. Click **Save settings**. The settings file is backed up first (in `~/GD1-checker-data/<device>/settings-backups/`). Turning on writing also needs you to type the device's name.

### Files

Each device has a **Files** tab. The left side is the Mac Studio's folders, the right side is the device's folders. The buttons at the top switch between **Both**, **Mac Studio only** and **device only**; Patchbay remembers your choice.

- Double-click a folder to open it. Click to select, Cmd+click or Shift+click to select several.
- **Copy from the device:** drag files or folders from the right side to the left side (or onto a folder on the left). This only reads the device. It never overwrites anything on the Mac: a second copy gets a name like `game (2).db`.
- **Copy to the device:** drag from the left side to the right side, or drop files straight from Finder onto the right side, or use **Upload from this computer**.
- **Move on the device:** drag a file onto a folder on the right side.
- **Download** saves a device file to the Downloads folder of the computer you're using (handy on the laptop).
- **New folder**, **Rename** and **Delete** work on the selected items on the device.
- **Rename** anything: click the ✎ pencil at the end of a row, or right-click a file or folder for a menu (Open, Rename, Download, Copy, Delete, New folder). This works on both sides. On the Mac side it happens straight away. On the device side you type the device's name to confirm, as with every device change.
- **Rename the device:** click **✎ Rename device** next to its name on the right side. Its scans, history, picture and backups move to the new name, and from then on you type the new name to confirm changes.

- **Preview:** double-click a file (or right-click > Preview) to look inside: text, pictures, a hex view for other files, or an .nro's details and damage checks.
- **Edit in place:** in the preview of a text file, click **Edit**, change it, and **Save**. On the device you then type its name to confirm, and the old file is backed up first. On the Mac the old version is backed up too.
- **Search:** 🔍 Search on the device side finds files and folders by name anywhere on the device. Click a result to go there.
- **Transfer queue:** copies show below the panes with a progress bar, speed and time left, plus **Pause**, **Resume** and **Cancel**. (Some devices drop a transfer that stays paused for a long time; just start it again.)
- **Select many:** click, Cmd+click, Shift+click, **Select all** or Cmd+A. The count shows next to the buttons.
- **Ask AI about this:** right-click any file or folder and the chat opens with a question about it.
- **Compare:** select two files (two on one side, or one on each side, like the device's copy and a backup) and press **Compare**, or right-click > Compare with…
- **Bookmarks:** ☆ next to the folder path saves the folder you're in. Your bookmarks show under the path; click one to jump there.

Anything that changes the device (upload, move, rename, new folder, delete) first shows you a list of exactly what will happen, and only goes ahead after you type the device's name. Every file that gets replaced or deleted is backed up to the Mac Studio first, and every change is in the device's history, so the AI (or `python3 gd1check.py undo <number>`) can put it back.

Writing to a device still needs `"allow_writes": true` in that device's settings file.

### Backups

Each device has a **Backups** tab for making a full copy of everything on it.

**Just some files or folders:** in the Files tab, select them on the device side and click **Back up** (or right-click > **Back up**). The Backups tab's **Back up only some files or folders…** button takes you there. You can also ask in the chat, e.g. "back up /switch/ftpd". These go into the same backup folder, marked "Only …" in the list.

1. Click **Change…** and pick where backups go: any folder on the Mac Studio, or an external drive or network share (they're under **Drives**). Make the folder in Finder first if it doesn't exist yet. Each device gets its own folder inside, named after it.
2. Click **Back up now**. You see how many files and how much data is done, the speed, and roughly how long is left. You can close the page, or switch to the laptop, and the backup keeps going on the Mac Studio.
3. Click **Stop** any time. If a backup stops for any reason (you stopped it, the device went off, the Mac slept), the button changes to **Resume the unfinished backup**, and it carries on where it left off.

Each backup is a dated folder, like `GD1/2026-10-30_02-10-00/`, holding every file laid out exactly as on the device. Files that haven't changed since the last backup aren't copied again: they're linked to the earlier copy, so each new backup only uses the space of what changed. You can delete old backup folders in Finder, and the other backups stay complete.

Backing up only reads the device. Before it starts copying, it checks there's enough free space. Files that can't be read are skipped and listed in that backup's `_backup.json`.

To put files back, click **Show in Files** next to a backup, then drag files from the left side to the device. As always, you type the device's name to confirm, and the device's current file is backed up first.

**More backup tools:**
- **Browse and restore:** open any backup like a folder. Tick files or folders and press **Put back**, or put back everything. You see exactly what will change first, type the device's name, and anything on the device that gets replaced is backed up before it changes.
- **Labels:** type a label before you start (like "before the Atmosphère 1.8 update"), or click **Label…** on a backup later.
- **Check:** re-reads every file in a backup and compares it with the fingerprint (checksum) taken when it was made, so you know it isn't damaged.
- **Options:** keep only the newest N backups (older ones are deleted after each backup), zip each finished backup to save space, and save a second copy of each backup somewhere else, like your unRAID share or an external drive.
- **Safety backups:** when you approve a plan in the chat that changes files on the device, Patchbay first backs up the folders it will change. These show as "Before: …" in the list.

For a very long backup you can also run it in Terminal (it resumes the same way):
```
python3 gd1check.py --device GD1 backup
python3 gd1check.py --device GD1 backup --to /Volumes/MyDrive/Backups
```

## Use it from your laptop

The Mac Studio does all the work (scanning, AI, copying). Your laptop only shows the page, so it needs nothing installed.

**On the Mac Studio, once:**

1. Choose a password for the laptop:
   ```
   python3 patchbay.py --set-password
   ```
   Type it twice. You won't see it as you type. (Run this again any time to change it. That also signs out every computer.)
2. Stop the Mac Studio from sleeping, or the laptop loses it: open **System Settings > Energy** and turn on **Prevent automatic sleeping when the display is off**.

**On the Mac Studio, each time:**

```
python3 patchbay.py --lan
```

Terminal prints the address for the laptop, like `http://Joshuas-Mac-Studio.local:8765`. If the Mac asks whether "python3" may accept incoming network connections, click **Allow**. (To always start it this way, add `"lan_access": true` to `config.json`.)

**On the laptop:**

1. Join the same Wi-Fi or network as the Mac Studio.
2. Open Safari or Chrome and type the address Terminal printed.
3. Sign in with your password. The laptop stays signed in for 30 days, or until you press **Sign out**.

If the `.local` address doesn't open, use the number address Terminal printed instead (like `http://192.168.1.20:8765`).

Safety notes:
- Without `--lan`, only the Mac Studio itself can open the page.
- After 5 wrong passwords, that computer has to wait before trying again, and the wait gets longer each time.
- Only use `--lan` on your home network. The page isn't encrypted, so don't run it on public Wi-Fi.
- The AI and all your files stay on the Mac Studio. The laptop never talks to the devices or to Ollama directly.

## Everyday use

All commands are run from inside the `gd1-checker` folder.

| What you want | Type this |
| --- | --- |
| Check the Mac can reach GD1 | `python3 gd1check.py test` |
| Look through GD1's folders and pick one | `python3 gd1check.py browse` |
| Check GD1's files for damage | `python3 gd1check.py scan` |
| Have the AI explain the last scan | `python3 gd1check.py explain` |
| Open the app (devices, chat, files) | `python3 patchbay.py` |
| Open the app so the laptop can use it too | `python3 patchbay.py --lan` |
| Back up everything on GD1 | `python3 gd1check.py backup` |
| Run any command for another device | add `--device GD2`, e.g. `python3 gd1check.py --device GD2 scan` |
| Put a good copy of a file back | `python3 gd1check.py restore /path/on/gd1/file.db` |
| See every change made | `python3 gd1check.py history` |
| Undo change number 3 | `python3 gd1check.py undo 3` |

**Finding the right folder:** run `browse`. It lists the folders and files on GD1, each with a number.
- Type a folder's number to open it, or `..` to go back up.
- Type a file's number to preview it. You see its first lines and whether it shows crash damage.
- Type a path like `/var/db` to jump straight there.
- When you're in the right folder, type `pick`. Then choose `r` to scan only this folder, or `a` to add it to the folders you already scan. Your choice is saved in `config.json`.

Browsing only reads from GD1. It never changes anything.

**Tip:** run `scan` while GD1 is working normally, so you have good copies saved. After a crash, run `scan` again. It shows what broke, and `restore` puts back the newest good copy.

## Safety

- Writing to GD1 is **off** until you set `"allow_writes": true` in `config.json`.
- `restore` and `undo` show you exactly what will change, then make you type `GD1` to confirm.
- Before writing, the current file on GD1 is copied to `backups/` on your Mac. If that copy fails, nothing is changed.
- The new file is uploaded under a temporary name and only swapped in once it's fully sent. So a dropped connection can't leave a half-written file behind.
- `explain` only talks to Ollama on your own Mac (`localhost`). The script refuses to send your files anywhere else.

## Where things are saved

Everything goes in `~/GD1-checker-data/<device name>/`, for example `~/GD1-checker-data/GD1/` (you can change this with `data_folder` in `config.json`):

- `snapshots/<date>/`: a copy of GD1's files from each scan
- `reports/<date>.txt`: what each scan found, plus the AI's explanation if you asked for one
- `backups/<date>/`: GD1's file as it was before each restore or undo
- `history.json`: a list of every change
- `uploads/`: files you dropped onto the device from Finder, kept as they were sent
- `portrait.jpg`: the device's picture

Snapshots can get big, so delete old snapshot folders whenever you like. Keep at least one good one.

## Settings in config.json

| Setting | What it means |
| --- | --- |
| `host` | GD1's IP address |
| `ports` | FTP ports to try, in order |
| `username` / `password` | Leave as anonymous for an FTP server with no login |
| `passive_mode` | Leave `true`. Set it to `false` only if listing folders hangs |
| `remote_folders` | Which folders on GD1 to check, e.g. `["/data", "/etc"]` |
| `include` / `exclude` | File patterns to check or skip, e.g. `["*.db", "*.json"]` |
| `max_file_mb` | Skip files bigger than this |
| `allow_writes` | Must be `true` for restore and undo |
| `ollama_model` | Which local AI model `explain` uses |
| `about` | Optional: one sentence about the device, so the AI knows what it is |
| `backup_folder` | Where full backups go (set it on the Backups tab) |
| `backup_folders` / `backup_exclude` | Optional: back up only some folders, or skip patterns, e.g. `["*.tmp"]` |
| `lan_access` | Only in `config.json`: `true` always lets the laptop in (same as `--lan`) |

Each extra device has the same settings in its own file in the `devices` folder. The AI model and data folder always come from `config.json`.

## Tested

The checker was tested against a fake FTP server holding damaged files (truncated and wiped databases, cut-off JSON, null padding, empty files, leftover temp files) and found every one. It has since worked on the real GD1.

The app was tested with two fake FTP devices and one switched-off device: device pictures and the online check, adding devices, the chat, and on the Files screen dragging both ways, dropping files in, uploads that replace a file, new folder, rename, move, deleting a folder and undoing that delete. Signing in from a second computer was tested too, including wrong passwords, the lockout and signing out. The .nro checks were tested on made-up .nro files damaged in different ways (cut off, wiped header, zeroed code, null-padded icon section), along with changing the name and icon, editing a project, building it, installing the result, and putting a damaged .nro back from a backup. Firmware identification was tested on a fake FreeBSD device and a fake Switch SD card, and settings changes, downloading source code and running project commands were tested too. The web lookups couldn't be tested here (no internet in the test machine); they'll work on your Mac. Backups were tested with 1,500 files and a 30 MB file: stopping and resuming, linking unchanged files (three backups took the space of one), the copy matching the device exactly, and running from Terminal. It hasn't run on the real Mac Studio, laptop or GD1 yet.
