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
- [The private builder](#the-private-builder)
- [Reviewing code projects](#reviewing-code-projects)
- [Files on your Mac and Apple Calendar](#files-on-your-mac-and-apple-calendar)
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
5. Install [Ollama](https://ollama.com) (the AI that runs on your Mac), open it, then get a model and load it:
   ```
   ollama pull qwen3.6:35b-a3b
   ollama run qwen3.6:35b-a3b
   ```
   (type `/bye` to leave Ollama's own chat; the model stays loaded for PatchBay.)

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

### Console view, plans and project rules

- **▮ Console** (top of the chat) switches the chat to a dark, terminal-style view: one line per step, with a timer
  while a step runs. Click a step to see what it found. Click again to go back to the normal view.
- **Accept edits** (next to it): when ticked, code changes on your Mac and their builds happen without a card. The
  old file is still backed up first, and the change shows right in the chat. Changes to devices always need a card.
- The bar next to it shows how full the AI's memory of this chat is (for example `10.4k / 32k`).
- For a bigger change, the AI first writes a short **Plan** checklist. It does one step at a time and only ticks a
  step after the project builds.
- **Project rules:** put a file called `PATCHBAY.md` in a project's folder. Write the build command, the style, and
  what must not be touched. The AI reads it before every question about that project. PatchBay has its own
  `PATCHBAY.md` too.
- Searching code is fast: the AI can search with patterns, only some file types, and lines around each match.

## AI models and Claude

**Which model answers.** PatchBay uses the model you load in Ollama, nothing else. Load one in Terminal with
`ollama run qwen3.6:35b-a3b` (then type `/bye`). The top of the chat shows the model in use, read from Ollama
itself. If nothing is loaded, the chat says so instead of picking one. If several are loaded, the one used most
recently answers. PatchBay keeps the model you loaded in memory until you press **Free up memory**, so answers start
quickly. To switch, free the memory (or `ollama stop <model>`) and load another. When a job gets hard, it thinks
harder on the same model. When the same step fails twice, a second model (Gemma 4) reviews what went wrong and
gives advice, then lets go of the memory again.

**🩺 Setup → AI** shows the model in use, the models on your Mac with what uses each one and a **Delete** button,
and **Get more free models** with descriptions, sizes and a memory check. **Memory** shows what Ollama has loaded
right now, and **Free up memory** unloads it.

**Coding.** For changes to code, the AI looks at a short map of the project first (files, functions and their line
numbers), changes only the lines that need it, and every changed file is checked for mistakes before the card even
shows. After saving, the project is built straight away (when automatic Mac steps are on) and the AI gets the real
errors with the code around them. Each question only gets the tools it needs, so more of the AI's memory goes to
your code.

The free-model list is set in `model_labels.py` (CATALOG).

**Long chats.** Once a chat no longer fits the AI's memory, older messages move into notes plus search (see
`chat_memory` under Settings). After each answer, PatchBay quietly sends the AI the start of the next prompt, so the
next reply starts in a few seconds even on models Ollama runs with llama.cpp instead of MLX.

**Models from ollama.com uploaders.** Some community uploads of Qwen 3.6 come with an older prompt template that
ignores "don't think". Replies then start late and their hidden reasoning can end up in the chat's notes. Make a
fixed copy (same weights, no download) with a file named `Modelfile` containing:

```
FROM eriktonon/qwen3.6-moe-uncensored:35b-a3b
TEMPLATE {{ .Prompt }}
RENDERER qwen3.5
PARSER qwen3.5
SYSTEM ""
PARAMETER temperature 0.7
PARAMETER top_p 0.8
PARAMETER top_k 20
PARAMETER min_p 0
PARAMETER presence_penalty 1.5
PARAMETER repeat_penalty 1
PARAMETER num_ctx 32768
PARAMETER num_batch 2048
```

Then run `ollama create qwen36-uncensored-fixed -f Modelfile` and load `qwen36-uncensored-fixed`. On a 36GB Mac
that took one such model from 48 to 55 right out of 60 memory questions (the official Qwen 3.6 35B got 56).

**🔎 Deep research.** Press 🔎 next to the message box (it turns dark), type a question and Send. PatchBay splits
the question into smaller ones, searches the web, reads the pages, takes notes with numbered sources, looks again
for anything missing, and writes a report with a sources list. It takes a few minutes and runs on your Mac's AI;
only search words leave the Mac, with personal details (device names, addresses, your name) taken out. Reports are
saved in `~/PatchBay-data/research/`, and you can keep chatting about the report afterwards.


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

## The private builder

Get code written by Claude without Claude ever seeing who you are or what it's for. It's **off** until you tick
**Turn on the private builder** in the **☁ Claude** panel. Then ask in the chat, for example "use the private builder
to make a tool that merges two CSV files by a key column".

1. The AI on your Mac turns your idea into a plain, generic spec (only data types, functions, steps, edge cases and
   tests). Nothing leaves the Mac for this.
2. PatchBay checks the spec. It stops on anything private: words on your block list, your device names and
   addresses, user names, saved passwords, project names and folders, the Mac's name, your git email, home-folder
   paths, IP and MAC addresses, emails, `.local` host names, API keys and tokens. If the local AI can fix it, it
   writes the spec once more without them; otherwise nothing is sent and the chat says what was found. With the
   double-check on, the local AI also reads the spec once for anything that still looks private.
3. A card shows **exactly** the spec that will be sent and a rough cost. Nothing goes until you press **Send to
   Claude**. Only the spec is sent, as one fresh request: no chat, no memory, no paths, no device details.
4. Claude's files show on a second card. **Save the files** puts them in
   `~/Patchbay-projects/private-builds/<name>/` (or a folder you approved in Setup → Mac). Old versions are backed
   up, and each file can be undone in 🩺 Setup → Mac.

**Block list**: `config/redaction_list.json` in the PatchBay folder (made empty the first time; it stays on your Mac
and is not in git). Put your own words under `blocked_terms`, regular expressions under `blocked_patterns`, and
words the double-check flagged that are fine to send under `allowed_terms`. See `config/redaction_list.example.json`.

**Cost**: the private builder uses your Anthropic API key, which is billed separately from a Claude Pro plan. Pick
its model in the ☁ Claude panel (default: Claude Sonnet, cheaper). A small module usually costs a few cents; the
card shows a rough range first, and the monthly limit still applies. The spec, Claude's answer and every request
are kept on the Mac (`data-cache/private-builds/` and **See what was sent**).

**From Terminal**, in the PatchBay folder:

```
python3 -m harness.run --input idea.txt --output-dir ~/Patchbay-projects/private-builds/my-tool
python3 -m harness.run --input idea.txt --dry-run                  # stop before anything is sent
python3 -m harness.run --input idea.txt --output-dir out --local-model qwen3.6:27b --claude-model claude-opus-5-5
python3 -m harness.run --undo out                                  # put the folder back as it was
python3 -m harness.sanitizer spec_sanitized.md                     # check a spec by itself (exit code 0 = clean)
```

It shows the spec and the cost and asks before sending and before writing. Tests: `python3 -m unittest discover -s
tests -t .`

## Reviewing code projects

Paste a GitHub link (yours or anyone's public one) and ask for a review, for example "look through this and critique
it". PatchBay lists every file, reads the code in pieces, keeps notes, and then writes a review: what the project is,
how it's built, what's good, problems by priority with the file and line, security, ease of use, and what to fix
first. It skips pictures, downloaded libraries, generated files and anything that may hold passwords. A big project
takes 10-20 minutes; Stop works any time. Reviews are saved in `~/PatchBay-data/reviews/`. A folder you approved in
Setup → Mac can be reviewed the same way ("review my project in ~/Documents/MyGame").

## Files on your Mac and Apple Calendar

Both chats can work with files on this Mac and with Apple Calendar, but only where you allow it, in
**🩺 Setup → Mac**:

- **Folders**: click **Add a folder…** and pick one (or type its path). The chat can look inside it and read files
  (text and PDFs) freely. Saving, renaming, moving and deleting always show a card first; the old version is backed
  up, and deleting only moves things to the Trash. Whole-disk, home, system and Library folders can't be added, and
  password and key files are refused even inside an added folder.
- **Apple Calendar**: turn on the switch. The first time, macOS asks whether PatchBay may use Calendar: choose
  **OK**. Then you can ask things like "what's on my calendar this week?" or "move the dentist to Friday at 3".
  Adding, changing and deleting events show a card first.
- **Recent changes** in the same tab lists what was changed, each with **Undo**.

What the chat reads in your files or calendar stays on the Mac. It's never sent to Claude, and PatchBay refuses web
searches that repeat it. Even when Claude answers everything (**Always**), questions about your files or calendar
are answered by the AI on your Mac.

## Setup, updates and looks

**🩺 Setup** checks that everything works:
- Ollama (the AI) and which model is loaded
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

**Send PatchBay's changes to GitHub** (Updates tab): changes PatchBay made to itself stay on this Mac until you
send them. This lists the changed files and the exact changes; type **PatchBay** and press **Send to GitHub**. They go
to their own branch, `patchbay-edits`, never `main`, so you can look them over on GitHub and merge them there. Your
settings, passwords, backups and chats are never sent. It uses your Mac's own GitHub login if `git push` works in
Terminal; otherwise make a fine-grained token on GitHub (this repository only, **Contents: Read and write**) and save
it under **GitHub login**. It's kept in the Keychain.

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
   label next to **AI** at the top of the chat says where the AI is running. **Use this computer** switches back.

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
- **Your Mac and calendar**: only folders you added, and Calendar only when its switch is on. Every change shows
  a card first and can be undone. None of it goes to Claude or into web searches.
- **The private builder** sends Claude only a generic spec, after the privacy check and your **Send to Claude**.
- **Run by hand.** Nothing runs on a schedule.
- **🕘 Activity** lists everything PatchBay did, with **Undo** where possible.

## Where things are saved

- **Device data**: `~/PatchBay-data/<device name>/` (set with `data_folder`). This holds the scan fingerprints
  and reports, the Needs attention files, change history, chats, and the device's picture.
- **Backups**: wherever you chose on the Backups tab (default `~/Patchbay-backups/<device name>/`).
- **Projects** (source code): `~/Patchbay-projects/`.
- **Device settings**: `config.json` for your first device, and one file per device in the `devices` folder.
- **PatchBay's own data** (themes, saved prompts, caches): `data-cache/` inside the PatchBay folder.
- **Private builder**: block list in `config/redaction_list.json`; each build's spec and Claude's answer in
  `data-cache/private-builds/`; the code in `~/Patchbay-projects/private-builds/`.

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
| `mac_folders` | Folders on this Mac the chat may use (set in Setup → Mac) |
| `calendar_access` | `true` lets the chat use Apple Calendar (set in Setup → Mac) |
| `about` | One sentence about the device, so the AI knows what it is |
| `backup_folder` | Where backups go (set on the Backups tab) |
| `lan_access` | `true` always lets the laptop in |
| `chat_memory` | On unless set to `false`. Long chats keep their older part in notes plus search, so details from early on aren't lost |

Where the AI runs and the data folder are shared by all devices and come from `config.json`.
