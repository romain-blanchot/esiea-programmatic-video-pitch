#!/usr/bin/env python3
"""
Moteur de rendu vidéo programmatique (exécuté par GitHub Actions, déclenché par Apps Script).

1. Lit le manifest du run via la Web App Apps Script (scènes, images PNG des slides, voix off).
2. Voix off : WAV Gemini TTS déjà dans Drive, sinon repli edge-tts (gratuit, sans clé).
3. Calibre la durée totale dans la fenêtre 45-60 s (accélération légère ou maintien de fin).
4. Assemble avec FFmpeg : léger zoom (Ken Burns), fondus enchaînés entre scènes, sous-titres incrustés
   synchronisés sur la voix de chaque scène, piste audio recalée sur la timeline vidéo.
5. Renvoie le MP4 + un GIF d'aperçu à la Web App (stockage Drive, partage, brouillon Gmail).

Usage : WEBAPP_URL=... WEBAPP_TOKEN=... python render.py RUN_ID
Test local sans réseau : python render.py --local dossier_avec_manifest.json
"""
import asyncio
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import wave

import requests

RATE = 24000          # fréquence de la piste audio de travail (mono 16 bits)
FADE = 0.5            # durée des fondus enchaînés (s)
LEAD_IN = 0.25        # silence avant la voix de chaque scène (s)
TAIL = 0.45           # respiration après la voix de chaque scène (s)
END_HOLD = 1.2        # maintien de la dernière slide (s)
MAX_TEMPO = 1.12      # accélération maximale acceptable de la voix


def sh(cmd):
    """Lance une commande et lève une erreur lisible en cas d'échec."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("Commande en échec : %s\n%s" % (" ".join(cmd[:6]), r.stderr[-1500:]))
    return r.stdout


def wav_duration(path):
    with wave.open(path, "rb") as w:
        return w.getnframes() / float(w.getframerate())


def to_work_wav(src, dst, tempo=1.0):
    """Convertit n'importe quel audio en WAV mono 24 kHz 16 bits (avec tempo éventuel)."""
    af = ["-af", "atempo=%.4f" % tempo] if abs(tempo - 1.0) > 1e-3 else []
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", src] + af + ["-ac", "1", "-ar", str(RATE), "-sample_fmt", "s16", dst])


# ---------------------------------------------------------------- accès Web App
class WebApp:
    def __init__(self, url, token, run_id):
        self.url, self.token, self.run_id = url, token, run_id

    def get(self, **params):
        params.update(token=self.token, run_id=self.run_id)
        r = requests.get(self.url, params=params, timeout=120)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError("Web App : %s" % data.get("error"))
        return data

    def manifest(self):
        return self.get(action="manifest")["manifest"]

    def download(self, file_id, dst):
        f = self.get(action="file", id=file_id)["file"]
        with open(dst, "wb") as fh:
            fh.write(base64.b64decode(f["b64"]))
        return dst

    def post(self, payload):
        payload.update(token=self.token, run_id=self.run_id)
        r = requests.post(self.url, data=json.dumps(payload), headers={"Content-Type": "text/plain"}, timeout=300)
        r.raise_for_status()
        return r.json()


