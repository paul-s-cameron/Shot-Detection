# SPDX-License-Identifier: GPL-2.0-or-later
#
# Shot Detection: find the cuts in movie strips and split them.
#
# A rewrite of "Detect Shots and Split Strips" by tintwotin
# (https://github.com/tin2tin/Shot_Detection). Detection is done by PySceneDetect
# by Brandon Castellano (https://www.scenedetect.com).
#
# How it works: each clip is read once, by worker.py in a separate process, which
# records a score per frame (how much the picture changed from the frame before).
# The cuts are then worked out from those scores, so the threshold and the minimum
# scene length can be changed without reading the video again.
#
# The add-on's name, version and other details are in blender_manifest.toml.
# Its panel is in Sequencer > Side Bar > Shots tab.

import hashlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from glob import glob

import bpy
from bpy.props import BoolProperty, EnumProperty, FloatProperty, IntProperty, PointerProperty
from bpy.types import Operator, Panel, PropertyGroup

from .libs import Libraries

TAB = "Shots"
MARKER_PREFIX = "cut "                  # timeline markers this add-on owns start with this
SIDECAR_SUFFIX = ".shots.json"          # score file written beside a video
LIBS = Libraries(__package__, ["scenedetect[opencv]==0.7"], probe="scenedetect")

_scores = {}        # analysis key -> {"first": source frame of scores[0], "scores": [float]}
_counts = {}        # (key, first, end, threshold, min length) -> number of cuts, for the panel
_queue = None       # the running analysis, see start_queue()
_note = ""          # result of the last analysis, for the panel


# ---------------------------------------------------------------------------
# Strips
# ---------------------------------------------------------------------------

# Blender 5.1 renamed these strip properties and says the old names go in 6.0.
_RENAMED = {
    "frame_offset_start": "left_handle_offset",
    "frame_offset_end": "right_handle_offset",
    "frame_duration": "content_duration",
    "frame_start": "content_start",
    "frame_final_start": "left_handle",
    "frame_final_end": "right_handle",
}


def sprop(strip, old_name):
    new_name = _RENAMED[old_name]
    return getattr(strip, new_name) if hasattr(strip, new_name) else getattr(strip, old_name)


def seq_scene(context=None):
    """Scene the Sequencer is editing (Blender 5.0+ has a separate sequencer scene)."""
    ctx = context or bpy.context
    return getattr(ctx, "sequencer_scene", None) or ctx.scene


def all_strips(scene):
    se = scene.sequence_editor
    return list(se.strips_all) if se else []


def selected_movies(scene):
    return [s for s in all_strips(scene) if s.select and s.type == "MOVIE"]


def scene_fps(scene):
    return round(scene.render.fps / scene.render.fps_base, 3)


def used_range(strip):
    """(first, end) source frames the strip shows; end is one past the last."""
    first = int(round(sprop(strip, "frame_offset_start")))
    end = int(round(sprop(strip, "frame_duration") - sprop(strip, "frame_offset_end")))
    return first, max(first + 1, end)


# ---------------------------------------------------------------------------
# Which file to read: the source, or one of Blender's proxies
# ---------------------------------------------------------------------------

MEDIA_ITEMS = (
    ("RAW", "Source File", "Read the original media file"),
    ("AUTO", "Preview Proxy", "Read the proxy size the Sequencer preview is set to, if it exists"),
    ("PROXY_25", "25% Proxy", "Read Blender's 25% proxy if it exists, otherwise the source file"),
    ("PROXY_50", "50% Proxy", "Read Blender's 50% proxy if it exists, otherwise the source file"),
    ("PROXY_75", "75% Proxy", "Read Blender's 75% proxy if it exists, otherwise the source file"),
    ("PROXY_100", "100% Proxy", "Read Blender's 100% proxy if it exists, otherwise the source file"),
)
PROXY_PERCENT = {"PROXY_25": 25, "PROXY_50": 50, "PROXY_75": 75, "PROXY_100": 100}


def real_path(path):
    return os.path.realpath(bpy.path.abspath(path)) if path else ""


