# SPDX-License-Identifier: GPL-2.0-or-later
"""Reads one video and records how much each frame differs from the one before.

Blender starts this file as a separate process, so reading a long video never makes
Blender stutter, it can be stopped at once, and a crash in the video library cannot take
Blender down. It does not import bpy.

    python -s worker.py <libraries folder> <video> <result file> <fps> <first frame> <end frame>

Progress and errors go to standard output, one JSON object per line. The scores are
written to the result file at the end.
"""
import json
import sys
import threading


def say(**message):
    print(json.dumps(message), flush=True)


def frame_count(timecode):
    number = getattr(timecode, "frame_num", None)
    return int(number if number is not None else timecode.get_frames())


def main():
    libraries, video_path, result_path, fps, first, end = sys.argv[1:7]
    fps, first, end = float(fps), int(first), int(end)
    sys.path.append(libraries)          # last: Blender's own numpy is found first, as inside Blender

    from scenedetect import ContentDetector, FrameTimecode, SceneManager, StatsManager, open_video

    video = open_video(video_path, framerate=fps)
    total = frame_count(video.duration)
    first = max(0, min(first, total - 1))
    end = max(first + 1, min(end, total))

    stats = StatsManager()
    manager = SceneManager(stats)
    manager.add_detector(ContentDetector())      # its threshold is not used; only the scores are kept

    finished = threading.Event()

    def report_progress():
        while not finished.wait(0.25):
            try:
                say(frame=int(video.frame_number), first=first, end=end)
            except Exception:
                pass

    threading.Thread(target=report_progress, daemon=True).start()
    if first > 0:
        video.seek(FrameTimecode(timecode=first, fps=fps))
    manager.detect_scenes(video, end_time=FrameTimecode(timecode=end, fps=fps))
    finished.set()

    key = ["content_val"]
    scores = [round(float(stats.get_metrics(f, key)[0]), 2) if stats.metrics_exist(f, key) else 0.0
              for f in range(first, end)]
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump({"first": first, "scores": scores}, f)
    say(done=True, frames=len(scores))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        say(error=f"{type(e).__name__}: {e}")
        sys.exit(1)
