# Changelog

All notable changes to this project are listed here. Versions follow `__version__` in `pyearthforge/__init__.py`.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-11-25

First public release, under the name **EarthForge** (`pip install pyearthforge`, then `from pyearthforge import raster, vector, cluster, pca`). The package was developed under the working name `modules`; the entries below list everything it contains.

### Packaging
- Renamed the package from `modules` to `pyearthforge`. Imports change from `from modules import ...` to `from pyearthforge import ...`, and `modules.FULL_TRACEBACKS` becomes `pyearthforge.FULL_TRACEBACKS`.
- Installed and imported as `pyearthforge` (`pip install pyearthforge`, `from pyearthforge import ...`). The project is called EarthForge; the `py` prefix keeps it apart from an unrelated `earthforge` package on PyPI.
- GitHub Action `.github/workflows/publish.yml` builds and publishes to PyPI when a GitHub release is published (trusted publishing, no tokens), with a manual TestPyPI run.
- `pyproject.toml` with all dependencies, extras `[jupyter]` (progress-bar widgets) and `[docs]` (documentation tools), MIT license, and built wheels.

### Added
- **Your own geometries:** `vector.Rectangle(xmin, ymin, xmax, ymax)`, `vector.Polygon([(x, y), ...])`, `vector.Point(x, y)` and `vector.fromGeometry(...)` (GeoJSON geometry, Feature or FeatureCollection; WKT; shapely; several at once, with `properties`). All default to EPSG:4326 (lon/lat), and any CRS works. Use them as `region=` for sampling, PCA and clustering, or anywhere a vector is accepted.
- `Vector.buffer(metres)` grows or shrinks geometries by a distance in metres, also for lon/lat data (e.g. a circle around a point).
- PCA and Clustering docs: new "Sampling with your own geometry" sections.

- **One-step clustering with a region:** `img.cluster(cluster.kMeans(5), region=aoi, numPixels=..., seed=...)` trains the clusterer on pixels inside the geometry and applies it, like `img.pca(3, region=aoi)`. `table.cluster(cluster.kMeans(4), columns=[...], region=aoi)` does the same for tables. The result is identical to training and applying in two steps.

- **`region=` for sampling and training:** `img.sample(region=...)`, `PCA.train(region=...)`, `img.pca(n, region=...)` and `Clusterer.train(region=...)` draw their pixels only inside a geometry, reprojected automatically. The full `numPixels` are drawn inside the region. For vector training data, `region` keeps the rows inside it.

### Changed
- **Sparse sampling:** when samples are spread thinly over a large image, `sample()` (and so clustering/PCA training) reads only the small tiles that hold samples instead of whole blocks. Results are identical. On a 2.9 GB raster, 2,000 samples took 2.2 s and 1 MB instead of 3.1–5.6 s and about 110 MB. On a 200,000 × 200,000 mosaic it reads about 5,000 tiles instead of every block.
- When blocks run in parallel, numpy and scikit-learn are limited to one thread per worker (threadpoolctl), so they don't compete with the workers for cores.

- **Clustering** (`from pyearthforge import cluster`): `kMeans`, `wekaKMeans` (random, k-means++ or farthest-first start; Euclidean or Manhattan distance), `wekaCascadeKMeans` (chooses k by Calinski–Harabasz), `wekaXMeans` (chooses k by BIC), `miniBatchKMeans`, `bisectingKMeans` and `gaussianMixture`. Train with `.train(img or vector)`, apply with `img.cluster(trained)` (lazy, saved as uint8) or `vector.cluster(trained)`. `TrainedClusterer` has `info()`, `centers()`, `schema()` and `nClusters`.
- **PCA** (`from pyearthforge import pca`): `pca.PCA(nComponents, standardize).train(...)`, applied with `img.pca(model)` or `vector.pca(model)`. Reports `explainedVariance()` and `loadings()`, and matches scikit-learn.
- `Image.sample(numPixels, seed)` returns random pixels as a dataset, reading only the blocks that contain them.
- `Vector.columnNames()` returns column names, like `bandNames()` for images.
- Trained models are applied in chunks of 262,144 pixels, and the memory budget accounts for them: clustering an 8000 × 8000 × 4 raster peaks at 60 MB with one worker.