def preview_proxy_size(context):
    """The proxy size shown in a Sequencer preview, or RAW."""
    areas = [context.area] if getattr(context, "area", None) else []
    screen = getattr(context, "screen", None)
    areas += list(screen.areas) if screen else []
    for area in areas:
        if area and area.type == "SEQUENCE_EDITOR":
            size = getattr(area.spaces.active, "proxy_render_size", "SCENE")
            if size in PROXY_PERCENT:
                return size
    return "RAW"


def find_proxy_file(proxy_dir, clip_name, percent):
    if not proxy_dir or not os.path.isdir(proxy_dir):
        return ""
    stem = os.path.splitext(clip_name)[0]
    for folder in (proxy_dir, os.path.join(proxy_dir, clip_name), os.path.join(proxy_dir, stem)):
        exact = os.path.join(folder, f"proxy_{percent}.avi")
        if os.path.isfile(exact):
            return exact
        for path in sorted(glob(os.path.join(folder, f"proxy_{percent}*.avi"))):
            if not path.endswith("_part.avi") and os.path.isfile(path):
                return path
    wanted = {clip_name.casefold(), stem.casefold()}
    for path in sorted(glob(os.path.join(proxy_dir, "**", f"proxy_{percent}.avi"), recursive=True)):
        if os.path.basename(os.path.dirname(path)).casefold() in wanted:
            return path
    return ""


def detection_media(context, scene, strip, requested):
    """(path to read, label, why the source is used although a proxy was asked for)."""
    source = real_path(strip.filepath)
    size = preview_proxy_size(context) if requested == "AUTO" else requested
    if size == "RAW":
        return source, "source", ""
    proxy = getattr(strip, "proxy", None)
    if proxy is None:
        return source, "source", "proxies are not turned on for this strip"
    if proxy.use_proxy_custom_file and proxy.filepath:
        custom = real_path(proxy.filepath)
        if os.path.isfile(custom):
            return custom, "custom proxy", ""
        return source, "source", "the custom proxy file is missing"
    percent = PROXY_PERCENT[size]
    se = scene.sequence_editor
    if se.proxy_storage == "PROJECT" and se.proxy_dir:
        root = real_path(se.proxy_dir)
    elif proxy.use_proxy_custom_directory and proxy.directory:
        root = real_path(proxy.directory)
    else:
        root = os.path.join(os.path.dirname(source), "BL_proxy")
    found = find_proxy_file(root, os.path.basename(source), percent)
    if found:
        return found, f"{percent}% proxy", ""
    return source, "source", f"no {percent}% proxy was found"


# ---------------------------------------------------------------------------
# Scores: in memory, and kept in a file so a clip is only ever read once
# ---------------------------------------------------------------------------

def analysis_key(media_path, fps):
    """Names one reading of one file. A changed file or frame rate gives a new key."""
    st = os.stat(media_path)
    raw = f"{os.path.normcase(media_path)}|{st.st_size}|{int(st.st_mtime)}|{fps:.3f}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def folder_cache_file(key):
    folder = bpy.utils.extension_path_user(__package__, path="scores", create=True)
    return os.path.join(folder, key + ".json")


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def covers(entry, first, end):
    return bool(entry) and entry["first"] <= first and end <= entry["first"] + len(entry["scores"])


def load_scores(key, source_path):
    """Scores for a key: from memory, else the file beside the video, else the add-on's folder."""
    if key in _scores:
        return _scores[key]
    entry = None
    sidecar = _read_json(source_path + SIDECAR_SUFFIX) if source_path else None
    if isinstance(sidecar, dict):
        entry = sidecar.get("analyses", {}).get(key)
    if entry is None:
        entry = _read_json(folder_cache_file(key))
    if isinstance(entry, dict) and isinstance(entry.get("scores"), list):
        _scores[key] = {"first": int(entry.get("first", 0)), "scores": entry["scores"]}
        return _scores[key]
    return None


