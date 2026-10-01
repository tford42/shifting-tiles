#!/usr/bin/env python3
"""Fullscreen shifting-tiles photo wall.

Shows photos from a folder and its subfolders, cropped into a dense grid.
Every few seconds a block of tiles slides away and new photos slide in,
in the style of the macOS Shifting Tiles screensaver. Order is random.

Requires Python 3.8 or newer, tkinter, and Pillow.

    pip install Pillow

On Debian or Ubuntu, if the window toolkit is missing:

    sudo apt install python3-tk

Run it and choose a folder when asked:

    python3 shifting_tiles.py

Or pass the folder on the command line:

    python3 shifting_tiles.py ~/Pictures

Esc or Q quits.

HEIC/HEIF photos are included only when pillow-heif is installed.
Photos are cropped to fill each tile, like the Mac screensaver.
"""

from __future__ import annotations

import os
import queue
import random
import signal
import sys
import threading
import time
import traceback
from pathlib import Path

if sys.version_info < (3, 8):
    sys.stderr.write("Python 3.8 or newer is required.\n")
    raise SystemExit(1)

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox
except ImportError:
    sys.stderr.write(
        "tkinter is required.\n"
        "On Debian or Ubuntu: sudo apt install python3-tk\n"
    )
    raise SystemExit(1)

try:
    from PIL import Image, ImageFile, ImageOps, ImageTk
except ImportError:
    sys.stderr.write(
        "Pillow is required, including its tkinter image support.\n"
        "    pip install Pillow\n"
        "On Debian or Ubuntu you may also need:\n"
        "    sudo apt install python3-tk python3-pil.imagetk\n"
    )
    raise SystemExit(1)

ImageFile.LOAD_TRUNCATED_IMAGES = True

try:
    import pillow_heif

    pillow_heif.register_heif_opener()
    _HEIF = True
except Exception:
    _HEIF = False

# Rough tile size. The grid changes so tiles stay near this and the screen stays full.
TILE_TARGET = 300
MIN_TILE = 180
MIN_COLUMNS = 4
MIN_ROWS = 3

SLIDE_MS_PER_PIXEL = 2.0
SLIDE_MS_MIN = 750
SLIDE_MS_MAX = 1400
FRAME_MS = 33
PAUSE_AFTER_LOAD = (1.2, 2.2)
PAUSE_BETWEEN = (1.8, 3.6)

EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".jpe",
    ".jfif",
    ".png",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".webp",
}
if _HEIF:
    EXTENSIONS.update({".heic", ".heif"})

SKIP_DIRS = {"__MACOSX", "@eaDir", ".git", ".thumbnails"}


def _resample(name):
    resampling = getattr(Image, "Resampling", Image)
    return getattr(resampling, name)


def ease_in_out(t):
    """Smooth acceleration, the kind of curve the Mac screensaver uses."""
    if t <= 0:
        return 0.0
    if t >= 1:
        return 1.0
    if t < 0.5:
        return 4 * t * t * t
    p = -2 * t + 2
    return 1 - (p * p * p) / 2


def axis_edges(length, count):
    """Pixel boundaries that cover `length` exactly with `count` positive spans."""
    length = int(length)
    count = int(count)
    if count < 1 or length < count:
        raise ValueError("cannot split %s px into %s tiles" % (length, count))
    edges = [0]
    for i in range(1, count):
        edge = int(round(i * length / count))
        if edge <= edges[-1]:
            edge = edges[-1] + 1
        latest = length - (count - i)
        if edge > latest:
            edge = latest
        edges.append(edge)
    edges.append(length)
    return edges


