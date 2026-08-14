#!/bin/bash
# The only file you need. Double-click it.
# Sets itself up on first run, then asks you for links.

set -u
# If this cd fails the script would carry on in whatever directory it happened
# to start in and operate on the wrong files, so treat it as fatal.
cd "$(dirname "$0")" || {
  echo "Could not open the Shorts Bot folder. Has it been moved or renamed?"
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
}

for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
  [ -x "$p" ] && eval "$($p shellenv)"
done

clear
echo ""
echo "==================================================="
echo "  SHORTS BOT"
echo "==================================================="
echo ""

# ---------- set up anything that's missing, quietly if possible

NEED_SETUP=0
command -v ffmpeg  >/dev/null 2>&1 || NEED_SETUP=1
command -v yt-dlp  >/dev/null 2>&1 || NEED_SETUP=1

if [ "$NEED_SETUP" = "1" ]; then
  echo "First run, so a few things need installing."
  echo "This takes 10-15 minutes. It only happens once."
  echo ""

  if ! command -v brew >/dev/null 2>&1; then
    echo "  >> It will ask for your Mac login password."
    echo "  >> Nothing appears on screen while you type. Keep typing, press Enter."
    echo ""
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)" || {
      echo ""
      echo "!! Install did not finish. Close this window and double-click again."
      read -n 1 -s -r -p "Press any key to close..."
      exit 1
    }
    for p in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      [ -x "$p" ] && eval "$($p shellenv)"
    done
  fi

  command -v ffmpeg >/dev/null 2>&1 || brew install ffmpeg || {
    echo "!! Could not install the video editor. Close this and try again."
    read -n 1 -s -r -p "Press any key to close..."
    exit 1
  }
  command -v yt-dlp >/dev/null 2>&1 || brew install yt-dlp || \
    pip3 install -U --break-system-packages yt-dlp

  clear
  echo ""
  echo "==================================================="
  echo "  SHORTS BOT"
  echo "==================================================="
  echo ""
  echo "Setup finished."
  echo ""
fi

# keep the downloader fresh in the background. YouTube changes things often
# and a stale downloader fails in confusing ways.
( brew upgrade yt-dlp >/dev/null 2>&1 || \
  pip3 install -U --break-system-packages yt-dlp >/dev/null 2>&1 ) &

mkdir -p shorts_bot_data/inbox shorts_bot_data/output

# ---------- the interruption clip has to exist

# Clips are numbered: 1.mov, 2.mp4, 3.mov ... and the highest number is the one
# used. This is only a "does one exist at all" check so we can fail with a
# useful message before Python starts; find_interruption() in the Python does
# the actual picking, and it is the one to trust. "interruption.*" is still
# accepted here so an older setup does not suddenly stop working.
FOUND=""
for f in shorts_bot_data/*; do
  [ -f "$f" ] || continue
  base=$(basename "$f")
  stem="${base%.*}"
  ext=$(printf '%s' "${base##*.}" | tr '[:upper:]' '[:lower:]')
  case "$ext" in
    mp4|mov|mkv|webm|m4v|avi) ;;
    *) continue ;;
  esac
  case "$stem" in
    ''|*[!0-9]*) [ "$stem" = "interruption" ] && FOUND="$f" ;;
    *) FOUND="$f" ;;
  esac
done

if [ -z "$FOUND" ]; then
  echo "!! Missing your interruption video."
  echo ""
  echo "!! I've opened the folder. Put your clip in it and"
  echo "!! name it by number:   1.mp4"
  echo "!! Later versions just go up:  2.mp4,  3.mp4 ..."
  echo "!! The highest number is always the one used."
  echo "!! (.mov, .mkv, .webm, .m4v and .avi are fine too.)"
  echo ""
  echo "!! Then double-click this file again."
  echo ""
  open shorts_bot_data 2>/dev/null
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

# ---------- main loop

# Everything from here is handled by Python. Reading pasted input in bash is a
# trap: macOS ships bash 3.2, which cannot do sub-second read timeouts, so a
# multi-line paste came back with only its first line.

# Find the bot script by pattern rather than by exact name, so renaming it
# (shorts_bot.py -> "shorts_bot V1.py", V2, etc.) does not break this launcher.
# Newest match wins. Quoted everywhere: the name can contain spaces.
BOT=""
for f in shorts_bot*.py; do
  [ -f "$f" ] || continue
  if [ -z "$BOT" ] || [ "$f" -nt "$BOT" ]; then BOT="$f"; fi
done

if [ -z "$BOT" ]; then
  echo "!! Could not find the bot script in this folder."
  echo "!! It should be named shorts_bot.py (a version suffix is fine,"
  echo "!! e.g. 'shorts_bot V1.py'). Has it been renamed or moved?"
  echo ""
  ls -1 *.py 2>/dev/null | sed 's/^/     found: /'
  echo ""
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

python3 "$BOT" --interactive
STATUS=$?

# Exit 130 is a clean ctrl-c, not a failure worth alarming anyone about.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 130 ]; then
  echo ""
  echo "==================================================="
  echo "  It stopped with an error (code $STATUS)."
  echo "  The details are above, and in:"
  echo "  shorts_bot_data/shorts_bot.log"
  echo "==================================================="
fi

echo ""
read -n 1 -s -r -p "Press any key to close..."
