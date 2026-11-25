# Mosaic

`raster.mosaic()` combines many rasters (tiles, scenes, or computed images) into one image **without loading them into memory**. It works like a GDAL virtual raster (VRT): creating the mosaic reads only each file's metadata, and pixels are read later, only where and when they are needed.

```python
from pyearthforge import raster

m = raster.mosaic("tiles/")                    # every .tif in a folder
m.meta()
m.select("B4", "B3", "B2").visualize(min=0, max=3000)
m.save("mosaic.tif")                           # streamed, with a progress bar
```
```
Saving mosaic.tif: 100%|██████████| 256/256 [00:08<00:00, 30.5block/s]
Saved: C:\...\mosaic.tif  (4 band(s), uint16)
```

## How memory stays low

| Step | What is read |
|---|---|
| `raster.mosaic(...)` | Only the metadata of each file (size, position, CRS, bands). |
| `visualize()` | A downsampled preview, with the longest side ≤ `max_size` pixels. |
| `save()` | The mosaic is written in **1024 × 1024 pixel blocks**, one band at a time. For each block, only the part of each tile that overlaps the block is read. |
| `clip(aoi)` | Only the tiles and pixels inside the area of interest. |

Memory use depends on the block size (about 4 MB per band per block), **not on the size of the inputs**. Measured on a mosaic of 9 overlapping tiles totalling 8000 × 8000 px × 4 bands (512 MB): saving peaked at **22 MB** of memory and took 8.5 s, and the result was identical to the original raster.

A mosaic is an ordinary lazy `Image`, so you can chain anything on it before saving: `select`, band math, `normalizedDifference`, `clip`, `where`, … Each step still reads only what it needs.

## Signature

```python
raster.mosaic(images, method="last", res=None, save=False)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `images` | list, folder, or pattern | – | A list of `Image`s and/or file paths; a folder (all `.tif`/`.tiff` files); or a glob pattern such as `"tiles/*.tif"` or `"scenes/**/B04.tif"`. |
| `method` | `str` | `"last"` | How overlapping pixels are combined. See [Methods](#methods). |
| `res` | number or `(x, y)` | first image's | Pixel size of the mosaic. Images with a different pixel size are resampled (nearest neighbour). |
| `save` | `bool` or `str` | `False` | `True` saves `mosaic_mosaic_<epoch>.tif`; a string saves to that path. |

**Returns:** `Image` covering the union of all inputs. Band names come from the first image.

**Raises:** `ValueError` if no rasters are found, the CRSs or band counts differ, or `method`/`res` are invalid.

## Methods

| `method` | Overlapping pixels get… |
|---|---|
| `"last"` | the value of the **last** image in the list (later images on top) |
| `"first"` | the value of the **first** image in the list (earlier images on top) |
| `"min"` | the smallest value |
| `"max"` | the largest value, e.g. a maximum-NDVI composite |
| `"mean"` | the average of all images that have data there |

NoData pixels never cover valid ones, whatever the method. A tile's NoData border doesn't hide the tile underneath.

## Rules for the inputs

- **Same CRS.** All images must use the same coordinate reference system. Reprojection is not supported yet.
- **Same number of bands.** Use `.select()` first if needed.
- **Any position and pixel size.** Tiles may overlap, have gaps (gaps become NoData), sit on different pixel grids, or have different resolutions. Pixels are resampled into the mosaic's grid with nearest neighbour.
- **Computed inputs work.** For example, mosaic the NDVI of several scenes:

```python
paths = ["scene_1.tif", "scene_2.tif", "scene_3.tif"]
ndvis = [raster.image(p).normalizedDifference(["B8", "B4"]) for p in paths]
raster.mosaic(ndvis, method="max", save="max_ndvi.tif")
```

## Data type of the saved file

`save()` keeps the source's **integer type** (for example `uint16` for Sentinel-2 or Landsat digital numbers) when pixel values pass through unchanged, which is the case for `"last"`, `"first"`, `"min"` and `"max"` mosaics of plain files. This keeps the output the same size as the input instead of doubling it to float32. `"mean"` mosaics and anything computed (band math, indices) are saved as float32.

NoData pixels in an integer file get the source's NoData value, or **0** if the source has none. If 0 is a real value in your data, choose another one:

```python
m.save("mosaic.tif", nodata=65535)
m.save("mosaic_f32.tif", dtype="float32")       # force a type
```

## Large mosaics: tips

- **Many cores help.** Mosaic blocks are computed in parallel: about 2× faster with 4–8 workers in our tests. See [Many cores and lots of RAM](performance.md#many-cores-and-lots-of-ram).
- **Clip early.** `raster.mosaic("tiles/").clip(aoi).save(...)` reads only the tiles and pixels inside the area of interest.
- **Pick the compression to suit the job.** `compress=None` writes fastest; `"zstd"` is fast with good compression; `"deflate"` (the default) is the most widely supported.
- **Add overviews to big source tiles** (`gdaladdo`) so that `visualize()` previews are fast. Without them, a preview of a huge mosaic has to touch every block of every tile.
- **Keep the output on a local disk.** Writing to a network drive or USB stick is often the slowest part.
- **Many tiles are fine.** At most 64 files are kept open at a time, so mosaics of thousands of tiles don't run out of file handles.
