# Band math

Arithmetic between images and numbers, band by band. Every operation returns a **new lazy `Image`**. Nothing is computed until you `visualize()`, `save()` or access `.data`.

```python
from pyearthforge import raster
img = raster.image("sampleImage1.tif")

nir, red = img.select("B8"), img.select("B4")
ratio = nir.divide(red)                       # method style
ratio = nir / red                             # operator style, same result
ndvi = img.normalizedDifference(["B8", "B4"])
```

For text formulas, see [Expressions](expressions.md). For reclassification, see [Reclassify with `where()`](reclassify.md).

- [Methods](#methods)
- [Operators](#operators)
- [Comparisons and logic](#comparisons-and-logic)
- [`normalizedDifference(bandNames=None, save=False)`](#normalizeddifferencebandnamesnone-savefalse)
- [Rules for combining images](#rules-for-combining-images)
- [The `save` argument](#the-save-argument)
- [Invalid results become NaN](#invalid-results-become-nan)
- [Recipes](#recipes)

---

## Methods

### Binary: `image.op(other, save=False)`

`other` can be an `Image` or a number (`int`, `float` or NumPy scalar).

| Method | Computes |
|---|---|
| `add(other)` | `image + other` |
| `subtract(other)` | `image - other` |
| `multiply(other)` | `image * other` |
| `divide(other)` | `image / other` (division by 0 → NaN) |
| `pow(other)` | `image ** other` |

### Unary: `image.op(save=False)`

| Method | Computes |
|---|---|
| `abs()` | absolute value |
| `sqrt()` | square root (negative → NaN) |
| `log()` | natural log (≤ 0 → NaN) |
| `log10()` | base-10 log (≤ 0 → NaN) |
| `exp()` | e<sup>x</sup> |

```python
nir, red = img.select("B8"), img.select("B4")

total = nir.add(red)                   # nir + red
diff = nir.subtract(red)               # nir - red
scaled = nir.multiply(0.0001)          # nir * 0.0001
ratio = nir.divide(red)                # nir / red
squared = nir.pow(2)                   # nir ** 2

magnitude = diff.abs()                 # abs(diff)
root = nir.sqrt()
log_ratio = ratio.log()
db = ratio.log10() * 10                # decibels
back = log_ratio.exp()                 # undoes log()

diff = nir.subtract(red, save="diff.tif")    # any of them can save directly
```

**Raises:**
- `TypeError` if `other` is neither an `Image` nor a number.
- `ValueError` if the images are on different grids or have incompatible band counts. See [Rules](#rules-for-combining-images).

---

## Operators

Python operators work as shorthand for the methods above. They have no `save` argument; call `.save()` on the result instead.

| Expression | Same as |
|---|---|
| `a + b`, `a - b`, `a * b`, `a / b`, `a ** b` | `a.add(b)`, `a.subtract(b)`, `a.multiply(b)`, `a.divide(b)`, `a.pow(b)` |
| `2 * a`, `1 - a`, `1 / a`, `2 ** a` | the number can be on either side |
| `-a` | `a.multiply(-1)` |
| `abs(a)` | `a.abs()` |

```python
evi = 2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)
```

---

## Comparisons and logic

These return images of **1 (true) and 0 (false)**, and NaN where an input is NoData. Use them as masks, or as tests in [`where()`](reclassify.md). Every method also takes `save=False`.

| Method | Operator | 1 where… |
|---|---|---|
| `lt(other)` | `a < other` | a < other |
| `lte(other)` | `a <= other` | a ≤ other |
| `gt(other)` | `a > other` | a > other |
| `gte(other)` | `a >= other` | a ≥ other |
| `eq(other)` | – | a = other (a list: a equals any of them) |
| `neq(other)` | – | a ≠ other (a list: a equals none of them) |
| `And(other)` | `a & other` | both non-zero |
| `Or(other)` | `a \| other` | either non-zero |
| `Not()` | `~a` | a is zero |

```python
ndvi = img.normalizedDifference(["B8", "B4"])
moderate = ndvi.gt(0.34).And(ndvi.lt(0.5))       # method style
moderate = (ndvi > 0.34) & (ndvi < 0.5)          # operator style, same result
not_water = ndvi.lt(0).Not()                     # or ~ndvi.lt(0)
extreme = ndvi.lt(0).Or(ndvi.gt(0.8))            # or (ndvi < 0) | (ndvi > 0.8)
land = classified.neq([0, 1])                    # neither class 0 nor class 1
fraction = moderate.data.mean()                  # share of pixels that are moderate
```

- `And` / `Or` / `Not` are capitalized because `and` / `or` / `not` are Python keywords (`.and()` is a `SyntaxError`). The lowercase spellings `and_()`, `or_()` and `not_()` work too. Using `and` / `or` / `if` on an image raises a `TypeError` that explains this.
- `==` and `!=` are deliberately **not** overloaded, so images keep working as dict keys and in lists. Use `.eq()` / `.neq()`.
- With operators, add parentheses around each comparison, because `&` binds more tightly than `>`.

---

## `normalizedDifference(bandNames=None, save=False)`

Calculates the Normalized Difference between two bands: `(first − second) / (first + second)` for every pixel. Pixels where the sum is 0 become NaN.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `bandNames` | list of 2 band names or numbers | first two bands | The two bands, in order. |
| `save` | `bool` or `str` | `False` | See [The `save` argument](#the-save-argument). Auto file name: `<image>_nd_<epoch>.tif`. |

**Returns:** a single-band `Image` named `"nd"`, with values from −1 to 1. Rename it with `.rename()`.

**Raises:** `ValueError` if `bandNames` doesn't contain exactly 2 bands, or the image has fewer than 2 bands.

| Index | Call (Sentinel-2) | Suggested palette |
|---|---|---|
| NDVI (vegetation) | `normalizedDifference(["B8", "B4"])` | `"ndvi"` |
| NDWI (water, McFeeters) | `normalizedDifference(["B3", "B8"])` | `"ndwi"` |
| MNDWI (water) | `normalizedDifference(["B3", "B11"])` | `"water"` |
| NDBI (built-up) | `normalizedDifference(["B11", "B8"])` | `"ndbi"` |
| NBR (burn) | `normalizedDifference(["B8", "B12"])` | `"nbr"` |
| NDSI (snow) | `normalizedDifference(["B3", "B11"])` | `"ndsi"` |

---

## Rules for combining images

When both operands are images:

1. **Same pixel grid.** Width, height, CRS and transform must match, or a `ValueError` is raised. Bands from the same file always match.
2. **Band pairing.**
   - Same number of bands: paired in order (band 1 with band 1, and so on).
   - One image has 1 band: that band is used against every band of the other.
   - Anything else: `ValueError`.
3. **Output band names** come from the left operand. When the left image has 1 band and the right has several, the right image's names are used.

```python
img.select("B4", "B3", "B2") / img.select("B8")     # 3 bands: B4/B8, B3/B8, B2/B8 → names ['B4','B3','B2']
img.select("B4", "B3") + img.select("B8", "B2")     # 2 bands, paired in order
img.select("B4", "B3") + img.select("B8", "B2", "B1")   # ValueError
```

A number is applied to every band: `img * 0.0001` scales all 26 bands.

---

## The `save` argument

Every band-math method (`add`, `subtract`, `multiply`, `divide`, `pow`, `abs`, `sqrt`, `log`, `log10`, `exp`, the comparison and logic methods, `normalizedDifference`, `expression`, `where`) has a `save` argument:

| Value | Effect |
|---|---|
| `False` (default) | Nothing is written. |
| `True` | Saves to `<image stem>_<operation>_<unix epoch>.tif` in the current working directory. |
| `"path.tif"` | Saves to that path. |

The method **always returns the resulting `Image`**, so you can keep chaining or visualize it:

```python
ratio = img.select("B8").divide(img.select("B4"), save=True)
# Saved: C:\...\sampleImage1_divide_1795498682.tif  (1 band(s), float32)
ratio.visualize(palette="viridis")

img.normalizedDifference(["B8", "B4"], save="ndvi.tif")
```

`save=` uses [`Image.save()`](raster.md#imagesave) with its defaults. To choose a compression or rename the bands first, call `.save()` yourself:

```python
img.normalizedDifference(["B8", "B4"]).rename("NDVI").save("ndvi.tif", compress="zstd")
```

---

## Invalid results become NaN

Results that are not finite are set to `NaN`. NaN pixels are drawn transparent and saved as NoData. This covers:
- division by zero,
- `log` of values ≤ 0, `sqrt` of negative values,
- overflow to ±infinity.

NoData pixels in the source file also become `NaN` when read, so they stay NoData through any calculation.

All calculations run in **float32**.

---

## Recipes

```python
img = raster.image("sampleImage1.tif")
blue, green, red, nir, swir1 = (img.select(b) for b in ["B2", "B3", "B4", "B8", "B11"])

# Scale Sentinel-2 L2A digital numbers to reflectance
refl = img.select("B2", "B3", "B4", "B8").multiply(0.0001)

# EVI
evi = (2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)).rename("EVI")

# SAVI (L = 0.5)
savi = ((nir - red) / (nir + red + 0.5) * 1.5).rename("SAVI")

# Simple ratio, saved directly
sr = nir.divide(red, save="simple_ratio.tif")

# Stack of indices in one file
stack = (img.normalizedDifference(["B8", "B4"]).rename("NDVI")
            .addBands(img.normalizedDifference(["B3", "B8"]).rename("NDWI"))
            .addBands(img.normalizedDifference(["B11", "B8"]).rename("NDBI")))
stack.save("indices.tif")
```

For EVI and SAVI, scale the bands to reflectance (0–1) first. The constants in those formulas assume reflectance values.