def save_scores(key, source_path, media_label, entry, where):
    """Keep scores in memory and, unless caching is off, in a file. Returns a warning or ''."""
    _scores[key] = entry
    _counts.clear()
    if where == "OFF":
        return ""
    record = {"media": media_label, "first": entry["first"], "scores": entry["scores"]}
    if where == "BESIDE":
        path = source_path + SIDECAR_SUFFIX
        try:
            data = _read_json(path)
            if not isinstance(data, dict) or not isinstance(data.get("analyses"), dict):
                data = {"about": "Shot Detection scores for the video beside this file. Safe to delete.",
                        "analyses": {}}
            data["analyses"][key] = record
            _write_json(path, data)
            return ""
        except OSError as e:
            where_failed = f"Could not write {os.path.basename(path)} beside the video ({e.strerror or e}); "
    else:
        where_failed = ""
    try:
        _write_json(folder_cache_file(key), record)
    except OSError as e:
        return f"{where_failed}the scores are kept for this session only ({e.strerror or e})."
    return f"{where_failed}the scores were saved in the add-on's folder instead." if where_failed else ""


def forget_scores(key, source_path):
    _scores.pop(key, None)
    _counts.clear()
    path = source_path + SIDECAR_SUFFIX if source_path else ""
    data = _read_json(path) if path else None
    try:
        if isinstance(data, dict) and key in data.get("analyses", {}):
            del data["analyses"][key]
            if data["analyses"]:
                _write_json(path, data)
            else:
                os.remove(path)
        cached = folder_cache_file(key)
        if os.path.isfile(cached):
            os.remove(cached)
    except OSError:
        pass


def cut_frames(entry, first, end, threshold, min_len):
    """Source frames where a new shot starts, within [first, end)."""
    scores, base = entry["scores"], entry["first"]
    out, last = [], first
    for f in range(max(first, base), min(end, base + len(scores))):
        if scores[f - base] >= threshold and f - last >= min_len:
            out.append(f)
            last = f
    return out


def strip_cuts(strip, settings):
    """Timeline frames where this strip would be split, or None if it has not been analysed."""
    entry = _scores.get(strip.get("shots_key", ""))
    if entry is None:
        return None
    first, end = used_range(strip)
    offset = int(round(sprop(strip, "frame_start")))
    left, right = sprop(strip, "frame_final_start"), sprop(strip, "frame_final_end")
    frames = (f + offset for f in cut_frames(entry, first, end, settings.threshold, settings.min_scene_len))
    return [t for t in frames if left < t < right]


def refresh_markers(scene):
    """Show the cuts of every analysed strip as timeline markers, or remove them."""
    markers = scene.timeline_markers
    for m in [m for m in markers if m.name.startswith(MARKER_PREFIX)]:
        markers.remove(m)
    settings = scene.shots
    if not settings.show_markers:
        return
    frames = set()
    for strip in all_strips(scene):
        if strip.type == "MOVIE":
            frames.update(strip_cuts(strip, settings) or [])
    for n, frame in enumerate(sorted(frames), 1):
        markers.new(f"{MARKER_PREFIX}{n}", frame=frame)


def settings_changed(self, context):
    _counts.clear()
    refresh_markers(self.id_data)


class ShotSettings(PropertyGroup):
    media: EnumProperty(name="Read", items=MEDIA_ITEMS, default="RAW",
                        description="Which file to read when analysing. A proxy is much faster on large footage")
    threshold: FloatProperty(
        name="Threshold", default=27.0, min=0.1, soft_min=5.0, soft_max=100.0, update=settings_changed,
        description="How big a change between two frames counts as a cut. Lower finds more cuts. "
                    "Changing it does not read the video again")
    min_scene_len: IntProperty(
        name="Min Shot Length", default=15, min=1, soft_max=240, update=settings_changed,
        description="Shortest shot, in frames. A cut closer than this to the one before is ignored")
    show_markers: BoolProperty(
        name="Show Cuts as Markers", default=True, update=settings_changed,
        description="Mark where analysed strips would be split with timeline markers. "
                    "They are removed when this is turned off")
    cache: EnumProperty(
        name="Keep Scores",
        items=[("BESIDE", "Beside the Video", "In a small .shots.json file next to the source video"),
               ("FOLDER", "Add-on Folder", "In the add-on's own user folder, out of sight"),
               ("OFF", "This Session Only", "In memory only. The video is read again after Blender restarts")],
        default="BESIDE",
        description="Where the scores from an analysis are kept, so a video only has to be read once")


