# Playout

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
- **Buttons:** **Next** (take the next item now), **+ Live Card**, **+ Commercials**, **Loop**,
  **Automatic / Manual**, and **TAKE LIVE** (stops the playlist and puts vMix on air at once).
- **Playlist:** thumbnail, name, Duration, Start, End, Time to start, and Scheduler
  (Sequential or a fixed time). Each item has buttons to play it, Cue, Edit, move Up/Down,
  Copy and Delete.
- **Right side:** a program monitor with L/R audio meters, the Functions panel, the 3 stream
  destinations, and a big clock.

Keyboard: **Space** = Next, **F1** = Play, **F2** = Pause, **F12** = Take live.

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

## Live streaming (3 destinations)

1. Click **Setup** on a destination and pick the **Facebook Live** preset (or YouTube or Custom).
2. Paste the **Stream key**. In Facebook it is under Live Producer > Streaming software.
3. Click **Go live** on that destination, or **GO LIVE (all set)** to start every
   destination that is set up.

The program is encoded once and sent to every destination. Each destination starts,
stops and reconnects on its own: if one drops, the others keep going, and the dropped
one retries every 5 seconds. Stream settings are under `stream` in `config.json`:

| key | default | meaning |
|---|---|---|
| `width`, `height` | 1920x1080 | stream size |
| `video_kbps` | 4500 | Facebook recommends 4000 to 6000 for 1080p |
| `audio_kbps` | 128 | |
| `encoder` | `libx264` | use `h264_nvenc` on a PC with an NVIDIA card to save CPU |
| `preset` | `veryfast` | x264 speed preset |
| `ffmpeg_path` | (built in) | leave empty to use the FFmpeg that installs with the app |

Stream keys are saved in `data/state.json` on this PC only.

## Install (Windows)

1. Download **Playout-Setup.exe** (GitHub > Actions > latest "Build Playout installer" run > Artifacts).
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
| `scheduler.py` | fixed-time items |
| `store.py` | saved state and named playlists |
| `web/index.html` | control screen |
