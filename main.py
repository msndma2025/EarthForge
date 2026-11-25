from pyearthforge import add, subtract, raster
x = add(3,4)
print(x)

# ---- raster ----
path = "sampleImage1.tif"

img = raster.image(path)   # lazy: reads metadata only, no pixels
print(img)
img.meta()
print("Number of bands:", img.bandNames().size())

# ---- visualize ----
help(img.visualize)        # shows the docstring

img.visualize(bands=["B4", "B3", "B2"], min=1000, max=4000, gamma=1.2)   # true color
img.select("B8").visualize(palette="viridis")                             # single band + colorbar

# ---- indices ----
ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
ndvi.visualize(min=-0.2, max=0.8, palette="ndvi")

ndwi = img.normalizedDifference(["B3", "B8"], save=True)   # also saves a GeoTIFF
ndwi.visualize(min=-0.6, max=0.4, palette="ndwi", colorbar_orientation="horizontal")

# ---- band math ----
nir, red = img.select("B8"), img.select("B4")
ratio = nir.divide(red, save="ratio.tif")    # method style, saved
ratio = nir / red                            # operator style, same result
ratio.visualize(palette="RdYlGn", save_chart=True, dpi=200)

# ---- an Image can be passed as `bands` ----
img.visualize(bands=img.select("B8"), min=250, max=2500)

# ---- expression ----
evi = img.expression(
    "2.5 * ((NIR - RED) / (NIR + 6 * RED - 7.5 * BLUE + 1))",
    {"NIR": img.select("B8") * 0.0001,
     "RED": img.select("B4") * 0.0001,
     "BLUE": img.select("B2") * 0.0001},
).rename("EVI")
evi.visualize(min=0, max=1, palette="evi")

# ---- reclassify with where ----
classes = ndvi.where(
    (ndvi.lt(0.1), 1),                          # water / bare
    (ndvi.gte(0.1).And(ndvi.lt(0.34)), 2),      # sparse vegetation
    (ndvi.gte(0.34).And(ndvi.lt(0.5)), 3),      # moderate vegetation
    (ndvi.gte(0.5), 4),                         # dense vegetation
    otherwise=0,
).rename("class")
classes.visualize(labels={1: "Water / bare", 2: "Sparse", 3: "Moderate", 4: "Dense"},
                  palette=["0000FF", "D2B48C", "9ACD32", "006400"])

# ---- palettes ----
print(raster.palettes())
raster.showPalettes()
