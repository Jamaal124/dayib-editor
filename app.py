from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_file
from dotenv import load_dotenv
import assemblyai as aai
import subprocess
import os
import json

load_dotenv()

app = Flask(__name__)
app.config['UPLOAD_FOLDER'] = 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024
app.secret_key = 'dayib-secret-2024'

aai.settings.api_key = os.getenv('ASSEMBLYAI_API_KEY')

PASSWORD = 'dayib2024'

def login_required(f):
    def wrapper(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        if request.form['password'] == PASSWORD:
            session['logged_in'] = True
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

        words = []
        filler_words = ['um', 'uh', 'like', 'you know', 'basically', 'literally', 'actually', 'right', 'so', 'okay', 'kind of', 'sort of']

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

@app.route('/export', methods=['POST'])
@login_required
def export_video():
    try:
        data = request.json
        filepath = data['filepath']
        removed_words = data['removed_words']
        add_captions = data.get('add_captions', False)
        words = data.get('words', [])

        if not removed_words:
            return jsonify({'error': 'No segments selected for removal'}), 400

        probe = subprocess.run([
            'ffprobe', '-v', 'quiet', '-print_format', 'json',
            '-show_format', filepath
        ], capture_output=True, text=True)

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
            return jsonify({'error': 'Nothing left to export'}), 400

        n = len(keep_segments)
        filter_parts = []

        for i, (start, end) in enumerate(keep_segments):
            filter_parts.append(f"[0:v]trim=start={start}:end={end},setpts=PTS-STARTPTS[v{i}];")
            filter_parts.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[a{i}];")

        video_concat = ''.join([f'[v{i}]' for i in range(n)])
        video_concat += f'concat=n={n}:v=1[outv];'

        if n == 1:
            audio_chain = '[a0]acopy[outa]'
        else:
            audio_chain = ''
            prev = 'a0'
            for i in range(1, n):
                out = f'ax{i}'
                audio_chain += f'[{prev}][a{i}]acrossfade=d=0.05[{out}];'
                prev = out
            audio_chain += f'[{prev}]acopy[outa]'

        filter_complex = ''.join(filter_parts) + video_concat + audio_chain

        cut_path = os.path.join(app.config['UPLOAD_FOLDER'], 'cut_' + os.path.basename(filepath))

        subprocess.run([
            'ffmpeg', '-y', '-i', filepath,
            '-filter_complex', filter_complex,
            '-map', '[outv]', '-map', '[outa]',
            cut_path
        ], check=True)

        if add_captions and words:
            # Build SRT subtitle file
            srt_path = os.path.join(app.config['UPLOAD_FOLDER'], 'captions.srt')
            
            # Adjust word timestamps based on removed segments
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

            with open(srt_path, 'w') as f:
                for i, word in enumerate(words):
                    adj_start = adjust_time(word['start'])
                    adj_end = adjust_time(word['end'])
                    if adj_end > adj_start:
                        f.write(f"{i+1}\n")
                        f.write(f"{ms_to_srt(adj_start)} --> {ms_to_srt(adj_end)}\n")
                        f.write(f"{word['text']}\n\n")

            output_path = os.path.join(app.config['UPLOAD_FOLDER'], 'edited_' + os.path.basename(filepath))
            
            subprocess.run([
                'ffmpeg', '-y', '-i', cut_path,
                '-vf', f"subtitles={srt_path}:force_style='FontSize=24,PrimaryColour=&H00c9a84c&,OutlineColour=&H00000000&,Outline=2,Alignment=2'",
                output_path
            ], check=True)
        else:
            output_path = cut_path

        return jsonify({'download_url': f'/download/{os.path.basename(output_path)}'})

    except Exception as e:
        print(f"EXPORT ERROR: {e}")
        return jsonify({'error': str(e)}), 500

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