# esiea-programmatic-video-pitch

Rendering engine for programmatic job-application videos (45 to 60 seconds).

A Google Sheet "control tower" driven by Google Apps Script prepares everything (job offer analysis, structured script, Google Slides, voice-over) and then calls this repository through the GitHub REST API (`workflow_dispatch`). The workflow never needs to be started by hand.

## What the workflow does

1. Reads the run manifest from the Apps Script Web App (scenes, slide images, voice-over files).
2. Uses the Gemini TTS voice-over stored in Google Drive, or falls back to `edge-tts` (free, no key).
3. Fits the total duration into the 45 to 60 second window (slight speed-up or longer last slide).
4. Builds the video with FFmpeg: slow zoom on each slide, cross-fades, burned-in subtitles synced with the voice.
5. Sends the MP4 and a GIF preview back to the Web App, which stores them in Google Drive, shares them by link and creates the Gmail draft.

## Files

```
render.py                     rendering script (Python 3.12 + FFmpeg)
requirements.txt              requests, edge-tts
.github/workflows/render.yml  workflow triggered by Apps Script
```

## Setup

Repository secrets (Settings > Secrets and variables > Actions > New repository secret):

| Secret | Value |
|---|---|
| `WEBAPP_URL` | Apps Script Web App URL (ends with `/exec`) |
| `WEBAPP_TOKEN` | Shared token shown by the sheet menu "1. Installer / réparer les onglets" |

Apps Script needs a fine-grained personal access token restricted to this repository with **Actions: Read and write**. It is stored in the sheet (CONFIDENTIEL tab, then moved to Script Properties), never in this repository.

## Privacy

No personal data is committed here. Slide images and audio only transit through the runner; the final MP4 goes back to Google Drive. A debug artifact is uploaded only when a run fails and is deleted after 3 days. Actions minutes are free and unlimited on a public repository; switch it to private if you prefer (a render uses about 3 of the 2,000 free monthly minutes).

## Local test

```
python render.py --local some_folder/
```

`some_folder/` must contain `manifest.json`, `scene_N.png` and `voix_scene_N.wav`.
