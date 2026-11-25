# Errors

The library raises short, specific exceptions. Tracebacks show **your code and the message only**, without the library's internal frames.

In Jupyter / IPython:
```
---------------------------------------------------------------------------
ValueError                                Traceback (most recent call last)
Cell In[3], line 2
----> new_image.visualize(bands=new_image.select('B8'), palette='Spectral')

ValueError: Band 'B8' not found. This image has: ['B4']
```

In a plain Python script, one short library line remains (`raise error...`) because standard Python always shows the frame that raised the error.

```python
from earthforge import RasterNotFoundError, RasterReadError
```

| Exception | Subclass of | Raised by | Cause |
|---|---|---|---|
| `RasterNotFoundError` | `FileNotFoundError` | `raster.image()` | The path does not exist or is not a file. |
| `RasterReadError` | `Exception` | `raster.image()` | The file exists but cannot be read as a raster. |
| `VectorNotFoundError` | `FileNotFoundError` | `vector.Open()`, `vector.layers()` | The path does not exist or is not a file. |
| `VectorReadError` | `Exception` | `vector.Open()`, `vector.layers()` | Unsupported format, or the file can't be read. |
| `ValueError` | – | `select()`, `rename()`, `addBands()`, band math, `normalizedDifference()`, `expression()`, `where()`, `remap()`, class methods, `clip()`, `visualize()`, `save()`, `vector.Open()` | Invalid band, layer, sheet or column; wrong number of bands or names; images on different grids; invalid expression or rule; vector without geometry/CRS or not overlapping; invalid visualization parameter; saving over a source file. |
| `TypeError` | – | band math, `addBands()`, `where()`, `expression()`, `updateMask()`, `clip()`, `visualize()`, `if img:` | Wrong type of operand, rule, mask, map value or geometry; an `Image` used as a True/False value. |

```python
from earthforge.vector import VectorNotFoundError, VectorReadError
```

## Messages

### Loading
```
RasterNotFoundError: Raster file not found: 'sampleImage.tif'
Looked in: C:\Users\...\python_scripts\sampleImage.tif
```
The second line shows the absolute path that was checked. This helps when a notebook's working directory is different from what you expect.

```
RasterReadError: Could not read 'fake.tif' as a raster (not a valid GeoTIFF?)
```

### Bands
```
ValueError: Band 'B99' not found. This image has: ['B1', 'B2', ...]
ValueError: Band number must be 1..26, got 30
ValueError: select() needs at least one band. Available: [...]
ValueError: rename() needs 1 name(s), got 2: ['a', 'b']
ValueError: Band name(s) ['B4'] already exist. Use .rename() first.
```

### Band math
```
ValueError: Band math needs the same number of bands, or one image with 1 band. Got 2 (['B4', 'B3']) and 3 (['B4', 'B3', 'B2']).
ValueError: Images are not on the same pixel grid (size, CRS or transform differ). 763x551 EPSG:4326  vs  8000x8000 EPSG:32643
ValueError: normalizedDifference() needs exactly 2 bands, got 1: ['B8']
TypeError: add() expects an Image or a number, got str
TypeError: unsupported operand type(s) for +: 'Image' and 'str'
```

### Comparisons and logic
```
TypeError: An Image has no single True/False value. For pixel-wise logic use a.And(b) or a & b, a.Or(b) or a | b, a.Not() or ~a.
```
Raised by `ndvi.gt(0) and ndvi.lt(1)`, `if img:`, and so on.

### Expressions
```
ValueError: Unknown name 'FOO' in expression. Add it to the map, e.g. {'FOO': img.select('B8')}, or use a band name: [...]
ValueError: Invalid expression 'B8 +': invalid syntax
ValueError: Unsupported syntax in expression: 'B8 if B4 else 1'. Supported: numbers, names, b('band'), ... For if/else logic use .where().
ValueError: The expression must use at least one band or image.
ValueError: b(99): band index must be 0..25 (0-based)
TypeError: map value for 'X' must be an Image or a number, got str
```

### Reclassify (`where`)
```
ValueError: where() needs at least one rule, e.g. where(ndvi.lt(0.1), 1)
ValueError: A {test, value} set only works when the value is a number (sets have no order). Use a tuple instead: (test, value).
ValueError: Each where() rule must be a (test, value) pair, got: ...
ValueError: where(): the value has 3 bands; it needs 1 or 1 (same as the image).
TypeError: where(): each rule's test must be an Image, e.g. ndvi.lt(0.1)
TypeError: where(): the value must be an Image or a number, got str
TypeError: 'otherwise' must be a number
```

### Remap
```
ValueError: remap(): `from_values` has 2 values but `to_values` has 1; they must match.
ValueError: remap() needs `to_values`, or a dict {old: new} as `from_values`.
ValueError: remap(): when `from_values` is a dict, leave out `to_values`.
ValueError: remap(): all values must be numbers, got ['a']
TypeError: remap(): `default` must be a number (or float('nan') for NoData).
```

