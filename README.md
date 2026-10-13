# Endora Playout

A playout server in the style of AxelTech XPlayout. It runs a **playlist** of
**commercials** and **live cards**. A live card puts the vMix NDI output on air. The
finished program goes out over NDI (**PLAYOUT PGM**) and to **3 live stream
destinations** (Facebook, YouTube or any RTMP/RTMPS server) at the same time.

```
vMix --NDI--> Playout --NDI "PLAYOUT PGM"--> anything that takes NDI
                 |  \--RTMPS--> Facebook Page
 commercials ----+   \--RTMPS--> Facebook Group / 2nd page
 (media folder)       \--RTMP--> YouTube / custom
```

## The screen

- **On-air bar:** Play, Pause and Stop, a thumbnail, the item name, and the ON-AIR / LIVE tally.
- **Counters:** Duration, Position, Remain, Playlist Remain, and Automatic/Manual mode.
- **Buttons:** **Next** (take the next item now), **+ Live Card**, **+ Add Media**, **Stinger**,
  **Auto Stinger**, **Loop**, **Automatic / Manual**, and **TAKE LIVE** (stops the playlist and puts vMix on air at once).
- **Play / Pause / Stop** sit next to the tally. **Stop** stops the playlist and puts **colour bars** on air;
  **TAKE LIVE** stops it and puts the live feed on air.
- **Playlist:** thumbnail, name, Duration, Start, End, Time to start, and Scheduler
  (Sequential or a fixed time). Each item has buttons to play it, Cue, Edit, move Up/Down,
  Copy and Delete.
- **Header:** clock, date, format, **CPU / GPU / RAM** and **network** (upload / download) of the PC, plus Playout's own CPU and memory.
- **Right side:** a program monitor with L/R audio meters, the vMix NDI input picker, **PC sound**
  (program audio on the Windows default output or a chosen device, with volume), the Functions panel,
  the 3 stream destinations with **Quality**, and the **CG panel** (Manual CG and CG Library tabs).

Keyboard: **Space** = Next, **F1** = Play, **F2** = Pause, **F12** = Take live.

On a wide screen the page does not scroll: the playlist side and the monitor side scroll on their own
(the program monitor stays at the top). Click **Functions** to fold its buttons away.

**Full Screen** (Functions) shows the program output full screen on the second display. Press it
again, or Esc on that display, to close it.

### Stinger transitions
Press the gear next to **Auto Stinger**, then **Browse...** and pick the stinger (a short QuickTime
MOV with alpha, with or without sound) from any folder; it is copied into `cg/stingers`. **Cut point** is when the stinger covers the
screen (empty = half way).
- **Stinger ▶** plays it now and changes to the next item at the cut point.
- **Auto Stinger** uses it for every change: Next, playing a playlist item, and clip to clip in
  Automatic mode (it starts early so the cut lands exactly on the end of the clip).

### GPU video decoding
Clips and video CGs are decoded on the graphics card when the PC has one (D3D11 / NVIDIA / Intel),
which keeps CPU use down. Set `"hwaccel": "off"` in `config.json` to decode on the CPU. Use a GPU
encoder under **Quality** for streaming as well (NVENC / Quick Sync / AMF).

### How items play
- A **commercial** plays its file, then the next item starts. The next clip is loaded
  ahead of time, so cuts are frame-accurate with no black or live flash in between.
- A **live card** shows the vMix feed. Give it a duration (for example a 15:00 segment)
  and it moves on by itself. With no duration it stays live until you press **Next**.
- **Automatic** runs item after item. **Manual** goes back to live after each item and
  cues the next one, which then waits for Play or Next.
- **Fixed start (Edit > Fixed start):** the item jumps on air at that time of day, even if
  something else is playing. Use it for timed ad breaks.
- **Stop / TAKE LIVE** always ends on the vMix live feed, never black.

A typical show playlist:
`LIVE (15:00) -> Ad 1 -> Ad 2 -> Ad 3 -> LIVE (15:00) -> Ad 4 -> Ad 5 -> LIVE (until Next)`

### Live card sources
Each live card picks its own source in **+ Live Card** (or **Edit** on a live card):
- **Main vMix input**: the NDI source chosen on the main screen.
- **Another NDI source** on the network.
- **USB / HDMI capture device** (AverMedia, Elgato, Magewell, webcams...). Plug it in, press the refresh
  button, pick the video device and its audio device.
- **Colour bars + tone** for tests.

## CG (graphics with alpha)

