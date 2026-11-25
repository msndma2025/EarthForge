# Expressions

`Image.expression()` evaluates a formula written as text. It is often easier to read than chained band-math calls.

```python
evi = img.expression(
    "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
    {
        "NIR": img.select("B8").multiply(0.0001),
        "RED": img.select("B4").multiply(0.0001),
        "BLUE": img.select("B2").multiply(0.0001),
    },
).rename("EVI")
```

The result is a normal lazy `Image`: chain more operations on it, `visualize()` it, or `save()` it.

!!! info "Safe by design"
    The formula is parsed into a syntax tree and only the operations listed below are allowed. It is **never** run as Python code, so something like `__import__("os")` is rejected.

## Signature

```python
Image.expression(expression, map=None, save=False)
Image.Expression(...)      # alias, same function
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `expression` | `str` | – | The formula. |
| `map` | `dict` | `None` | Variable name → `Image` or number. |
| `save` | `bool` or `str` | `False` | `True` saves `<image>_expression_<epoch>.tif`; a string saves to that path. |

**Returns:** `Image`. Band names follow the [band-math rules](band-math.md#rules-for-combining-images) (the left-most image's names), so use `.rename()` to name the result.

## What you can write

| Syntax | Example | Notes |
|---|---|---|
| Variables from `map` | `NIR`, `RED` | Images must be on the same pixel grid. Numbers are also allowed. |
| Band names | `B8`, `B4` | This image's bands can be used directly, without a `map`. |
| `b('name')` | `b('B8')` | Band by name. |
| `b(index)` | `b(7)` | Band by **0-based** index (`b(0)` is the first band). |
| Numbers | `2.5`, `1e-4` | |
| Arithmetic | `+  -  *  /  **` | `**` is power. Unary `-` works too. |
| Parentheses | `( … )` | |
| Comparisons | `<  <=  >  >=  ==  !=` | Give 1 (true) or 0 (false). Chains like `0.2 < x < 0.5` work. |
| Logic, symbols | `&&  \|\|  !` | |
| Logic, Python style | `and  or  not` | |
| Functions | `abs()`, `sqrt()`, `log()`, `log10()`, `exp()` | |

Not supported: the ternary `cond ? a : b`, `%`, and other functions. For if/else logic use [`where()`](reclassify.md).

## Examples

```python
# NDVI from band names directly
ndvi = img.expression("(B8 - B4) / (B8 + B4)").rename("NDVI")

# Same with b()
ndvi = img.expression("(b('B8') - b('B4')) / (b('B8') + b('B4'))")

# SAVI with a constant passed in the map
savi = img.expression(
    "((NIR - RED) / (NIR + RED + L)) * (1 + L)",
    {"NIR": img.select("B8") * 0.0001, "RED": img.select("B4") * 0.0001, "L": 0.5},
).rename("SAVI")

# Water mask: 1 where green > NIR and NIR is low
water = img.expression("B3 > B8 && B8 < 1500").rename("water")
water.visualize(palette=["white", "blue"], min=0, max=1)

# Values within a range
mid = img.expression("1000 < B8 < 2000")

# Images from different variables, saved directly
img.expression("sqrt(abs(NIR - RED))", {"NIR": img.select("B8"), "RED": img.select("B4")},
               save="diff.tif")
```

## Errors

| Message | Cause |
|---|---|
| `Unknown name 'FOO' in expression. Add it to the map, …` | The name is neither in `map` nor a band name. |
| `Invalid expression 'B8 +': invalid syntax` | The formula can't be parsed. |
| `Unsupported syntax in expression: '…'` | It uses something not in the table above. |
| `The expression must use at least one band or image.` | Something like `"1 + 2"`, which has no image in it. |
| `b(99): band index must be 0..25 (0-based)` | The index is out of range. |
| `map value for 'X' must be an Image or a number, got str` | A `map` value has the wrong type. |
