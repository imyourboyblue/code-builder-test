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

## The Patchbay app

Start it on the Mac Studio, in Terminal, from inside the `gd1-checker` folder:

```
python3 patchbay.py
```

Your browser opens the **Devices** screen. Leave the Terminal window open while you use it. To stop, click on Terminal and press Ctrl+C.

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

The AI can look at anything on the device without asking. To change a file, it shows you a card with the exact change, and nothing is written until you type the device's name (like `GD1`) and press **Confirm**. The chat is kept while the app runs, so you can switch devices or computers and pick up where you left off.

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

To build Switch homebrew, the Mac Studio needs devkitPro installed (from devkitpro.org). If `make` works in Terminal in your project folder, it works here too.

### Update a homebrew app for your firmware (like Chiaki)

In a device's chat, click **Update a homebrew app so it works with my firmware**, or just ask, for example "Update Chiaki so it works with my Atmosphère". The AI then works through these steps, telling you what it's doing:

1. It reads which firmware and Atmosphère version the console runs, and the app's current version.
2. It looks up the newest releases of Atmosphère, libnx and the app itself. If a newer release of the app already supports your firmware, it tells you first.
3. It checks the build tools on the Mac Studio (devkitPro, libnx). If something is missing or old, it gives you one command to paste into Terminal, like `sudo dkp-pacman -Syu`. Commands that need your Mac password always go through you.
4. It downloads the app's source code from GitHub into `~/Patchbay-projects/`. You click **Download it** on a card first.
5. It runs the setup steps the project needs, like `cmake` or `git submodule update`. Each one is a card you click **Run it** on.
6. It builds, reads the errors, fixes the code, and builds again until it works. Each code change is a card you click **Save the change** on.
7. It installs the new .nro on the console. You type the device's name, and the old .nro is backed up first. Then test it on the console.

Only general things (version numbers, library names, error messages) are looked up on the web. Your code and files are never sent anywhere.

### What firmware is it running?

Each device's picture shows its firmware or operating system once it's known, for example `FreeBSD 14.1-RELEASE` or `Switch 19.0.1, Atmosphère 1.8.0`. New devices are identified automatically. For others, click **Identify firmware** on the tile, or ask in the chat "What firmware is GD1 running?".

It works on any device. It reads version files (like `/etc/os-release`, `freebsd-version` or Android's `build.prop`), the FTP server's greeting, folder names, and the greetings of the device's other services (SSH, web pages). It only reads. If that isn't enough, the AI searches the web for the most telling clue (only the clue, such as a version string) and saves the answer.

### Change settings by asking

You can change any setting in plain words, for example:
- "Search 192.168.50.203 port 8080": it checks what answers there, then either adds it as a new device or points this device there.
- "Only scan /data and /etc"
- "Use the qwen3.8:27b model"
- "Turn on writing for GD1"

It shows a card with every old and new value. Click **Save settings**. The settings file is backed up first (in `~/GD1-checker-data/<device>/settings-backups/`). Turning on writing also needs you to type the device's name.

### Files

Each device has a **Files** tab. The left side is the Mac Studio's folders, the right side is the device's folders.

- Double-click a folder to open it. Click to select, Cmd+click or Shift+click to select several.
- **Copy from the device:** drag files or folders from the right side to the left side (or onto a folder on the left). This only reads the device. It never overwrites anything on the Mac: a second copy gets a name like `game (2).db`.
- **Copy to the device:** drag from the left side to the right side, or drop files straight from Finder onto the right side, or use **Upload from this computer**.
- **Move on the device:** drag a file onto a folder on the right side.
- **Download** saves a device file to the Downloads folder of the computer you're using (handy on the laptop).
- **New folder**, **Rename** and **Delete** work on the selected items on the device.

Anything that changes the device (upload, move, rename, new folder, delete) first shows you a list of exactly what will happen, and only goes ahead after you type the device's name. Every file that gets replaced or deleted is backed up to the Mac Studio first, and every change is in the device's history, so the AI (or `python3 gd1check.py undo <number>`) can put it back.

Writing to a device still needs `"allow_writes": true` in that device's settings file.

### Backups

Each device has a **Backups** tab for making a full copy of everything on it.

1. Click **Change…** and pick where backups go: any folder on the Mac Studio, or an external drive or network share (they're under **Drives**). Make the folder in Finder first if it doesn't exist yet. Each device gets its own folder inside, named after it.
2. Click **Back up now**. You see how many files and how much data is done, the speed, and roughly how long is left. You can close the page, or switch to the laptop, and the backup keeps going on the Mac Studio.
3. Click **Stop** any time. If a backup stops for any reason (you stopped it, the device went off, the Mac slept), the button changes to **Resume the unfinished backup**, and it carries on where it left off.

Each backup is a dated folder, like `GD1/2026-10-30_02-10-00/`, holding every file laid out exactly as on the device. Files that haven't changed since the last backup aren't copied again: they're linked to the earlier copy, so each new backup only uses the space of what changed. You can delete old backup folders in Finder, and the other backups stay complete.

Backing up only reads the device. Before it starts copying, it checks there's enough free space. Files that can't be read are skipped and listed in that backup's `_backup.json`.

To put files back, click **Show in Files** next to a backup, then drag files from the left side to the device. As always, you type the device's name to confirm, and the device's current file is backed up first.

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
