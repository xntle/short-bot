# shorts_bot

Splices your interruption clip into the middle of short videos and exports them.

## Normal use

Double-click **`START HERE - double click me V1.command`**. That's it.
(The launcher finds the bot script itself, so adding a version suffix to
either file is safe.)

It installs anything missing on first run, then asks for links. Paste a link and
press Enter. Type `folder` to process everything sitting in `inbox`. Type `quit`
to stop.

## Replacing your interruption clip

Clips live in `shorts_bot_data/` and are named by number:

```
1.mov   your first clip
2.mp4   a newer one - this is the one that gets used
3.mov   newer still - now this one is
```

**The highest number always wins.** To swap clips, drop in the next number up.
Nothing to rename, nothing to delete. Old numbers stay as history and are
ignored. `.mp4`, `.mov`, `.mkv`, `.webm`, `.m4v` and `.avi` all work, and the
bot converts whatever you give it, so a big ProRes export straight out of an
editor is fine.

The bot prints which clip it picked at the start of every run:

```
interruption clip: 2.mp4
```

Worth a glance before a big batch. The older name `interruption.mp4` still
works if nothing numbered is present.

Everything below is for driving it from a terminal instead.

## Manual setup

```bash
brew install ffmpeg          # macOS. required.
brew install yt-dlp          # only if you want URL downloading
python3 "shorts_bot V1.py"        # creates the folders
```

## Folder layout

Created next to the script as `shorts_bot_data/`:

```
1.mov              <- YOUR clip. put it here. required.
                      numbered by version: highest number is the one used.
                      drop in 2.mp4 later and it takes over. keep the old
                      ones or delete them, either is fine.
urls.txt           <- one URL per line, # for comments
inbox/             <- or just drop video files here
downloads/         <- fetched sources (cached)
output/            <- finished videos, numbered 1.mp4, 2.mp4, 3.mp4 ...
                      numbering carries on across runs, so a later batch
                      never overwrites an earlier one
processed/         <- inbox files moved here after they're done
state.json         <- remembers what's already been processed
```

Two ways to feed it, and you can mix them:

- paste URLs into `urls.txt`
- drop video files into `inbox/`

## Running it from a terminal

```bash
python3 "shorts_bot V1.py"                    # one pass over inbox + urls.txt
python3 "shorts_bot V1.py" --url "LINK"       # process one link now
python3 "shorts_bot V1.py" --watch            # runs forever, rescans every 60s
python3 "shorts_bot V1.py" --split-at 0.5     # force exact midpoint
python3 "shorts_bot V1.py" --force            # redo something already done
python3 "shorts_bot V1.py" --open             # open the output folder when finished
python3 "shorts_bot V1.py" --cookies-from firefox   # force which browser cookies come from
python3 "shorts_bot V1.py" --no-cookies       # never touch browser cookies
```

By default the cut lands at a random point between 75% and 90% of the way in,
so a batch doesn't come out with the interruption in the identical spot every
time. `--split-at 0.5` pins it to the exact middle.

`--watch` is the hands-off mode: leave it running, drop files or paste URLs whenever,
and finished videos appear in `output/`. Nothing is ever processed twice, so you can
leave URLs in `urls.txt` permanently.

## Making it start automatically on login (macOS)

Save this as `~/Library/LaunchAgents/com.local.shortsbot.plist`, fixing the two paths:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0">
<dict>
  <key>Label</key><string>com.local.shortsbot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/FULL/PATH/TO/shorts_bot.py</string>
    <string>--watch</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/tmp/shortsbot.log</string>
  <key>StandardErrorPath</key><string>/tmp/shortsbot.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.local.shortsbot.plist
tail -f /tmp/shortsbot.log
```

To stop: `launchctl unload ~/Library/LaunchAgents/com.local.shortsbot.plist`

## How the splicing works

Two strategies, chosen per video.

**Fast path (stream copy).** The source's video and audio packets are copied
through untouched, so nothing is re-encoded and no quality is lost. Roughly 16x
faster than re-encoding. Requires two things to be true:

- the cut lands exactly on a keyframe, so the random point inside `SPLIT_RANGE`
  is snapped to the nearest keyframe. Placement still varies per video.
- every segment shares an identical stream layout, so your interruption clip is
  re-rendered once per distinct source format and cached in
  `shorts_bot_data/.prepared/`. A batch of similar shorts pays that cost once.

**Safe path (re-encode).** Used automatically when the source isn't h264/aac,
has no usable keyframes, or the fast path fails for any reason. Everything is
normalised to 1080x1920 and joined. Always works.

The window tells you which one ran. `--split-at 0.5` forces the exact midpoint,
which usually pushes the job onto the safe path since a keyframe rarely sits
there.

## What it does to the video

Every segment is re-encoded to the same format before joining, which is why the
splice is reliable regardless of what the sources look like:

- 1080x1920 vertical, scaled to fit and padded with black
- 30fps, H.264, yuv420p
- AAC stereo at 44.1kHz, with silence added if a source has no audio track

Change `WIDTH`, `HEIGHT`, `FPS`, or `SPLIT_AT` at the top of the script to adjust.

## Notes

- Re-encoding is CPU-bound. Roughly a few seconds per short on a modern Mac.
- A failed video logs the error and the run continues to the next one.
- Sources under about 1 second are skipped, since there's nothing to split.
- Only run this on videos you own or are licensed to reuse. Downloading and
  re-exporting other people's Shorts is a copyright problem and against YouTube's
  terms, whatever the tooling makes technically possible.

## "Sign in to confirm you're not a bot"

YouTube shows this when it decides a request looks automated. It is not a bug in
the app, and it can start happening on links that worked yesterday.

The app handles it by itself: it downloads anonymously, and only if YouTube
refuses does it retry using cookies from a browser you're already signed into.
Once a browser works it sticks with it for the rest of the run. Browsers are
tried in the order Firefox, Chrome, Brave, Edge, Chromium, Vivaldi, Opera,
Safari, which is roughly how reliably their cookie stores can actually be read.

If it still fails, in order:

1. `pip install -U yt-dlp`. YouTube changes things constantly and yt-dlp ships
   fixes weekly. A months-old copy is the single most common cause.
2. Sign in to YouTube in Firefox or Chrome, then run again.
3. On macOS, cookie reads need permission: System Settings > Privacy & Security
   > Full Disk Access, add Terminal, restart Terminal. Quitting the browser
   first also helps, since a running browser can hold the cookie file locked.
4. If the error mentions 429 or "too many requests", the IP is rate limited.
   Wait an hour or two. Turn off a VPN if one is on, since shared VPN exit
   addresses get flagged quickly.

`--cookies-from BROWSER` forces a specific browser. `--no-cookies` turns the
whole mechanism off.

## Verified

Tested end to end on synthetic sources: a 12s 720x1280 clip with audio and a 9s
1920x1080 clip with no audio at all, with a 3s 640x640 interruption. Both produced
correct 15s and 12s outputs at 1080x1920/30fps/AAC-stereo, with a pixel check
confirming the interruption lands at the midpoint.
# short-bot
# short-bot
# short-bot