# ---------------------------------------------------------------------------
# Analysis queue: one worker process per clip, one clip at a time
# ---------------------------------------------------------------------------

def make_job(context, scene, strip):
    settings = scene.shots
    path, label, fallback = detection_media(context, scene, strip, settings.media)
    if not os.path.isfile(path):
        return None
    first, end = used_range(strip)
    fps = scene_fps(scene)
    return {"strip": strip.name, "path": path, "source": real_path(strip.filepath), "label": label,
            "fallback": fallback, "fps": fps, "first": first, "end": end,
            "key": analysis_key(path, fps), "frame": first, "error": ""}


def _pump(pipe, lines):
    for line in pipe:
        lines.put(line)
    pipe.close()


def _start_worker(job):
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "worker.py")
    job["result"] = os.path.join(_queue["tmp"], f"{job['key']}.json")
    # -s keeps the worker away from the per-user site folder, so it uses the add-on's libraries.
    cmd = [sys.executable, "-s", worker, LIBS.folder(), job["path"], job["result"],
           str(job["fps"]), str(job["first"]), str(job["end"])]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    lines = queue.Queue()
    reader = threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True)
    reader.start()
    _queue.update(proc=proc, lines=lines, reader=reader, log=[])


def _finish_worker(job, scene):
    """Take the worker's result into memory and the cache, and tag the strip with it."""
    proc = _queue["proc"]
    result = _read_json(job["result"]) if proc.returncode == 0 else None
    if not result:
        tail = [line for line in _queue["log"] if line][-1:] or ["the worker stopped without a result"]
        job["error"] = job["error"] or tail[0]
        print(f"[Shot Detection] '{job['strip']}' failed: {job['error']}")
        return
    entry = {"first": int(result["first"]), "scores": result["scores"]}
    job["warning"] = save_scores(job["key"], job["source"], job["label"], entry, scene.shots.cache)
    tag_strip(scene, job)


def tag_strip(scene, job):
    for strip in all_strips(scene):
        if strip.name == job["strip"]:
            strip["shots_key"] = job["key"]
            strip["shots_media"] = job["label"]


def queue_tick():
    """Runs a few times a second while clips are being analysed."""
    global _queue, _note
    q = _queue
    if q is None:
        return None
    scene = bpy.data.scenes.get(q["scene"])
    job = q["jobs"][q["index"]] if q["index"] < len(q["jobs"]) else None
    if job is None or scene is None:
        failed = [j for j in q["jobs"] if j["error"]]
        warnings = [j["warning"] for j in q["jobs"] if j.get("warning")]
        done = len(q["jobs"]) - len(failed)
        _note = f"Analysed {done} clip(s)." + (f" {len(failed)} failed, see console." if failed else "")
        if warnings:
            _note += " " + warnings[0]
        shutil.rmtree(q["tmp"], ignore_errors=True)
        _queue = None
        if scene is not None:
            _counts.clear()
            refresh_markers(scene)
        redraw_sequencers()
        return None
    if q.get("proc") is None:
        try:
            _start_worker(job)
        except OSError as e:
            job["error"] = str(e)
            q["index"] += 1
        return 0.1
    while True:
        try:
            line = q["lines"].get_nowait().strip()
        except queue.Empty:
            break
        try:
            message = json.loads(line)
        except ValueError:
            q["log"].append(line)           # not ours: something the libraries printed
            continue
        if "frame" in message:
            job["frame"] = message["frame"]
        if "error" in message:
            job["error"] = message["error"]
    if q["proc"].poll() is not None and not q["reader"].is_alive() and q["lines"].empty():
        _finish_worker(job, scene)
        q["proc"] = None
        q["index"] += 1
    redraw_sequencers()
    return 0.1


