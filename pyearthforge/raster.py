# raster.py  -> a MODULE for working with GeoTIFFs, built on rasterio
#
# Images are LAZY:
#   - raster.image() only reads the file's metadata, not the pixels.
#   - select(), band math, normalizedDifference() ... only build a "recipe".
#   - Pixels are read only when needed:
#       visualize()  -> reads a downsampled preview (small, fast)
#       save()       -> streams the full image in chunks (RAM stays low)
#       .data        -> reads everything at full resolution (uses the most RAM)
#
# Usage:
#   from pyearthforge import raster
#   img  = raster.image("sampleImage1.tif")
#   ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
#   ndvi.visualize(min=-0.2, max=0.8, palette="ndvi")
#   ratio = img.select("B8").divide(img.select("B4"), save=True)   # saves a GeoTIFF

import ast
import glob
import math
import operator as _operator
import os
import re
import time
from collections import OrderedDict
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import rasterio
import rasterio.windows
from affine import Affine
from matplotlib.colors import BoundaryNorm, LinearSegmentedColormap, ListedColormap, PowerNorm
from matplotlib.patches import Patch
from rasterio.coords import BoundingBox
from rasterio.enums import Resampling
from rasterio.errors import RasterioIOError
from rasterio.features import geometry_mask
from rasterio.windows import Window

from ._errors import LibraryError, clean_class, clean_errors

# Side, in pixels, of the square blocks written by save(): 1024 x 1024 float32
# pixels = 4 MB per band, per step. Must be a multiple of the 256 px GeoTIFF tiles.
# Change it with e.g. raster.BLOCK_SIZE = 2048 (fewer, larger steps; more memory).
BLOCK_SIZE = 1024

# Upper limit for the default number of parallel workers in save().
# Change it with e.g. raster.MAX_WORKERS = 16 on a machine with many cores.
MAX_WORKERS = 8


def _block_size():
    size = BLOCK_SIZE
    if not isinstance(size, int) or size < 256 or size % 256:
        raise ValueError(f"raster.BLOCK_SIZE must be a multiple of 256 (e.g. 1024 or 2048), got {size!r}")
    return size


