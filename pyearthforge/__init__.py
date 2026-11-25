"""EarthForge: lazy, memory-efficient raster processing and visualization for GeoTIFFs.

    from pyearthforge import raster, vector, cluster, pca

    img = raster.image("scene.tif")
    ndvi = img.normalizedDifference(["B8", "B4"]).rename("NDVI")
    ndvi.visualize(min=-0.2, max=0.8, palette="ndvi")
    ndvi.save("ndvi.tif")

Documentation: https://github.com/msndma2025/EarthForge
"""

from .add import add
from .subtract import subtract
from . import raster          # exposes the whole raster MODULE, so you call raster.image(), raster.meta()
from .raster import RasterNotFoundError, RasterReadError

# `vector`, `cluster` and `pca` are NOT imported here, so `from pyearthforge import raster`
# stays fast (geopandas / scikit-learn are only loaded when you use them).
# `from pyearthforge import vector, cluster, pca` still works: Python imports them on demand.

# Set to True to see the library's internal frames in tracebacks (for debugging).
FULL_TRACEBACKS = False

__all__ = ["add", "subtract", "raster", "vector", "cluster", "pca", "RasterNotFoundError", "RasterReadError"]
__version__ = "0.1.0"