- **Records:** `Vector.show(n=10, columns=None)` shows some or all records (`show("all")`). `Vector.get(column)` returns a column's values, and `get(i)` / `first()` return one `Feature`.
- **Filtering:** `Vector.filter(Filter...)` with `Filter.eq`, `neq`, `lt`, `lte`, `gt`, `gte`, `rangeContains`, `inList`, `stringContains`, `stringStartsWith`, `stringEndsWith`, `notNull`, `isNull`, `And`, `Or`, `Not`, and the operators `& | ~`. Also `filterBounds()` (spatial), `sort()` and `limit()`.
- **Custom columns:** `Vector.map(func)`, where the function returns `f.set({...})` or `vector.Feature(geometry, {...})`, or `None` to drop the feature. `Feature` has `get`, `set`, `properties`, `propertyNames`, `geometry`, `area` (m², on the ellipsoid), `length` and `centroid`.
- **Joins:** `merge()` appends features, `join(other, on=...)` matches on a column (also with tables), and `spatialJoin(other, predicate=...)` matches by location.
- **Sample data:** `sample_data/` holds 600 stations (CSV), a multi-sheet Excel survey and district polygons, generated reproducibly by `sample_data/make_sample_data.py`.

- **`Vector.visualize()`** draws vectors as a map: polygons filled with outlines, lines and points. `column=` colors features by an attribute (numbers: palette and colorbar; text: legend).
- **`Image.visualize(overlay=...)`** draws one or more vectors on top of an image, reprojected to the image's CRS. `overlay_color` sets one color or one per vector.

- **Parallel saving.** `save(workers=None)` computes blocks in parallel threads (one per core, up to `raster.MAX_WORKERS = 8`), within a memory budget of about a quarter of the free RAM. The output is identical to one worker. Mosaics and calculations run about 2× faster on multi-core PCs.
- `Image.split(save=False)` returns one single-band image per band. `save=True` or a folder path writes one GeoTIFF per band.
- `raster.stack(images, names=None)` combines the bands of several images or files (a list, folder or pattern) into one image. Images on another grid are resampled onto the first image's grid. Repeated band names are prefixed with the file name.
- `raster.BLOCK_SIZE` (default 1024) and `raster.MAX_WORKERS` (default 8) can be changed.
- Every public function, method and property now has a description and a usage example, both in its docstring (shown by `help()` and in the API reference) and on its docs page. `tools/check_docstrings.py` checks this.

### Added (this release, earlier)
- **`raster.mosaic(images, method="last", res=None)`** combines any number of rasters (a list, a folder or a glob pattern) without loading them. It works like a GDAL VRT: each saved block reads only the overlapping parts of the tiles. Methods: `last`, `first`, `min`, `max`, `mean`. Tiles may overlap, sit on different grids or have different resolutions. Computed images work too.
- `save()` shows a **progress bar** (tqdm; a widget in Jupyter). Turn it off with `progress=False`.
- `save(dtype=..., nodata=...)` chooses the file's data type and NoData value.
- At most 64 source files are kept open at once, so mosaics of thousands of tiles work.

### Changed
- `save()` writes **1024 × 1024 blocks** instead of full-width strips, so memory stays small for very wide images. It is also faster: 3.2 s instead of 4.5 s for NDVI, and 6.4 s instead of 16 s for 4 bands, on an 8000 × 8000 raster.
- `save()` **keeps integer data types** (e.g. uint16) when values pass through unchanged (select, clip, updateMask, first/last/min/max mosaics), instead of always writing float32. Computed results are still float32.

### Added (earlier)
- **`vector` module.** `vector.Open(path, layer=None, x=None, y=None, crs=None)` reads Shapefile, GeoJSON, KML, KMZ, GeoPackage, Parquet/GeoParquet, CSV and Excel (any sheet). Tables become points (coordinate columns), shapes (WKT column) or plain tables. `vector.layers(path)` lists layers or sheets.
- `Vector.meta()`, `columns()`, `size()`, `hasGeometry()`, `geometryTypes()`, `toGeoJSON()` and `save(path)` (`.geojson`, `.shp`, `.gpkg`, `.kml`, `.parquet`, `.csv`, `.xlsx`). The `data`, `crs` and `bounds` attributes.
- `Image.geometry()` returns the image extent as a `Vector`, ready to print or export (e.g. `.save("extent.geojson")`).
- `Image.clip(vector)` crops to the vector's bounding box and masks pixels outside the shapes. It reprojects automatically, and the result matches `rasterio.mask`.
- `Image.updateMask(mask)` sets pixels to NoData where the mask is 0.
- `eq()` / `neq()` accept a list of values, e.g. `classified.eq([3, 4])`.
- `Image.selectClasses(classes)` keeps the listed classes; all others become NoData.
- `Image.mergeClasses(classes, into)` or `mergeClasses({new: [olds]})` merges classes.

### Changed
- **`Image.bounds` is now a method: `bounds(save=False)`.** It returns the same `BoundingBox`, and `save=True` (or a path) also writes the box as GeoJSON. Replace `img.bounds.left` with `img.bounds().left`.
- `FULL_TRACEBACKS` moved to the package: `import pyearthforge; pyearthforge.FULL_TRACEBACKS = True`.

