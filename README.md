# Dayib Editor

A web app that turns a pile of raw video clips into a finished, captioned edit — by
transcribing them, flagging the filler words and dead air, and letting you cut by
clicking on the transcript rather than scrubbing a timeline.

Built for a working content creator and in live use. Deployed on Railway behind a
password.

<!-- Add a screenshot or a short GIF of the editor here — for a UI project this is the
     single highest-value thing in the README. A 10-second GIF of selecting filler words
     and hitting export would do it. -->

---

## The problem

She records in sections rather than one long take — less costly when a sentence gets
fluffed. That leaves a folder of clips, each with a few seconds of dead air at the end
where she reaches over to stop the recording, and a scattering of "um"s throughout.
Stitching and cleaning that by hand in a conventional editor is slow, repetitive work.

Dayib Editor does the tedious part automatically and leaves the judgement calls to her.

## How it works

```
upload clips ──> transcribe each ──> trim trailing dead air ──> merge ──> transcribe merged
                                                                              │
                          edit in the browser  <───  words + silences  <───────┘
                                    │
                                    ▼
                    export job queued ──> ffmpeg ──> poll for progress ──> download
```

1. **Upload** one clip or several. Each is transcribed on its own first, purely to find
   where the last spoken word ends, so the trailing dead air can be trimmed before the
   clips are joined.
2. **Normalise and merge.** Every clip is re-encoded to a common resolution, codec and
   sample rate, then concatenated in upload order.
3. **Transcribe the merged video**, so word timings line up with the file the editor and
   the export will actually work on.
4. **Edit in the browser.** Filler words (`um`, `uh`, `like`, `basically`, …) are
   flagged automatically, as is any silence longer than 0.5 s. Select what to remove.
5. **Export** returns immediately with a job URL. The encode runs in a background
   worker; the job page polls for progress and offers the download when it's ready.
   Exports stay available for 7 days.

Optional on export: a title card with configurable colour, font and duration, and
burned-in captions.

## Engineering notes

The parts that were less obvious than they look:

**Exports don't block the request.** A long encode would time out an HTTP request, so
`POST /export` queues a job and returns `202` with a job URL straight away. Progress is
tracked per segment rather than by parsing ffmpeg's `-progress` output.

**The app must run as a single process.** Jobs live in an in-memory dict. Under multiple
gunicorn workers the job would be created in one process while the poll request landed
in another, producing a phantom "job not found". Hence `--workers 1 --threads 8` —
threads are fine here because the heavy lifting happens in ffmpeg subprocesses, so the
GIL isn't the bottleneck. The single `ThreadPoolExecutor` worker is also deliberate: it
serialises encodes so concurrent exports queue instead of fighting over CPU.

**Title and captions share one encode pass.** Applying each as its own full re-encode
would double the work and the quality loss. Both filters are built up and applied
together.

**Caption timings are re-mapped after cuts.** Removing segments shifts everything after
them earlier, so every caption timestamp is adjusted by the total duration removed
before it — otherwise subtitles drift further out of sync the further into the video you
get.

**Two different colour formats.** `drawtext` (the title) wants `0xRRGGBB`, while
`subtitles`/libass wants `&HBBGGRR` with the bytes reversed. Passing an HTML hex colour
straight into either one comes out with red and blue swapped.

**Titles are passed via `textfile=`, not `text=`.** An apostrophe or a colon in the title
breaks ffmpeg's filter escaping — "Abdi's Test" was enough to do it.

**Segment cuts use input seeking.** `-ss`/`-t` are placed before `-i`, so ffmpeg jumps
straight to the timestamp instead of decoding the whole file up to that point for every
segment. Still frame-accurate because the segments are re-encoded.

**ffmpeg's thread count is capped.** ffmpeg auto-detects the host machine's core count,
which oversubscribes the CPU inside a container with cgroup limits. `FFMPEG_THREADS`
overrides it.

## Stack

Python · Flask · AssemblyAI (speech-to-text) · ffmpeg / ffprobe · gunicorn · Docker ·
Railway

## Running locally

Requires **ffmpeg** and **ffprobe** on your PATH, plus the Liberation fonts if you want
the title card (`fonts-liberation` on Debian/Ubuntu; the Dockerfile installs both).

```bash
git clone https://github.com/Jamaal124/dayib-editor
cd dayib-editor

python -m venv venv
source venv/bin/activate         # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root:

```ini
SECRET_KEY=any-long-random-string
APP_PASSWORD=the-password-you-want-to-log-in-with
ASSEMBLYAI_API_KEY=your-assemblyai-key
FFMPEG_THREADS=4                 # optional
```

Then:

```bash
python app.py
```

Open <http://127.0.0.1:5000> and log in with `APP_PASSWORD`.

Get an AssemblyAI key at [assemblyai.com](https://www.assemblyai.com/) — the free tier is
enough to try this out.

### With Docker

```bash
docker build -t dayib-editor .
docker run -p 5000:5000 --env-file .env -e PORT=5000 dayib-editor
```

## Deployment

Runs on Railway from the included `Dockerfile`. The start command is pinned in the
image:

```
gunicorn --workers 1 --threads 8 --timeout 120 --bind 0.0.0.0:$PORT app:app
```

`--workers 1` is required, not a tuning choice — see the engineering notes above.
Set `SECRET_KEY`, `APP_PASSWORD` and `ASSEMBLYAI_API_KEY` as environment variables in
the Railway dashboard.

## Limitations

- **Jobs don't survive a restart.** The job store is in memory, so a redeploy loses
  in-flight exports. The job page degrades gracefully to an "expired" state rather than
  erroring. Persisting jobs to SQLite or Redis is the obvious next step.
- **Single shared password.** Fine for one user; a real multi-user version would need
  proper accounts.
- **Uploads are transcribed twice** in the multi-clip path — once per clip to find the
  trim point, then once on the merged file. That's a deliberate trade for correct
  timings, but it doubles the transcription cost.
- **Filler words are a fixed English list.** No per-user vocabulary, no other languages.

## Roadmap

- Persist the job store so exports survive redeploys
- Async upload/transcription (currently blocks the request)
- Subscription billing