CGs are made in **CG Composer** (**Save to Playout**). The CG Library tab lists only CG Composer
files; **Import CG (.cgp)** brings in one exported on another PC (PNG / MOV / MP4 files are not added
to the library directly). Files CG Composer can use:
**PNG** (with alpha), **JPEG** (opaque), and **QuickTime MOV** (ProRes 4444, QT Animation and
PNG-in-MOV keep their alpha). Video CGs can loop or take themselves out at the end.

- **Manual CG tab**: press **+ Add CG**, pick the file and place it. **IN** / **OUT** take it on and
  off air at any time, even while a commercial plays. **Only on live** hides it while a commercial is
  on air and shows it again on live. **ALL OUT** clears every CG.
- **Live card CGs**: in the live card dialog, **+ Add CG** adds CGs that go on air with that card
  only. **Delay** starts it some seconds after the card starts, **On for** takes it out after some
  seconds (empty = whole card).
- **CG setup** has three tabs, like vMix:
  - **General**: file, name, fade, loop, only on live (manual) or delay / on for (live card).
  - **Colour Key**: green / blue screen key with **Pick from picture**, Chroma Key, Edge softness and
    Spill removal sliders and auto presets 1-3, or a **Luma key** for black backgrounds. Use it for
    JPEG or MP4 CGs made on a green screen.
  - **Position**: Zoom, Zoom X, Zoom Y, Pan X, Pan Y, Rotate and Crop X1 / Y1 / X2 / Y2, each with a
    Reset, quick place buttons, and drag on the preview.
- Keying and effects run as FFmpeg filters on the decode thread; a looping video CG is kept in
  memory after its first loop, so it costs almost nothing after that.

### CG Composer
A separate app (Start menu > **CG Composer**) with the same three tabs. Open a PNG / JPEG / MOV / MP4,
key it and place it, then **Save to Playout**: it is saved as a `.cgp` file in the Playout CG
library and shows in the **CG Library** tab, where **+ Manual** adds it with all its settings.
**Export .cgp** saves the file anywhere (to use on another PC: Import CG (.cgp) in the CG Library tab).
A `.cgp` is a zip holding the settings and the CG file.

## Live streaming (3 destinations)

1. Click **Setup** on a destination and pick the **Facebook Live** preset (or YouTube or Custom).
2. Paste the **Stream key**. In Facebook it is under Live Producer > Streaming software.
3. Click **Go live** on that destination, or **GO LIVE (all set)** to start every
   destination that is set up.

The program is encoded once and sent to every destination. Each destination starts,
stops and reconnects on its own: if one drops, the others keep going, and the dropped
one retries every 5 seconds.

**Quality** (next to Live stream) sets the resolution (**720p** or **1080p**), the video bitrate
(any value, with presets), the audio bitrate and the encoder (CPU x264, NVIDIA NVENC, Intel Quick Sync
or AMD AMF). Running destinations restart with the new settings. The starting values are under
`stream` in `config.json`:

| key | default | meaning |
|---|---|---|
| `width`, `height` | 1920x1080 | stream size |
| `video_kbps` | 4500 | Facebook recommends 4000 to 6000 for 1080p |
| `audio_kbps` | 128 | |
| `encoder` | `libx264` | use `h264_nvenc` on a PC with an NVIDIA card to save CPU |
| `preset` | `veryfast` | x264 speed preset |
| `ffmpeg_path` | (built in) | leave empty to use the FFmpeg that installs with the app |

Stream keys are saved in `data/state.json` on this PC only.

## Recording

**Recording** (under Live stream) records the program output, with CGs and sound, to an MP4 file in
`Documents\Playout\recordings` (named `PGM_<date>_<time>.mp4`). Pick **1080p** or 720p, **10 / 20 / 50 Mbps**
(constant bitrate) and the encoder, then press **● REC**. A GPU encoder (NVIDIA / Intel / AMD) keeps the
CPU free; if the PC does not have it, Playout records with the CPU instead and says so. The file is
written in fragments, so it stays playable even if the PC loses power while recording.

## Failover and auto restart

