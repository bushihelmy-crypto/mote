---
name: ffmpeg
description: >-
  Process audio and video with the official ffmpeg/ffprobe tools — convert,
  trim, concatenate, scale, extract frames/audio, apply filters, and inspect
  media. Deterministic, scriptable media manipulation without a GUI.
when_to_use: >-
  When you need to transcode, cut, join, resize, extract, or otherwise process
  audio/video files, or inspect media metadata. Run the official ffmpeg/ffprobe
  via the Bash tool.
---

# FFmpeg audio/video processing (official CLI, via Bash)

FFmpeg is the standard tool for media processing. Use the **official `ffmpeg`
and `ffprobe` binaries** directly — every operation is one command.

## Prerequisites

- ffmpeg installed: `ffmpeg -version` (bundles `ffprobe`)
- Install: `apt install ffmpeg` / `brew install ffmpeg` / `dnf install ffmpeg`

## Inspect first (ffprobe — cheap introspection)

Always probe unknown media before processing so you know codecs/duration/size:

```bash
# Human-readable summary
ffprobe -hide_banner input.mp4

# Machine-readable JSON (preferred for agents)
ffprobe -v quiet -print_format json -show_format -show_streams input.mp4

# Just the duration in seconds
ffprobe -v quiet -show_entries format=duration -of csv=p=0 input.mp4
```

## Common operations

```bash
# Transcode / change container
ffmpeg -i input.mov -c:v libx264 -c:a aac output.mp4

# Trim (fast, no re-encode): from 10s, length 15s
ffmpeg -ss 00:00:10 -i input.mp4 -t 15 -c copy clip.mp4

# Extract audio
ffmpeg -i input.mp4 -vn -c:a libmp3lame audio.mp3

# Extract a single frame at 5s
ffmpeg -ss 5 -i input.mp4 -frames:v 1 frame.png

# Extract frames at 1 fps
ffmpeg -i input.mp4 -vf fps=1 frames/frame_%04d.png

# Scale/resize (keep aspect: width 1280, height auto)
ffmpeg -i input.mp4 -vf scale=1280:-2 out_720p.mp4

# Change frame rate
ffmpeg -i input.mp4 -r 30 out_30fps.mp4

# Mux/replace audio track
ffmpeg -i video.mp4 -i audio.mp3 -c:v copy -map 0:v:0 -map 1:a:0 -shortest out.mp4

# GIF from a clip
ffmpeg -ss 2 -t 3 -i input.mp4 -vf "fps=12,scale=480:-1" out.gif
```

## Concatenate

Same codec/params — use the concat demuxer (no re-encode):
```bash
printf "file '%s'\n" /abs/a.mp4 /abs/b.mp4 > list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy joined.mp4
```
Different codecs — re-encode via the concat filter:
```bash
ffmpeg -i a.mp4 -i b.mp4 -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" -map "[v]" -map "[a]" joined.mp4
```

## Filters (fade, overlay, crop, etc.)

```bash
# Fade in first 1s, fade out last 1s of a 10s clip
ffmpeg -i in.mp4 -vf "fade=t=in:st=0:d=1,fade=t=out:st=9:d=1" out.mp4

# Overlay (picture-in-picture) top-right
ffmpeg -i main.mp4 -i pip.mp4 -filter_complex "[1]scale=320:-1[p];[0][p]overlay=W-w-10:10" out.mp4

# Crop WxH from (x,y)
ffmpeg -i in.mp4 -vf "crop=640:480:0:0" out.mp4
```

## Agent guidance

1. **Probe with ffprobe (JSON) before processing** — know the codecs, duration,
   resolution, and stream layout so your command matches the input.
2. Use `-c copy` (stream copy) for trim/concat when codecs match — it's near
   instant and lossless; only re-encode when you actually change the content.
3. **Verify the output**: check the file exists, size > 0, and re-`ffprobe` it to
   confirm duration/streams are as expected. "Ran without error" is not enough —
   silent effect drops are common (e.g. a filter applied to the wrong stream).
4. Put `-ss` before `-i` for fast seeking on trims; after `-i` for frame-accurate
   (slower) seeking.
5. Use absolute paths and add `-y` to overwrite or `-n` to never overwrite,
   rather than relying on interactive prompts.
6. Add `-hide_banner` (and `-v quiet` for ffprobe JSON) to keep output clean for
   parsing.
