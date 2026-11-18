# SPDX-License-Identifier: GPL-2.0-or-later
"""Installs the Python libraries an add-on needs into the add-on's own user folder.

Nothing is written into Blender's own Python folder, so no administrator rights are
needed and the libraries Blender ships are never replaced. The folder is the one Blender
gives each extension: it is kept when the extension is upgraded and removed when the
extension is uninstalled, and each Blender version has its own.

pip runs in the background. It installs into a fresh folder beside the real one, and the
two are swapped only if pip succeeds, so a failed download leaves working libraries alone.
"""
import importlib
import importlib.metadata
import importlib.util
import os
import shutil
import subprocess
import sys
import time

import bpy

# A lock file next to the folder stops two Blenders installing into it at once. The running
# install touches it every tick; one that has not been touched for this long was left behind.
LOCK_STALE_SECONDS = 120


def inside(path, folder):
    return os.path.normcase(os.path.join(path, "")).startswith(os.path.normcase(os.path.join(folder, "")))


def _remove_tree(path):
    if os.path.isdir(path):
        shutil.rmtree(path)


def _take_lock(path):
    try:
        if os.path.exists(path) and time.time() - os.path.getmtime(path) > LOCK_STALE_SECONDS:
            os.remove(path)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.close(fd)
    return True


def _drop_lock(path):
    try:
        os.remove(path)
    except OSError:
        pass


class Libraries:
    def __init__(self, package, requirements, probe):
        self.package = package              # the add-on's __package__
        self.requirements = requirements    # what to hand to pip
        self.probe = probe                  # the module whose presence means "installed"
        self.found = None                   # where that module would be imported from
        self.proc = None                    # the running pip, if any
        self.started = 0.0
        self.message = ""                   # result of the last install, for the panel
        self._new = self._final = self._lock = ""

    def folder(self):
        return bpy.utils.extension_path_user(self.package, path="libs")

    def use(self):
        """Put the folder last on Python's search path: a library Blender ships is found first."""
        folder = self.folder()
        if os.path.isdir(folder) and folder not in sys.path:
            sys.path.append(folder)

    def drop(self):
        folder = self.folder()
        if folder in sys.path:
            sys.path.remove(folder)

    def find(self):
        """Note where the library would be imported from, without importing it."""
        importlib.invalidate_caches()
        try:
            spec = importlib.util.find_spec(self.probe)
        except (ImportError, ValueError):
            spec = None
        self.found = spec.origin if spec and spec.origin else None
        return self.found

    def loaded(self):
        """True when Python has already imported something from the folder. Windows locks such files."""
        folder = self.folder()
        for module in list(sys.modules.values()):
            path = getattr(module, "__file__", None)
            if path and inside(path, folder):
                return True
        return False

    def _write_constraints(self, path):
        """List the libraries Blender ships, so pip picks versions that agree with them."""
        own = self.folder()
        pins = set()
        for dist in importlib.metadata.distributions():
            name = dist.metadata["Name"]
            if name and not inside(str(dist.locate_file("")), own):
                pins.add(f"{name}=={dist.version}")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(pins)) + "\n")
        return path

    def start(self):
        """Start pip in the background. Returns an error message, or None once it is running."""
        if self.proc is not None:
            return "An install is already running."
        if not getattr(bpy.app, "online_access", True):
            return ("Blender is set to work offline. Turn on Preferences > System > Network > "
                    "Allow Online Access, then click this button again.")
        final = self.folder()
        if self.loaded():
            return "These libraries are in use. Restart Blender, then click this button first."
        new, lock = final + ".new", final + ".lock"
        try:
            os.makedirs(os.path.dirname(final), exist_ok=True)
            if not _take_lock(lock):
                return f"Another Blender is installing into this folder. If none is, delete {lock}"
            try:
                _remove_tree(new)
                os.makedirs(new)
                constraints = self._write_constraints(os.path.join(new, "blender_constraints.txt"))
                if importlib.util.find_spec("pip") is None:
                    subprocess.run([sys.executable, "-m", "ensurepip"], capture_output=True)
                # -s keeps pip away from the per-user site folder, which Blender does not load.
                cmd = [sys.executable, "-s", "-m", "pip", "install", "--target", new,
                       "--constraint", constraints, "--no-warn-script-location",
                       "--disable-pip-version-check"] + list(self.requirements)
                print(f"[{self.package}] Running:", " ".join(cmd))
                proc = subprocess.Popen(cmd)       # pip prints straight to the console as it works
            except OSError:
                _drop_lock(lock)
                raise
        except OSError as e:
            return f"Could not prepare the library folder {final}: {e}"
        self.proc, self.started, self.message = proc, time.time(), ""
        self._new, self._final, self._lock = new, final, lock
        return None

    def poll(self):
        """None while pip is still running, else its exit code. Keeps the lock file fresh."""
        code = self.proc.poll()
        if code is None:
            try:
                os.utime(self._lock)
            except OSError:
                pass
        return code

    def finish(self, code):
        """Called once pip has ended. Returns (report level, message)."""
        self.proc = None
        level, message = "INFO", "Libraries are ready."
        try:
            if code != 0:
                level, message = "ERROR", "Install failed. See Window > Toggle System Console."
            elif self.loaded():
                level, message = "ERROR", "The libraries came into use during the install. Restart Blender and try again."
            if level == "ERROR":
                _remove_tree(self._new)
            else:
                _remove_tree(self._final)
                os.replace(self._new, self._final)
        except OSError as e:
            level, message = "ERROR", f"Could not put the new libraries in place: {e}"
        finally:
            _drop_lock(self._lock)
        if level != "ERROR":
            self.use()
            if self.find() is None:
                level, message = "WARNING", "Installed, but the library was not found. Restart Blender."
        self.message = message
        return level, message

    def stop(self):
        """Abandon a running install, when the add-on is disabled."""
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
            _remove_tree(self._new)
        except (OSError, subprocess.SubprocessError):
            pass
        _drop_lock(self._lock)