def grid_shape(width, height, target=TILE_TARGET, min_tile=MIN_TILE,
               min_cols=MIN_COLUMNS, min_rows=MIN_ROWS):
    """How many columns and rows fill this screen with tiles near `target` px."""
    width = max(1, int(width))
    height = max(1, int(height))
    cols = max(1, width // target)
    rows = max(1, height // target)
    while cols < min_cols and (cols + 1) <= width and width / (cols + 1) >= min_tile:
        cols += 1
    while rows < min_rows and (rows + 1) <= height and height / (rows + 1) >= min_tile:
        rows += 1
    while cols > 1 and width / cols < min_tile:
        cols -= 1
    while rows > 1 and height / rows < min_tile:
        rows -= 1
    return max(1, min(cols, width)), max(1, min(rows, height))


def shift_vector(direction, span_x, span_y):
    """Pixel travel for a block that fully leaves in `direction`."""
    if direction == "right":
        return span_x, 0
    if direction == "left":
        return -span_x, 0
    if direction == "down":
        return 0, span_y
    if direction == "up":
        return 0, -span_y
    raise ValueError(direction)


def choose_group(cols, rows):
    """A random block of tiles, biased toward the small groups the Mac saver uses."""
    candidates = []

    def add(gw, gh, weight):
        if gw < 1 or gh < 1 or gw > cols or gh > rows:
            return
        # A slide of the entire wall just looks like a wipe. Keep one tile put
        # whenever the wall has more than one.
        if cols * rows > 1 and gw * gh == cols * rows:
            return
        candidates.append((gw, gh, weight))

    add(1, 1, 2)
    add(2, 1, 5)
    add(1, 2, 5)
    add(2, 2, 6)
    add(3, 1, 3)
    add(1, 3, 3)
    add(3, 2, 3)
    add(2, 3, 3)
    add(4, 1, 2)
    add(1, 4, 2)
    add(4, 2, 1)
    add(3, 3, 1)
    if not candidates:
        candidates.append((1, 1, 1))
    weights = [item[2] for item in candidates]
    gw, gh, _weight = random.choices(candidates, weights=weights, k=1)[0]
    c0 = random.randrange(cols - gw + 1)
    r0 = random.randrange(rows - gh + 1)
    horizontal = ("left", "right")
    vertical = ("up", "down")
    if gw > gh:
        directions = horizontal * 3 + vertical
    elif gh > gw:
        directions = vertical * 3 + horizontal
    else:
        directions = horizontal + vertical
    return c0, r0, gw, gh, random.choice(directions)


def collect_images(folder):
    """Photo paths under `folder`, including subfolders. Hidden folders are skipped."""
    found = []
    seen_dirs = set()

    def walk_error(err):
        print("Skipping %s: %s" % (err.filename, err.strerror), file=sys.stderr, flush=True)

    for dirpath, dirnames, filenames in os.walk(folder, followlinks=True, onerror=walk_error):
        real = os.path.realpath(dirpath)
        if real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name not in SKIP_DIRS
        ]
        for name in filenames:
            if name.startswith("."):
                continue
            if os.path.splitext(name)[1].lower() not in EXTENSIONS:
                continue
            full = Path(dirpath) / name
            try:
                found.append(str(full.resolve()))
            except OSError:
                continue
    return list(dict.fromkeys(found))


def load_cover(path, width, height):
    """Open `path` and center-crop it so it fills a tile of `width` by `height`."""
    with Image.open(path) as opened:
        try:
            opened.draft("RGB", (width * 3, height * 3))
        except Exception:
            pass
        opened.load()
        image = opened
        try:
            transposed = ImageOps.exif_transpose(image)
            if transposed is not None:
                image = transposed
        except Exception:
            pass
        if image.mode != "RGB":
            image = image.convert("RGB")
        src_w, src_h = image.size
        if src_w < 1 or src_h < 1:
            raise ValueError("empty image")
        scale = max(width / src_w, height / src_h)
        if src_w > width * 2 and src_h > height * 2:
            mid_w = max(width * 2, int(src_w * scale * 2))
            mid_h = max(height * 2, int(src_h * scale * 2))
            image = image.resize((mid_w, mid_h), _resample("BOX"))
            src_w, src_h = image.size
            scale = max(width / src_w, height / src_h)
        new_w = max(width, int(round(src_w * scale)))
        new_h = max(height, int(round(src_h * scale)))
        image = image.resize((new_w, new_h), _resample("LANCZOS"))
        left = (new_w - width) // 2
        top = (new_h - height) // 2
        # copy() detaches the pixels from the file, which is about to close.
        return image.crop((left, top, left + width, top + height)).copy()


class Deck:
    """Shuffled photos, without repeating until the unseen ones run out."""

    def __init__(self, paths):
        self.paths = list(dict.fromkeys(paths))
        self.queue = []

    def next_path(self, avoid):
        avoid = set(avoid)
        while self.queue:
            path = self.queue.pop()
            if path not in avoid:
                return path
        fresh = [path for path in self.paths if path not in avoid]
        self.queue = fresh if fresh else list(self.paths)
        if not self.queue:
            raise RuntimeError("no photos")
        random.shuffle(self.queue)
        return self.queue.pop()


class TileCache:
    """Resized tiles, shared by the loader threads. The wall only needs recent ones."""

    def __init__(self, limit=160):
        self.limit = limit
        self.lock = threading.Lock()
        self.data = {}
        self.order = []

    def get(self, path, width, height):
        key = (path, width, height)
        with self.lock:
            image = self.data.get(key)
            if image is not None:
                self.order.remove(key)
                self.order.append(key)
                return image.copy()
        image = load_cover(path, width, height)
        with self.lock:
            current = self.data.get(key)
            if current is None:
                self.data[key] = image
                self.order.append(key)
                while len(self.order) > self.limit:
                    old = self.order.pop(0)
                    self.data.pop(old, None)
            else:
                image = current
        return image.copy()


class ShiftingTiles:
    """Fullscreen wall. Blocks of tiles slide off; new photos slide in under the ones that stay."""

    def __init__(self, root, paths, fullscreen=True):
        self.root = root
        self.paths = list(dict.fromkeys(paths))
        self.fullscreen = fullscreen
        self.alive = True
        self.deck = Deck(self.paths)
        self.cache = TileCache()
        self.jobs = queue.Queue()
        self.results = queue.Queue()
        self.stop = threading.Event()
        self.cells = []
        self.showing = {}
        self.reserved = {}
        self.failed = set()
        self.reported = set()
        self.tries = {}
        self.built = False
        self.phase = "loading"
        self.filled = 0
        self.loaded_ok = 0
        self.cols = 0
        self.rows = 0
        self.x_edges = []
        self.y_edges = []
        self.width = 0
        self.height = 0
        self.status_on = False
        self.pause_after_load = PAUSE_AFTER_LOAD
        self.pause_between = PAUSE_BETWEEN
        self.last_move = None
        self.shift_serial = 0
        self.pending_shift = None
        self.anim = None
        self._afters = set()
        self._size_after = None
        self._pending_size = None

        self.root.title("Shifting Tiles")
        self.root.configure(bg="black", cursor="none")
        self.canvas = tk.Canvas(
            self.root, bg="black", highlightthickness=0, bd=0, cursor="none",
        )
        self.canvas.pack(fill="both", expand=True)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.bind_all("<Escape>", self._on_key)
        self.root.bind_all("<q>", self._on_key)
        self.root.bind_all("<Q>", self._on_key)
        if fullscreen:
            try:
                self.root.attributes("-fullscreen", True)
            except tk.TclError:
                self.root.geometry(
                    "%sx%s+0+0" % (self.root.winfo_screenwidth(), self.root.winfo_screenheight())
                )
        try:
            self.root.focus_force()
        except tk.TclError:
            pass
        self.root.deiconify()
        workers = max(1, min(3, os.cpu_count() or 2))
        for _ in range(workers):
            threading.Thread(target=self._worker, daemon=True).start()
        self.canvas.bind("<Configure>", self._on_configure)
        self._install_signals()
        self.later(30, self._poll)

    def _install_signals(self):
        def handle(_signum, _frame):
            if self.alive:
                try:
                    self.root.after(0, self.quit)
                except Exception:
                    self.alive = False
                    self.stop.set()

        for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                signal.signal(sig, handle)
            except Exception:
                pass

    def later(self, ms, func):
        holder = {}

        def wrapper():
            self._afters.discard(holder.get("id"))
            if not self.alive:
                return
            try:
                func()
            except Exception:
                traceback.print_exc()
                self.quit()

        holder["id"] = self.root.after(int(ms), wrapper)
        self._afters.add(holder["id"])
        return holder["id"]

    def _on_key(self, _event):
        self.quit()
        return "break"

    def quit(self):
        if not self.alive:
            return
        self.alive = False
        self.stop.set()
        for after_id in list(self._afters):
            try:
                self.root.after_cancel(after_id)
            except Exception:
                pass
        self._afters.clear()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _on_configure(self, event):
        if self.built or not self.alive:
            return
        if event.width < 80 or event.height < 80:
            return
        self._pending_size = (event.width, event.height)
        if self._size_after is not None:
            try:
                self.root.after_cancel(self._size_after)
            except Exception:
                pass
            self._afters.discard(self._size_after)
        self._size_after = self.later(400, self._build)

    def _build(self):
        if self.built or not self.alive or self._pending_size is None:
            return
        self.built = True
        self._size_after = None
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 80 or height < 80:
            width, height = self._pending_size
        self.width = width
        self.height = height
        self.cols, self.rows = grid_shape(width, height)
        self.x_edges = axis_edges(width, self.cols)
        self.y_edges = axis_edges(height, self.rows)
        self.canvas.configure(scrollregion=(0, 0, width, height))
        self.cells = [[None for _ in range(self.cols)] for _ in range(self.rows)]
        total = self.cols * self.rows
        print("Wall is %s x %s (%s photos on screen)." % (self.cols, self.rows, total), flush=True)
        self._set_status("Loading photos… 0/%s" % total)
        for r in range(self.rows):
            for c in range(self.cols):
                self._request(("cell", r, c), self._cell_size(r, c))

    def _cell_size(self, row, col):
        return (
            self.x_edges[col + 1] - self.x_edges[col],
            self.y_edges[row + 1] - self.y_edges[row],
        )

    def _cell_origin(self, row, col):
        return self.x_edges[col], self.y_edges[row]

    def _avoid(self):
        return set(self.showing) | set(self.reserved) | set(self.failed)

    def _reserve(self, path):
        if path:
            self.reserved[path] = self.reserved.get(path, 0) + 1

    def _unreserve(self, path):
        if not path:
            return
        left = self.reserved.get(path, 0) - 1
        if left > 0:
            self.reserved[path] = left
        else:
            self.reserved.pop(path, None)

    def _hold(self, path):
        if path:
            self.showing[path] = self.showing.get(path, 0) + 1

    def _release(self, path):
        if not path:
            return
        left = self.showing.get(path, 0) - 1
        if left > 0:
            self.showing[path] = left
        else:
            self.showing.pop(path, None)

    def _request(self, token, size):
        width, height = size
        path = self.deck.next_path(self._avoid())
        self._reserve(path)
        self.tries[token] = self.tries.get(token, 0) + 1
        self.jobs.put((token, path, width, height))

    def _give_up(self, token, size):
        width, height = size
        self.jobs.put((token, None, width, height))

    def _retry(self, token):
        if self.tries.get(token, 0) >= 8:
            if token[0] == "cell":
                _kind, row, col = token
                self._give_up(token, self._cell_size(row, col))
            elif token[0] == "shift":
                _kind, serial, index = token
                shift = self.pending_shift
                if shift is None or shift["serial"] != serial or shift["started"]:
                    return
                row, col = shift["coords"][index]
                self._give_up(token, self._cell_size(row, col))
            return
        if token[0] == "cell":
            _kind, row, col = token
            if self.cells and self.cells[row][col] is not None:
                return
            self._request(token, self._cell_size(row, col))
        elif token[0] == "shift":
            _kind, serial, index = token
            shift = self.pending_shift
            if shift is None or shift["serial"] != serial or shift["started"]:
                return
            row, col = shift["coords"][index]
            self._request(token, self._cell_size(row, col))

    def _worker(self):
        while not self.stop.is_set():
            try:
                token, path, width, height = self.jobs.get(timeout=0.25)
            except queue.Empty:
                continue
            image = None
            error = None
            try:
                if path is None:
                    image = Image.new("RGB", (max(1, width), max(1, height)), "#000000")
                else:
                    image = self.cache.get(path, width, height)
            except Exception as exc:
                error = str(exc)
            self.results.put((token, path, image, error))

    def _poll(self):
        if not self.alive:
            return
        try:
            while True:
                self._handle_result(self.results.get_nowait())
        except queue.Empty:
            pass
        if self.alive:
            self.later(30, self._poll)

    def _handle_result(self, result):
        token, path, image, error = result
        if not self.alive:
            return
        if image is None:
            if path:
                self.failed.add(path)
                self._unreserve(path)
                if path not in self.reported:
                    self.reported.add(path)
                    print("Skipping unreadable photo: %s" % path, file=sys.stderr, flush=True)
                    if error:
                        print("  %s" % error, file=sys.stderr, flush=True)
            self._retry(token)
            return
        kind = token[0]
        if kind == "shift":
            _kind, serial, index = token
            shift = self.pending_shift
            if (
                shift is None
                or shift["serial"] != serial
                or shift["started"]
                or shift["tiles"][index] is not None
            ):
                self._unreserve(path)
                return
        photo = ImageTk.PhotoImage(image)
        if kind == "cell":
            _kind, row, col = token
            if self.cells[row][col] is not None:
                self._unreserve(path)
                return
            x, y = self._cell_origin(row, col)
            item = self.canvas.create_image(x, y, image=photo, anchor="nw", tags=("static",))
            self.cells[row][col] = {"item": item, "photo": photo, "path": path}
            self._unreserve(path)
            if path:
                self._hold(path)
                self.loaded_ok += 1
            self.filled += 1
            total = self.cols * self.rows
            self._set_status("Loading photos… %s/%s" % (self.filled, total))
            if self.filled == total:
                self._on_grid_full()
        elif kind == "shift":
            _kind, _serial, index = token
            shift = self.pending_shift
            row, col = shift["coords"][index]
            x, y = self._cell_origin(row, col)
            shift["tiles"][index] = {
                "photo": photo, "path": path, "r": row, "c": col, "x": x, "y": y,
            }
            self._maybe_start_shift()

    def _set_status(self, text):
        self.status_on = True
        self._badge("status", self.height // 2, text)

    def _badge(self, tag, y, text):
        self.canvas.delete(tag)
        text_id = self.canvas.create_text(
            self.width // 2, y, text=text, fill="white",
            font=("TkDefaultFont", 14), tags=(tag,),
        )
        box = self.canvas.bbox(text_id)
        if box:
            rect = self.canvas.create_rectangle(
                box[0] - 12, box[1] - 8, box[2] + 12, box[3] + 8,
                fill="black", outline="", tags=(tag,),
            )
            self.canvas.tag_lower(rect, text_id)
        self.canvas.tag_raise(tag)
        return text_id

    def _on_grid_full(self):
        self.canvas.delete("status")
        self.status_on = False
        if self.loaded_ok == 0:
            try:
                self.root.configure(cursor="")
            except tk.TclError:
                pass
            messagebox.showerror("Shifting Tiles", "None of the photos could be opened.")
            self.quit()
            return
        self._badge("hint", self.height - 36, "Esc or Q to quit")
        self.later(4000, self._hide_hint)
        self.phase = "waiting"
        self._plan_shift(self.pause_after_load)

    def _hide_hint(self):
        try:
            self.canvas.delete("hint")
        except tk.TclError:
            pass

    def _plan_shift(self, pause):
        if not self.alive or self.cols < 1 or self.rows < 1:
            return
        self.phase = "waiting"
        move = None
        for _ in range(8):
            move = choose_group(self.cols, self.rows)
            if move != self.last_move:
                break
        self.last_move = move
        c0, r0, gw, gh, direction = move
        span_x = self.x_edges[c0 + gw] - self.x_edges[c0]
        span_y = self.y_edges[r0 + gh] - self.y_edges[r0]
        total_dx, total_dy = shift_vector(direction, span_x, span_y)
        coords = []
        for index in range(gw * gh):
            coords.append((r0 + index // gw, c0 + index % gw))
        self.shift_serial += 1
        serial = self.shift_serial
        low, high = pause
        self.pending_shift = {
            "serial": serial,
            "c0": c0,
            "r0": r0,
            "gw": gw,
            "gh": gh,
            "direction": direction,
            "total_dx": total_dx,
            "total_dy": total_dy,
            "coords": coords,
            "tiles": [None] * len(coords),
            "not_before": time.monotonic() + random.uniform(low, high),
            "started": False,
        }
        for index, (row, col) in enumerate(coords):
            self._request(("shift", serial, index), self._cell_size(row, col))

    def _maybe_start_shift(self):
        shift = self.pending_shift
        if shift is None or shift["started"] or not self.alive:
            return
        if any(tile is None for tile in shift["tiles"]):
            return
        delay = shift["not_before"] - time.monotonic()
        if delay > 0.02:
            self.later(int(delay * 1000), self._maybe_start_shift)
            return
        shift["started"] = True
        self._begin_animation(shift)

    def _begin_animation(self, shift):
        self.phase = "animating"
        total_dx = shift["total_dx"]
        total_dy = shift["total_dy"]
        movers = []
        for row in range(shift["r0"], shift["r0"] + shift["gh"]):
            for col in range(shift["c0"], shift["c0"] + shift["gw"]):
                cell = self.cells[row][col]
                self.canvas.itemconfig(cell["item"], tags=("moving",))
                x, y = self._cell_origin(row, col)
                movers.append({
                    "item": cell["item"],
                    "x0": x,
                    "y0": y,
                    "keep": False,
                    "path": cell["path"],
                    "photo": cell["photo"],
                })
        for tile in shift["tiles"]:
            # New photos start just outside the block, on the side they enter from.
            item = self.canvas.create_image(
                tile["x"] - total_dx,
                tile["y"] - total_dy,
                image=tile["photo"],
                anchor="nw",
                tags=("moving",),
            )
            movers.append({
                "item": item,
                "x0": tile["x"] - total_dx,
                "y0": tile["y"] - total_dy,
                "x1": tile["x"],
                "y1": tile["y"],
                "keep": True,
                "path": tile["path"],
                "photo": tile["photo"],
                "r": tile["r"],
                "c": tile["c"],
            })
        # Tiles that stay put are opaque and sit above the slide, so the moving
        # photos are visible only inside the block. That is the same as clipping.
        for mover in movers:
            self.canvas.tag_lower(mover["item"])
        self.canvas.tag_raise("static")
        self.canvas.tag_raise("hint")
        distance = max(abs(total_dx), abs(total_dy), 1)
        duration = int(distance * SLIDE_MS_PER_PIXEL)
        if duration < SLIDE_MS_MIN:
            duration = SLIDE_MS_MIN
        elif duration > SLIDE_MS_MAX:
            duration = SLIDE_MS_MAX
        steps = max(8, int(round(duration / FRAME_MS)))
        self.anim = {
            "movers": movers,
            "total_dx": total_dx,
            "total_dy": total_dy,
            "steps": steps,
            "step": 0,
        }
        self._anim_frame()

    def _apply_shift(self, dx, dy):
        for mover in self.anim["movers"]:
            self.canvas.coords(mover["item"], mover["x0"] + dx, mover["y0"] + dy)

    def _anim_frame(self):
        anim = self.anim
        if anim is None or not self.alive:
            return
        anim["step"] += 1
        t = ease_in_out(anim["step"] / anim["steps"])
        self._apply_shift(round(anim["total_dx"] * t), round(anim["total_dy"] * t))
        if anim["step"] >= anim["steps"]:
            self._finish_animation()
        else:
            self.later(FRAME_MS, self._anim_frame)

    def _finish_animation(self):
        anim = self.anim
        if anim is None:
            return
        self._apply_shift(anim["total_dx"], anim["total_dy"])
        for mover in anim["movers"]:
            if not mover["keep"]:
                continue
            self.canvas.coords(mover["item"], mover["x1"], mover["y1"])
            self.canvas.itemconfig(mover["item"], tags=("static",))
            self.cells[mover["r"]][mover["c"]] = {
                "item": mover["item"],
                "photo": mover["photo"],
                "path": mover["path"],
            }
            self._unreserve(mover["path"])
            self._hold(mover["path"])
        for mover in anim["movers"]:
            if mover["keep"]:
                continue
            self.canvas.delete(mover["item"])
            self._release(mover["path"])
        self.canvas.tag_raise("hint")
        self.anim = None
        self.pending_shift = None
        self._plan_shift(self.pause_between)


def ask_folder(root, argv):
    if argv:
        folder = Path(argv[0]).expanduser()
        if not folder.is_dir():
            print("Not a folder: %s" % folder, file=sys.stderr, flush=True)
            return None
        return folder
    chosen = filedialog.askdirectory(
        title="Choose a folder of photos",
        initialdir=str(Path.home()),
        parent=root,
    )
    if not chosen:
        print("No folder selected.", flush=True)
        return None
    folder = Path(chosen)
    if not folder.is_dir():
        print("Not a folder: %s" % folder, file=sys.stderr, flush=True)
        return None
    return folder


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        sys.stderr.write(
            "Cannot open a display (%s).\n"
            "Run this on the laptop's desktop, not a text-only session.\n" % exc
        )
        return 1
    root.withdraw()
    try:
        folder = ask_folder(root, argv)
        if folder is None:
            root.destroy()
            return 0 if not argv else 1
        print("Looking for photos in %s" % folder, flush=True)
        paths = collect_images(folder)
        if not paths:
            root.deiconify()
            messagebox.showerror(
                "Shifting Tiles",
                "No photos found in:\n%s\n\nLooking for: %s"
                % (folder, ", ".join(sorted(EXTENSIONS))),
            )
            root.destroy()
            return 1
        print("%s photos. Esc or Q quits." % len(paths), flush=True)
        ShiftingTiles(root, paths, fullscreen=True)
        root.mainloop()
    except KeyboardInterrupt:
        return 0
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