class LocalFiles:
    """Mode test : le dossier contient manifest.json, scene_N.png et voix_scene_N.wav."""

    def __init__(self, folder):
        self.folder = folder

    def manifest(self):
        with open(os.path.join(self.folder, "manifest.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def download(self, file_id, dst):
        import shutil
        shutil.copy(os.path.join(self.folder, file_id), dst)
        return dst

    def post(self, payload):
        print("POST simulé :", {k: (v if len(str(v)) < 80 else "%d octets" % len(str(v))) for k, v in payload.items()})
        return {"ok": True}


# ---------------------------------------------------------------- voix off
async def _edge(text, voice, dst):
    import edge_tts
    await edge_tts.Communicate(text, voice, rate="+5%").save(dst)


def prepare_audio(m, src, work):
    """Renvoie la liste des WAV de travail par scène (téléchargés ou générés)."""
    tts = m.get("tts") or {}
    out = []
    for sc in m["scenes"]:
        n = sc["scene_id"]
        raw = os.path.join(work, "raw_%d" % n)
        if tts.get("mode") == "GEMINI" and sc.get("audio_file_id"):
            src.download(sc["audio_file_id"], raw + ".wav")
            raw += ".wav"
        else:
            asyncio.run(_edge(sc["texte"], tts.get("voix_edge") or "fr-FR-HenriNeural", raw + ".mp3"))
            raw += ".mp3"
        dst = os.path.join(work, "voix_%d.wav" % n)
        to_work_wav(raw, dst)
        out.append(dst)
    return out


def fit_durations(m, wavs, work):
    """Calibre la durée totale dans [duree_min, duree_max] ; renvoie (wavs, durées voix, maintien final)."""
    vmin = float(m["video"].get("duree_min_sec", 45))
    vmax = float(m["video"].get("duree_max_sec", 60))
    n = len(wavs)
    overhead = n * (LEAD_IN + TAIL) + END_HOLD - (n - 1) * FADE
    voice = [wav_duration(w) for w in wavs]
    total = sum(voice) + overhead
    tempo = 1.0
    if total > vmax - 0.3:
        tempo = min(MAX_TEMPO, sum(voice) / (vmax - 0.3 - overhead))
        if tempo > 1.0:
            fitted = []
            for i, w in enumerate(wavs):
                dst = os.path.join(work, "voix_fit_%d.wav" % (i + 1))
                to_work_wav(w, dst, tempo)
                fitted.append(dst)
            wavs = fitted
            voice = [wav_duration(w) for w in wavs]
    total = sum(voice) + overhead
    end_hold = END_HOLD + max(0.0, vmin - total)
    return wavs, voice, end_hold, tempo


# ---------------------------------------------------------------- sous-titres
def srt_time(t):
    ms = int(round(t * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, (ms // 60000) % 60, (ms // 1000) % 60, ms % 1000)


def chunk_text(text, max_words=9):
    """Découpe la voix off en blocs courts, en coupant de préférence sur la ponctuation."""
    words = text.split()
    chunks, cur = [], []
    for w in words:
        cur.append(w)
        if len(cur) >= max_words or (len(cur) >= 4 and re.search(r"[.,;:!?]$", w)):
            chunks.append(" ".join(cur))
            cur = []
    if cur:
        if chunks and len(cur) <= 2:
            chunks[-1] += " " + " ".join(cur)
        else:
            chunks.append(" ".join(cur))
    return chunks


def build_srt(scenes, starts, voice, path):
    idx, lines = 1, []
    for sc, t0, d in zip(scenes, starts, voice):
        chunks = chunk_text(sc["texte"])
        weights = [max(1, len(c)) for c in chunks]
        tot = float(sum(weights))
        t = t0
        for c, wgt in zip(chunks, weights):
            dt = d * wgt / tot
            lines += [str(idx), "%s --> %s" % (srt_time(t), srt_time(t + dt - 0.04)), c, ""]
            idx += 1
            t += dt
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


# ---------------------------------------------------------------- assemblage
def compose_audio(wavs, starts, total, path):
    """Place chaque voix à l'instant de début de sa scène sur une piste silencieuse."""
    buf = bytearray(int(total * RATE) * 2)
    for w, t in zip(wavs, starts):
        with wave.open(w, "rb") as fh:
            data = fh.readframes(fh.getnframes())
        off = int(t * RATE) * 2
        buf[off:off + len(data)] = data[: max(0, len(buf) - off)]
    with wave.open(path, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(buf))


def render(src, workdir, out_mp4, out_gif, crf=24):
    m = src.manifest()
    W, H = int(m["video"]["width"]), int(m["video"]["height"])
    scenes = m["scenes"]
    images = [src.download(sc["image_file_id"], os.path.join(workdir, "scene_%d.png" % sc["scene_id"])) for sc in scenes]
    wavs = prepare_audio(m, src, workdir)
    wavs, voice, end_hold, tempo = fit_durations(m, wavs, workdir)

    # Durée de chaque segment vidéo et instant de début (les fondus se chevauchent).
    n = len(scenes)
    seg = [LEAD_IN + v + TAIL + (FADE if i < n - 1 else end_hold) for i, v in enumerate(voice)]
    starts_video = []
    t = 0.0
    for i in range(n):
        starts_video.append(t)
        t += seg[i] - FADE
    total = starts_video[-1] + seg[-1]
    voice_starts = [s + LEAD_IN for s in starts_video]

    audio = os.path.join(workdir, "piste.wav")
    compose_audio(wavs, voice_starts, total, audio)
    srt = os.path.join(workdir, "sous_titres.srt")
    build_srt(scenes, voice_starts, voice, srt)

    fps = 25
    inputs, filters = [], []
    for i, img in enumerate(images):
        inputs += ["-loop", "1", "-t", "%.3f" % seg[i], "-i", img]
        frames = int(seg[i] * fps) + 1
        filters.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=white,"
            "scale=%d:%d,zoompan=z='min(zoom+0.00035,1.05)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d,"
            "setsar=1,format=yuv420p[v%d]" % (i, W, H, W, H, W * 2, H * 2, frames, W, H, fps, i)
        )
    last = "v0"
    for i in range(1, n):
        offset = starts_video[i]
        filters.append("[%s][v%d]xfade=transition=fade:duration=%.2f:offset=%.3f[x%d]" % (last, i, FADE, offset, i))
        last = "x%d" % i
    if m["video"].get("sous_titres", True):
        style = ("FontName=DejaVu Sans,FontSize=13,PrimaryColour=&H00FFFFFF,BackColour=&H99000000,"
                 "BorderStyle=3,Outline=1,Shadow=0,MarginV=18,Alignment=2")
        srt_esc = srt.replace("\\", "/").replace(":", "\\:")
        filters.append("[%s]subtitles='%s':force_style='%s'[vout]" % (last, srt_esc, style))
    else:
        filters.append("[%s]null[vout]" % last)

    cmd = ["ffmpeg", "-y", "-loglevel", "error"] + inputs + ["-i", audio,
           "-filter_complex", ";".join(filters), "-map", "[vout]", "-map", "%d:a" % n,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-r", str(fps),
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-t", "%.3f" % total, out_mp4]
    sh(cmd)
    if os.path.getsize(out_mp4) > 18 * 1024 * 1024 and W > 1280:
        small = out_mp4.replace(".mp4", "_720.mp4")
        sh(["ffmpeg", "-y", "-loglevel", "error", "-i", out_mp4, "-vf", "scale=1280:720", "-c:v", "libx264",
            "-crf", "26", "-preset", "veryfast", "-c:a", "copy", "-movflags", "+faststart", small])
        os.replace(small, out_mp4)
    sh(["ffmpeg", "-y", "-loglevel", "error", "-t", "4.5", "-i", out_mp4, "-vf",
        "fps=10,scale=480:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse", out_gif])

    mp3 = os.path.join(workdir, "voix_off_complete.mp3")
    sh(["ffmpeg", "-y", "-loglevel", "error", "-i", audio, "-b:a", "96k", mp3])
    info = "Rendu %dx%d, %d scènes, %.1f s, tempo voix x%.2f, voix %s" % (
        W, H, n, total, tempo, (m.get("tts") or {}).get("mode", "EDGE"))
    return total, mp3, info, (m.get("tts") or {}).get("mode") != "GEMINI"


def b64(path):
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode()


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--local":
        src = LocalFiles(sys.argv[2])
        outdir = sys.argv[2]
    else:
        run_id = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("RUN_ID", "")
        url, token = os.environ.get("WEBAPP_URL", ""), os.environ.get("WEBAPP_TOKEN", "")
        if not url or not token:
            sys.exit("Secrets WEBAPP_URL / WEBAPP_TOKEN manquants dans le dépôt (Settings > Secrets and variables > Actions).")
        src = WebApp(url, token, run_id)
        outdir = os.environ.get("OUT_DIR", "out")
    os.makedirs(outdir, exist_ok=True)
    mp4, gif = os.path.join(outdir, "pitch.mp4"), os.path.join(outdir, "apercu.gif")
    try:
        with tempfile.TemporaryDirectory() as work:
            total, mp3, info, send_audio = render(src, work, mp4, gif)
            payload = {"action": "render_done", "mp4_b64": b64(mp4), "gif_b64": b64(gif),
                       "duration": round(total, 2), "log": info}
            if send_audio:
                payload["audio_b64"] = b64(mp3)
            print(info)
            resp = src.post(payload)
            print("Réponse Web App :", resp)
            if not resp.get("ok"):
                raise RuntimeError("La Web App a refusé le rendu : %s" % resp.get("error"))
    except Exception as exc:  # le statut ERREUR remonte dans la Sheet
        print("ERREUR :", exc, file=sys.stderr)
        try:
            src.post({"action": "render_done", "error": str(exc)[:1500]})
        finally:
            sys.exit(1)


if __name__ == "__main__":
    main()