def _memory_budget():
    """Bytes that blocks in progress may use: about a quarter of the free RAM
    (between 256 MB and 8 GB), or 512 MB if free RAM can't be measured."""
    try:
        import psutil
        free = psutil.virtual_memory().available
        return int(min(max(free // 4, 256 * 2**20), 8 * 2**30))
    except Exception:
        return 512 * 2**20


def _parallel_plan(workers, n_windows, bytes_per_window):
    """(workers, blocks in flight) that fit the CPU cores and the memory budget."""
    if workers is None:
        workers = min(os.cpu_count() or 1, max(1, int(MAX_WORKERS)))
    elif not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError(f"workers must be a whole number of 1 or more, got {workers!r}")
    fits = max(1, _memory_budget() // max(1, bytes_per_window))   # blocks that fit in memory
    workers = max(1, min(workers, n_windows, fits))
    in_flight = max(workers, min(2 * workers, fits))
    return workers, in_flight


def _compute_window(window, bands, reader):
    """All bands of one block: [(band_number, float32 pixels), ...]."""
    out = []
    for band, node in enumerate(bands, start=1):
        arr = _evaluate(node, reader, window)
        if np.ndim(arr) == 0:                               # constant band
            arr = np.full((int(window.height), int(window.width)), arr, dtype="float32")
        reader.clear()                                      # free cached reads
        out.append((band, arr))
    return window, out


def _compute_blocks(windows, bands, workers, in_flight):
    """Yield (window, [(band, pixels), ...]) for every block.

    With several workers, blocks run in threads (GDAL reads and numpy release
    the GIL), each thread with its own open files. One block is one task with
    all its bands, so each source tile is decompressed only once. At most
    `in_flight` blocks are in progress or waiting to be written, so memory
    stays bounded. Results come back in completion order; GeoTIFF blocks can
    be written in any order.
    """
    if workers <= 1:
        with _Reader() as reader:
            for window in windows:
                yield _compute_window(window, bands, reader)
        return

    import contextlib
    import itertools
    import threading
    from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

    local, readers, lock = threading.local(), [], threading.Lock()

    def run(window):
        reader = getattr(local, "reader", None)
        if reader is None:
            reader = local.reader = _Reader()
            with lock:
                readers.append(reader)
        return _compute_window(window, bands, reader)

    # Our workers already use the cores: stop numpy / scikit-learn from also
    # starting a thread per core inside each worker (they would compete).
    try:
        from threadpoolctl import threadpool_limits
        limiter = threadpool_limits(limits=1)
    except Exception:
        limiter = contextlib.nullcontext()

    try:
        with limiter, ThreadPoolExecutor(max_workers=workers) as pool:
            queue = iter(windows)
            pending = {pool.submit(run, w) for w in itertools.islice(queue, in_flight)}
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    yield future.result()
                    following = next(queue, None)
                    if following is not None:
                        pending.add(pool.submit(run, following))
    finally:
        for reader in readers:
            reader.__exit__(None, None, None)


def _progress_bar(total, desc, enabled=True):
    """A tqdm progress bar (a widget in Jupyter, text in a terminal), or a silent stand-in."""
    if enabled:
        try:
            from tqdm.auto import tqdm
            return tqdm(total=total, desc=desc, unit="block", leave=True)
        except Exception:
            pass

    class _Silent:
        def update(self, n=1):
            pass

        def close(self):
            pass
    return _Silent()


def _to_dtype(arr, dtype, nodata):
    """float32 pixels (NaN = NoData) -> the output type, with NoData filled in."""
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        missing = ~np.isfinite(arr)
        arr = np.clip(np.rint(arr), info.min, info.max)
        arr[missing] = nodata
        return arr.astype(dtype)
    if nodata is not None and not np.isnan(nodata):
        arr = np.where(np.isnan(arr), nodata, arr)
    return arr.astype(dtype, copy=False)


# ====================================================================== #
#  Errors
# ====================================================================== #

class RasterNotFoundError(FileNotFoundError):
    """Raised when the raster path does not exist."""


class RasterReadError(LibraryError):
    """Raised when the file exists but cannot be read as a raster."""


# ====================================================================== #
#  Band name list
# ====================================================================== #

class BandList(list):
    """A list of band names with `size()` and `get()` helpers.

    Behaves like a normal Python list (indexing, `len()`, `in`, loops).

    Examples
    --------
    >>> img.bandNames().size()     # number of bands
    26
    >>> img.bandNames().get(0)     # first band name
    'B1'
    """

    def size(self):
        """Returns the number of bands.

        Same as ``len(img.bandNames())``.

        Examples
        --------
        >>> img.bandNames().size()
        26
        """
        return len(self)

    def get(self, index):
        """Returns the band name at a position in the list.

        Parameters
        ----------
        index : int
            0-based position; negative numbers count from the end.

        Examples
        --------
        >>> img.bandNames().get(0)
        'B1'
        >>> img.bandNames().get(-1)          # last band
        'MSK_CLASSI_SNOW_ICE'
        """
        return self[index]


# ====================================================================== #
#  Palettes
# ====================================================================== #

# Built-in palettes (lists of hex colors). Any matplotlib colormap name also works.
PALETTES = {
    # --- vegetation ---
    "ndvi": ["FFFFFF", "CE7E45", "DF923D", "F1B555", "FCD163", "99B718", "74A901",
             "66A000", "529400", "3E8601", "207401", "056201", "004C00", "023B01",
             "012E01", "011D01", "011301"],
    "evi": ["8C510A", "D8B365", "F6E8C3", "C7E9C0", "74C476", "238B45", "00441B"],
    "vegetation": ["A50026", "F46D43", "FEE08B", "D9EF8B", "66BD63", "006837"],
    # --- water ---
    "ndwi": ["8B4513", "D2B48C", "F5F5DC", "ADD8E6", "4169E1", "00008B"],
    "water": ["FFFFFF", "C6DBEF", "6BAED6", "2171B5", "08306B"],
    "precipitation": ["FFFFFF", "00FFFF", "0080FF", "DA00FF", "FFA400", "FF0000"],
    "soil_moisture": ["8C510A", "D8B365", "F6E8C3", "C7EAE5", "5AB4AC", "01665E"],
    # --- land, urban, fire, snow ---
    "ndbi": ["2C7BB6", "ABD9E9", "FFFFBF", "FDAE61", "D7191C"],
    "nbr": ["D73027", "FC8D59", "FEE08B", "FFFFBF", "D9EF8B", "91CF60", "1A9850"],
    "ndsi": ["000000", "4D4D4D", "A6A6A6", "DCEBFA", "FFFFFF"],
    "elevation": ["006633", "E5FFCC", "662A00", "D8D8D8", "F5F5F5"],
    # --- climate / night ---
    "temperature": ["040274", "040281", "0502A3", "0502B8", "0502CE", "0502E6", "0602FF",
                    "235CB1", "307EF3", "269DB1", "30C8E2", "32D3EF", "3BE285", "3FF38F",
                    "86E26F", "3AE237", "B5E22E", "D6E21F", "FFF705", "FFD611", "FFB613",
                    "FF8B13", "FF6E08", "FF500D", "FF0000", "DE0101", "C21301", "A71001",
                    "911003"],
    "night_lights": ["000000", "1B1B3A", "4B0082", "FF8C00", "FFD700", "FFFFFF"],
    # --- general purpose ---
    "diverging": ["B2182B", "EF8A62", "FDDBC7", "F7F7F7", "D1E5F0", "67A9CF", "2166AC"],
    "change": ["D7191C", "FDAE61", "FFFFFF", "A6D96A", "1A9641"],
    "blue_red": ["0000FF", "00FFFF", "00FF00", "FFFF00", "FF0000"],
}

# Popular matplotlib colormaps shown by showPalettes() next to the built-ins.
_MPL_FAVORITES = ["viridis", "plasma", "inferno", "magma", "cividis", "RdYlGn", "RdYlBu",
                  "Spectral", "coolwarm", "terrain", "YlGn", "YlOrRd", "Blues", "Greens", "gray"]


def palettes():
    """Lists the built-in palettes.

    Any matplotlib colormap name (e.g. "viridis", "RdYlGn") also works as a palette.
    Add "_r" to any name to reverse it, e.g. "ndvi_r".

    Returns
    -------
    list of str
        Built-in palette names, sorted alphabetically.

    Examples
    --------
    >>> raster.palettes()
    ['blue_red', 'change', 'diverging', 'elevation', 'evi', 'nbr', ...]
    >>> "ndvi" in raster.palettes()
    True
    """
    return sorted(PALETTES)


def showPalettes(names=None, save_chart=False, dpi=200):
    """Shows a chart with a color strip for each palette.

    Parameters
    ----------
    names : list of str, optional
        Palettes to show. Default: all built-in palettes + popular matplotlib ones.
    save_chart : bool, default False
        Also save the chart as "palettes_<epoch time>.png".
    dpi : int, default 200
        Resolution of the saved PNG.

    Returns
    -------
    str or None
        The saved file name when save_chart=True, otherwise None.

    Examples
    --------
    >>> raster.showPalettes()                                    # all palettes
    >>> raster.showPalettes(["ndvi", "ndvi_r", "viridis"])        # just these
    >>> raster.showPalettes(save_chart=True, dpi=300)            # also save a PNG
    """
    names = list(names) if names else palettes() + _MPL_FAVORITES
    gradient = np.linspace(0, 1, 256)[None, :]
    fig, axes = plt.subplots(len(names), 1, figsize=(8, 0.32 * len(names) + 0.8))
    for ax, name in zip(np.atleast_1d(axes), names):
        ax.imshow(gradient, aspect="auto", cmap=_make_cmap(name))
        ax.set_axis_off()
        if not isinstance(name, str):
            label = "custom list"
        elif name in PALETTES or name.endswith("_r") and name[:-2] in PALETTES:
            label = name
        else:
            label = f"{name} (matplotlib)"
        ax.text(-0.01, 0.5, label, va="center", ha="right", fontsize=9, transform=ax.transAxes)
    fig.suptitle("Palettes (add '_r' to reverse)", fontsize=11)
    fig.subplots_adjust(left=0.25, right=0.98, top=1 - 0.5 / fig.get_figheight(),
                        bottom=0.02, hspace=0.45)
    saved = None
    if save_chart:
        saved = f"palettes_{int(time.time())}.png"
        fig.savefig(saved, dpi=dpi, bbox_inches="tight")
        print(f"Chart saved: {Path(saved).resolve()}  ({dpi} dpi)")
    plt.show()
    plt.close(fig)          # free the figure's memory
    return saved


def _make_cmap(palette):
    """Turn a palette name or a list of colors into a matplotlib colormap."""
    if palette is None:
        return matplotlib.colormaps["gray"]
    if isinstance(palette, str):
        reverse = palette.endswith("_r") and palette[:-2] in PALETTES
        base = palette[:-2] if reverse else palette
        if base in PALETTES:
            cmap = _make_cmap(PALETTES[base])
            return cmap.reversed() if reverse else cmap
        if palette in matplotlib.colormaps:
            return matplotlib.colormaps[palette]
        raise ValueError(
            f"Unknown palette '{palette}'. Built-in: {palettes()}. "
            f"Any matplotlib colormap also works (e.g. 'viridis', 'RdYlGn'). "
            f"Run raster.showPalettes() to see them."
        )
    # list of colors: hex without '#' (e.g. "00FF00") needs a '#' for matplotlib
    colors = ["#" + c if isinstance(c, str) and re.fullmatch(r"[0-9a-fA-F]{6}", c) else c
              for c in palette]
    if len(colors) < 2:
        raise ValueError("A palette list needs at least 2 colors.")
    try:
        return LinearSegmentedColormap.from_list("custom", colors)
    except ValueError:
        raise ValueError(f"Invalid color in palette: {list(palette)}") from None


# ====================================================================== #
#  Lazy evaluation engine
# ====================================================================== #
#
# Every band of an Image is a small "recipe" (a tuple), not pixels:
#   ("read",  path, band_number)        -> read this band from this file
#   ("const", value)                    -> a constant number
#   ("op",    name, left, right)        -> add / subtract / ... / lt / gt / and / or
#   ("unary", name, child)              -> abs / sqrt / log / log10 / exp / not
#   ("where", test, value, base)        -> value where test is non-zero, else base
#   ("remap", child, pairs, default)    -> replace listed values; others -> default
#   ("mask",  child, mask)              -> NaN where mask is 0 or NoData
#   ("crop",  child, row_off, col_off, height, width)
#                                       -> a window of the child's grid (used by clip)
#   ("clip",  child, shapes, transform, (height, width))
#                                       -> NaN outside the shapes (grid = this crop)
#   ("model", inputs, trained, output)  -> a trained clusterer / PCA applied to the
#                                          input bands; `output` = which result band
#   ("mosaic", sources, method, transform, (height, width))
#       sources = ((band_recipe, src_transform, (src_h, src_w), src_bounds), ...)
#                                       -> each window reads only the overlapping
#                                          part of each source (like a GDAL VRT)
#
# _evaluate() walks the recipe and computes pixels for ONE window at a time.
# `window` is always in the coordinates of the grid the node belongs to;
# a "crop" node shifts it into its child's grid.


def _as_mask(fn):
    """Wrap a comparison so it returns float32 1/0, and NaN where an input is NaN."""
    def op(a, b):
        out = np.asarray(fn(a, b), dtype="float32")
        if out.ndim == 0:
            return np.float32(out)
        out[np.isnan(a) | np.isnan(b)] = np.nan
        return out
    return op


def _not(a):
    out = np.asarray(a == 0, dtype="float32")
    if out.ndim:
        out[np.isnan(a)] = np.nan
    return out


_BINARY_OPS = {
    "add": np.add,
    "subtract": np.subtract,
    "multiply": np.multiply,
    "divide": np.divide,
    "pow": np.power,
    # comparisons -> 1 (true) / 0 (false)
    "lt": _as_mask(np.less),
    "lte": _as_mask(np.less_equal),
    "gt": _as_mask(np.greater),
    "gte": _as_mask(np.greater_equal),
    "eq": _as_mask(np.equal),
    "neq": _as_mask(np.not_equal),
    # logic: any non-zero value counts as true
    "and": _as_mask(lambda a, b: (a != 0) & (b != 0)),
    "or": _as_mask(lambda a, b: (a != 0) | (b != 0)),
}
_UNARY_OPS = {
    "abs": np.abs,
    "sqrt": np.sqrt,
    "log": np.log,
    "log10": np.log10,
    "exp": np.exp,
    "not": _not,
}


class _Reader:
    """Keeps source files open while computing, and remembers bands that were
    already read for the current band (so NDVI reads B8 and B4 only once).

    At most MAX_OPEN_FILES files are open at once (least recently used are
    closed), so mosaics of thousands of tiles don't run out of file handles.
    """

    MAX_OPEN_FILES = 64

    def __init__(self):
        self._datasets = OrderedDict()
        self._memo = {}
        self._block = (None, None)          # last model result: (key, arrays)

    def block_cache_get(self, key):
        return self._block[1] if self._block[0] == key else None

    def block_cache_set(self, key, value):
        self._block = (key, value)          # keeps only the latest block

    def _open(self, path):
        ds = self._datasets.pop(path, None)
        if ds is None:
            ds = rasterio.open(path)
            if len(self._datasets) >= self.MAX_OPEN_FILES:
                _, oldest = self._datasets.popitem(last=False)
                oldest.close()
        self._datasets[path] = ds                   # most recently used goes last
        return ds

    def read(self, path, band, window=None, out_shape=None):
        win_key = None if window is None else (window.col_off, window.row_off,
                                               window.width, window.height)
        key = (path, band, win_key, out_shape)
        if key not in self._memo:
            ds = self._open(path)
            # out_dtype="float32": half the RAM of float64, plenty for imagery.
            # out_shape: rasterio downsamples while reading (uses overviews if present).
            arr = ds.read(band, window=window, out_shape=out_shape,
                          out_dtype="float32", resampling=Resampling.nearest)
            if ds.nodata is not None and not np.isnan(ds.nodata):
                arr[arr == ds.nodata] = np.nan      # NoData -> NaN, so math skips it
            self._memo[key] = arr
        return self._memo[key]

    def read_into(self, path, band, out, window=None):
        """Read a band straight into an existing array (no temporary copy)."""
        ds = self._open(path)
        ds.read(band, out=out, window=window, resampling=Resampling.nearest)
        if ds.nodata is not None and not np.isnan(ds.nodata):
            out[out == ds.nodata] = np.nan

    def clear(self):
        """Forget cached bands (called after each output band, to free RAM)."""
        self._memo.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for ds in self._datasets.values():
            ds.close()
        self._datasets.clear()
        self._memo.clear()
        self._block = (None, None)


def _evaluate(node, reader, window=None, out_shape=None):
    """Compute one band recipe for one window. Returns a float32 array (or a scalar)."""
    kind = node[0]
    if kind == "read":
        return reader.read(node[1], node[2], window, out_shape)
    if kind == "const":
        return np.float32(node[1])
    if kind == "op":
        a = _evaluate(node[2], reader, window, out_shape)
        b = _evaluate(node[3], reader, window, out_shape)
        with np.errstate(all="ignore"):
            out = _BINARY_OPS[node[1]](a, b)
    elif kind == "unary":
        a = _evaluate(node[2], reader, window, out_shape)
        with np.errstate(all="ignore"):
            out = _UNARY_OPS[node[1]](a)
    elif kind == "where":
        test = _evaluate(node[1], reader, window, out_shape)
        value = _evaluate(node[2], reader, window, out_shape)
        base = _evaluate(node[3], reader, window, out_shape)
        hit = np.nan_to_num(test, nan=0.0) != 0     # NaN (NoData) test -> not a hit
        out = np.where(hit, value, base).astype("float32", copy=False)
    elif kind == "remap":
        source = _evaluate(node[1], reader, window, out_shape)
        pairs, default = node[2], node[3]
        if default is None:                         # unmatched values keep their value
            out = np.array(source, dtype="float32", copy=True)
        else:
            out = np.full(np.shape(source), default, dtype="float32")
        for old, new in pairs:                      # compare against the ORIGINAL values
            out[source == old] = new
        out[np.isnan(source)] = np.nan              # NoData stays NoData
    elif kind == "mask":
        value = _evaluate(node[1], reader, window, out_shape)
        mask = _evaluate(node[2], reader, window, out_shape)
        keep = np.nan_to_num(mask, nan=0.0) != 0
        out = np.where(keep, value, np.float32(np.nan)).astype("float32", copy=False)
    elif kind == "crop":
        _, child, row_off, col_off, height, width = node
        if window is None:
            child_window = Window(col_off, row_off, width, height)
        else:
            child_window = Window(col_off + window.col_off, row_off + window.row_off,
                                  window.width, window.height)
        return _evaluate(child, reader, child_window, out_shape)
    elif kind == "clip":
        _, child, shapes, transform, (height, width) = node
        value = _evaluate(child, reader, window, out_shape)
        win = window if window is not None else Window(0, 0, width, height)
        win_transform = rasterio.windows.transform(win, transform)
        rows, cols = out_shape if out_shape is not None else (int(win.height), int(win.width))
        if (rows, cols) != (win.height, win.width):           # downsampled preview
            win_transform = win_transform * Affine.scale(win.width / cols, win.height / rows)
        outside = geometry_mask(shapes, out_shape=(rows, cols), transform=win_transform)
        out = np.where(outside, np.float32(np.nan), value).astype("float32", copy=False)
    elif kind == "mosaic":
        out = _evaluate_mosaic(node, reader, window, out_shape)
    elif kind == "model":
        return _evaluate_model(node, reader, window, out_shape)
    else:
        raise RuntimeError(f"Unknown recipe node: {kind}")
    if isinstance(out, np.ndarray):
        out[~np.isfinite(out)] = np.nan             # x/0, log(0), sqrt(-1) -> NaN
    return out


def _evaluate_mosaic(node, reader, window, out_shape):
    """One window of a mosaic: read only the overlapping part of each source."""
    _, sources, method, transform, (height, width) = node
    win = window if window is not None else Window(0, 0, width, height)
    rows, cols = out_shape if out_shape is not None else (int(round(win.height)), int(round(win.width)))
    out_t = rasterio.windows.transform(win, transform)
    if (rows, cols) != (win.height, win.width):                 # downsampled preview
        out_t = out_t * Affine.scale(win.width / cols, win.height / rows)
    left, top = out_t * (0, 0)
    right, bottom = out_t * (cols, rows)

    out = np.full((rows, cols), np.nan, dtype="float32")
    if method == "mean":
        total = np.zeros((rows, cols), dtype="float64")
        count = np.zeros((rows, cols), dtype="uint32")
    for band_node, src_t, (src_h, src_w), (s_left, s_bottom, s_right, s_top) in sources:
        if s_left >= right or s_right <= left or s_bottom >= top or s_top <= bottom:
            continue                                            # fast reject: no overlap
        # the source's footprint, in pixels of this output window
        fw = rasterio.windows.from_bounds(s_left, s_bottom, s_right, s_top, transform=out_t)
        c0, r0 = max(0, int(round(fw.col_off))), max(0, int(round(fw.row_off)))
        c1 = min(cols, int(round(fw.col_off + fw.width)))
        r1 = min(rows, int(round(fw.row_off + fw.height)))
        if c1 <= c0 or r1 <= r0:
            continue
        # the matching window of the source (may be fractional: resampled on read)
        b = rasterio.windows.bounds(Window(c0, r0, c1 - c0, r1 - r0), out_t)
        sw = rasterio.windows.from_bounds(*b, transform=src_t)
        sc0, sr0 = max(0.0, sw.col_off), max(0.0, sw.row_off)
        sc1 = min(float(src_w), sw.col_off + sw.width)
        sr1 = min(float(src_h), sw.row_off + sw.height)
        if sc1 <= sc0 or sr1 <= sr0:
            continue
        values = _evaluate(band_node, reader, Window(sc0, sr0, sc1 - sc0, sr1 - sr0), (r1 - r0, c1 - c0))
        values = np.broadcast_to(values, (r1 - r0, c1 - c0))
        valid = np.isfinite(values)
        dest = out[r0:r1, c0:c1]                                # a view into `out`
        if method == "last":
            dest[valid] = values[valid]
        elif method == "first":
            take = valid & np.isnan(dest)
            dest[take] = values[take]
        elif method == "min":
            np.fmin(dest, values, out=dest)
        elif method == "max":
            np.fmax(dest, values, out=dest)
        else:                                                   # mean
            total[r0:r1, c0:c1][valid] += values[valid]
            count[r0:r1, c0:c1][valid] += 1
    if method == "mean":
        has = count > 0
        out[has] = (total[has] / count[has]).astype("float32")
    return out


def _evaluate_model(node, reader, window, out_shape):
    """Apply a trained clusterer / PCA to one window. All its output bands are
    computed at once and kept for the next band of the same window."""
    _, inputs, trained, output = node
    win_key = None if window is None else (window.col_off, window.row_off, window.width, window.height)
    key = (id(trained), id(inputs), win_key, out_shape)
    result = reader.block_cache_get(key)
    if result is None:
        arrays = [_evaluate(n, reader, window, out_shape) for n in inputs]
        shape = next((np.shape(a) for a in arrays if np.ndim(a)), None)
        if shape is None:                                   # only constants
            shape = out_shape or (int(window.height), int(window.width))
        flat = [np.broadcast_to(a, shape).reshape(-1) for a in arrays]
        npix = flat[0].size
        result = None
        # apply the model to MODEL_CHUNK pixels at a time, so its temporary
        # arrays stay small (a few MB) whatever the block size
        for start in range(0, npix, MODEL_CHUNK):
            stop = min(npix, start + MODEL_CHUNK)
            X = np.stack([f[start:stop] for f in flat], axis=1).astype("float64")
            valid = np.isfinite(X).all(axis=1)
            values = trained._apply(X[valid])                          # (valid rows, outputs)
            if result is None:
                result = np.full((values.shape[1], npix), np.nan, dtype="float32")
            result[:, start:stop][:, valid] = values.T
        result = result.reshape((result.shape[0],) + tuple(shape))
        reader.block_cache_set(key, result)
    return result[output]


# Pixels per step when a trained model (clusterer, PCA) is applied to a block.
MODEL_CHUNK = 262_144

# Side of the small pieces read around sampled pixels when samples are sparse.
SAMPLE_TILE = 256


def _sample_windows(rows, cols, width, height):
    """Windows to read for these pixels.

    Dense samples: whole blocks. Sparse samples (spread thinly over a large
    image): one small window per 256 x 256 tile that has samples, just big
    enough to cover that tile's sampled pixels (often a single pixel). GDAL
    reads files tile by tile, so this reads only the tiles that hold samples
    and computes only the pixels that are needed.
    """
    block, tile = _block_size(), SAMPLE_TILE
    n_blocks = len(np.unique((rows // block) * (-(-width // block)) + cols // block))
    tile_ids = (rows // tile) * (-(-width // tile)) + cols // tile
    unique, inverse = np.unique(tile_ids, return_inverse=True)
    order = np.argsort(inverse, kind="stable")
    groups = np.split(order, np.cumsum(np.bincount(inverse))[:-1])

    # Each tiny window has a fixed cost (a separate read call); measured on a
    # 2.9 GB raster, they win while samples touch fewer than ~6 tiles per block
    # (6 of the 16 tiles in a 1024 px block). On slow disks or files too big
    # for the file cache they win by more, since whole blocks read far more data.
    tiles_per_block = (block // tile) ** 2
    if len(unique) <= n_blocks * tiles_per_block * 6 / 16:            # sparse: tiny windows
        windows = []
        for g in groups:
            r0, r1 = int(rows[g].min()), int(rows[g].max())
            c0, c1 = int(cols[g].min()), int(cols[g].max())
            windows.append(Window(c0, r0, c1 - c0 + 1, r1 - r0 + 1))
        return windows, groups, tile

    per_row = -(-width // block)                                         # dense: whole blocks
    block_ids = (rows // block) * per_row + cols // block
    unique_b, inverse_b = np.unique(block_ids, return_inverse=True)
    order_b = np.argsort(inverse_b, kind="stable")
    groups_b = np.split(order_b, np.cumsum(np.bincount(inverse_b))[:-1])
    windows = [Window(int(b % per_row) * block, int(b // per_row) * block,
                      min(block, width - int(b % per_row) * block), min(block, height - int(b // per_row) * block))
               for b in unique_b]
    return windows, groups_b, block


def _positions_inside(area, transform, width, total, n, rng):
    """n random pixel positions (flat indices) whose centres fall inside `area`."""
    import shapely
    pool = np.empty(0, dtype="int64")
    for _ in range(40):
        want = n - len(pool)
        candidates = rng.integers(0, total, size=max(4 * want, 1024), dtype="int64")
        r, c = np.divmod(candidates, width)
        xs, ys = transform * (c + 0.5, r + 0.5)
        candidates = candidates[shapely.contains_xy(area, xs, ys)]
        pool = np.unique(np.concatenate([pool, candidates]))
        if len(pool) >= n:
            break
    if len(pool) > n:
        pool = rng.choice(pool, size=n, replace=False)
    return pool


def _window_cost(bands, block):
    """Estimated bytes to compute one block of these bands (for the memory budget)."""
    pixels = block * block
    cost = pixels * 4 * (len(bands) + 2)
    seen = set()
    stack = list(bands)
    while stack:                                            # models need room for their inputs
        node = stack.pop()
        if node[0] == "model" and id(node[1]) not in seen:
            seen.add(id(node[1]))
            cost += pixels * 4 * (len(node[1]) + 2) + MODEL_CHUNK * 8 * (len(node[1]) + 16)
        stack.extend(_children(node))
    return cost


def _children(node):
    """The sub-recipes inside a recipe node (tuples that start with a node kind)."""
    if node[0] == "mosaic":
        return [source[0] for source in node[1]]
    if node[0] == "model":
        return list(node[1])
    return [part for part in node[1:]
            if isinstance(part, tuple) and part and isinstance(part[0], str)]


def _preserves_values(node):
    """True if the recipe only passes pixel values through (reads, crops, clips,
    masks, first/last/min/max mosaics), so integer data can stay integer."""
    kind = node[0]
    if kind == "read":
        return True
    if kind in ("crop", "clip", "mask"):
        return _preserves_values(node[1])
    if kind == "mosaic":
        return node[2] != "mean" and all(_preserves_values(s[0]) for s in node[1])
    if kind == "model":                                     # cluster numbers are whole numbers
        from .cluster import TrainedClusterer
        return isinstance(node[2], TrainedClusterer)
    return False


# ---- expression() support: which Python syntax maps to which operation ----
_EXPR_BINOPS = {ast.Add: _operator.add, ast.Sub: _operator.sub, ast.Mult: _operator.mul,
                ast.Div: _operator.truediv, ast.Pow: _operator.pow}
_EXPR_COMPARE = {ast.Lt: "lt", ast.LtE: "lte", ast.Gt: "gt", ast.GtE: "gte",
                 ast.Eq: "eq", ast.NotEq: "neq"}
_FLIPPED = {"lt": "gt", "lte": "gte", "gt": "lt", "gte": "lte", "eq": "eq", "neq": "neq"}
_PY_COMPARE = {"lt": _operator.lt, "lte": _operator.le, "gt": _operator.gt,
               "gte": _operator.ge, "eq": _operator.eq, "neq": _operator.ne}
_EXPR_FUNCS = ("abs", "sqrt", "log", "log10", "exp")


def _compare(name, a, b):
    """a <name> b where either side may be an Image or a number."""
    if isinstance(a, Image):
        return a._math(b, name)
    if isinstance(b, Image):
        return b._math(a, _FLIPPED[name])           # 0.1 < x  is  x > 0.1
    return float(_PY_COMPARE[name](a, b))


def _logic(name, a, b):
    """a and/or b where either side may be an Image or a number."""
    if isinstance(a, Image):
        return a._math(b, name)
    if isinstance(b, Image):
        return b._math(a, name)                     # and / or are symmetric
    return float(bool(a) and bool(b)) if name == "and" else float(bool(a) or bool(b))


# ====================================================================== #
#  Image
# ====================================================================== #

class Image:
    """A lazy raster image: operations build a recipe, and pixels are read only when needed.

    Create one with `raster.image(path)`.
    """

    # Makes `2 * img` and `np.float32(2) * img` use Image's own math.
    __array_ufunc__ = None

    def __init__(self, path):
        # __init__ runs when the object is created: Image("file.tif")
        # Only METADATA is read here. No pixels are loaded.
        file = Path(path)
        if not file.is_file():
            raise RasterNotFoundError(
                f"Raster file not found: '{path}'\n"
                f"Looked in: {file.resolve()}"
            )
        try:
            src = rasterio.open(path)
        except RasterioIOError:
            # 'from None' hides rasterio's own traceback from the error output
            raise RasterReadError(f"Could not read '{path}' as a raster (not a valid GeoTIFF?)") from None

        with src:
            self.path = str(path)
            self._grid = {
                "width": src.width, "height": src.height,
                "transform": src.transform, "crs": src.crs,
                "bounds": src.bounds, "res": src.res,
                "driver": src.driver, "dtype": src.dtypes[0], "nodata": src.nodata,
            }
            # Band names come from the GeoTIFF band descriptions; unnamed -> "B1", "B2", ...
            self._names = [desc if desc else f"B{i}"
                           for i, desc in enumerate(src.descriptions, start=1)]
            # absolute path, so it still works if the working folder changes later
            abs_path = str(file.resolve())
            self._bands = [("read", abs_path, i) for i in range(1, src.count + 1)]

    @classmethod
    def _create(cls, path, grid, bands, names):
        """Build an Image from recipes, without opening any file."""
        new = object.__new__(cls)
        new.path = path
        new._grid = grid
        new._bands = list(bands)
        new._names = list(names)
        return new

    # ------------------------------------------------------------------ #
    #  Basic properties
    # ------------------------------------------------------------------ #

    def bounds(self, save=False):
        """Returns the image's bounding box, and optionally saves it as GeoJSON.

        Parameters
        ----------
        save : bool or str, default False
            True saves the bounding box as a rectangle to
            "<image>_bounds_<epoch>.geojson" in the current folder; a string
            saves to that path (GeoJSON for .geojson / .json; other vector
            formats such as .shp or .kml also work). GeoJSON is written in
            EPSG:4326 (longitude/latitude).

        Returns
        -------
        BoundingBox
            (left, bottom, right, top) in the image's CRS. Read the values as
            ``b.left``, ``b.bottom``, ``b.right``, ``b.top``.

        Examples
        --------
        >>> extent = evi.bounds()
        >>> extent.left, extent.top
        >>> extent = evi.bounds(save=True)              # also writes a .geojson
        >>> extent = evi.bounds(save="evi_extent.geojson")
        """
        box = self._grid["bounds"]
        if save is True or (save and isinstance(save, (str, Path))):
            from shapely.geometry import box as _box
            from . import vector as _vector
            stem = Path(self.path).stem
            path = save if save is not True else f"{stem}_bounds_{int(time.time())}.geojson"
            rectangle = _vector._from_geometries(
                [_box(box.left, box.bottom, box.right, box.top)], crs=self._grid["crs"],
                properties={"name": [stem]}, source=f"{stem} (bounds)", fmt="Bounding box")
            rectangle.save(path)
        elif save is not False and save is not None:
            raise TypeError("bounds(): `save` must be True, False or a file path")
        return box

    @property
    def res(self):
        """The pixel size (x, y), in CRS units.

        Degrees for a geographic CRS such as EPSG:4326, metres for UTM.

        Examples
        --------
        >>> img.res
        (8.983152841195215e-05, 8.983152841195215e-05)
        >>> xres, yres = raster.image("utm_scene.tif").res      # e.g. (10.0, 10.0) metres
        """
        return self._grid["res"]

    @property
    def data(self):
        """All pixels at full resolution, as a float32 numpy array (bands, rows, cols).

        Computed every time it is accessed, and uses the most memory of any
        operation. Store it in a variable if you need it more than once
        (``arr = img.data``). For large images prefer visualize() (preview)
        or save() (streamed). NoData pixels are NaN.

        Examples
        --------
        >>> arr = img.select("B8").data          # compute once, keep the array
        >>> arr.shape
        (1, 551, 763)
        >>> float(np.nanmean(arr))               # average NIR value
        """
        return self._compute()

    def bandNames(self):
        """Returns the band names, in band order.

        Returns
        -------
        BandList
            A list of names, with ``.size()`` (number of bands) and ``.get(i)``.

        Examples
        --------
        >>> img.bandNames()
        ['B1', 'B2', 'B3', ...]
        >>> img.bandNames().size()
        26
        """
        return BandList(self._names)

    # ------------------------------------------------------------------ #
    #  Band selection / naming
    # ------------------------------------------------------------------ #

    def select(self, *bands):
        """Selects bands, returning a new image that contains only those bands.

        No pixels are read or copied; the new image just points at the chosen bands.

        Parameters
        ----------
        *bands : str, int, or a list of them
            Band names (e.g. "B4") or 1-based band numbers (e.g. 4), separately
            or as one list. The new image keeps the order you give.

        Returns
        -------
        Image

        Raises
        ------
        ValueError
            If no bands are given, or a band does not exist.

        Examples
        --------
        >>> nir = img.select("B8")
        >>> rgb = img.select("B4", "B3", "B2")
        >>> rgb = img.select(["B4", "B3", "B2"])
        >>> rgb = img.select([4, 3, 2])
        """
        bands = self._flatten(bands)
        if len(bands) == 0:
            raise ValueError(f"select() needs at least one band. Available: {self._names}")
        idx = [self._band_index(b) for b in bands]
        return Image._create(self.path, self._grid,
                             [self._bands[i] for i in idx], [self._names[i] for i in idx])

    def rename(self, *names):
        """Renames the bands, returning a new image.

        Parameters
        ----------
        *names : str, or a list of str
            One new name per band, in band order.

        Returns
        -------
        Image

        Raises
        ------
        ValueError
            If the number of names doesn't match the number of bands.

        Examples
        --------
        >>> ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
        >>> img.select("B4", "B3").rename(["red", "green"])
        """
        names = self._flatten(names)
        if len(names) != len(self._bands):
            raise ValueError(f"rename() needs {len(self._bands)} name(s), got {len(names)}: {names}")
        if not all(isinstance(n, str) for n in names):
            raise ValueError(f"Band names must be strings, got {names}")
        return Image._create(self.path, self._grid, self._bands, names)

    def addBands(self, other):
        """Adds the bands of another image after this image's bands, returning a new image.

        Parameters
        ----------
        other : Image
            Must be on the same pixel grid, with band names not already in this image.

        Returns
        -------
        Image

        Raises
        ------
        TypeError
            If `other` is not an Image.
        ValueError
            If the grids differ or band names would be duplicated.

        Examples
        --------
        >>> stack = img.select("B4", "B8").addBands(ndvi.rename("NDVI"))
        """
        if not isinstance(other, Image):
            raise TypeError("addBands() expects an Image")
        self._check_same_grid(other)
        clash = set(self._names) & set(other._names)
        if clash:
            raise ValueError(f"Band name(s) {sorted(clash)} already exist. Use .rename() first.")
        return Image._create(self.path, self._grid,
                             self._bands + other._bands, self._names + other._names)

    def split(self, save=False):
        """Splits the image into single-band images, one per band.

        Parameters
        ----------
        save : bool or str, default False
            True also saves each band as its own GeoTIFF, named
            "<image>_<band>.tif", in the current folder; a string saves them
            into that folder (created if needed).

        Returns
        -------
        list of Image
            One single-band image per band, in band order.

        Examples
        --------
        >>> bands = img.split()
        >>> bands[3].bandNames()                 # ['B4']
        >>> img.select("B2", "B3", "B4", "B8").split(save="bands/")
        """
        parts = [Image._create(self.path, self._grid, [node], [name])
                 for node, name in zip(self._bands, self._names)]
        if save is True or (save and isinstance(save, (str, Path))):
            folder = Path(".") if save is True else Path(save)
            folder.mkdir(parents=True, exist_ok=True)
            stem = Path(self.path).stem
            bar = _progress_bar(total=len(parts), desc=f"Splitting {stem}", enabled=len(parts) > 1)
            try:
                for part, name in zip(parts, self._names):
                    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", name)
                    part.save(folder / f"{stem}_{safe}.tif", progress=False)
                    bar.update(1)
            finally:
                bar.close()
        elif save is not False and save is not None:
            raise TypeError("split(): `save` must be True, False or a folder path")
        return parts

    # ------------------------------------------------------------------ #
    #  Band math
    # ------------------------------------------------------------------ #

    def add(self, other, save=False):
        """Adds `other` to the image, band by band.

        Operator form: ``img + other``.

        Parameters
        ----------
        other : Image or number
            Paired band by band; a 1-band image or a number is applied to every band.
        save : bool or str, default False
            True saves "<image>_add_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> brighter = img.select("B4").add(500)
        >>> total = img.select("B4").add(img.select("B3"))        # or img.select("B4") + img.select("B3")
        """
        return self._math(other, "add", save)

    def subtract(self, other, save=False):
        """Subtracts `other` from the image, band by band.

        Operator form: ``img - other``.

        Parameters
        ----------
        other : Image or number
            Paired band by band; a 1-band image or a number is applied to every band.
        save : bool or str, default False
            True saves "<image>_subtract_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> diff = img.select("B8").subtract(img.select("B4"))     # or img.select("B8") - img.select("B4")
        >>> offset = img.select("B4", "B3", "B2").subtract(1000, save="offset.tif")
        """
        return self._math(other, "subtract", save)

    def multiply(self, other, save=False):
        """Multiplies the image by `other`, band by band.

        Operator form: ``img * other``.

        Parameters
        ----------
        other : Image or number
            Paired band by band; a 1-band image or a number is applied to every band.
        save : bool or str, default False
            True saves "<image>_multiply_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> reflectance = img.multiply(0.0001)
        """
        return self._math(other, "multiply", save)

    def divide(self, other, save=False):
        """Divides the image by `other`, band by band.

        Operator form: ``img / other``. Division by zero gives NaN.

        Parameters
        ----------
        other : Image or number
            Paired band by band; a 1-band image or a number is applied to every band.
        save : bool or str, default False
            True saves "<image>_divide_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> ratio = img.select("B8").divide(img.select("B4"), save=True)
        """
        return self._math(other, "divide", save)

    def pow(self, other, save=False):
        """Raises the image to the power of `other`, band by band.

        Operator form: ``img ** other``.

        Parameters
        ----------
        other : Image or number
            Paired band by band; a 1-band image or a number is applied to every band.
        save : bool or str, default False
            True saves "<image>_pow_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> squared = img.select("B8").pow(2)                       # or img.select("B8") ** 2
        >>> root = img.select("B8").pow(0.5)
        """
        return self._math(other, "pow", save)

    def abs(self, save=False):
        """Calculates the absolute value of every band.

        Operator form: ``abs(img)``. `save` works as in add().

        Examples
        --------
        >>> change = (after.select("B8") - before.select("B8")).abs()
        >>> magnitude = abs(img.normalizedDifference(["B8", "B4"]))
        """
        return self._unary("abs", save)

    def sqrt(self, save=False):
        """Calculates the square root of every band.

        Negative values become NaN. `save` works as in add().

        Examples
        --------
        >>> root = img.select("B8").sqrt()
        >>> distance = (img.select("B4") ** 2 + img.select("B3") ** 2).sqrt()
        """
        return self._unary("sqrt", save)

    def log(self, save=False):
        """Calculates the natural logarithm of every band.

        Values <= 0 become NaN. `save` works as in add().

        Examples
        --------
        >>> log_nir = img.select("B8").log()
        >>> ratio = (img.select("B8") / img.select("B4")).log()      # log ratio
        """
        return self._unary("log", save)

    def log10(self, save=False):
        """Calculates the base-10 logarithm of every band.

        Values <= 0 become NaN. `save` works as in add().

        Examples
        --------
        >>> decibels = backscatter.log10() * 10          # radar backscatter to dB
        """
        return self._unary("log10", save)

    def exp(self, save=False):
        """Calculates e raised to the power of every band.

        `save` works as in add().

        Examples
        --------
        >>> linear = img.select("B8").log().exp()        # undo log()
        """
        return self._unary("exp", save)

    # ------------------------------------------------------------------ #
    #  Comparisons and logic  ->  1 where true, 0 where false, NaN on NoData
    # ------------------------------------------------------------------ #

    def lt(self, other, save=False):
        """Tests whether the image is less than `other`.

        Returns 1 where true and 0 where false (NaN on NoData).
        Operator form: ``img < other``. `save` works as in add().

        Examples
        --------
        >>> water = ndvi.lt(0)                       # or ndvi < 0
        >>> darker = img.select("B4").lt(img.select("B3"))
        """
        return self._math(other, "lt", save)

    def lte(self, other, save=False):
        """Tests whether the image is less than or equal to `other`.

        Returns 1 where true and 0 where false (NaN on NoData).
        Operator form: ``img <= other``. `save` works as in add().

        Examples
        --------
        >>> sparse = ndvi.lte(0.3)                   # or ndvi <= 0.3
        """
        return self._math(other, "lte", save)

    def gt(self, other, save=False):
        """Tests whether the image is greater than `other`.

        Returns 1 where true and 0 where false (NaN on NoData).
        Operator form: ``img > other``. `save` works as in add().

        Examples
        --------
        >>> vegetation = ndvi.gt(0.3)                # or ndvi > 0.3
        >>> share = ndvi.gt(0.3).data.mean()         # fraction of vegetated pixels
        """
        return self._math(other, "gt", save)

    def gte(self, other, save=False):
        """Tests whether the image is greater than or equal to `other`.

        Returns 1 where true and 0 where false (NaN on NoData).
        Operator form: ``img >= other``. `save` works as in add().

        Examples
        --------
        >>> dense = ndvi.gte(0.6)                    # or ndvi >= 0.6
        """
        return self._math(other, "gte", save)

    def eq(self, other, save=False):
        """Tests whether the image is equal to `other`, e.g. to pick a class.

        Returns 1 where true and 0 where false (NaN on NoData). There is no
        ``==`` operator form, so images keep working in lists and dicts.

        Parameters
        ----------
        other : Image, number, or list of numbers
            A list picks several values at once: 1 where the pixel equals ANY of them.
        save : bool or str, default False
            True saves "<image>_eq_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> dense = classified.eq(4)            # 1 where class 4, else 0
        >>> veg = classified.eq([3, 4])         # 1 where class 3 or 4
        """
        if isinstance(other, (list, tuple, set)):
            return self._any_of(other, save, "eq")
        return self._math(other, "eq", save)

    def neq(self, other, save=False):
        """Tests whether the image is not equal to `other`.

        Returns 1 where true and 0 where false (NaN on NoData). There is no
        ``!=`` operator form. A list gives 1 where the pixel equals NONE of
        the values. `save` works as in add().

        Examples
        --------
        >>> not_water = classified.neq(1)
        >>> land = classified.neq([0, 1])            # neither class 0 nor class 1
        """
        if isinstance(other, (list, tuple, set)):
            return self._any_of(other, False, "neq").Not()._maybe_save(save, "neq")
        return self._math(other, "neq", save)

    # ------------------------------------------------------------------ #
    #  Classes and masks
    # ------------------------------------------------------------------ #

    def selectClasses(self, classes, save=False):
        """Selects one or more classes: keeps those pixel values, all others become NoData.

        Parameters
        ----------
        classes : number or list of numbers
            The class values to keep.
        save : bool or str, default False
            True saves "<image>_selectClasses_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image
            Same values for the chosen classes, NaN (NoData) everywhere else.

        Examples
        --------
        >>> water = classified.selectClasses(1)
        >>> vegetation = classified.selectClasses([3, 4])
        """
        values = self._class_values(classes, "selectClasses")
        result = self.remap(values, values, default=float("nan"))
        return result._maybe_save(save, "selectClasses")

    def mergeClasses(self, classes, into=None, save=False):
        """Merges two or more classes into one class value.

        Pixels of the other classes are left unchanged.

        Parameters
        ----------
        classes : list of numbers, or dict
            The class values to merge, together with `into`. Or a dict
            ``{new_value: [old_values]}`` to do several merges at once
            (then leave out `into`).
        into : number, optional
            The value the merged classes get. Required unless `classes` is a dict.
        save : bool or str, default False
            True saves "<image>_mergeClasses_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Raises
        ------
        ValueError
            If `into` is missing, or the same class is listed in two groups.

        Examples
        --------
        >>> merged = classified.mergeClasses([1, 2], into=1)
        >>> merged = classified.mergeClasses({1: [1, 2], 2: [3, 4]})   # 4 classes -> 2
        """
        if isinstance(classes, dict):
            if into is not None:
                raise ValueError("mergeClasses(): when `classes` is a dict, leave out `into`.")
            groups = classes
        else:
            if into is None:
                raise ValueError("mergeClasses() needs `into`, e.g. mergeClasses([1, 2], into=1), "
                                 "or a dict {new_value: [old_values]}.")
            groups = {into: classes}
        mapping = {}
        for new, olds in groups.items():
            for old in self._class_values(olds, "mergeClasses"):
                if old in mapping and mapping[old] != new:
                    raise ValueError(f"mergeClasses(): class {old:g} is listed in more than one group.")
                mapping[old] = new
        result = self.remap(mapping)
        return result._maybe_save(save, "mergeClasses")

    def updateMask(self, mask, save=False):
        """Masks the image: pixels where `mask` is 0 (or NoData) become NoData.

        Parameters
        ----------
        mask : Image
            1 band (applied to every band) or the same number of bands as this
            image. Any non-zero value keeps the pixel.
        save : bool or str, default False
            True saves "<image>_updateMask_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Examples
        --------
        >>> dense_only = img.select("B4", "B3", "B2").updateMask(classified.eq(4))
        >>> no_water = ndvi.updateMask(ndvi.gt(0))
        """
        if not isinstance(mask, Image):
            raise TypeError("updateMask() expects an Image, e.g. classified.eq(3)")
        masks = self._match_bands(mask, "updateMask(): the mask")
        nodes = [("mask", node, m) for node, m in zip(self._bands, masks)]
        result = Image._create(self.path, self._grid, nodes, self._names)
        return result._maybe_save(save, "updateMask")

    # ------------------------------------------------------------------ #
    #  Geometry and clipping
    # ------------------------------------------------------------------ #

    def geometry(self):
        """Returns the image extent (footprint) as a Vector with one polygon.

        The polygon is in the image's CRS. Print it, inspect it with .meta(),
        or export it, e.g. ``img.geometry().save("extent.geojson")``.

        Returns
        -------
        vector.Vector

        Examples
        --------
        >>> extent = img.geometry()
        >>> print(extent)
        >>> extent.toGeoJSON()
        >>> extent.save("extent.geojson")
        """
        from shapely.geometry import Polygon
        from . import vector as _vector
        g = self._grid
        t, w, h = g["transform"], g["width"], g["height"]
        corners = [t * (0, 0), t * (w, 0), t * (w, h), t * (0, h)]
        name = Path(self.path).stem
        return _vector._from_geometries([Polygon(corners)], crs=g["crs"],
                                        properties={"name": [name]},
                                        source=f"{name} (extent)", fmt="Image extent")

    def clip(self, geometry, save=False):
        """Clips the image to a vector: crops to its extent and masks pixels outside it.

        Pixels outside the shapes become NoData. The result is also cropped to
        the vector's bounding box, so it is smaller, faster and uses less memory.
        The vector is reprojected to the image's CRS automatically.

        Parameters
        ----------
        geometry : vector.Vector
            e.g. ``vector.Open("boundary.shp")`` or another image's ``.geometry()``.
        save : bool or str, default False
            True saves "<image>_clip_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image

        Raises
        ------
        TypeError
            If `geometry` is not a Vector.
        ValueError
            If the vector has no geometries or no CRS, or does not overlap the image.

        Examples
        --------
        >>> from pyearthforge import raster, vector
        >>> aoi = vector.Open("boundary.shp")
        >>> clipped = img.clip(aoi)
        >>> img.normalizedDifference(["B8", "B4"]).clip(aoi, save="ndvi_aoi.tif")
        """
        from . import vector as _vector
        if not isinstance(geometry, _vector.Vector):
            raise TypeError("clip() expects a Vector, e.g. vector.Open('boundary.shp')")
        g = self._grid
        shapes = geometry._shapes_in(g["crs"])
        import shapely
        minx, miny, maxx, maxy = shapely.total_bounds(np.asarray(shapes, dtype=object))

        # pixel window of the vector's bounding box, rounded outwards, inside the image
        win = rasterio.windows.from_bounds(minx, miny, maxx, maxy, transform=g["transform"])
        col0 = max(0, math.floor(win.col_off + 1e-9))
        row0 = max(0, math.floor(win.row_off + 1e-9))
        col1 = min(g["width"], math.ceil(win.col_off + win.width - 1e-9))
        row1 = min(g["height"], math.ceil(win.row_off + win.height - 1e-9))
        if col1 <= col0 or row1 <= row0:
            raise ValueError("The vector does not overlap the image.")
        crop = Window(col0, row0, col1 - col0, row1 - row0)
        transform = rasterio.windows.transform(crop, g["transform"])
        height, width = int(crop.height), int(crop.width)

        grid = dict(g)
        grid.update(width=width, height=height, transform=transform,
                    bounds=BoundingBox(*rasterio.windows.bounds(crop, g["transform"])))
        nodes = [("clip", ("crop", node, row0, col0, height, width), shapes, transform, (height, width))
                 for node in self._bands]
        result = Image._create(self.path, grid, nodes, self._names)
        return result._maybe_save(save, "clip")

    def And(self, other, save=False):
        """Calculates the logical AND of the image and `other`.

        Returns 1 where both are non-zero and 0 elsewhere (NaN on NoData).
        Operator form: ``a & b``. Capitalized because `and` is a Python keyword.
        `save` works as in add().

        Examples
        --------
        >>> moderate = ndvi.gt(0.34).And(ndvi.lt(0.5))
        >>> moderate = (ndvi > 0.34) & (ndvi < 0.5)
        """
        return self._math(other, "and", save)

    def Or(self, other, save=False):
        """Calculates the logical OR of the image and `other`.

        Returns 1 where either is non-zero and 0 elsewhere (NaN on NoData).
        Operator form: ``a | b``. `save` works as in add().

        Examples
        --------
        >>> extreme = ndvi.lt(0).Or(ndvi.gt(0.8))
        >>> extreme = (ndvi < 0) | (ndvi > 0.8)      # same, with operators
        """
        return self._math(other, "or", save)

    def Not(self, save=False):
        """Calculates the logical NOT of the image.

        Returns 1 where the image is 0 and 0 elsewhere (NaN on NoData).
        Operator form: ``~img``. `save` works as in add().

        Examples
        --------
        >>> land = ndvi.lt(0).Not()                  # everything that isn't water
        >>> land = ~(ndvi < 0)                       # same, with operators
        """
        return self._unary("not", save)

    # Lowercase spellings. `.and()` / `.or()` / `.not()` are impossible in Python
    # (reserved words), so these follow Python's convention of a trailing underscore.
    and_ = And
    or_ = Or
    not_ = Not

    # ------------------------------------------------------------------ #
    #  Reclassification
    # ------------------------------------------------------------------ #

    def where(self, *rules, otherwise=None, save=False):
        """Reclassifies a raster: replaces pixel values wherever a condition is true.

        Rules are applied in order, so where rules overlap the last matching
        rule wins (the same as chaining several .where() calls).

        Parameters
        ----------
        *rules : (test, value) pairs
            `test` is an Image (a condition such as ndvi.lt(0.1)); pixels where it
            is non-zero get `value` (a number or an Image). Accepted forms:

            - ``where(test, value)``                         one rule
            - ``where((test1, 1), (test2, 2), ...)``         several tuples
            - ``where([(test1, 1), (test2, 2)])``            a list of tuples
            - ``where({test1: 1, test2: 2})``                a dict (keeps order)
            - ``where({test1, 1}, {test2, 2})``              2-item sets, when the
              value is a number (sets have no order, so the Image is taken as
              the test and the number as the value)
        otherwise : number, optional
            Value for pixels that match no rule. Default: keep the original value.
            Use e.g. ``otherwise=0`` for a clean class map.
        save : bool or str, default False
            True saves "<image>_where_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image
            Same band names as this image. NoData pixels in a test never match.

        Raises
        ------
        ValueError
            If no rules are given, a rule is malformed, or band counts don't match.
        TypeError
            If a test is not an Image, or a value / `otherwise` is not a number or Image.

        Examples
        --------
        >>> ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
        >>> classes = ndvi.where(
        ...     (ndvi.lt(0.1), 1),                          # water / bare
        ...     (ndvi.gte(0.1).And(ndvi.lt(0.34)), 2),      # sparse vegetation
        ...     (ndvi.gte(0.34).And(ndvi.lt(0.5)), 3),      # moderate vegetation
        ...     (ndvi.gte(0.5), 4),                         # dense vegetation
        ...     otherwise=0,
        ... ).rename("class")
        >>> classes.visualize(labels={1: "Water", 2: "Sparse", 3: "Moderate", 4: "Dense"},
        ...                   palette=["0000FF", "D2B48C", "9ACD32", "006400"])
        >>> ndvi.where(ndvi.lt(0), 0)                       # clip negatives to 0
        >>> ndvi.where({ndvi < 0.1: 1, ndvi >= 0.5: 2}, otherwise=0)
        """
        pairs = self._parse_rules(rules)
        n = len(self._bands)
        if otherwise is None:
            nodes = list(self._bands)
        elif isinstance(otherwise, (int, float, np.number)):
            nodes = [("const", float(otherwise))] * n
        else:
            raise TypeError("'otherwise' must be a number")
        for test, value in pairs:
            tests = self._match_bands(test, "where(): the test")
            values = self._match_bands(value, "where(): the value")
            nodes = [("where", t, v, base) for t, v, base in zip(tests, values, nodes)]
        result = Image._create(self.path, self._grid, nodes, self._names)
        return result._maybe_save(save, "where")

    def remap(self, from_values, to_values=None, default=None, save=False):
        """Remaps pixel values: replaces each listed value with a new value.

        Useful for recoding class maps, e.g. merging classes or converting
        codes from one classification scheme to another. Values are matched
        exactly, so it is meant for class / integer values. Every listed value
        is matched against the ORIGINAL pixel values, so a chain like 1 -> 2,
        2 -> 3 does not turn 1 into 3.

        Parameters
        ----------
        from_values : list of numbers, or dict
            The values to replace. Or a dict {old: new} (then leave `to_values` out).
        to_values : list of numbers, optional
            The new values, one for each entry in `from_values`.
        default : number, optional
            Value for pixels whose value is not listed. Default: keep their
            original value. Use ``default=float("nan")`` to turn them into NoData.
        save : bool or str, default False
            True saves "<image>_remap_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image
            Same band names as this image. Applied to every band. NoData stays NoData.

        Raises
        ------
        ValueError
            If the lists are empty or have different lengths, or a value is not a number.
        TypeError
            If `default` is not a number.

        Examples
        --------
        >>> merged = classes.remap([1, 2, 3, 4], [1, 1, 2, 2])          # 4 classes -> 2
        >>> merged = classes.remap({1: 1, 2: 1, 3: 2, 4: 2})            # same, as a dict
        >>> veg = classes.remap([3, 4], [1, 1], default=0)               # binary mask
        >>> only_water = classes.remap([1], [1], default=float("nan"))   # others -> NoData
        """
        if isinstance(from_values, dict):
            if to_values is not None:
                raise ValueError("remap(): when `from_values` is a dict, leave out `to_values`.")
            from_values, to_values = list(from_values.keys()), list(from_values.values())
        if to_values is None:
            raise ValueError("remap() needs `to_values`, or a dict {old: new} as `from_values`.")
        from_values, to_values = list(from_values), list(to_values)
        if not from_values:
            raise ValueError("remap() needs at least one value to replace.")
        if len(from_values) != len(to_values):
            raise ValueError(f"remap(): `from_values` has {len(from_values)} values but "
                             f"`to_values` has {len(to_values)}; they must match.")
        numbers = (int, float, np.number)
        bad = [v for v in from_values + to_values
               if not isinstance(v, numbers) or isinstance(v, bool)]
        if bad:
            raise ValueError(f"remap(): all values must be numbers, got {bad}")
        if default is not None and (not isinstance(default, numbers) or isinstance(default, bool)):
            raise TypeError("remap(): `default` must be a number (or float('nan') for NoData).")

        pairs = tuple((float(a), float(b)) for a, b in zip(from_values, to_values))
        default = None if default is None else float(default)
        nodes = [("remap", node, pairs, default) for node in self._bands]
        result = Image._create(self.path, self._grid, nodes, self._names)
        return result._maybe_save(save, "remap")

    # ------------------------------------------------------------------ #
    #  Expressions
    # ------------------------------------------------------------------ #

    def expression(self, expression, map=None, save=False):
        """Evaluates a mathematical expression on the image's bands.

        The text is parsed safely; it is never run as Python code.

        Parameters
        ----------
        expression : str
            The formula. It can use:

            - names from `map`, and this image's band names directly (e.g. ``B8``)
            - ``b('B8')`` (by name) or ``b(7)`` (0-based index)
            - numbers, ``+ - * / **``, ``( )``
            - comparisons ``< <= > >= == !=`` (give 1 / 0)
            - logic ``&&`` ``||`` ``!`` or ``and`` ``or`` ``not``
            - functions ``abs sqrt log log10 exp``
        map : dict, optional
            Variable name -> Image or number, e.g. {"NIR": img.select("B8")}.
        save : bool or str, default False
            True saves "<image>_expression_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image
            Band names follow the band-math rules (the left-most image's names);
            use .rename() to name the result.

        Raises
        ------
        ValueError
            If the formula is invalid, uses unsupported syntax or unknown names,
            or contains no band or image.
        TypeError
            If a `map` value is not an Image or a number.

        Examples
        --------
        >>> evi = img.expression(
        ...     "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
        ...     {"NIR": img.select("B8").multiply(0.0001),
        ...      "RED": img.select("B4").multiply(0.0001),
        ...      "BLUE": img.select("B2").multiply(0.0001)},
        ... ).rename("EVI")
        >>> ndvi = img.expression("(B8 - B4) / (B8 + B4)")
        >>> water = img.expression("b('B3') > b('B8') && B8 < 1500")
        """
        if not isinstance(expression, str) or not expression.strip():
            raise ValueError("expression() needs a non-empty formula string")
        variables = dict(map or {})
        for name, value in variables.items():
            if isinstance(value, Image):
                self._check_same_grid(value)
            elif not isinstance(value, (int, float, np.number)):
                raise TypeError(f"map value for '{name}' must be an Image or a number, "
                                f"got {type(value).__name__}")

        # && || !  ->  Python's and / or / not
        text = expression.replace("&&", " and ").replace("||", " or ")
        text = re.sub(r"!(?!=)", " not ", text)
        try:
            tree = ast.parse(text.strip(), mode="eval")
        except SyntaxError as e:
            raise ValueError(f"Invalid expression {expression!r}: {e.msg}") from None
        result = self._eval_expr(tree.body, variables)
        if not isinstance(result, Image):
            raise ValueError("The expression must use at least one band or image.")
        return result._maybe_save(save, "expression")

    Expression = expression     # alias, so img.Expression(...) also works

    def normalizedDifference(self, bandNames=None, save=False):
        """Calculates the Normalized Difference between two bands.

        Computes ``(first - second) / (first + second)`` for every pixel. This
        is the formula behind NDVI, NDWI, NDBI, NBR, NDSI and similar indices.
        Pixels where the sum is 0 become NaN.

        Parameters
        ----------
        bandNames : list of 2 band names or numbers, optional
            The two bands, in order, e.g. ["B8", "B4"] for NDVI.
            Default: the first two bands of the image.
        save : bool or str, default False
            True saves "<image>_nd_<epoch>.tif"; a string saves to that path.

        Returns
        -------
        Image
            A single-band image named "nd", with values from -1 to 1.
            Use .rename() to give it a meaningful name.

        Raises
        ------
        ValueError
            If `bandNames` does not contain exactly 2 bands, a band does not
            exist, or the image has fewer than 2 bands.

        Examples
        --------
        >>> ndvi = img.normalizedDifference(["B8", "B4"])      # vegetation
        >>> ndwi = img.normalizedDifference(["B3", "B8"])      # water
        >>> ndbi = img.normalizedDifference(["B11", "B8"])     # built-up
        >>> img.normalizedDifference(["B8", "B4"], save="ndvi.tif")
        """
        if bandNames is None:
            if len(self._names) < 2:
                raise ValueError("normalizedDifference() needs an image with at least 2 bands.")
            bandNames = self._names[:2]
        bandNames = self._flatten([bandNames])
        if len(bandNames) != 2:
            raise ValueError(f"normalizedDifference() needs exactly 2 bands, got {len(bandNames)}: {bandNames}")
        a = self._bands[self._band_index(bandNames[0])]
        b = self._bands[self._band_index(bandNames[1])]
        node = ("op", "divide", ("op", "subtract", a, b), ("op", "add", a, b))
        result = Image._create(self.path, self._grid, [node], ["nd"])
        return result._maybe_save(save, "nd")

    # Python operators: img1 / img2, img * 0.0001, 1 - img, -img, abs(img), img ** 2
    def __add__(self, other):      return self._math(other, "add", operator=True)
    def __radd__(self, other):     return self._math(other, "add", reverse=True, operator=True)
    def __sub__(self, other):      return self._math(other, "subtract", operator=True)
    def __rsub__(self, other):     return self._math(other, "subtract", reverse=True, operator=True)
    def __mul__(self, other):      return self._math(other, "multiply", operator=True)
    def __rmul__(self, other):     return self._math(other, "multiply", reverse=True, operator=True)
    def __truediv__(self, other):  return self._math(other, "divide", operator=True)
    def __rtruediv__(self, other): return self._math(other, "divide", reverse=True, operator=True)
    def __pow__(self, other):      return self._math(other, "pow", operator=True)
    def __rpow__(self, other):     return self._math(other, "pow", reverse=True, operator=True)
    def __neg__(self):             return self._math(-1, "multiply")
    def __abs__(self):             return self._unary("abs")

    # Comparison / logic operators: img < 0.1, (a > 1) & (b < 2), a | b, ~a
    # (== and != are NOT overloaded, so Images still work in lists, sets and dicts;
    #  use .eq() / .neq() instead.)
    def __lt__(self, other):       return self._math(other, "lt", operator=True)
    def __le__(self, other):       return self._math(other, "lte", operator=True)
    def __gt__(self, other):       return self._math(other, "gt", operator=True)
    def __ge__(self, other):       return self._math(other, "gte", operator=True)
    def __and__(self, other):      return self._math(other, "and", operator=True)
    def __rand__(self, other):     return self._math(other, "and", reverse=True, operator=True)
    def __or__(self, other):       return self._math(other, "or", operator=True)
    def __ror__(self, other):      return self._math(other, "or", reverse=True, operator=True)
    def __invert__(self):          return self._unary("not")

    __hash__ = object.__hash__      # Images can be dict keys / set items (used by where())

    def __bool__(self):
        # `if img:` or `a and b` can't work on a whole image; say what to use instead.
        raise TypeError(
            "An Image has no single True/False value. For pixel-wise logic use "
            "a.And(b) or a & b, a.Or(b) or a | b, a.Not() or ~a."
        )

    # ------------------------------------------------------------------ #
    #  Sampling, clustering, PCA
    # ------------------------------------------------------------------ #

    def sample(self, numPixels=5000, seed=0, geometries=True, progress=True, region=None):
        """Samples random pixels and returns their band values as a dataset (one row per pixel).

        Reads as little as possible: when the samples are spread thinly over a
        large image (e.g. 5,000 pixels on a huge mosaic), only small 256 x 256
        pieces around the sampled pixels are read instead of whole blocks.
        Memory use stays small. Pixels with NoData in any band are left out, so
        the result can have fewer rows than asked.

        Parameters
        ----------
        numPixels : int, default 5000
            Number of random pixels to sample.
        seed : int, default 0
            Random seed: the same seed gives the same pixels.
        geometries : bool, default True
            Add a point (the pixel centre) to each row. False gives a plain table.
        progress : bool, default True
            Show a progress bar for large images.
        region : vector.Vector, optional
            Only sample pixels whose centre is inside these shapes (e.g. a
            boundary). Reprojected to the image's CRS automatically. The full
            `numPixels` are drawn inside the region when it has that many pixels.

        Returns
        -------
        vector.Vector
            One column per band, plus point geometries in the image's CRS.

        Raises
        ------
        ValueError
            If numPixels is not a positive whole number, or the region doesn't
            overlap the image.
        TypeError
            If `region` is not a Vector.

        Examples
        --------
        >>> samples = img.select("B2", "B3", "B4", "B8").sample(2000)
        >>> samples.show(5)
        >>> samples.save("training_pixels.csv")
        >>> aoi = vector.Open("boundary.shp")
        >>> inside = img.sample(1000, region=aoi)            # only inside the boundary
        >>> cluster.kMeans(5).train(samples)
        """
        import pandas as pd
        from . import vector as _vector
        if not isinstance(numPixels, (int, np.integer)) or isinstance(numPixels, bool) or numPixels < 1:
            raise ValueError(f"numPixels must be a whole number of 1 or more, got {numPixels!r}")
        rng = np.random.default_rng(seed)

        source, area = self, None
        if region is not None:
            if not isinstance(region, _vector.Vector):
                raise TypeError("region must be a Vector, e.g. vector.Open('boundary.shp')")
            source = self.clip(region)                      # crops to the region's extent
            import shapely
            area = shapely.union_all(list(region._shapes_in(self._grid["crs"])))
            shapely.prepare(area)
        g = source._grid
        width, height = g["width"], g["height"]
        total = width * height
        n = int(min(numPixels, total))
        if area is None:
            flat = rng.choice(total, size=n, replace=False)
        else:
            flat = _positions_inside(area, g["transform"], width, total, n, rng)
        flat = np.sort(flat)
        rows, cols = np.divmod(flat, width)

        windows, members, size = _sample_windows(rows, cols, width, height)
        by_window = {(w.col_off, w.row_off): m for w, m in zip(windows, members)}
        count = len(source._bands)
        values = np.full((len(flat), count), np.nan, dtype="float32")
        workers, in_flight = _parallel_plan(None, len(windows), _window_cost(source._bands, size))
        bar = _progress_bar(total=len(windows), desc=f"Sampling {Path(self.path).name}",
                            enabled=progress and len(windows) > 4)
        try:
            for window, results in _compute_blocks(windows, source._bands, workers, in_flight):
                idx = by_window[(window.col_off, window.row_off)]
                r, c = rows[idx] - window.row_off, cols[idx] - window.col_off
                for band, arr in results:
                    values[idx, band - 1] = arr[r, c]
                bar.update(1)
        finally:
            bar.close()

        keep = np.isfinite(values).all(axis=1)
        table = pd.DataFrame(values[keep].astype("float64"), columns=list(source._names))
        if geometries:
            xs, ys = g["transform"] * (cols[keep] + 0.5, rows[keep] + 0.5)
            import geopandas as gpd
            data = gpd.GeoDataFrame(table, geometry=gpd.points_from_xy(xs, ys), crs=g["crs"])
        else:
            data = table
        return _vector.Vector(data, f"{Path(self.path).stem} (sample)", "Pixel sample")

    def cluster(self, clusterer, outputName="cluster", numPixels=5000, seed=0, region=None):
        """Assigns every pixel to a cluster.

        Give a trained clusterer (``cluster.wekaKMeans(5).train(img)``), or an
        untrained one (``cluster.wekaKMeans(5)``) to train it on this image
        right away. The result is a lazy single-band image of cluster numbers
        (0, 1, 2, ...). Pixels with NoData in any input band stay NoData.
        Saved files are uint8 (cluster numbers) with NoData = 255.

        Parameters
        ----------
        clusterer : cluster.TrainedClusterer or cluster.Clusterer
            A trained clusterer, or an untrained one to train on this image.
        outputName : str, default "cluster"
            Name of the output band.
        numPixels : int, default 5000
            Pixels sampled for training, when `clusterer` is not trained yet.
        seed : int, default 0
            Random seed for the training sample.
        region : vector.Vector, optional
            When `clusterer` is not trained yet: train only on pixels inside
            these shapes. The whole image is still clustered.

        Returns
        -------
        Image

        Raises
        ------
        TypeError
            If `clusterer` is not a clusterer.
        ValueError
            If the image lacks a band the clusterer was trained on.

        Examples
        --------
        >>> trained = cluster.wekaKMeans(5).train(img, numPixels=5000)
        >>> clusters = img.cluster(trained)
        >>> clusters.visualize(labels={i: f"Cluster {i}" for i in range(5)})
        >>> clusters.save("clusters.tif")
        >>> quick = img.cluster(cluster.kMeans(5))                     # train and apply in one step
        >>> local = img.cluster(cluster.kMeans(5), region=aoi)         # trained inside aoi only
        """
        from .cluster import Clusterer, TrainedClusterer
        if isinstance(clusterer, Clusterer):
            clusterer = clusterer.train(self, numPixels=numPixels, seed=seed, region=region)
        if not isinstance(clusterer, TrainedClusterer):
            raise TypeError("cluster() expects a clusterer, e.g. cluster.kMeans(5) or cluster.kMeans(5).train(img).")
        inputs = self._model_inputs(clusterer.schema(), "clusterer")
        grid = dict(self._grid)
        grid.pop("sources", None)
        grid.update(dtype="uint8" if clusterer.nClusters < 255 else "uint16",
                    nodata=255 if clusterer.nClusters < 255 else 65535)
        return Image._create(self.path, grid, [("model", inputs, clusterer, 0)], [outputName])

    def pca(self, model, numPixels=10000, standardize=False, seed=0, region=None):
        """Transforms the image into principal components (bands PC1, PC2, ...).

        Give a trained PCA (``pca.PCA(3).train(img)``), or a number of
        components to train one on this image right away. The result is lazy:
        visualizing reads a preview and saving streams it block by block.

        Parameters
        ----------
        model : pca.TrainedPCA, int or None
            A trained PCA, or the number of components to keep (None = all).
        numPixels : int, default 10000
            Pixels sampled for training, when `model` is a number.
        standardize : bool, default False
            Scale bands to unit variance before training, when `model` is a number.
        seed : int, default 0
            Random seed for the training sample.
        region : vector.Vector, optional
            When `model` is a number: learn the components only from pixels
            inside these shapes. The whole image is still transformed.

        Returns
        -------
        Image
            Float32 bands named PC1, PC2, ...

        Examples
        --------
        >>> model = pca.PCA(3).train(img.select("B2", "B3", "B4", "B8", "B11", "B12"))
        >>> model.info()
        >>> pcs = img.pca(model)
        >>> pcs.visualize(bands=["PC1", "PC2", "PC3"])
        >>> quick = img.select("B2", "B3", "B4", "B8").pca(2)        # train and apply in one step
        >>> local = img.pca(3, region=aoi)                         # components learned inside aoi
        """
        from .pca import PCA, TrainedPCA
        if model is None or (isinstance(model, (int, np.integer)) and not isinstance(model, bool)):
            model = PCA(model, standardize=standardize).train(self, numPixels=numPixels, seed=seed,
                                                               region=region)
        if isinstance(model, PCA):
            raise TypeError("This PCA isn't trained yet. Train it first, e.g. pca.PCA(3).train(img).")
        if not isinstance(model, TrainedPCA):
            raise TypeError("pca() expects a trained PCA (pca.PCA(3).train(img)) or a number of components.")
        inputs = self._model_inputs(model.schema(), "PCA")
        grid = dict(self._grid)
        grid.pop("sources", None)
        nodes = [("model", inputs, model, i) for i in range(model.nComponents)]
        return Image._create(self.path, grid, nodes, model.componentNames())

    def _model_inputs(self, names, label):
        missing = [n for n in names if n not in self._names]
        if missing:
            raise ValueError(f"The {label} was trained on bands {names}, but this image lacks {missing}. "
                             f"This image has: {self._names}")
        return tuple(self._bands[self._names.index(n)] for n in names)

    # ------------------------------------------------------------------ #
    #  Saving
    # ------------------------------------------------------------------ #

    def save(self, path=None, compress="deflate", dtype=None, nodata=None, progress=True,
             workers=None, _label=None):
        """Saves the image as a GeoTIFF, streaming it block by block.

        The image is computed and written in 1024 x 1024 pixel blocks, one band
        at a time, so memory use stays small (a few MB per band) however large
        the image is. On a computer with several CPU cores, blocks are computed
        in parallel. Band names are written into the file, compression uses
        all CPU cores, and a progress bar shows how far it is.

        Parameters
        ----------
        path : str, optional
            Output file. Default: "<image name>_<band names>_<epoch time>.tif"
            in the current folder.
        compress : str or None, default "deflate"
            "deflate" (smaller, widely supported), "zstd" (a bit faster),
            "lzw" (fastest compression, larger file), or None (no compression:
            fastest, largest file).
        dtype : str, optional
            Data type of the file, e.g. "uint16", "int16", "float32".
            Default: the source's integer type when pixel values are passed
            through unchanged (select, clip, mosaic, ...), otherwise "float32".
            Saving computed values to an integer type rounds them and clips
            them to the type's range.
        nodata : number, optional
            Value written for NoData pixels. Default: NaN for float types; for
            integer types the source's NoData value, or 0.
        progress : bool, default True
            Show a progress bar.
        workers : int, optional
            How many blocks are computed at the same time (threads).
            Default: one per CPU core, up to 8 (see raster.MAX_WORKERS).
            Fewer are used if the blocks in progress would need more than about
            a quarter of the free memory. Use 1 to process one block at a time.

        Returns
        -------
        str
            The absolute path of the saved file.

        Raises
        ------
        ValueError
            If `path` is one of the files this image reads from, or `dtype` /
            `nodata` are invalid.

        Examples
        --------
        >>> img.normalizedDifference(["B8", "B4"]).rename("NDVI").save("ndvi.tif")
        >>> img.select("B4", "B3", "B2").save()
        >>> mosaic.save("mosaic.tif", compress="zstd")
        """
        if path is None:
            label = _label or "_".join(self._names)
            label = re.sub(r"[^A-Za-z0-9_-]+", "-", label)[:60]
            path = f"{Path(self.path).stem}_{label}_{int(time.time())}.tif"
        out = Path(path).resolve()
        sources = {node_path for node_path in self._source_paths()}
        if str(out) in sources:
            raise ValueError(f"Cannot save over a file this image reads from: {out}")

        try:
            out_dtype = np.dtype(dtype if dtype is not None else self._output_dtype())
        except TypeError:
            raise ValueError(f"Unknown dtype {dtype!r}. Use e.g. 'uint8', 'uint16', 'int16', 'float32'.") from None
        is_int = np.issubdtype(out_dtype, np.integer)
        if nodata is None:
            nodata = self._default_nodata(out_dtype)
        if is_int:
            info = np.iinfo(out_dtype)
            if not (isinstance(nodata, (int, float, np.number)) and np.isfinite(nodata)
                    and info.min <= nodata <= info.max):
                raise ValueError(f"nodata={nodata!r} doesn't fit in {out_dtype.name} "
                                 f"({info.min} to {info.max}).")

        g = self._grid
        width, height, count = g["width"], g["height"], len(self._bands)
        profile = {
            "driver": "GTiff", "dtype": out_dtype.name, "count": count,
            "width": width, "height": height, "crs": g["crs"], "transform": g["transform"],
            "nodata": nodata, "BIGTIFF": "IF_SAFER",
        }
        if compress:
            # the predictor makes imagery compress much better (3 = floating point,
            # 2 = integer); num_threads lets GDAL compress with every CPU core
            profile.update(compress=compress, predictor=2 if is_int else 3, num_threads="ALL_CPUS")
        if width >= 256 and height >= 256:
            profile.update(tiled=True, blockxsize=256, blockysize=256)
        # square blocks, aligned to the 256 px tiles: a 200,000 px wide mosaic
        # still needs only 1024 x 1024 pixels (4 MB) per band in memory
        block = _block_size()
        windows = [Window(col, row, min(block, width - col), min(block, height - row))
                   for row in range(0, height, block) for col in range(0, width, block)]
        # one block with all its bands, plus temporaries while computing
        bytes_per_window = _window_cost(self._bands, block)
        workers, in_flight = _parallel_plan(workers, len(windows), bytes_per_window)

        bar = _progress_bar(total=len(windows) * count, desc=f"Saving {out.name}",
                            enabled=progress and len(windows) * count > 1)
        try:
            with rasterio.open(out, "w", **profile) as dst:
                for i, name in enumerate(self._names, start=1):
                    dst.set_band_description(i, name)
                for window, results in _compute_blocks(windows, self._bands, workers, in_flight):
                    for i, arr in results:
                        dst.write(_to_dtype(arr, out_dtype, nodata), i, window=window)
                    bar.update(len(results))
        finally:
            bar.close()
        note = f", {workers} workers" if workers > 1 else ""
        print(f"Saved: {out}  ({count} band(s), {out_dtype.name}{note})")
        return str(out)

    def _output_dtype(self):
        """Integer type of the source when values pass through unchanged, else float32."""
        source = self._grid.get("dtype")
        if source and np.issubdtype(np.dtype(source), np.integer) \
                and all(_preserves_values(node) for node in self._bands):
            return source
        return "float32"

    def _default_nodata(self, dtype):
        if not np.issubdtype(dtype, np.integer):
            return float("nan")
        source = self._grid.get("nodata")
        info = np.iinfo(dtype)
        if source is not None and np.isfinite(source) and info.min <= source <= info.max:
            return source
        return 0

    # ------------------------------------------------------------------ #
    #  Metadata
    # ------------------------------------------------------------------ #

    def meta(self):
        """Prints a summary of the image and returns its metadata. Reads no pixels.

        Shows the size, number of bands, band names, data type, NoData value,
        CRS, resolution, bounds and transform.

        Returns
        -------
        dict
            driver, dtype, nodata, width, height, count, crs, transform, bandNames
            (and sources, for a mosaic).

        Examples
        --------
        >>> img.meta()                               # print the summary
        >>> m = img.meta()
        >>> m["crs"], m["count"]
        (CRS.from_epsg(4326), 26)
        """
        g = self._grid
        computed = any(node[0] != "read" for node in self._bands)
        dtype = self._output_dtype() if computed else g["dtype"]
        m = {
            "driver": g["driver"],
            "dtype": dtype,
            "nodata": self._default_nodata(np.dtype(dtype)) if computed else g["nodata"],
            "width": g["width"], "height": g["height"], "count": len(self._bands),
            "crs": g["crs"], "transform": g["transform"],
            "bandNames": list(self._names),
        }
        if "sources" in g:
            m["sources"] = g["sources"]
        note = (f"  (mosaic of {g['sources']} images)" if "sources" in g
                else "  (computed)" if computed else "")
        print("=" * 45)
        print(f" Metadata for: {self.path}{note}")
        print("=" * 45)
        print(f" Driver     : {m['driver']}")
        print(f" Width      : {m['width']} px")
        print(f" Height     : {m['height']} px")
        print(f" Bands      : {m['count']}")
        print(f" Band names : {m['bandNames']}")
        print(f" Data type  : {m['dtype']}")
        print(f" NoData     : {m['nodata']}")
        print(f" CRS        : {m['crs']}")
        print(f" Resolution : {g['res']}")
        print(f" Bounds     : {g['bounds']}")
        print(f" Transform  :\n{m['transform']}")
        print("=" * 45)
        return m

    # ------------------------------------------------------------------ #
    #  Visualization
    # ------------------------------------------------------------------ #

    def visualize(self, bands=None, min=None, max=None, gamma=1.0, palette=None,
                  colorbar=True, colorbar_label=None, colorbar_orientation="vertical",
                  labels=None, overlay=None, overlay_color="red", max_size=1500,
                  save_chart=False, dpi=200):
        """Displays the image as a map chart.

        Each band is stretched linearly from `min` to `max`, then gamma
        correction is applied. Shows an RGB composite (3 bands), a single band
        with a palette and colorbar, or a class map with a legend (`labels`).
        Only a downsampled preview is read (see `max_size`), so this is fast
        and light on memory even for large images.

        Parameters
        ----------
        bands : str, int, or list, optional
            1 band  -> single-band view, colored with `palette` + a colorbar.
            3 bands -> RGB composite, in the order [red, green, blue].
            Band names (e.g. ["B4", "B3", "B2"]) or 1-based numbers ([4, 3, 2]).
            Default: the first three bands, or the first band if fewer than 3.
        min : float or list of float, optional
            Value shown as the lowest color. One number for all bands, or one
            per band. Default: each band's 2nd percentile.
        max : float or list of float, optional
            Value shown as the highest color. Default: each band's 98th percentile.
        gamma : float or list of float, default 1.0
            Gamma correction. Above 1 brightens, below 1 darkens.
        palette : str or list of colors, optional  (single band only)
            - a built-in palette: "ndvi", "ndwi", "evi", "ndbi", "nbr", "ndsi",
              "elevation", "temperature", "precipitation", "water", ... (see raster.palettes())
            - any matplotlib colormap: "viridis", "RdYlGn", "Spectral", "terrain", ...
            - add "_r" to reverse any of them, e.g. "ndvi_r"
            - or a list of colors: ["0000FF", "FFFFFF", "008000"] (hex with or without '#')
            Default: "gray". Run raster.showPalettes() to see them all.
        colorbar : bool, default True  (single band only)
            Show a colorbar with the real pixel values.
        colorbar_label : str, optional
            Colorbar title. Default: the band name.
        colorbar_orientation : "vertical" or "horizontal", default "vertical"
        labels : dict, optional  (single band only)
            For class maps (e.g. the output of where()): {value: "name"}.
            Draws one flat color per class and a legend instead of a colorbar.
            Colors come from `palette`: a list with one color per class, or a
            palette name to sample from (default "tab10"). Pixels whose value
            is not in `labels` are transparent. `min`/`max`/`gamma` are ignored.
        overlay : vector.Vector or list of Vectors, optional
            Vectors drawn on top of the image as outlines (polygons), lines or
            dots (points), e.g. a boundary or sample points. They are
            reprojected to the image's CRS. The map keeps the image's extent.
        overlay_color : str or list of str, default "red"
            Color of the overlay; a list gives one color per overlay vector.
        max_size : int, default 1500
            Longest side, in pixels, of the preview that is read and drawn.
            Larger = more detail but more RAM. Images smaller than this are shown
            at full resolution.
        save_chart : bool, default False
            Also save the chart as "<image name>_<epoch time>.png" in the current folder.
        dpi : int, default 200
            Resolution of the saved PNG. Only used when save_chart=True.

        Returns
        -------
        str or None
            The saved PNG file name when save_chart=True, otherwise None.

        Examples
        --------
        >>> img.visualize(bands=["B4", "B3", "B2"], min=1000, max=4000, gamma=1.2)
        >>> img.normalizedDifference(["B8", "B4"]).visualize(min=-0.2, max=0.8, palette="ndvi")
        >>> img.select("B8").visualize(palette="viridis", colorbar_orientation="horizontal")
        >>> classes.visualize(labels={1: "Water", 2: "Sparse", 3: "Moderate", 4: "Dense"},
        ...                   palette=["0000FF", "D2B48C", "9ACD32", "006400"])
        >>> img.visualize(bands=["B4", "B3", "B2"], min=1000, max=4000, overlay=aoi)
        >>> img.visualize(bands=[4, 3, 2], save_chart=True, dpi=300)
        """
        # NOTE: `min` and `max` hide Python's built-in min()/max() inside this
        # method. They are the clearest names for these parameters; we use numpy instead.

        # visualize(bands=img.select("B8")) -> show that image
        if isinstance(bands, Image):
            return bands.visualize(
                bands=None, min=min, max=max, gamma=gamma, palette=palette,
                colorbar=colorbar, colorbar_label=colorbar_label,
                colorbar_orientation=colorbar_orientation, labels=labels, overlay=overlay,
                overlay_color=overlay_color, max_size=max_size,
                save_chart=save_chart, dpi=dpi,
            )

        # ---- 1. pick the bands ----
        if bands is None:
            bands = self._names[:3] if len(self._names) >= 3 else self._names[:1]
        bands = self._flatten([bands])
        if len(bands) not in (1, 3):
            raise ValueError(f"Give 1 band (single band) or 3 bands (RGB), got {len(bands)}: {bands}")
        if palette is not None and len(bands) == 3:
            raise ValueError("'palette' only works with a single band. Remove it or pass 1 band.")
        if colorbar_orientation not in ("vertical", "horizontal"):
            raise ValueError("colorbar_orientation must be 'vertical' or 'horizontal'")
        if labels is not None:
            if len(bands) != 1:
                raise ValueError("'labels' only works with a single band (a class map).")
            if not isinstance(labels, dict) or not labels:
                raise ValueError("'labels' must be a non-empty dict like {1: 'Water', 2: 'Forest'}")
            class_values = sorted(labels)
            class_colors = self._class_colors(palette, len(class_values))
            cmap = None
        else:
            cmap = _make_cmap(palette).copy() if len(bands) == 1 else None   # validate early

        view = self.select(bands)
        names = view._names
        n = len(names)

        # ---- 2. read a downsampled preview (this is the only pixel read) ----
        g = self._grid
        preview = self._preview_shape(max_size)
        arr = view._compute(out_shape=preview)          # float32 (n, rows, cols)
        shown_h, shown_w = arr.shape[1:]

        # ---- 3. min / max / gamma, one value per band ----
        mins = self._per_band(min, n, "min")
        maxs = self._per_band(max, n, "max")
        gammas = self._per_band(gamma, n, "gamma")
        if mins is None:
            mins = [self._safe_percentile(band, 2, 0.0) for band in arr]
        if maxs is None:
            maxs = [self._safe_percentile(band, 98, 1.0) for band in arr]

        # ---- 4. plot, with real map coordinates on the axes ----
        b = g["bounds"]
        extent = (b.left, b.right, b.bottom, b.top)
        fig, ax = plt.subplots(figsize=(9, 7))

        if labels is not None:
            # Class map: one flat color per class value + a legend.
            band = arr[0]
            band[~np.isin(band, class_values)] = np.nan     # unknown values -> transparent
            class_cmap = ListedColormap(class_colors)
            class_cmap.set_bad(alpha=0)
            edges = ([class_values[0] - 0.5]
                     + [(a + b) / 2 for a, b in zip(class_values, class_values[1:])]
                     + [class_values[-1] + 0.5])
            norm = BoundaryNorm(edges, len(class_values))
            ax.imshow(np.ma.masked_invalid(band), cmap=class_cmap, norm=norm,
                      extent=extent, interpolation="nearest")
            handles = [Patch(facecolor=c, edgecolor="none", label=f"{labels[v]}  ({v:g})")
                       for v, c in zip(class_values, class_colors)]
            ax.legend(handles=handles, title=colorbar_label or names[0], loc="upper left",
                      bbox_to_anchor=(1.01, 1), frameon=False)
            bands_text = f"{names[0]}  ({len(class_values)} classes)"
            mins, maxs = [class_values[0]], [class_values[-1]]
        elif n == 1:
            # Single band: PowerNorm does the min/max stretch + gamma, and lets
            # the colorbar show REAL values.
            cmap.set_bad(alpha=0)                       # NaN / NoData -> transparent
            lo, hi = mins[0], maxs[0]
            if hi <= lo:
                hi = lo + 1.0
            norm = PowerNorm(gamma=1.0 / gammas[0], vmin=lo, vmax=hi)
            shown = ax.imshow(np.ma.masked_invalid(arr[0]), cmap=cmap, norm=norm, extent=extent)
            if colorbar:
                horizontal = colorbar_orientation == "horizontal"
                cbar = fig.colorbar(shown, ax=ax, orientation=colorbar_orientation, extend="both",
                                    fraction=0.046 if horizontal else 0.035,
                                    pad=0.12 if horizontal else 0.03)
                cbar.set_label(colorbar_label or names[0])
            if isinstance(palette, str):
                palette_name = palette
            else:
                palette_name = "custom" if palette is not None else "gray"
            bands_text = f"{names[0]}  (palette: {palette_name})"
        else:
            # RGB: stretch each band to 0..1 IN PLACE (no extra copies), apply gamma.
            for i in range(n):
                span = (maxs[i] - mins[i]) or 1.0
                arr[i] -= mins[i]
                arr[i] /= span
                np.clip(arr[i], 0, 1, out=arr[i])
                if gammas[i] != 1.0:
                    arr[i] **= (1.0 / gammas[i])
            # Pack into uint8 RGBA (4x smaller than float32, and matplotlib
            # draws uint8 without making its own float copies).
            rgba = np.empty(arr.shape[1:] + (4,), dtype="uint8")
            for i in range(3):
                rgba[..., i] = np.nan_to_num(arr[i] * 255.0)
            rgba[..., 3] = np.where(np.isnan(arr).any(axis=0), 0, 255)   # NaN -> transparent
            del arr
            ax.imshow(rgba, extent=extent)
            bands_text = f"R: {names[0]}  G: {names[1]}  B: {names[2]}"

        crs = g["crs"]
        geographic = crs is not None and crs.is_geographic
        ax.set_xlabel("Longitude" if geographic else "X")
        ax.set_ylabel("Latitude" if geographic else "Y")
        ax.set_title(f"{Path(self.path).name}\n{bands_text}")

        if overlay is not None:
            from . import vector as _vector
            vectors = overlay if isinstance(overlay, (list, tuple)) else [overlay]
            colors = (list(overlay_color) if isinstance(overlay_color, (list, tuple))
                      else [overlay_color] * len(vectors))
            if len(colors) != len(vectors):
                raise ValueError(f"overlay_color needs 1 color or {len(vectors)} (one per overlay vector)")
            for vec, col in zip(vectors, colors):
                if not isinstance(vec, _vector.Vector) or not vec.hasGeometry():
                    raise TypeError("overlay expects a Vector with geometries, e.g. vector.Open('boundary.shp')")
                vec._draw_outline(ax, crs, col)
            ax.set_xlim(extent[0], extent[1])                   # keep the image's extent
            ax.set_ylim(extent[2], extent[3])

        if labels is not None:
            vis_text = f"{names[0]}: class values {', '.join(f'{v:g}' for v in class_values)}"
        else:
            vis_text = "  |  ".join(
                f"{name}: min={lo:.4g}, max={hi:.4g}, gamma={gm:g}"
                for name, lo, hi, gm in zip(names, mins, maxs, gammas)
            )
        if preview is not None:
            vis_text += f"\npreview {shown_w}×{shown_h} px of {g['width']}×{g['height']} px"
        fig.text(0.5, 0.01, vis_text, ha="center", fontsize=8, color="gray")

        # ---- 5. save (must happen BEFORE show, show() can clear the figure) ----
        saved = None
        if save_chart:
            saved = f"{Path(self.path).stem}_{int(time.time())}.png"
            fig.savefig(saved, dpi=dpi, bbox_inches="tight")
            print(f"Chart saved: {Path(saved).resolve()}  ({dpi} dpi)")

        plt.show()
        plt.close(fig)          # free the figure's memory
        return saved

    # ------------------------------------------------------------------ #
    #  Helpers (the leading _ means "internal, not for users")
    # ------------------------------------------------------------------ #

    def _compute(self, window=None, out_shape=None):
        """Compute all bands for a window (or the whole image) as float32 (bands, rows, cols)."""
        if out_shape is not None:
            rows, cols = out_shape
        elif window is not None:
            rows, cols = window.height, window.width
        else:
            rows, cols = self._grid["height"], self._grid["width"]
        if len(self._bands) == 1:
            # single band: return the computed array directly (no extra copy)
            with _Reader() as reader:
                band = _evaluate(self._bands[0], reader, window, out_shape)
            if np.ndim(band) == 0:
                return np.full((1, rows, cols), band, dtype="float32")
            return band.astype("float32", copy=False)[None]
        out = np.empty((len(self._bands), rows, cols), dtype="float32")
        with _Reader() as reader:
            for i, node in enumerate(self._bands):
                if node[0] == "read":
                    # plain band: read directly into the output, no temporary array
                    reader.read_into(node[1], node[2], out[i], window)
                else:
                    out[i] = _evaluate(node, reader, window, out_shape)
                reader.clear()          # free cached source bands before the next band
        return out

    def _preview_shape(self, max_size):
        """(rows, cols) for a preview whose longest side is max_size, or None if already small."""
        h, w = self._grid["height"], self._grid["width"]
        scale = max(h, w) / float(max_size)
        if scale <= 1:
            return None
        return (int(round(h / scale)) or 1, int(round(w / scale)) or 1)

    def _math(self, other, op, save=False, reverse=False, operator=False):
        """Build a band-math recipe between this image and an Image or a number."""
        left, names = self._bands, self._names
        if isinstance(other, Image):
            self._check_same_grid(other)
            right = other._bands
            if len(left) == len(right):
                pass
            elif len(left) == 1:
                left, names = left * len(right), other._names
            elif len(right) == 1:
                right = right * len(left)
            else:
                raise ValueError(
                    f"Band math needs the same number of bands, or one image with 1 band. "
                    f"Got {len(left)} ({self._names}) and {len(right)} ({other._names})."
                )
        elif isinstance(other, (int, float, np.number)):
            right = [("const", float(other))] * len(left)
        elif operator:
            return NotImplemented       # lets Python raise its normal TypeError for img + "x"
        else:
            raise TypeError(f"{op}() expects an Image or a number, got {type(other).__name__}")
        if reverse:
            left, right = right, left
        nodes = [("op", op, a, b) for a, b in zip(left, right)]
        result = Image._create(self.path, self._grid, nodes, names)
        return result._maybe_save(save, op)

    @staticmethod
    def _class_values(classes, label):
        """One number or a list of numbers -> list of floats (validated)."""
        if isinstance(classes, (int, float, np.number)) and not isinstance(classes, bool):
            return [float(classes)]
        if not isinstance(classes, (list, tuple, set)) or not classes:
            raise ValueError(f"{label}() needs a class value or a non-empty list of class values.")
        bad = [c for c in classes if not isinstance(c, (int, float, np.number)) or isinstance(c, bool)]
        if bad:
            raise ValueError(f"{label}(): class values must be numbers, got {bad}")
        return [float(c) for c in classes]

    def _any_of(self, values, save, label):
        """1 where the pixel equals any of `values`, else 0 (NaN on NoData)."""
        values = self._class_values(list(values), label)
        result = self._math(values[0], "eq")
        for value in values[1:]:
            result = result._math(self._math(value, "eq"), "or")
        return result._maybe_save(save, label)

    def _match_bands(self, x, label):
        """Recipes of `x` (Image or number), one per band of this image, for where()."""
        n = len(self._bands)
        if isinstance(x, Image):
            self._check_same_grid(x)
            if len(x._bands) == n:
                return list(x._bands)
            if len(x._bands) == 1:
                return x._bands * n
            raise ValueError(f"{label} has {len(x._bands)} bands; "
                             f"it needs 1 or {n} (same as the image).")
        if isinstance(x, (int, float, np.number)):
            return [("const", float(x))] * n
        raise TypeError(f"{label} must be an Image or a number, got {type(x).__name__}")

    @staticmethod
    def _parse_rules(rules):
        """Turn every accepted where() rule format into a list of (test, value) pairs."""
        if not rules:
            raise ValueError("where() needs at least one rule, e.g. where(ndvi.lt(0.1), 1)")
        # single rule: where(test, value)
        if len(rules) == 2 and isinstance(rules[0], Image) and \
                not isinstance(rules[1], (tuple, list, set, frozenset, dict)):
            rules = [(rules[0], rules[1])]

        def to_pair(rule):
            if isinstance(rule, (set, frozenset)):
                images = [x for x in rule if isinstance(x, Image)]
                others = [x for x in rule if not isinstance(x, Image)]
                if len(rule) == 2 and len(images) == 1 and len(others) == 1:
                    return images[0], others[0]
                raise ValueError("A {test, value} set only works when the value is a number "
                                 "(sets have no order). Use a tuple instead: (test, value).")
            if isinstance(rule, (tuple, list)) and len(rule) == 2:
                test, value = rule
                if not isinstance(test, Image):
                    raise TypeError("where(): each rule's test must be an Image, e.g. ndvi.lt(0.1)")
                return test, value
            raise ValueError(f"Each where() rule must be a (test, value) pair, got: {rule!r}")

        pairs = []
        for rule in rules:
            if isinstance(rule, dict):
                pairs.extend(to_pair(item) for item in rule.items())
            elif isinstance(rule, list) and rule and not isinstance(rule[0], Image):
                for item in rule:                   # a list of rules
                    if isinstance(item, dict):
                        pairs.extend(to_pair(i) for i in item.items())
                    else:
                        pairs.append(to_pair(item))
            else:
                pairs.append(to_pair(rule))
        return pairs

    def _eval_expr(self, node, variables):
        """Turn a parsed expression (Python ast) into an Image, using Image operations."""
        ev = lambda n: self._eval_expr(n, variables)     # noqa: E731

        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
                and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in variables:
                return variables[node.id]
            if node.id in self._names:
                return self.select(node.id)
            raise ValueError(
                f"Unknown name '{node.id}' in expression. Add it to the map, e.g. "
                f"{{'{node.id}': img.select('B8')}}, or use a band name: {self._names}"
            )
        if isinstance(node, ast.BinOp) and type(node.op) in _EXPR_BINOPS:
            return _EXPR_BINOPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp):
            value = ev(node.operand)
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.Not):
                return value.Not() if isinstance(value, Image) else float(not value)
        if isinstance(node, ast.Compare) and all(type(op) in _EXPR_COMPARE for op in node.ops):
            # a < b < c  means  (a < b) and (b < c)
            result, left = None, ev(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = ev(comparator)
                part = _compare(_EXPR_COMPARE[type(op)], left, right)
                result = part if result is None else _logic("and", result, part)
                left = right
            return result
        if isinstance(node, ast.BoolOp):
            name = "and" if isinstance(node.op, ast.And) else "or"
            values = [ev(v) for v in node.values]
            result = values[0]
            for value in values[1:]:
                result = _logic(name, result, value)
            return result
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and not node.keywords \
                and len(node.args) == 1:
            func = node.func.id
            if func == "b" and isinstance(node.args[0], ast.Constant):
                arg = node.args[0].value
                if isinstance(arg, str):
                    return self.select(arg)
                if isinstance(arg, int) and not isinstance(arg, bool):
                    if not 0 <= arg < len(self._names):
                        raise ValueError(f"b({arg}): band index must be 0..{len(self._names) - 1} (0-based)")
                    return self.select(arg + 1)
            if func in _EXPR_FUNCS:
                value = ev(node.args[0])
                if isinstance(value, Image):
                    return getattr(value, func)()
                return float(getattr(np, func)(value))
        raise ValueError(
            f"Unsupported syntax in expression: '{ast.unparse(node)}'. Supported: numbers, "
            f"names, b('band'), + - * / **, comparisons, && || ! (and/or/not), "
            f"{', '.join(_EXPR_FUNCS)}. For if/else logic use .where()."
        )

    def _unary(self, op, save=False):
        nodes = [("unary", op, node) for node in self._bands]
        result = Image._create(self.path, self._grid, nodes, self._names)
        return result._maybe_save(save, op)

    def _maybe_save(self, save, label):
        """Handle the `save` argument of band-math methods; always returns self."""
        if save is True:
            self.save(_label=label)
        elif save:                                  # a path
            self.save(save)
        return self

    def _check_same_grid(self, other):
        a, b = self._grid, other._grid
        same = (a["width"] == b["width"] and a["height"] == b["height"]
                and a["crs"] == b["crs"]
                and np.allclose(tuple(a["transform"])[:6], tuple(b["transform"])[:6]))
        if not same:
            raise ValueError(
                "Images are not on the same pixel grid (size, CRS or transform differ). "
                f"{a['width']}x{a['height']} {a['crs']}  vs  {b['width']}x{b['height']} {b['crs']}"
            )

    def _source_paths(self):
        """All file paths this image reads from."""
        found = set()
        stack = list(self._bands)
        while stack:
            node = stack.pop()
            if node[0] == "read":
                found.add(node[1])
            stack.extend(_children(node))
        return found

    def _band_index(self, band):
        """Turn a band name ("B4") or 1-based number (4) into a 0-based index."""
        if isinstance(band, Image):
            raise TypeError("Give band names or numbers here (e.g. 'B8' or 8), not an Image. "
                            "To show an Image, call .visualize() on it.")
        if isinstance(band, str):
            if band not in self._names:
                raise ValueError(f"Band '{band}' not found. This image has: {self._names}")
            return self._names.index(band)
        if isinstance(band, (int, np.integer)) and 1 <= band <= len(self._names):
            return int(band) - 1
        raise ValueError(f"Band number must be 1..{len(self._names)}, got {band}")

    @staticmethod
    def _flatten(items):
        """Allow f("a", "b"), f(["a", "b"]) and f("a") to all mean ["a", "b"] / ["a"]."""
        if len(items) == 1 and isinstance(items[0], (list, tuple)):
            return list(items[0])
        return list(items)

    @staticmethod
    def _per_band(value, n, label):
        """Turn one number or a list into a list with one value per band."""
        if value is None:
            return None
        if isinstance(value, (int, float, np.number)):
            return [float(value)] * n
        value = list(value)
        if len(value) != n:
            raise ValueError(f"'{label}' needs 1 value or {n} values (one per band), got {len(value)}")
        return [float(v) for v in value]

    @staticmethod
    def _class_colors(palette, count):
        """One color per class: from a color list, or sampled from a palette name."""
        if isinstance(palette, (list, tuple)):
            if len(palette) != count:
                raise ValueError(f"'palette' has {len(palette)} colors but 'labels' has {count} classes; "
                                 f"give one color per class.")
            return ["#" + c if isinstance(c, str) and re.fullmatch(r"[0-9a-fA-F]{6}", c) else c
                    for c in palette]
        cmap = _make_cmap(palette or "tab10")
        if palette in (None, "tab10", "tab20", "Set1", "Set2", "Set3", "Paired", "Dark2", "Accent"):
            return [cmap(i % cmap.N) for i in range(count)]          # qualitative: take in order
        return [cmap(x) for x in np.linspace(0, 1, count)]           # continuous: spread evenly

    @staticmethod
    def _safe_percentile(band, q, fallback):
        """Percentile that ignores NaN; returns `fallback` if the band has no valid pixels."""
        valid = band[np.isfinite(band)]
        return float(np.percentile(valid, q)) if valid.size else fallback

    def __repr__(self):
        # what you see when you print(img). Reads no pixels.
        g = self._grid
        return f"<Image {Path(self.path).name} bands={self._names} shape=({len(self._bands)}, {g['height']}, {g['width']})>"


def image(path):
    """Opens a raster file as a lazy Image. Only the metadata is read.

    Parameters
    ----------
    path : str or Path
        Path to a GeoTIFF, or any format rasterio can read.

    Returns
    -------
    Image

    Raises
    ------
    RasterNotFoundError
        If the file does not exist.
    RasterReadError
        If the file exists but cannot be read as a raster.

    Examples
    --------
    >>> img = raster.image("sampleImage1.tif")
    """
    return Image(path)


_MOSAIC_METHODS = ("last", "first", "min", "max", "mean")


def mosaic(images, method="last", res=None, save=False):
    """Mosaics several rasters into one image, without loading them into memory.

    Works like a GDAL virtual raster (VRT): nothing is read when the mosaic is
    created. When you visualize it, a small preview is read; when you save it,
    it is written block by block (1024 x 1024 pixels), and each block reads
    only the overlapping part of each source. Memory use therefore depends on
    the block size, not on the size of the inputs, so very large mosaics work
    on ordinary PCs. Saving shows a progress bar.

    Parameters
    ----------
    images : list, str or Path
        The rasters to combine:

        - a list of Images and/or file paths
        - a folder (all .tif / .tiff files in it)
        - a glob pattern, e.g. "tiles/*.tif" or "scenes/**/B04.tif"

        All must have the same CRS and the same number of bands. They may be
        computed images (e.g. NDVI of each scene) and may overlap.
    method : str, default "last"
        How overlapping pixels are combined: "last" (later images on top),
        "first" (earlier images on top), "min", "max" or "mean".
        NoData pixels never cover valid ones.
    res : float or (float, float), optional
        Pixel size of the mosaic. Default: the first image's pixel size.
        Images with another pixel size are resampled (nearest neighbour).
    save : bool or str, default False
        True saves "mosaic_mosaic_<epoch>.tif"; a string saves to that path.

    Returns
    -------
    Image
        Covers the union of all inputs; band names come from the first image.

    Raises
    ------
    ValueError
        If no rasters are found, CRSs or band counts differ, or `method` /
        `res` are invalid.

    Examples
    --------
    >>> m = raster.mosaic(["tile_1.tif", "tile_2.tif", "tile_3.tif"])
    >>> m = raster.mosaic("tiles/")                          # every .tif in a folder
    >>> m = raster.mosaic("tiles/*.tif", method="max")
    >>> m.select("B4", "B3", "B2").visualize(min=0, max=3000)
    >>> m.save("mosaic.tif")                                 # streamed, with a progress bar
    >>> ndvis = [raster.image(p).normalizedDifference(["B8", "B4"]) for p in paths]
    >>> raster.mosaic(ndvis, method="max", save="max_ndvi.tif")
    """
    items = _image_inputs(images, "mosaic")
    if method not in _MOSAIC_METHODS:
        raise ValueError(f"method must be one of {list(_MOSAIC_METHODS)}, got {method!r}")
    first = items[0]
    crs, nbands = first._grid["crs"], len(first._bands)
    for item in items[1:]:
        if item._grid["crs"] != crs:
            raise ValueError(
                f"All images must have the same CRS. '{Path(first.path).name}' is {crs}, "
                f"'{Path(item.path).name}' is {item._grid['crs']}.")
        if len(item._bands) != nbands:
            raise ValueError(
                f"All images must have the same number of bands. '{Path(first.path).name}' has "
                f"{nbands}, '{Path(item.path).name}' has {len(item._bands)}. Use .select() first.")

    if res is None:
        xres, yres = first._grid["res"]
    elif isinstance(res, (int, float, np.number)) and res > 0:
        xres = yres = float(res)
    elif isinstance(res, (tuple, list)) and len(res) == 2 and all(r > 0 for r in res):
        xres, yres = float(res[0]), float(res[1])
    else:
        raise ValueError("res must be a positive number or a pair (x, y), e.g. res=10 or res=(10, 10)")

    left = min(i._grid["bounds"].left for i in items)
    bottom = min(i._grid["bounds"].bottom for i in items)
    right = max(i._grid["bounds"].right for i in items)
    top = max(i._grid["bounds"].top for i in items)
    width = max(1, int(math.ceil((right - left) / xres - 1e-6)))
    height = max(1, int(math.ceil((top - bottom) / yres - 1e-6)))
    transform = Affine(xres, 0.0, left, 0.0, -yres, top)

    nodes = []
    for b in range(nbands):
        sources = tuple(
            (i._bands[b], i._grid["transform"], (i._grid["height"], i._grid["width"]),
             tuple(i._grid["bounds"]))
            for i in items)
        nodes.append(("mosaic", sources, method, transform, (height, width)))

    dtypes = {str(i._output_dtype()) for i in items}
    grid = {
        "width": width, "height": height, "transform": transform, "crs": crs,
        "bounds": BoundingBox(left, top - height * yres, left + width * xres, top),
        "res": (xres, yres), "driver": "GTiff",
        "dtype": dtypes.pop() if len(dtypes) == 1 else "float32",
        "nodata": first._grid.get("nodata"), "sources": len(items),
    }
    result = Image._create("mosaic", grid, nodes, first._names)
    return result._maybe_save(save, "mosaic")


def stack(images, names=None, save=False):
    """Stacks the bands of several images into one multi-band image.

    Useful for combining single-band files (e.g. one GeoTIFF per band) into
    one image. Nothing is read until you visualize or save the result.

    The first image sets the grid (extent and pixel size). Images on a
    different grid, e.g. 20 m bands stacked onto 10 m bands, are resampled
    to it (nearest neighbour); areas they don't cover become NoData.

    Parameters
    ----------
    images : list, str or Path
        A list of Images and/or file paths, a folder (all .tif / .tiff files,
        in name order) or a glob pattern such as "bands/*_B0[2348].tif".
        All must have the same CRS.
    names : list of str, optional
        New band names, one per band of the result. Default: the original
        band names; where names repeat, they are prefixed with the file name.
    save : bool or str, default False
        True saves "stack_stack_<epoch>.tif"; a string saves to that path.

    Returns
    -------
    Image

    Raises
    ------
    ValueError
        If no images are given, CRSs differ, or `names` has the wrong length.

    Examples
    --------
    >>> s = raster.stack(["B02.tif", "B03.tif", "B04.tif", "B08.tif"],
    ...                  names=["blue", "green", "red", "nir"])
    >>> s = raster.stack("bands/")
    >>> s = raster.stack([img.select("B8"), ndvi.rename("NDVI")], save="nir_ndvi.tif")
    """
    items = _image_inputs(images, "stack")
    ref = items[0]
    g = ref._grid
    nodes, out_names, sources = [], [], []
    for item in items:
        ig = item._grid
        if ig["crs"] != g["crs"]:
            raise ValueError(
                f"All images must have the same CRS. '{Path(ref.path).name}' is {g['crs']}, "
                f"'{Path(item.path).name}' is {ig['crs']}.")
        same_grid = (ig["width"] == g["width"] and ig["height"] == g["height"]
                     and np.allclose(tuple(ig["transform"])[:6], tuple(g["transform"])[:6]))
        for node, name in zip(item._bands, item._names):
            if same_grid:
                nodes.append(node)
            else:                                   # resample onto the first image's grid
                source = ((node, ig["transform"], (ig["height"], ig["width"]), tuple(ig["bounds"])),)
                nodes.append(("mosaic", source, "last", g["transform"], (g["height"], g["width"])))
            out_names.append(name)
            sources.append(Path(item.path).stem)

    if names is not None:
        names = list(names)
        if len(names) != len(nodes) or not all(isinstance(n, str) for n in names):
            raise ValueError(f"stack(): `names` needs {len(nodes)} text names (one per band), got {names}")
        out_names = names
    else:
        out_names = _unique_names(out_names, sources)

    dtypes = {str(i._output_dtype()) for i in items}
    grid = dict(g)
    grid.pop("sources", None)
    grid["dtype"] = dtypes.pop() if len(dtypes) == 1 else "float32"
    result = Image._create("stack", grid, nodes, out_names)
    return result._maybe_save(save, "stack")


def _unique_names(names, sources):
    """Prefix repeated band names with their file name, then number any remaining repeats."""
    repeated = {n for n in names if names.count(n) > 1}
    named = [f"{src}_{n}" if n in repeated else n for n, src in zip(names, sources)]
    seen, unique = {}, []
    for n in named:
        seen[n] = seen.get(n, 0) + 1
        unique.append(n if seen[n] == 1 else f"{n}_{seen[n]}")
    return unique


def _image_inputs(images, label):
    """A list / folder / glob pattern -> a list of Images."""
    if isinstance(images, (str, Path)):
        text = str(images)
        if Path(text).is_dir():
            paths = sorted(p for p in Path(text).iterdir() if p.suffix.lower() in (".tif", ".tiff"))
        elif any(ch in text for ch in "*?["):
            paths = sorted(Path(p) for p in glob.glob(text, recursive=True))
        else:
            paths = [Path(text)]
        if not paths:
            raise ValueError(f"No rasters found for {text!r}.")
        items = list(paths)
    elif isinstance(images, (list, tuple)):
        items = list(images)
    else:
        raise TypeError(f"{label}() expects a list of images or paths, a folder, or a glob pattern.")
    if not items:
        raise ValueError(f"{label}() needs at least one image.")
    return [i if isinstance(i, Image) else Image(i) for i in items]


# ---- short, clean tracebacks for every public entry point ----
clean_class(Image)
mosaic = clean_errors(mosaic)
stack = clean_errors(stack)
image = clean_errors(image)
palettes = clean_errors(palettes)
showPalettes = clean_errors(showPalettes)