def start_queue(scene, jobs):
    global _queue, _note
    _note = ""
    _queue = {"scene": scene.name, "jobs": jobs, "index": 0, "proc": None,
              "tmp": tempfile.mkdtemp(prefix="shot_detection_")}
    if bpy.app.background:
        while _queue is not None:           # no event loop to run a timer, so wait here
            queue_tick()
            time.sleep(0.05)
    else:
        bpy.app.timers.register(queue_tick, first_interval=0.05)


def stop_queue():
    global _queue, _note
    q, _queue = _queue, None
    if q is None:
        return
    proc = q.get("proc")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    shutil.rmtree(q["tmp"], ignore_errors=True)
    _note = "Analysis cancelled."


def redraw_sequencers():
    wm = bpy.context.window_manager
    for window in (wm.windows if wm else []):
        for area in window.screen.areas:
            if area.type == "SEQUENCE_EDITOR":
                area.tag_redraw()


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------

class SEQUENCER_OT_shots_setup(Operator):
    """Install or repair the scene detection libraries in the add-on's own folder. No administrator rights needed"""
    bl_idname = "sequencer.shots_setup"
    bl_label = "Install / Repair"
    bl_options = {"REGISTER", "INTERNAL"}

    _timer = None

    def execute(self, context):
        error = LIBS.start()
        if error:
            self.report({"ERROR"}, error)
            return {"CANCELLED"}
        if bpy.app.background or context.window is None:
            code = LIBS.poll()
            while code is None:             # no window means no timer to check back with
                time.sleep(1.0)
                code = LIBS.poll()
            return self._done(code)
        self.report({"INFO"}, "Installing in the background. Progress is in the System Console.")
        wm = context.window_manager
        self._timer = wm.event_timer_add(0.5, window=context.window)
        wm.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def modal(self, context, event):
        if event.type != "TIMER":
            return {"PASS_THROUGH"}
        code = LIBS.poll() if LIBS.proc is not None else 1
        redraw_sequencers()                 # keeps the panel's elapsed time moving
        if code is None:
            return {"PASS_THROUGH"}
        context.window_manager.event_timer_remove(self._timer)
        return self._done(code)

    def _done(self, code):
        level, message = LIBS.finish(code)
        self.report({level}, message)
        return {"CANCELLED"} if level == "ERROR" else {"FINISHED"}


class SEQUENCER_OT_shots_open_libs(Operator):
    """Open the folder the libraries are installed in. Blender removes it when the add-on is uninstalled"""
    bl_idname = "sequencer.shots_open_libs"
    bl_label = "Open Library Folder"

    def execute(self, context):
        folder = LIBS.folder()
        if not os.path.isdir(folder):
            self.report({"WARNING"}, "Nothing is installed in that folder yet.")
            return {"CANCELLED"}
        bpy.ops.wm.path_open(filepath=folder)
        return {"FINISHED"}


class SEQUENCER_OT_shots_analyse(Operator):
    """Read the selected movie strips once and score every frame. Afterwards the cuts follow the settings instantly"""
    bl_idname = "sequencer.shots_analyse"
    bl_label = "Analyse Selected"
    bl_options = {"REGISTER"}

    @classmethod
    def poll(cls, context):
        if _queue is not None or LIBS.proc is not None:
            cls.poll_message_set("Busy.")
            return False
        if LIBS.found is None:
            cls.poll_message_set("The libraries are not installed.")
            return False
        if not selected_movies(seq_scene(context)):
            cls.poll_message_set("Select a movie strip first.")
            return False
        return True

    def execute(self, context):
        global _note
        scene = seq_scene(context)
        todo, ready, missing = [], 0, 0
        for strip in selected_movies(scene):
            job = make_job(context, scene, strip)
            if job is None:
                missing += 1
                continue
            if covers(load_scores(job["key"], job["source"]), job["first"], job["end"]):
                tag_strip(scene, job)       # already read once: nothing to wait for
                ready += 1
            else:
                todo.append(job)
            if job["fallback"]:
                self.report({"WARNING"}, f"{strip.name}: reading the source file, {job['fallback']}.")
        if missing:
            self.report({"WARNING"}, f"{missing} strip(s) skipped: the media file was not found.")
        _counts.clear()
        if todo:
            start_queue(scene, todo)
            self.report({"INFO"}, f"Analysing {len(todo)} clip(s)" + (f", {ready} already analysed." if ready else "."))
        else:
            _note = f"{ready} clip(s) already analysed." if ready else ""
            refresh_markers(scene)
        return {"FINISHED"}


