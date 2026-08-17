import subprocess
import os


def get_last_word_end_time(transcript_json, buffer_seconds=0.2):
    """Return the timestamp (seconds) to trim a clip to, based on the last
    spoken word from AssemblyAI plus a small buffer. None if no speech."""
    words = transcript_json.get("words", [])
    if not words:
        return None
    return (words[-1]["end"] / 1000) + buffer_seconds


def normalize_clip(input_path, output_path, width=1080, height=1920, fps=30):
    """Re-encode a clip to a common resolution/fps so clips from different
    sources concatenate cleanly."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
               f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
        "-r", str(fps), "-c:v", "libx264", "-c:a", "aac", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg normalize failed: {result.stderr}")
    return output_path


def trim_clip(input_path, output_path, trim_at_seconds):
    """Cut a clip at trim_at_seconds, dropping the dead air after the last word."""
    cmd = [
        "ffmpeg", "-y", "-i", input_path, "-t", str(trim_at_seconds),
        "-c:v", "libx264", "-c:a", "aac", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg trim failed: {result.stderr}")
    return output_path


def merge_clips(clip_paths, output_path):
    """Concatenate already-normalized clips into one video."""
    list_file = os.path.join(os.path.dirname(output_path), "concat_list.txt")
    with open(list_file, "w") as f:
        for path in clip_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", list_file, "-c", "copy", output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg merge failed: {result.stderr}")
    os.remove(list_file)
    return output_path


def process_multi_clip_job(clip_paths, job_id, temp_dir, transcribe_fn):
    """Transcribe each clip -> trim trailing dead air -> normalize -> merge.
    Returns the path to the combined, cleaned video.

    transcribe_fn: your existing AssemblyAI function; takes a clip path,
    returns transcript JSON containing a 'words' list with 'end' times (ms)."""
    processed = []
    for i, clip_path in enumerate(clip_paths):
        transcript = transcribe_fn(clip_path)
        trim_at = get_last_word_end_time(transcript)

        norm_path = os.path.join(temp_dir, f"{job_id}_norm_{i}.mp4")
        normalize_clip(clip_path, norm_path)

        if trim_at:
            trimmed_path = os.path.join(temp_dir, f"{job_id}_trim_{i}.mp4")
            trim_clip(norm_path, trimmed_path, trim_at)
            processed.append(trimmed_path)
        else:
            processed.append(norm_path)

    merged_path = os.path.join(temp_dir, f"{job_id}_merged.mp4")
    merge_clips(processed, merged_path)
    return merged_path