### Classes and masks
```
ValueError: eq() needs a class value or a non-empty list of class values.
ValueError: selectClasses(): class values must be numbers, got ['a']
ValueError: mergeClasses() needs `into`, e.g. mergeClasses([1, 2], into=1), or a dict {new_value: [old_values]}.
ValueError: mergeClasses(): class 2 is listed in more than one group.
TypeError: updateMask() expects an Image, e.g. classified.eq(3)
ValueError: updateMask(): the mask has 3 bands; it needs 1 or 2 (same as the image).
```

### Mosaic
```
ValueError: mosaic() needs at least one image.
ValueError: No rasters found for 'tiles/*.tiff'.
ValueError: All images must have the same CRS. 'sampleImage1.tif' is EPSG:4326, 'a.tif' is EPSG:32643.
ValueError: All images must have the same number of bands. 'tile_1.tif' has 4, 'tile_2.tif' has 3. Use .select() first.
ValueError: method must be one of ['last', 'first', 'min', 'max', 'mean'], got 'median'
ValueError: res must be a positive number or a pair (x, y), e.g. res=10 or res=(10, 10)
TypeError: mosaic() expects a list of images or paths, a folder, or a glob pattern.
```

### Saving
```
ValueError: Cannot save over a file this image reads from: C:\...\sampleImage1.tif
ValueError: Unknown dtype 'uint99'. Use e.g. 'uint8', 'uint16', 'int16', 'float32'.
ValueError: nodata=-1 doesn't fit in uint16 (0 to 65535).
ValueError: workers must be a whole number of 1 or more, got 0
ValueError: raster.BLOCK_SIZE must be a multiple of 256 (e.g. 1024 or 2048), got 1000
```

### Split and stack
```
TypeError: split(): `save` must be True, False or a folder path
ValueError: stack() needs at least one image.
ValueError: All images must have the same CRS. 'a.tif' is EPSG:32643, 'sampleImage1.tif' is EPSG:4326.
ValueError: stack(): `names` needs 6 text names (one per band), got ['x']
TypeError: stack() expects a list of images or paths, a folder, or a glob pattern.
```