class SEQUENCER_OT_shots_cancel(Operator):
    """Stop analysing"""
    bl_idname = "sequencer.shots_cancel"
    bl_label = "Cancel"

    def execute(self, context):
        stop_queue()
        return {"FINISHED"}


class SEQUENCER_OT_shots_forget(Operator):
    """Throw away the scores of the selected movie strips, in memory and on disk, so the next Analyse reads them again"""
    bl_idname = "sequencer.shots_forget"
    bl_label = "Forget Analysis"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        return _queue is None and any("shots_key" in s for s in selected_movies(seq_scene(context)))

    def execute(self, context):
        scene = seq_scene(context)
        for strip in selected_movies(scene):
            if "shots_key" in strip:
                forget_scores(strip["shots_key"], real_path(strip.filepath))
                del strip["shots_key"]
        refresh_markers(scene)
        return {"FINISHED"}


def split_strips(strips, frame):
    """Split every strip in the list that spans the frame; the right-hand pieces join the list."""
    for strip in list(strips):
        if sprop(strip, "frame_final_start") < frame < sprop(strip, "frame_final_end"):
            right = strip.split(frame=frame, split_method="SOFT")
            if right is not None:
                right.select = True
                strips.append(right)


class SEQUENCER_OT_shots_split(Operator):
    """Split the selected analysed movie strips at their cuts, along with selected strips that overlap them (such as their audio)"""
    bl_idname = "sequencer.shots_split"
    bl_label = "Split at Cuts"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context):
        if _queue is not None:
            cls.poll_message_set("Busy analysing.")
            return False
        if not any("shots_key" in s for s in selected_movies(seq_scene(context))):
            cls.poll_message_set("Analyse a selected movie strip first.")
            return False
        return True

    def execute(self, context):
        scene = seq_scene(context)
        settings = scene.shots
        others = [s for s in all_strips(scene) if s.select and s.type != "MOVIE" and not s.lock]
        total = pieces = 0
        for strip in selected_movies(scene):
            key = strip.get("shots_key", "")
            if strip.lock or not key or load_scores(key, real_path(strip.filepath)) is None:
                continue
            cuts = strip_cuts(strip, settings)
            if not cuts:
                continue
            left, right = sprop(strip, "frame_final_start"), sprop(strip, "frame_final_end")
            group = [strip] + [o for o in others
                               if sprop(o, "frame_final_start") < right and sprop(o, "frame_final_end") > left]
            for frame in cuts:
                split_strips(group, frame)
            total += len(cuts)
            pieces += 1
        _counts.clear()
        refresh_markers(scene)
        if not total:
            self.report({"WARNING"}, "No cuts to split at. Lower the threshold, or analyse the strips first.")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Split {pieces} strip(s) at {total} cut(s).")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# Panel and menus
# ---------------------------------------------------------------------------

def count_cuts(strip, settings):
    key = strip.get("shots_key", "")
    if key not in _scores:
        return None
    first, end = used_range(strip)
    memo = (key, first, end, sprop(strip, "frame_start"), round(settings.threshold, 3), settings.min_scene_len)
    if memo not in _counts:
        _counts[memo] = len(strip_cuts(strip, settings) or [])
    return _counts[memo]


