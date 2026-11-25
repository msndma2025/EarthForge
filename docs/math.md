# Arithmetic helpers

```python
from earthforge import add, subtract
```

## `add(a, b)`

Returns `a + b`.

```python
add(3, 4)      # 7
```

## `subtract(a, b)`

Returns `a - b`.

```python
subtract(10, 5)   # 5
```

Both functions work with any values that support `+` / `-`, including numbers and NumPy arrays:

```python
from earthforge import raster, subtract
img = raster.image("sampleImage1.tif")
diff = subtract(img.select("B8").data, img.select("B4").data)   # NIR - Red, as an array
```