### After updating the library in Jupyter
```
TypeError: 'BoundingBox' object is not callable
```
The notebook still uses the previous version of the library. Restart the kernel (Kernel → Restart) and run the cells again. See [Using it in Jupyter](index.md#using-it-in-jupyter).

### Bounds
```
TypeError: bounds(): `save` must be True, False or a file path
```

### Clip
```
TypeError: clip() expects a Vector, e.g. vector.Open('boundary.shp')
ValueError: This dataset is a table without geometries; it can't be used to clip.
ValueError: The vector has no CRS, so it can't be matched to the image. Open it with a CRS, e.g. vector.Open(path, crs='EPSG:4326').
ValueError: The vector does not overlap the image.
```

### Vector
```
VectorNotFoundError: Vector file not found: 'nope.shp'
Looked in: C:\...\nope.shp
VectorReadError: Unsupported vector format '.txt'. Supported: .csv, .geojson, .geoparquet, .gpkg, .json, .kml, .kmz, .parquet, .shp, .xls, .xlsx
VectorReadError: Could not read 'broken.geojson' as GeoJSON: ...
ValueError: Sheet '2099' not found. Available: ['2023', '2024', 'notes']
ValueError: Layer index 9 is out of range. Available: [...]
ValueError: Give both x and y column names, e.g. x='lon', y='lat'.
ValueError: Column 'easting' not found. Columns: [...]
ValueError: This dataset is a table without geometries; it can't be converted to GeoJSON.
ValueError: This dataset is a table without geometries; save it as .csv, .parquet, .xlsx.
ValueError: Unsupported output format '.txt'. Use one of: .geojson, .json, .shp, .gpkg, .kml, .parquet, .csv, .xlsx
ValueError: This dataset is a table without geometries; there is nothing to draw.
```

### Clustering, PCA and sampling
```
TypeError: cluster() expects a clusterer, e.g. cluster.kMeans(5) or cluster.kMeans(5).train(img).
ValueError: The clusterer was trained on bands ['B2', 'B3', 'B4', 'B8'], but this image lacks ['B3', 'B4', 'B8']. This image has: [...]
ValueError: init must be one of ['random', 'kmeans++', 'farthestFirst'], got 'canopy'
ValueError: distanceFunction must be one of ['Euclidean', 'Manhattan'], got 'Cosine'
ValueError: nClusters must be a whole number of 1 or more, got 0
ValueError: Need 2 <= minClusters <= maxClusters, got 5 and 3
ValueError: covariance must be 'full', 'diag', 'spherical' or 'tied', got 'x'
ValueError: Column 'district' is not numeric, so it can't be used for training.
ValueError: Only 3 valid training samples; need at least 5. Increase numPixels or check for NoData.
TypeError: train() expects an Image or a Vector (e.g. img.sample(...) or vector.Open(...)).
ValueError: nComponents=9 is more than the 4 inputs ['B2', 'B3', 'B4', 'B8'].
TypeError: This PCA isn't trained yet. Train it first, e.g. pca.PCA(3).train(img).
ValueError: numPixels must be a whole number of 1 or more, got 0
TypeError: region must be a Vector, e.g. vector.Open('boundary.shp')
ValueError: The vector does not overlap the image.
ValueError: The clusterer was trained on columns ['B2', 'B3', 'B4', 'B8'], but this dataset lacks ['B2', 'B3', 'B4', 'B8'].
```

### Creating geometries
```
ValueError: Need xmin < xmax and ymin < ymax, got (73.2, 33.7, 73.1, 33.8).
ValueError: A polygon needs at least 3 corners given as (x, y) pairs.
ValueError: These corners don't make a valid polygon (do the edges cross, or are all corners on a line?).
ValueError: Could not read 'not wkt' as WKT, e.g. 'POLYGON ((73.10 33.69, ...))'.
TypeError: Can't make a geometry from int. Use a shapely geometry, a GeoJSON dict, WKT text, or a list of (x, y) corners.
ValueError: Property 'name' has 2 values for 1 geometries.
ValueError: distance must be a number of metres, got 'far'
ValueError: This dataset is a table without geometries; there is nothing to buffer.
```

### Records, filters, map and joins
```
ValueError: n must be a number of records (1 or more) or 'all', got 0
ValueError: Position 600 is out of range: this dataset has 600 features (0 to 599).
TypeError: get() takes a column name (text) or a feature position (number).
ValueError: Property 'nope' not found. This feature has: ['station_id', 'name', ...]
TypeError: set() takes a dict, set({'name': value}), or a name and a value, set('name', value)
TypeError: filter() expects a Filter, e.g. Filter.eq('district', 'North')
ValueError: Give at least one filter.
TypeError: Expected a Filter (e.g. Filter.eq('district', 'North')), got str
ValueError: This dataset is a table without geometries; it can't be filtered by location.
ValueError: limit() needs a number of features (0 or more), got -1
TypeError: The function given to map() must return a Feature (e.g. f.set({...}) or vector.Feature(geometry, {...})) or None, but it returned dict.
ValueError: how must be 'inner' or 'left', got 'outer'
ValueError: spatialJoin() needs geometries on both sides; use join() for tables.
ValueError: predicate must be one of ['intersects', 'within', 'contains', 'touches', 'crosses', 'overlaps', 'covers', 'covered_by'], got 'near'
ValueError: Can't merge a dataset with geometries and a table without geometries.
ValueError: Column 'nope' not found. Columns: ['name', 'area_class']
TypeError: overlay expects a Vector with geometries, e.g. vector.Open('boundary.shp')
ValueError: overlay_color needs 1 color or 2 (one per overlay vector)
```

### Visualization
```
ValueError: Give 1 band (single band) or 3 bands (RGB), got 2: ['B4', 'B3']
ValueError: 'min' needs 1 value or 3 values (one per band), got 2
ValueError: 'palette' only works with a single band. Remove it or pass 1 band.
ValueError: Unknown palette 'nope'. Built-in: ['blue_red', 'change', ...]. Any matplotlib colormap also works (e.g. 'viridis', 'RdYlGn'). Run raster.showPalettes() to see them.
ValueError: colorbar_orientation must be 'vertical' or 'horizontal'
ValueError: 'labels' must be a non-empty dict like {1: 'Water', 2: 'Forest'}
ValueError: 'labels' only works with a single band (a class map).
ValueError: 'palette' has 2 colors but 'labels' has 4 classes; give one color per class.
TypeError: Give band names or numbers here (e.g. 'B8' or 8), not an Image. To show an Image, call .visualize() on it.
```

## Handling errors

Because `RasterNotFoundError` subclasses `FileNotFoundError`, either one can be caught:

```python
from earthforge import raster, RasterNotFoundError

try:
    img = raster.image("missing.tif")
except RasterNotFoundError as e:
    print(e)
```

The errors are still ordinary `ValueError`, `TypeError`, `RasterNotFoundError` and `RasterReadError` instances, so `except ValueError:` and similar work as usual.

## A note on `SyntaxError`

A `SyntaxError` is raised by Python itself, before any library code runs, so its message can't be improved by the library. The most common case:

```python
ndvi.gt(0.2).and(ndvi.lte(0.3))     # SyntaxError: `and` is a Python keyword
ndvi.gt(0.2).And(ndvi.lte(0.3))     # correct
ndvi.gt(0.2).and_(ndvi.lte(0.3))    # also correct (lowercase spelling)
(ndvi > 0.2) & (ndvi <= 0.3)        # also correct (operators)
```

## Debugging the library

To see the library's internal frames in tracebacks (useful when reporting a bug):

```python
import earthforge
earthforge.FULL_TRACEBACKS = True
```

Errors that come from other packages (numpy, rasterio, matplotlib) always keep their full traceback.