### Added (earlier)
- `Image.expression(expression, map)` evaluates text formulas (also available as `Image.Expression`). Variables come from `map`, band names can be used directly, and `b('name')` / `b(index)` select bands. Supports `+ - * / **`, comparisons, `&& || !` / `and or not`, and `abs sqrt log log10 exp`. Parsed safely with `ast`, never `eval`.
- `Image.where(*rules, otherwise=None)` reclassifies a raster. It accepts a single `where(test, value)` rule or several rules given as tuples, a list, a dict or 2-item sets. Later rules win.
- Comparisons `lt`, `lte`, `gt`, `gte`, `eq`, `neq` and logic `And`, `Or`, `Not`, with operators `< <= > >= & | ~`.
- `visualize(labels={value: "name"})` draws class maps with flat colors and a legend.
- Using an `Image` as True/False (`if img:`, `a and b`) raises a `TypeError` that explains what to use instead.
- Documentation website (MkDocs Material) with a home page, cheat sheet, and API reference generated from docstrings. New pages: Expressions, Reclassify.

### Fixed
- `visualize(bands=img.select("B8"))` raised `TypeError: object of type 'Image' has no len()`. An `Image` passed as `bands` is now shown.

- `Image.remap(from_values, to_values=None, default=None)` replaces exact values with new ones (lists or a `{old: new}` dict). Unlisted values are kept, or set to `default`. Values are matched against the original pixels, so mappings don't chain.
- `and_()`, `or_()`, `not_()` as lowercase spellings of `And()`, `Or()`, `Not()`.
- `raster.FULL_TRACEBACKS` (default `False`) shows the library's internal frames when debugging.

### Changed
- **Short tracebacks.** Errors raised by the library no longer show its internal frames (or unrelated source lines). In Jupyter / IPython only your code line and the message are shown. Errors are still normal `ValueError` / `TypeError` / `RasterNotFoundError` / `RasterReadError` instances.
- "Band not found" message now reads `Band 'B8' not found. This image has: ['B4']`.
- Docstrings rewritten with descriptive summary lines (for example "Calculates the Normalized Difference between two bands.") and `Raises` sections.
- Error messages simplified, for example: `'palette' only works with a single band. Remove it or pass 1 band.`

### Added (earlier in this release)
- `raster.image(path)` opens a GeoTIFF as an `Image`.
- `Image.meta()` prints and returns metadata, including band names.
- `Image.bandNames()` returns a `BandList`: a list with `.size()` and `.get(i)`.
- `Image.select(*bands)` picks bands by name or 1-based number.
- `Image.rename(*names)` and `Image.addBands(other)`.
- Band math: `add`, `subtract`, `multiply`, `divide`, `pow` (with an `Image` or a number), and `abs`, `sqrt`, `log`, `log10`, `exp`.
  - Operators `+ - * / **`, unary `-` and `abs()`. Numbers work on either side.
  - Band pairing: same band count, or one single-band image applied to every band of the other.
  - Non-finite results (x/0, log(0), …) become `NaN`.
- `Image.normalizedDifference(bandNames)` returns a single band named `"nd"`.
- `save` argument on every band-math method and on `normalizedDifference`: `True` for an automatic file name, or a path.
- `Image.save(path=None, compress="deflate")` writes a float32 GeoTIFF in a streamed, tiled, multi-threaded way, with band names stored in the file.
- `Image.visualize()` shows charts with `bands`, `min`, `max`, `gamma` and `palette`.
  - RGB composites (3 bands) and single-band views with a colorbar.
  - `colorbar_label`, `colorbar_orientation` (`"vertical"` / `"horizontal"`), `max_size` (preview resolution).
  - `save_chart=True` saves a PNG named `<image>_<epoch>.png`, at `dpi=200` by default.
- 16 built-in palettes (`ndvi`, `ndwi`, `evi`, `ndbi`, `nbr`, `ndsi`, `elevation`, `temperature`, `precipitation`, `water`, `soil_moisture`, `night_lights`, `vegetation`, `diverging`, `change`, `blue_red`), `_r` reversal, `raster.palettes()`, `raster.showPalettes()` and `raster.PALETTES`.
- `RasterNotFoundError` and `RasterReadError` replace rasterio's long tracebacks.

### Changed (earlier in this release)
- **Images are now lazy.** `raster.image()` reads only metadata. `select()` and band math build recipes. Pixels are read by `visualize()` (downsampled preview), `save()` (streamed) or `.data`.
- `Image.data` is now computed on access as **float32** (it was previously loaded in full at the file's dtype when the image was opened).

## Before 0.1.0 (working name `modules`)

### Added
- `add(a, b)` and `subtract(a, b)`.
