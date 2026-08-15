from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor
import assemblyai as aai
import subprocess
import threading
import time
import os
import json
import uuid

load_dotenv()
app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs('uploads', exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.secret_key = os.environ['SECRET_KEY']
PASSWORD = os.environ['APP_PASSWORD']
aai.settings.api_key = os.getenv('ASSEMBLYAI_API_KEY')


# Cap ffmpeg/x264 threads: ffmpeg auto-detects the HOST machine's cores,
# which oversubscribes the CPU on containerised hosts (e.g. Railway cgroup limits).
FFMPEG_THREADS = os.environ.get('FFMPEG_THREADS', '4')
def hex_to_ass_colour(hex_colour):
    """Convert an HTML hex colour (#RRGGBB) to ffmpeg's &HBBGGRR format.
    ffmpeg/libass store colour bytes reversed (blue-green-red), which is why
    a naive #RRGGBB passed straight in comes out with red and blue swapped.
    """
    h = hex_colour.lstrip('#')
    if len(h) != 6:
        h = 'ffffff'
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{b}{g}{r}".upper()

# Map the dropdown choices to fonts the Dockerfile actually installs
# (fonts-liberation package). Unknown values fall back to serif.
TITLE_FONTS = {
    'serif': '/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf',
    'sans': '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
    'mono': '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf',
}

# How long finished exports (and job records) stick around.
RETENTION_DAYS = 7
RETENTION_SECONDS = RETENTION_DAYS * 24 * 3600

# ---------------------------------------------------------------------------
# Job store + worker
#
# IMPORTANT: jobs live in this process's memory. The app MUST run as a single
# process (gunicorn: --workers 1 --threads 8). With multiple workers the job
# dict exists in one process while the poll request lands in another, and the
# job page shows a phantom "job not found". A server restart also forgets
# in-flight jobs; the job page handles unknown IDs gracefully.
# ---------------------------------------------------------------------------
jobs = {}
jobs_lock = threading.Lock()

# 1 worker is intentional: it serializes encodes so concurrent exports queue
# instead of fighting over CPU. Threads are fine here — the heavy work happens
# in ffmpeg subprocesses, so the GIL is irrelevant.
executor = ThreadPoolExecutor(max_workers=1)


def _update_job(job_id, **fields):
    with jobs_lock:
        if job_id in jobs:
            jobs[job_id].update(fields)


def cleanup_old():
    """Delete uploads older than the retention window and prune stale jobs.

    Called at the start of each POST /export (piggyback — no scheduler needed).
    """
    cutoff = time.time() - RETENTION_SECONDS
    folder = app.config['UPLOAD_FOLDER']
    try:
        names = os.listdir(folder)
    except OSError:
        names = []
    for name in names:
        path = os.path.join(folder, name)
        try:
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass
    with jobs_lock:
        stale = [jid for jid, j in jobs.items() if j['created'] < cutoff]
        for jid in stale:
            del jobs[jid]


def run_export(job_id, data):
    """The old body of export_video(), moved off the request thread.

    Instead of returning JSON responses it writes status/progress into
    jobs[job_id].
    """
    temp_files = []
    _update_job(job_id, status='processing', progress=0)
    try:
        filepath = data['filepath']
        removed_words = data['removed_words']
        add_captions = data.get('add_captions', False)
        words = data.get('words', [])

        probe = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', filepath],
            capture_output=True, text=True
        )
        duration = float(json.loads(probe.stdout)['format']['duration'])

        removed_words.sort(key=lambda x: x['start'])
        keep_segments = []
        current = 0.0
        for seg in removed_words:
            start = seg['start'] / 1000
            end = seg['end'] / 1000
            if current < start:
                keep_segments.append((current, start))
            current = end
        if current < duration:
            keep_segments.append((current, duration))

        if not keep_segments:
            _update_job(job_id, status='failed', error='Nothing left to export')
            return

        # Cut each keep-segment. -ss/-t BEFORE -i = input seeking: ffmpeg jumps
        # straight to the timestamp (still frame-accurate when re-encoding)
        # instead of decoding the entire file up to that point for every segment.
        segment_files = []
        total_steps = len(keep_segments) + 1  # reserve the final chunk for concat + filter pass
        for i, (start, end) in enumerate(keep_segments):
            seg_path = os.path.join(app.config['UPLOAD_FOLDER'], f'seg_{job_id}_{i}.mp4')
            temp_files.append(seg_path)
            subprocess.run([
                'ffmpeg', '-y',
                '-ss', f'{start:.3f}', '-t', f'{end - start:.3f}',
                '-i', filepath,
                '-vf', 'scale=1280:-2',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                '-c:a', 'aac',
                '-threads', FFMPEG_THREADS,
                '-avoid_negative_ts', 'make_zero',
                seg_path
            ], check=True, capture_output=True)
            segment_files.append(seg_path)
            # Segment counting — don't bother parsing ffmpeg -progress output.
            _update_job(job_id, progress=int((i + 1) / total_steps * 100))

        list_path = os.path.join(app.config['UPLOAD_FOLDER'], f'segments_{job_id}.txt')
        temp_files.append(list_path)
        with open(list_path, 'w') as f:
            for seg_path in segment_files:
                f.write(f"file '{os.path.abspath(seg_path)}'\n")

        cut_path = os.path.join(app.config['UPLOAD_FOLDER'], f'cut_{job_id}_' + os.path.basename(filepath) + '.mp4')
        subprocess.run([
            'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
            '-i', list_path, '-c', 'copy', cut_path
        ], check=True, capture_output=True)

        # Build the optional title and caption filters, then apply them together
        # in ONE encode pass instead of a separate full re-encode for each.
        filters = []

        title_text = data.get('title_text', '')
        title_duration = int(data.get('title_duration', 3))
        if title_text:
            title_colour = hex_to_ass_colour(data.get('title_colour', '#ffffff'))
            title_font = TITLE_FONTS.get(data.get('title_font', 'serif'), TITLE_FONTS['serif'])
            # textfile= instead of text=: avoids ffmpeg filter-escaping bugs with
            # apostrophes/commas/colons in the title (e.g. "Abdi's Test").
            title_path = os.path.join(app.config['UPLOAD_FOLDER'], f'title_{job_id}.txt')
            temp_files.append(title_path)
            with open(title_path, 'w') as f:
                f.write(title_text)
            filters.append(
                f"drawtext=fontfile='{title_font}':textfile='{title_path}':fontcolor={title_colour}:fontsize=48:x=(w-text_w)/2:y=h/4:enable='lte(t,{title_duration})'"
            )

        if add_captions and words:
            srt_path = os.path.join(app.config['UPLOAD_FOLDER'], f'captions_{job_id}.srt')
            temp_files.append(srt_path)
            removed_sorted = sorted(removed_words, key=lambda x: x['start'])

            def adjust_time(ms):
                adjusted = ms
                for seg in removed_sorted:
                    if ms > seg['end']:
                        adjusted -= (seg['end'] - seg['start'])
                    elif ms > seg['start']:
                        adjusted = seg['start']
                return max(0, adjusted)

            def ms_to_srt(ms):
                ms = int(ms)
                h = ms // 3600000
                m = (ms % 3600000) // 60000
                s = (ms % 60000) // 1000
                ms = ms % 1000
                return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

            chunks = []
            chunk_size = 4
            for i in range(0, len(words), chunk_size):
                chunk = words[i:i + chunk_size]
                chunk_start = adjust_time(chunk[0]['start'])
                chunk_end = adjust_time(chunk[-1]['end'])
                chunk_text = ' '.join(w['text'] for w in chunk)
                if chunk_end > chunk_start:
                    chunks.append((chunk_start, chunk_end, chunk_text))

            with open(srt_path, 'w') as f:
                for i, (start, end, text) in enumerate(chunks):
                    f.write(f"{i+1}\n")
                    f.write(f"{ms_to_srt(start)} --> {ms_to_srt(end)}\n")
                    f.write(f"{text}\n\n")

            srt_escaped = srt_path.replace('\\', '/').replace(':', '\\:')
            filters.append(
                f"subtitles='{srt_escaped}':force_style='FontName=Liberation Serif,FontSize=16,PrimaryColour=&H00FFFFFF&,OutlineColour=&H00000000&,Outline=1,Shadow=1,Alignment=2,MarginV=30,Spacing=0.5'"
            )

        if filters:
            output_path = os.path.join(app.config['UPLOAD_FOLDER'], f'edited_{job_id}_' + os.path.basename(filepath) + '.mp4')
            temp_files.append(cut_path)
            subprocess.run([
                'ffmpeg', '-y', '-i', cut_path,
                '-vf', ','.join(filters),
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '23',
                '-c:a', 'aac',
                '-threads', FFMPEG_THREADS,
                output_path
            ], check=True, capture_output=True)
        else:
            output_path = cut_path

        _update_job(
            job_id,
            status='done',
            progress=100,
            download_url=f'/download/{os.path.basename(output_path)}'
        )

    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors='ignore') if e.stderr else str(e)
        print(f"EXPORT FFMPEG ERROR: {stderr}")
        _update_job(job_id, status='failed', error=f'ffmpeg failed: {stderr[-500:]}')
    except Exception as e:
        print(f"EXPORT ERROR: {e}")
        _update_job(job_id, status='failed', error=str(e))
    finally:
        # Always clean up intermediate files, even when export fails.
        for path in temp_files:
            try:
                os.remove(path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def login_required(f):
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            # Remember where the user was headed so a bookmarked /jobs/<id>
            # link survives the login bounce.
            return redirect(url_for('login', next=request.path))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['password'] == PASSWORD:
            session['logged_in'] = True
            next_url = request.form.get('next', '')
            # Open-redirect guard: only follow same-site paths.
            if next_url.startswith('/') and not next_url.startswith('//'):
                return redirect(next_url)
            return redirect(url_for('index'))
        else:
            error = 'Incorrect password'
    return render_template('login.html', error=error)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    return render_template('index.html')


# ---------------------------------------------------------------------------
# Upload (unchanged — async conversion is a planned follow-up)
# ---------------------------------------------------------------------------

@app.route('/upload', methods=['POST'])
@login_required
def upload_video():
    try:
        if 'video' not in request.files:
            return jsonify({'error': 'No video uploaded'}), 400
        file = request.files['video']
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400

        filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
        file.save(filepath)

        config = aai.TranscriptionConfig(punctuate=True, format_text=True)
        transcriber = aai.Transcriber()
        transcript = transcriber.transcribe(filepath, config=config)

        if transcript.status == aai.TranscriptStatus.error:
            return jsonify({'error': 'Transcription failed'}), 500

        filler_words = ['um', 'uh', 'like', 'you know', 'basically', 'literally',
                        'actually', 'right', 'so', 'okay', 'kind of', 'sort of']

        words = []
        for word in transcript.words:
            words.append({
                'text': word.text,
                'start': word.start,
                'end': word.end,
                'is_filler': word.text.lower().strip('.,!?') in filler_words
            })

        silences = []
        for i in range(len(transcript.words) - 1):
            current_end = transcript.words[i].end
            next_start = transcript.words[i + 1].start
            gap = (next_start - current_end) / 1000
            if gap > 0.8:
                silences.append({
                    'start': current_end,
                    'end': next_start,
                    'duration': round(gap, 2)
                })

        return jsonify({
            'message': 'Video uploaded and transcribed successfully',
            'filepath': filepath,
            'transcript': transcript.text,
            'words': words,
            'silences': silences
        })
    except Exception as e:
        print(f"ERROR: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Export: returns instantly with a job URL; the encode runs in the worker.
# ---------------------------------------------------------------------------

@app.route('/export', methods=['POST'])
@login_required
def export_video():
    data = request.json
    if not data or not data.get('filepath'):
        return jsonify({'error': 'No file to export'}), 400
    if not data.get('removed_words'):
        return jsonify({'error': 'No segments selected for removal'}), 400

    cleanup_old()

    job_id = uuid.uuid4().hex[:8]
    now = time.time()
    with jobs_lock:
        jobs[job_id] = {
            'status': 'queued',
            'progress': 0,
            'download_url': None,
            'error': None,
            'created': now,
        }
    executor.submit(run_export, job_id, data)

    return jsonify({'job_id': job_id, 'job_url': f'/jobs/{job_id}'}), 202


@app.route('/jobs/<job_id>')
@login_required
def job_page(job_id):
    # Renders for unknown IDs too — the page shows a friendly "expired" state.
    return render_template('job.html', job_id=job_id)


@app.route('/api/jobs/<job_id>')
@login_required
def job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if job is None:
            return jsonify({'error': 'unknown job'}), 404
        payload = dict(job)
    payload['available_until'] = time.strftime(
        '%d %b %Y', time.localtime(payload['created'] + RETENTION_SECONDS)
    )
    return jsonify(payload)


@app.route('/download/<filename>')
@login_required
def download(filename):
    return send_file(
        os.path.join(app.config['UPLOAD_FOLDER'], filename),
        as_attachment=True
    )


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)