class SEQUENCER_PT_shots(Panel):
    bl_label = "Shot Detection"
    bl_idname = "SEQUENCER_PT_shots"
    bl_space_type = "SEQUENCE_EDITOR"
    bl_region_type = "UI"
    bl_category = TAB

    def draw(self, context):
        layout = self.layout
        scene = seq_scene(context)
        settings = scene.shots
        ready = LIBS.found is not None
        installing = LIBS.proc is not None

        row = layout.row(align=True)
        row.enabled = not installing and _queue is None
        row.label(text="Libraries", icon="CHECKMARK" if ready else "ERROR")
        row.operator("sequencer.shots_setup", text="Install / Repair")
        row.operator("sequencer.shots_open_libs", text="", icon="FILE_FOLDER")
        if installing:
            secs = int(time.time() - LIBS.started)
            layout.label(text=f"Installing libraries... {secs // 60}:{secs % 60:02d}", icon="TIME")
        elif LIBS.message:
            layout.label(text=LIBS.message, icon="INFO")

        box = layout.box()
        box.enabled = ready and not installing
        box.label(text="1. Analyse (reads each video once)")
        box.prop(settings, "media")
        box.prop(settings, "cache")
        if _queue is not None:
            jobs, index = _queue["jobs"], _queue["index"]
            if index < len(jobs):
                job = jobs[index]
                span = max(1, job["end"] - job["first"])
                factor = min(1.0, max(0.0, (job["frame"] - job["first"]) / span))
                text = f"{index + 1}/{len(jobs)}: {job['strip']}"
                if hasattr(box, "progress"):
                    box.progress(factor=factor, text=text)
                else:
                    box.label(text=f"{text}  {int(factor * 100)}%", icon="TIME")
            box.operator("sequencer.shots_cancel", icon="CANCEL")
        else:
            big = box.row()
            big.scale_y = 1.3
            big.operator("sequencer.shots_analyse", icon="VIEWZOOM")
            if _note:
                box.label(text=_note, icon="INFO")

        box = layout.box()
        box.enabled = ready and not installing
        box.label(text="2. Choose the cuts (instant)")
        box.prop(settings, "threshold", slider=True)
        box.prop(settings, "min_scene_len")
        box.prop(settings, "show_markers")
        movies = selected_movies(scene)
        for strip in movies[:8]:
            n = count_cuts(strip, settings)
            if n is not None:
                box.label(text=f"{strip.name}: {n} cut(s)", icon="SEQUENCE")
            elif "shots_key" in strip:
                box.label(text=f"{strip.name}: analysed earlier, click Analyse", icon="FILE_REFRESH")
            else:
                box.label(text=f"{strip.name}: not analysed", icon="DOT")
        if len(movies) > 8:
            box.label(text=f"and {len(movies) - 8} more")
        if not movies:
            box.label(text="Select one or more movie strips", icon="INFO")

        col = layout.column(align=True)
        col.enabled = ready and not installing
        big = col.row()
        big.scale_y = 1.3
        big.operator("sequencer.shots_split", icon="SCULPTMODE_HLT")
        col.operator("sequencer.shots_forget", icon="TRASH")


def menu_shots(self, context):
    layout = self.layout
    layout.separator()
    layout.operator("sequencer.shots_analyse", text="Analyse Shots")
    layout.operator("sequencer.shots_split", text="Split at Shot Cuts")


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    ShotSettings,
    SEQUENCER_OT_shots_setup,
    SEQUENCER_OT_shots_open_libs,
    SEQUENCER_OT_shots_analyse,
    SEQUENCER_OT_shots_cancel,
    SEQUENCER_OT_shots_forget,
    SEQUENCER_OT_shots_split,
    SEQUENCER_PT_shots,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.shots = PointerProperty(type=ShotSettings)
    bpy.types.SEQUENCER_MT_strip.append(menu_shots)
    bpy.types.SEQUENCER_MT_context_menu.append(menu_shots)
    LIBS.use()
    LIBS.find()


def unregister():
    stop_queue()
    LIBS.stop()
    bpy.types.SEQUENCER_MT_context_menu.remove(menu_shots)
    bpy.types.SEQUENCER_MT_strip.remove(menu_shots)
    if hasattr(bpy.types.Scene, "shots"):
        del bpy.types.Scene.shots
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    LIBS.drop()
    _scores.clear()
    _counts.clear()