**Settings > Failover**: when the vMix (or a live card's) input is lost for a few seconds, Playout puts
colour bars, black or a filler clip ("We'll be right back", looped) on air instead of a frozen picture, shows
a red warning, writes it to the air log, and goes back to live by itself when the input returns.

Playout.exe runs through a small launcher. If Playout crashes or its output freezes for 15 seconds, it is
started again within a few seconds and carries on with the item that was on air, at the point it had
reached. The installer can also start Playout when Windows starts.

## Commercials: client, trim, volume, CGs

**Edit** on a commercial sets its **client** (advertiser, for the ad report), **trim** (in / out point, with
pictures of both frames), extra **volume**, and **CGs** that go on air only with that commercial (logo, bug,
lower third), like the CGs on a live card.

**Settings > Audio level > Normalize** plays every clip at the same loudness (default -23 LUFS, EBU).
Each clip is measured once in the background (FFmpeg EBU R128); files are not changed.

## Ad report

**Functions > Ad Report**: commercials aired in a date range, for one client or all, with plays and total air
time per commercial and the full spot list. **PDF**, **Excel** or **CSV**; on the playout PC the file is
saved in `Documents\Playout\reports` and opened. The station name on the report is set in Settings.

## Excel playlists and day schedule

**Playlist Manager > Import from Excel / CSV**: one row per item with columns Time, File, Name, Client, Type
(clip / live), Duration, In, Out (only File is needed; there is a template). Replace the playlist, add to it,
or only save it under a name.

**Functions > Day Schedule**: pick a saved playlist for each weekday and for special dates; Playout loads it
by itself at the chosen time (the on-air item finishes first). **Scheduled recording** starts and stops
recordings at set times, every day or on chosen days / a date.

## Phone remote and passwords

**Functions > Phone Remote** shows the address and a QR code. On a phone on the same Wi-Fi it opens a
simple remote: preview, Play / Pause / Stop / Next / Stinger / TAKE LIVE, playlist Take, manual CG IN / OUT,
REC, and the stream status with the Facebook countdown.

**Settings > Passwords**: a **remote password** (phones and other PCs log in once; this PC never asks) and a
**lock password** (needed to Unlock the screen and to open Settings; the lock survives a restart, and
Playout can start locked). Stream Deck / Companion triggers then need `?key=<remote password>`.

## Updates

Every build publishes a GitHub release. Playout checks for a newer one every 6 hours and shows
"Update available" and asks **Update now?** (only on the playout PC, and only an admin when users are
set up). **Update now** downloads it, then Playout closes, updates and starts again by itself; **Later**
leaves it for the banner's **Update** button or **Settings > Updates**. It never installs without asking. The repository is private, so put a GitHub access token with read access to it in
Settings > Updates (or make the repository public).

## Text CG (ticker, lower third, clock)

**+ Text CG** in the manual CG bar makes graphics from typed text, no image file needed: a **lower third**
(name + title), a scrolling **news ticker** (text typed in, or read from a .txt file that is re-read while on
air), a **clock / date**, or plain text. Pick the font (Iskoola Pota / Nirmala UI for Sinhala and Tamil),
size, colours, box opacity and position. Changing the text of a CG that is on air updates it at once.

## Alerts to your phone (Telegram)

Settings > Alerts: make a bot with @BotFather, paste its token, send "hi" to the bot, press **Find chat**,
then **Send test**. Pick which events send a message: backup PC took over, disk almost full, live input lost,
Facebook 8-hour limit, missing clip, recording failed, Playout restarted after a crash, stream dropped.

## Ad booking and invoices

**Ad Booking**: per client and campaign, the dates, spots per day (or in all) and rate. The table shows
booked against aired (from the air log, so it is proof of play) today, in the period and in all, how many
spots of today's booking are in the playlist, and the amount. **Invoice PDF** makes an invoice for one
client and period.

## Network feeds (SRT / RTMP / YouTube)

Live card > **Network feed**: `srt://0.0.0.0:9000` (listen for a sender) or `srt://host:port` (call),
`rtmp://0.0.0.0:1935/live/key` (OBS / a phone app pushes to this PC), `rtsp://camera`, an `.m3u8` link, or
a YouTube / Facebook live page link.

## SDI output (Blackmagic DeckLink)

Settings > SDI out: install Blackmagic Desktop Video and point Playout at an `ffmpeg.exe` built with
DeckLink support (`--enable-decklink`; the FFmpeg inside Playout does not have it), press **Find cards**
and pick the output.

## Next clip preview

Under the program monitor the **NEXT** item is shown with its time; **Preview** plays it (or any clip)
in a window, without touching the program output.

## Watch folder

Settings > Watch folder: ad files copied into that folder (or a network share) come into the media folder
by themselves once they finish copying, get their loudness measured and can be added to the end of the
playlist. Files that do not play go to a `rejected` folder with a note.

## Users and activity log

Settings > Users: once users are added everybody logs in by name. **Operators** run the playlist, CGs and
streams; only **admins** can open Settings, Day Schedule and Ad Booking. Air Logs > **Activity** shows who
changed what (also saved in `logs/activity-<date>.csv`).

## Backup PC

Install Playout on a second PC. On the main PC: Settings > Backup PC > "The MAIN PC" and a sync key. On
the second PC: "The BACKUP PC", the main PC's address and the same key. The backup copies the playlist,
CGs and missing clips and plays the same thing at the same point, so a switcher can change to its NDI
output at any time. If the main PC stops answering, the backup carries on by itself, sends an alert and can
start its own live streams; when the main is back the backup follows it again. Tick **Mirror every
clip and CG file** to copy the main PC's whole media and CG folders (files are only added, never deleted).

## Install (Windows)

1. Download **Playout-Setup.exe** (GitHub > Releases > latest, or Actions > latest "Build Playout installer" run > Artifacts).
2. Run it and click through the setup. It installs to Program Files, adds Start menu and desktop
   shortcuts, and allows Playout through Windows Firewall (needed for NDI).
3. Start **Playout** from the desktop shortcut. It opens in its own window, with no browser.
4. Your files are in **Documents\Playout**: `media` (commercials), `config.json`,
   saved playlists, and `logs` (air logs + `playout.log`).

Closing the Playout window stops the playout (it asks first). The control screen is also
available on the network at `http://PLAYOUT-PC:8080`, for use as a remote control from a tablet.

Needs Windows 10 or 11 (64-bit) with the Microsoft Edge WebView2 runtime, which comes with Windows 11 and up-to-date Windows 10.

Running from source instead: install Python 3.12, run `install.bat`, then `start.bat`
(this opens the browser version).

## First run

1. In vMix, go to **Settings > Outputs / NDI / SRT** and turn on NDI for an output.
   Set vMix to the same format as `config.json` (default 1080p25).
2. In Playout, open **Functions > Live Source**, pick `PC-NAME (vMix - Output 1)` and press **Use**.
3. Copy the ad files into `Documents\Playout\media` (**Functions > Media Folder** opens it), or use **Upload Clips**. mp4, mov, mxf, ts, mkv and most other formats work.
4. Build the playlist with **+ Live Card** and **+ Commercials**, then press **Play**.
5. Save it under **Playlist Manager** so you can load it again tomorrow.

## Control from vMix scripts / Stream Deck / Companion

Plain GET URLs:
`/trigger/play`, `/trigger/pause`, `/trigger/next`, `/trigger/stop` (or `/trigger/live`),
`/trigger/item/<item name>` (plays that playlist item now).

## Air logs

Every item that goes on air is written to `logs/asrun-YYYY-MM-DD.csv`, with its start,
end, planned and played seconds and status (played, skipped, stopped, missing file).
You can view it and download it from **Functions > Air Logs**, and use it as proof of play for advertisers.

## Settings (`config.json`)

| key | meaning |
|---|---|
| `output_name` | NDI name of the program output |
| `live_source` | NDI source to use at start (can also be picked on screen) |
| `width`, `height`, `fps` | program format. fps can be `25`, `50`, `29.97`, `30`, `59.94` or `60` |
| `web_port` | control screen port |

## Files

| file | what |
|---|---|
| `desktop.py` | Playout.exe: the app window |
| `app.py` | engine start-up and web API |
| `paths.py` | where user files live |
| `engine.py` | output clock, playlist runner, as-run log |
| `streamer.py` | H.264/AAC encoder and the 3 stream destinations |
| `ndi_io.py` | NDI input (frame sync) and output |
| `media.py` | clip decoding and thumbnails (FFmpeg via PyAV) |
| `scheduler.py` | fixed-time items, day playlists, scheduled recording |
| `cg.py` | CG layers (PNG / JPEG / MOV with alpha), manual and live-card CGs, stingers |
| `cgfx.py` | colour / luma key, crop, zoom, rotate (FFmpeg filter graph) |
| `cgedit.py` | CG setup previews and `.cgp` presets (shared with CG Composer) |
| `composer.py` | CG Composer app |
| `web/cgedit.html` | CG setup / CG Composer screen |
| `web/output.html` | full screen output page |
| `capture.py` | USB / HDMI capture devices and test bars for live cards |
| `loudness.py` | audio normalize (EBU R128 measurement) |
| `report.py` | ad report: PDF / Excel / CSV |
| `importer.py` | playlist import from Excel / CSV |
| `updater.py` | update check, download and install |
| `web/remote.html` | phone remote |
| `textcg.py` | text CGs: lower third, ticker, clock |
| `alerts.py` | Telegram alerts and what they watch |
| `booking.py` | ad bookings and invoices |
| `sdi.py` | DeckLink SDI output |
| `watchfolder.py` | watch folder import |
| `backup.py` | main / backup PC sync and takeover |
| `audio_out.py` | program sound on the PC's speakers |
| `recorder.py` | program recording to MP4 |
| `system.py` | CPU / GPU / RAM usage |
| `store.py` | saved state and named playlists |
| `web/index.html` | control screen |
