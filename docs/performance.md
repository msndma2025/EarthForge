# Memory and performance

The library is designed so memory use depends on **what you look at or save**, not on the size of the raster.

## How it works

1. **Lazy images.** `raster.image()` and `raster.mosaic()` read only metadata. `select()`, `rename()`, `addBands()`, band math, `normalizedDifference()`, `clip()` and the rest build a small recipe (a tree of operations) and read no pixels.
2. **Previews for display.** `visualize()` asks GDAL for a downsampled read (`out_shape`), at most `max_size` pixels (default 1500) on the longest side. GDAL uses the file's overviews if it has any.
3. **Streaming for export.** `save()` computes and writes the image in **1024 × 1024 pixel blocks** (aligned to the 256-px output tiles), one band at a time: about 4 MB per band per step. Square blocks keep this small even for very wide images. A full-width strip of a 200,000-px-wide mosaic would need about 200 MB per band.
4. **float32 for computing, integer types kept on disk.** Pixels are read as float32, half the memory of float64. When values pass through unchanged, `save()` writes the source's integer type (e.g. uint16), so the output isn't twice the size of the input.
5. **Reads are shared within a band.** In `normalizedDifference(["B8", "B4"])`, B8 and B4 are each read once, although the formula uses each of them twice. The cache is cleared after every output band.
6. **Figures are closed** after they are shown, so matplotlib doesn't keep them in memory.
7. **Clipping crops.** `clip()` reads only the window covering the vector's bounding box, so a small area of interest in a huge raster reads only that area. The polygon mask is rasterized window by window.
8. **Mosaics read only overlaps.** For each block, a [mosaic](mosaic.md) reads only the part of each tile that overlaps the block. Tiles that don't overlap are skipped with a quick bounds check.
9. **Open files are capped.** At most 64 source files are open at once per worker (least recently used are closed), so mosaics of thousands of tiles work.
10. **Blocks are computed in parallel** on computers with several cores, within a memory budget. See [Many cores and lots of RAM](#many-cores-and-lots-of-ram).
11. **Sampling reads only what it needs.** [`img.sample()`](raster.md#imagesample), which clustering and PCA use for training, reads only the small tiles that hold sampled pixels when samples are spread thinly over a large image, and whole blocks when they are dense. On a 200,000 × 200,000 mosaic, 5,000 samples read about 5,000 small tiles instead of all 38,000+ blocks.
12. **Applying models is chunked.** Clusterers and PCA are applied to 262,144 pixels at a time, and the memory budget counts their inputs. When blocks run in parallel, numpy and scikit-learn are limited to one thread per worker.
13. **Vectors load on demand.** `from earthforge import raster` doesn't import geopandas. It is loaded the first time you use `vector`, `clip()`, `geometry()` or `bounds(save=...)`.

## Many cores and lots of RAM

`save()` adapts to the computer it runs on:

- **Workers.** Blocks are computed in parallel threads, one per CPU core, up to 8 by default. Each worker has its own open files. One block, with all its bands, is one task, so each source tile is decompressed only once.
- **Memory budget.** The number of blocks in progress is capped at about **a quarter of the free RAM** (between 256 MB and 8 GB). A PC with little free memory automatically uses fewer workers, and a PC with lots of RAM keeps more blocks in progress.
- **Compression** always uses every core: GDAL compresses blocks in parallel while they're written.

| Computer | What happens |
|---|---|
| 2–4 cores, 4–8 GB RAM | 2–4 workers; memory for blocks typically 50–200 MB. |
| 8+ cores, 16+ GB RAM | 8 workers, 16 blocks in progress; a few hundred MB. Raise `raster.MAX_WORKERS` to use more cores. |
| Limited by the disk | Extra cores help little: writing (and compressing) the output is the slowest part. |

Measured on a 20-core PC, 8000 × 8000 raster (best of 3 runs). The outputs are identical whatever the number of workers:

| Job | Compression | 1 worker | 4 workers | 8 workers |
|---|---|---|---|---|
| NDVI, 1 band | none | 3.0 s | 1.4 s | 1.9 s |
| NDVI, 1 band | deflate | 7.4 s | 5.4 s | 5.5 s |
| × 0.0001, 4 bands | none | 10.5 s | 10.8 s | 10.6 s |
| × 0.0001, 4 bands | deflate | 19.1 s | 17.3 s | 17.4 s |
| Mosaic of 9 tiles, 4 bands | none | 7.9 s | 4.4 s | 3.9 s |
| Mosaic of 9 tiles, 4 bands | deflate | 12.2 s | 8.8 s | 8.6 s |

Parallel work pays off most for mosaics and calculations. When a job mostly writes a large output (the 4-band case writes 1 GB of float32), the disk and compression set the pace.

Settings for big machines and special cases:

```python
from earthforge import raster

raster.MAX_WORKERS = 16                         # allow up to 16 workers by default
m.save("mosaic.tif", workers=12)                # or choose for one save
m.save("mosaic.tif", workers=1)                 # one block at a time: least memory
raster.BLOCK_SIZE = 2048                        # larger blocks (multiple of 256): fewer, bigger steps
```

Larger blocks (`BLOCK_SIZE = 2048`) use 4× more memory per block and help a little when there are very many small steps. The default of 1024 suits most cases.

## Measured peak memory

Test raster: 8000 × 8000 px, 4 bands, uint16 (one band as float32 = 256 MB). Peak memory measured with Python's `tracemalloc`, with **one worker** (`workers=1`). GDAL's internal block cache is separate and not included. With the default workers, peak memory is higher (about 150–450 MB in these tests) but stays within the memory budget described above.

| Operation | Peak memory | Time |
|---|---|---|
| `raster.image(path)` | ~0 MB | 0.1 s |
| `select(...)`, `normalizedDifference(...)` (no output) | ~0 MB | 0.0 s |
| `visualize()`, RGB preview | 66 MB | 2.4 s |
| `visualize(palette=...)`, NDVI preview | 50 MB | 2.3 s |
| `normalizedDifference(...).save()` | 27 MB | 3.2 s |
| `(img * 0.0001).save()`, 4 bands, float32 | 15 MB | 6.4 s |
| `img.select(4 bands).save()`, stays uint16 | 14 MB | 5.0 s |
| `img.select("B1").data` | 256 MB | 2.3 s |
| `img.select("B1", "B2").data` | 512 MB | 3.7 s |

Mosaic of the same raster cut into 9 overlapping tiles:

| Operation | Peak memory | Time |
|---|---|---|
| `raster.mosaic("tiles/")` | ~0 MB | 0.1 s |
| `visualize()` preview of the mosaic | 66 MB | 4.0 s |
| `mosaic.save(compress=None)`, uint16 | 22 MB | 8.5 s |
| `mosaic.save(compress="zstd")` | 20 MB | 9.7 s |
| `mosaic.clip(10 × 10 km AOI).save()` | 19 MB | 0.3 s |

`.data` uses exactly the size of the result. Everything else stays small whatever the raster size. Save time grows roughly in proportion to the number of pixels written, and is usually limited by disk speed.

## Tips

- **Clip early.** `img.clip(aoi)` before other work means every later step reads only the area of interest. This works for mosaics too.
- **Avoid `.data` on large images.** It is the only operation whose memory grows with the image size. Use `visualize()` to look at the image and `save()` to export it.
- **Store `.data` if you need it twice.** It is recomputed on every access:
  ```python
  arr = img.select("B8").data   # compute once
  arr.mean(); arr.max()
  ```
- **Choose the compression to suit the job.** `compress=None` is fastest to write but makes the largest files. `"zstd"` is fast with good compression. `"deflate"` is the most widely supported.
- **Keep integer data integer.** Saving a plain selection, clip or mosaic keeps uint16/int16 automatically. For class maps, `save(dtype="uint8")` makes files much smaller.
- **Add overviews to large source files** (`gdaladdo` or `rasterio`'s `build_overviews`). `visualize()` then reads the small overview instead of decimating the full image, which makes previews of big rasters and mosaics much faster.
- **Use a lower `max_size`** (for example 800) for quick looks at very large rasters. Use a higher one when you need fine detail.
- **Write to a local disk.** Network drives and USB sticks are often the bottleneck for big outputs.
- **Limit GDAL's cache** if needed. GDAL keeps a block cache (by default 5% of RAM). To cap it:
  ```python
  import rasterio
  with rasterio.Env(GDAL_CACHEMAX=256):   # MB
      img.normalizedDifference(["B8", "B4"]).save("ndvi.tif")
  ```
