# vector.py  -> a MODULE for reading vector files and tables, built on geopandas
#
# Usage:
#   from earthforge import vector
#   aoi = vector.Open("boundary.shp")      # shp, geojson, kml, kmz, gpkg, parquet, csv, xlsx
#   aoi.meta()                             # summary: features, geometry type, CRS, columns
#   aoi.columns()                          # column names
#   img.clip(aoi)                          # clip a raster to it
#   img.geometry().save("extent.geojson")  # export a raster's extent

import json
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
from pyproj import CRS

from ._errors import LibraryError, clean_class, clean_errors

# file extension -> human-readable format name
_FORMATS = {
    ".shp": "ESRI Shapefile",
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".kml": "KML",
    ".kmz": "KMZ",
    ".gpkg": "GeoPackage",
    ".parquet": "Parquet",
    ".geoparquet": "Parquet",
    ".csv": "CSV",
    ".xlsx": "Excel",
    ".xls": "Excel",
}

# column names recognised as coordinates / geometry in tables (lower-case)
_X_NAMES = ("longitude", "lon", "long", "lng", "x")
_Y_NAMES = ("latitude", "lat", "y")
_WKT_NAMES = ("wkt", "geometry", "geom", "the_geom", "shape")


# ====================================================================== #
#  Errors
# ====================================================================== #

class VectorNotFoundError(FileNotFoundError):
    """Raised when the vector file does not exist."""


class VectorReadError(LibraryError):
    """Raised when the file exists but cannot be read as a vector or table."""


class _ChoiceError(ValueError):
    """Internal: a wrong layer / column choice by the user (re-raised as ValueError)."""


# ====================================================================== #
#  Column list
# ====================================================================== #

class ColumnList(list):
    """A list of column names with `size()` and `get()` helpers.

    Behaves like a normal Python list (indexing, `len()`, `in`, loops).
    """

    def size(self):
        """Returns the number of columns.

        Same as ``len(aoi.columns())``.

        Examples
        --------
        >>> aoi.columns().size()
        2
        """
        return len(self)

    def get(self, index):
        """Returns the column name at a position in the list.

        Parameters
        ----------
        index : int
            0-based position; negative numbers count from the end.

        Examples
        --------
        >>> aoi.columns().get(0)
        'name'
        """
        return self[index]


# ====================================================================== #
#  Feature: one geometry + its properties
# ====================================================================== #

class Feature:
    """A single feature: a geometry plus a set of named properties.

    Features are what `Vector.map()` passes to your function, and what
    `Vector.get(i)` / `Vector.first()` return. Create one yourself with
    ``vector.Feature(geometry, {"name": value, ...})``. Features don't change:
    `set()` returns a new feature.

    Parameters
    ----------
    geometry : shapely geometry, GeoJSON dict, or None
        The shape. None for a feature without geometry.
    properties : dict, optional
        Property names and values.

    Examples
    --------
    >>> from shapely.geometry import Point
    >>> f = vector.Feature(Point(73.12, 33.70), {"name": "A", "rain_mm": 12.5})
    >>> f.get("rain_mm")
    12.5
    >>> aoi.map(lambda f: vector.Feature(f.centroid(), {"name": f.get("name")}))   # polygons -> centre points
    """

    def __init__(self, geometry=None, properties=None, crs=None):
        if isinstance(geometry, dict):                       # GeoJSON geometry
            from shapely.geometry import shape
            geometry = shape(geometry)
        if properties is not None and not isinstance(properties, dict):
            raise TypeError("Feature properties must be a dict, e.g. {'name': 'A'}")
        self._geometry = geometry
        self._properties = dict(properties or {})
        self._crs = crs                                     # set when it comes from a Vector

    def get(self, name):
        """Returns the value of one property.

        Parameters
        ----------
        name : str
            Property (column) name.

        Raises
        ------
        ValueError
            If the feature has no property with that name.

        Examples
        --------
        >>> station = stations.first()
        >>> station.get("rain_mm")
        14.2
        >>> stations.map(lambda f: f.set({"rain_cm": f.get("rain_mm") / 10}))
        """
        if name not in self._properties:
            raise ValueError(f"Property '{name}' not found. This feature has: {list(self._properties)}")
        value = self._properties[name]
        return value.item() if isinstance(value, np.generic) else value

    def set(self, *args):
        """Returns a copy of the feature with properties added or changed.

        Call it with a dict, ``set({"a": 1, "b": 2})``, or with one name and
        value, ``set("a", 1)``. Existing properties with the same name are replaced.

        Returns
        -------
        Feature

        Examples
        --------
        >>> f.set({"rain_cm": f.get("rain_mm") / 10, "wet": f.get("rain_mm") > 20})
        >>> f.set("checked", True)
        """
        if len(args) == 1 and isinstance(args[0], dict):
            updates = args[0]
        elif len(args) == 2 and isinstance(args[0], str):
            updates = {args[0]: args[1]}
        else:
            raise TypeError("set() takes a dict, set({'name': value}), or a name and a value, set('name', value)")
        return Feature(self._geometry, {**self._properties, **updates}, self._crs)

    def properties(self):
        """Returns all properties as a dict (a copy).

        Examples
        --------
        >>> stations.first().properties()
        {'station_id': 'ST0001', 'district': 'North', 'rain_mm': 14.2, ...}
        """
        return {k: (v.item() if isinstance(v, np.generic) else v) for k, v in self._properties.items()}

    def propertyNames(self):
        """Returns the property names as a list.

        Examples
        --------
        >>> stations.first().propertyNames()
        ['station_id', 'name', 'district', ...]
        """
        return list(self._properties)

    def geometry(self):
        """Returns the shape as a shapely geometry (None if the feature has none).

        Examples
        --------
        >>> g = aoi.first().geometry()
        >>> g.geom_type, g.bounds
        """
        return self._geometry

    def area(self):
        """Returns the area in square metres.

        Geographic coordinates (degrees) are measured on the WGS84 ellipsoid;
        projected coordinates use the CRS units (usually metres). 0 for points
        and lines.

        Examples
        --------
        >>> districts.map(lambda f: f.set({"area_km2": f.area() / 1e6}))
        """
        return self._measure("area")

    def length(self):
        """Returns the length of a line, or the perimeter of a polygon, in metres.

        Measured like area(): on the WGS84 ellipsoid for geographic coordinates.

        Examples
        --------
        >>> roads.map(lambda f: f.set({"length_km": f.length() / 1000}))
        """
        return self._measure("length")

    def centroid(self):
        """Returns the centre point of the geometry (a shapely Point).

        Examples
        --------
        >>> districts.map(lambda f: vector.Feature(f.centroid(), f.properties()))   # labels as points
        """
        if self._geometry is None:
            raise ValueError("This feature has no geometry.")
        return self._geometry.centroid

    def _measure(self, what):
        geom = self._geometry
        if geom is None or geom.is_empty:
            return 0.0
        if self._crs is not None and CRS.from_user_input(self._crs).is_geographic:
            from pyproj import Geod
            area, perimeter = Geod(ellps="WGS84").geometry_area_perimeter(geom)
            if what == "area":
                return abs(area)
            return Geod(ellps="WGS84").geometry_length(geom)
        return float(geom.area if what == "area" else geom.length)

    def __repr__(self):
        kind = self._geometry.geom_type if self._geometry is not None else "no geometry"
        props = ", ".join(f"{k}={v!r}" for k, v in list(self.properties().items())[:6])
        more = ", ..." if len(self._properties) > 6 else ""
        return f"Feature({kind}, {{{props}{more}}})"


# ====================================================================== #
#  Filter: conditions on columns, combined with & | ~
# ====================================================================== #

class Filter:
    """A condition on feature properties, used with `Vector.filter()`.

    Build filters with the static methods (``Filter.eq``, ``Filter.gt``, ...)
    and combine them with ``&`` (and), ``|`` (or) and ``~`` (not), or with
    ``Filter.And`` / ``Filter.Or`` / ``Filter.Not``.

    Examples
    --------
    >>> from earthforge.vector import Filter
    >>> wet = stations.filter(Filter.gt("rain_mm", 20))
    >>> north_forest = stations.filter(Filter.eq("district", "North") & Filter.eq("land_cover", "forest"))
    >>> stations.filter(Filter.inList("district", ["North", "East"]) | ~Filter.notNull("rain_mm"))
    """

    def __init__(self, test, text):
        # Built by the static methods below; users don't call this directly.
        self._test = test            # function: DataFrame -> boolean Series
        self._text = text

    # ---- comparisons ----
    @staticmethod
    def eq(name, value):
        """Keeps features whose property equals `value`.

        Examples
        --------
        >>> stations.filter(Filter.eq("district", "North"))
        """
        return Filter(lambda d: _column(d, name) == value, f"{name} == {value!r}")

    @staticmethod
    def neq(name, value):
        """Keeps features whose property is not equal to `value`.

        Examples
        --------
        >>> stations.filter(Filter.neq("land_cover", "water"))
        """
        return Filter(lambda d: _column(d, name) != value, f"{name} != {value!r}")

    @staticmethod
    def lt(name, value):
        """Keeps features whose property is less than `value`.

        Examples
        --------
        >>> stations.filter(Filter.lt("elevation_m", 520))
        """
        return Filter(lambda d: _column(d, name) < value, f"{name} < {value!r}")

    @staticmethod
    def lte(name, value):
        """Keeps features whose property is less than or equal to `value`.

        Examples
        --------
        >>> stations.filter(Filter.lte("temp_c", 20))
        """
        return Filter(lambda d: _column(d, name) <= value, f"{name} <= {value!r}")

    @staticmethod
    def gt(name, value):
        """Keeps features whose property is greater than `value`.

        Examples
        --------
        >>> stations.filter(Filter.gt("rain_mm", 20))
        """
        return Filter(lambda d: _column(d, name) > value, f"{name} > {value!r}")

    @staticmethod
    def gte(name, value):
        """Keeps features whose property is greater than or equal to `value`.

        Examples
        --------
        >>> stations.filter(Filter.gte("ndvi", 0.5))
        """
        return Filter(lambda d: _column(d, name) >= value, f"{name} >= {value!r}")

    @staticmethod
    def rangeContains(name, minimum, maximum):
        """Keeps features whose property is between `minimum` and `maximum` (both included).

        Examples
        --------
        >>> stations.filter(Filter.rangeContains("ndvi", 0.2, 0.5))
        """
        return Filter(lambda d: _column(d, name).between(minimum, maximum),
                      f"{minimum!r} <= {name} <= {maximum!r}")

    @staticmethod
    def inList(name, values):
        """Keeps features whose property is one of `values`.

        Examples
        --------
        >>> stations.filter(Filter.inList("district", ["North", "East"]))
        """
        values = list(values)
        return Filter(lambda d: _column(d, name).isin(values), f"{name} in {values!r}")

    # ---- text ----
    @staticmethod
    def stringContains(name, text, ignoreCase=False):
        """Keeps features whose text property contains `text`.

        Examples
        --------
        >>> stations.filter(Filter.stringContains("name", "Lake"))
        >>> stations.filter(Filter.stringContains("name", "lake", ignoreCase=True))
        """
        return Filter(lambda d: _column(d, name).astype("string").str.contains(
            text, case=not ignoreCase, regex=False).fillna(False), f"{name} contains {text!r}")

    @staticmethod
    def stringStartsWith(name, text):
        """Keeps features whose text property starts with `text`.

        Examples
        --------
        >>> stations.filter(Filter.stringStartsWith("station_id", "ST00"))
        """
        return Filter(lambda d: _column(d, name).astype("string").str.startswith(text).fillna(False),
                      f"{name} starts with {text!r}")

    @staticmethod
    def stringEndsWith(name, text):
        """Keeps features whose text property ends with `text`.

        Examples
        --------
        >>> stations.filter(Filter.stringEndsWith("name", "Park"))
        """
        return Filter(lambda d: _column(d, name).astype("string").str.endswith(text).fillna(False),
                      f"{name} ends with {text!r}")

    # ---- missing values ----
    @staticmethod
    def notNull(*names):
        """Keeps features where all the given properties have a value (not empty / NaN).

        Examples
        --------
        >>> stations.filter(Filter.notNull("rain_mm"))
        >>> stations.filter(Filter.notNull("rain_mm", "temp_c"))
        """
        names = _flatten_names(names)
        return Filter(lambda d: pd.concat([_column(d, n).notna() for n in names], axis=1).all(axis=1),
                      f"{', '.join(names)} not empty")

    @staticmethod
    def isNull(name):
        """Keeps features where the property is empty / NaN.

        Examples
        --------
        >>> stations.filter(Filter.isNull("rain_mm"))      # stations with no reading
        """
        return Filter(lambda d: _column(d, name).isna(), f"{name} is empty")

    # ---- combining ----
    @staticmethod
    def And(*filters):
        """Keeps features that pass ALL the filters. Operator form: ``a & b``.

        Examples
        --------
        >>> stations.filter(Filter.And(Filter.eq("district", "North"), Filter.gt("rain_mm", 20)))
        """
        filters = _check_filters(filters)
        return Filter(lambda d: pd.concat([f._test(d) for f in filters], axis=1).all(axis=1),
                      "(" + " and ".join(f._text for f in filters) + ")")

    @staticmethod
    def Or(*filters):
        """Keeps features that pass ANY of the filters. Operator form: ``a | b``.

        Examples
        --------
        >>> stations.filter(Filter.Or(Filter.eq("land_cover", "water"), Filter.lt("ndvi", 0)))
        """
        filters = _check_filters(filters)
        return Filter(lambda d: pd.concat([f._test(d) for f in filters], axis=1).any(axis=1),
                      "(" + " or ".join(f._text for f in filters) + ")")

    @staticmethod
    def Not(filter):
        """Keeps features that do NOT pass the filter. Operator form: ``~f``.

        Examples
        --------
        >>> stations.filter(Filter.Not(Filter.eq("district", "North")))
        """
        (filter,) = _check_filters([filter])
        return Filter(lambda d: ~filter._test(d).astype(bool), f"not {filter._text}")

    def __and__(self, other):
        return Filter.And(self, other)

    def __or__(self, other):
        return Filter.Or(self, other)

    def __invert__(self):
        return Filter.Not(self)

    def __repr__(self):
        return f"Filter({self._text})"


def _column(data, name):
    if name not in data.columns:
        geom = data.geometry.name if isinstance(data, gpd.GeoDataFrame) else None
        cols = [str(c) for c in data.columns if c != geom]
        raise ValueError(f"Column '{name}' not found. Columns: {cols}")
    return data[name]


def _flatten_names(names):
    if len(names) == 1 and isinstance(names[0], (list, tuple)):
        names = names[0]
    if not names:
        raise ValueError("Give at least one column name.")
    return [str(n) for n in names]


def _check_filters(filters):
    if len(filters) == 1 and isinstance(filters[0], (list, tuple)):
        filters = filters[0]
    if not filters:
        raise ValueError("Give at least one filter.")
    for f in filters:
        if not isinstance(f, Filter):
            raise TypeError(f"Expected a Filter (e.g. Filter.eq('district', 'North')), got {type(f).__name__}")
    return list(filters)


# ====================================================================== #
#  Vector
# ====================================================================== #

class Vector:
    """A vector dataset (features with geometry and attributes) or a plain table.

    Create one with `vector.Open(path)`. A raster's extent is also a Vector:
    `img.geometry()`.
    """

    def __init__(self, data, source, fmt, layer=None, layers=None):
        # Built by vector.Open() / img.geometry(); users don't call this directly.
        self._data = data
        self.path = str(source)
        self._format = fmt
        self._layer = layer
        self._layers = list(layers or [])

    # ------------------------------------------------------------------ #
    #  Properties
    # ------------------------------------------------------------------ #

    @property
    def data(self):
        """The features as a geopandas GeoDataFrame (a pandas DataFrame for tables without geometry).

        Use it for anything geopandas or pandas can do: filtering rows,
        statistics, joins, plotting.

        Examples
        --------
        >>> aoi.data.head()
        >>> lake = aoi.data[aoi.data["name"] == "lake"]
        >>> aoi.data.to_crs(32643).area.sum()        # total area in m² (UTM)
        """
        return self._data

    @property
    def crs(self):
        """The coordinate reference system (pyproj CRS), or None if unknown / no geometry.

        Examples
        --------
        >>> aoi.crs.to_epsg()
        4326
        """
        return self._data.crs if self.hasGeometry() else None

    @property
    def bounds(self):
        """The extent as (minx, miny, maxx, maxy) in the vector's CRS, or None for tables.

        Examples
        --------
        >>> minx, miny, maxx, maxy = aoi.bounds
        >>> aoi.bounds
        (73.095, 33.69, 73.148, 33.716)
        """
        if not self.hasGeometry() or len(self._data) == 0:
            return None
        return tuple(float(v) for v in self._data.total_bounds)

    # ------------------------------------------------------------------ #
    #  Information
    # ------------------------------------------------------------------ #

    def hasGeometry(self):
        """Returns True if the dataset has geometries, False for a plain table.

        Tables (CSV, Excel, Parquet) without coordinate or WKT columns have no
        geometries; they can be inspected and saved, but not used to clip.

        Examples
        --------
        >>> vector.Open("boundary.shp").hasGeometry()
        True
        >>> vector.Open("population.csv").hasGeometry()      # no coordinates
        False
        """
        return isinstance(self._data, gpd.GeoDataFrame) and self._data.geometry.name in self._data

    def size(self):
        """Returns the number of features (rows).

        Same as ``len(aoi)``.

        Examples
        --------
        >>> vector.Open("stations.csv").size()
        3
        """
        return len(self._data)

    def columns(self):
        """Returns the attribute column names (the geometry column is not included).

        Returns
        -------
        ColumnList
            A list of names, with ``.size()`` (number of columns) and ``.get(i)``.

        Examples
        --------
        >>> aoi.columns()
        ['name', 'area_km2', 'district']
        >>> aoi.columns().size()
        3
        """
        geom = self._data.geometry.name if self.hasGeometry() else None
        return ColumnList(str(c) for c in self._data.columns if c != geom)

    def columnNames(self):
        """Returns the attribute column names, like `bandNames()` does for images.

        The geometry column is not included. Same as `columns()`.

        Returns
        -------
        ColumnList
            A list of names, with ``.size()`` (number of columns) and ``.get(i)``.

        Examples
        --------
        >>> stations.columnNames()
        ['station_id', 'name', 'district', 'land_cover', 'ndvi', ...]
        >>> stations.columnNames().size()
        12
        >>> "rain_mm" in stations.columnNames()
        True
        """
        return self.columns()

    def geometryTypes(self):
        """Returns the geometry types present, with their counts.

        An empty dict for a table without geometries.

        Examples
        --------
        >>> aoi.geometryTypes()
        {'Polygon': 2}
        >>> vector.Open("stations.csv").geometryTypes()
        {'Point': 3}
        """
        if not self.hasGeometry():
            return {}
        counts = self._data.geometry.geom_type.value_counts(dropna=True)
        return {str(k): int(v) for k, v in counts.items()}

    def meta(self):
        """Prints a summary of the dataset and returns its metadata.

        Shows the format, layer or sheet, number of features, geometry types,
        CRS, bounds, and every column with its data type.

        Returns
        -------
        dict
            path, format, layer, layers, features, geometryTypes, crs, bounds,
            columns (name -> data type).

        Examples
        --------
        >>> vector.Open("boundary.shp").meta()                 # print the summary
        >>> m = vector.Open("survey.xlsx", layer="2024").meta()
        >>> m["features"], m["crs"]
        (3, 'EPSG:4326')
        """
        geom = self._data.geometry.name if self.hasGeometry() else None
        dtypes = {str(c): str(t) for c, t in self._data.dtypes.items() if c != geom}
        m = {
            "path": self.path,
            "format": self._format,
            "layer": self._layer,
            "layers": list(self._layers),
            "features": len(self._data),
            "geometryTypes": self.geometryTypes(),
            "crs": _crs_text(self.crs),
            "bounds": self.bounds,
            "columns": dtypes,
        }
        types = ", ".join(f"{k} ({v})" for k, v in m["geometryTypes"].items()) or "none (table)"
        print("=" * 50)
        print(f" Metadata for: {Path(self.path).name}")
        print("=" * 50)
        print(f" Format     : {m['format']}")
        if m["layer"] is not None:
            others = f"   (available: {m['layers']})" if len(m["layers"]) > 1 else ""
            print(f" Layer      : {m['layer']}{others}")
        print(f" Features   : {m['features']}")
        print(f" Geometry   : {types}")
        print(f" CRS        : {m['crs']}")
        if m["bounds"] is not None:
            print(f" Bounds     : ({', '.join(_number(v) for v in m['bounds'])})")
        print(f" Columns    : {len(dtypes)}")
        width = max((len(c) for c in dtypes), default=0)
        for name, dtype in dtypes.items():
            print(f"   {name:<{width}}  {dtype}")
        print("=" * 50)
        return m

    # ------------------------------------------------------------------ #
    #  Records
    # ------------------------------------------------------------------ #

    def show(self, n=10, columns=None):
        """Shows records as a table: the first `n`, or all of them.

        In Jupyter the table is displayed as a formatted table; elsewhere it is
        printed. Geometries are shown as short WKT text.

        Parameters
        ----------
        n : int or "all", default 10
            How many records to show from the top. "all" (or None) shows every record.
        columns : list of str, optional
            Only show these columns.

        Raises
        ------
        ValueError
            If `n` is not a positive number or "all", or a column doesn't exist.

        Examples
        --------
        >>> stations.show()                                  # first 10 records
        >>> stations.show(25)                                # first 25
        >>> stations.show("all")                             # every record
        >>> stations.show(5, columns=["station_id", "district", "rain_mm"])
        """
        data = self._data
        if columns is not None:
            columns = [columns] if isinstance(columns, str) else list(columns)
            for c in columns:
                _column(data, c)
            data = data[columns]
        else:
            data = pd.DataFrame(data)
            if self.hasGeometry():
                geom = self._data.geometry.name
                data[geom] = self._data.geometry.to_wkt().str.slice(0, 60)
        if n is None or (isinstance(n, str) and n.lower() == "all"):
            part = data
        elif isinstance(n, int) and not isinstance(n, bool) and n >= 1:
            part = data.head(n)
        else:
            raise ValueError(f"n must be a number of records (1 or more) or 'all', got {n!r}")
        print(f"{Path(self.path).name}: showing {len(part)} of {len(data)} records")
        with pd.option_context("display.max_rows", None, "display.max_columns", None,
                               "display.width", 200, "display.max_colwidth", 60):
            try:
                from IPython import get_ipython
                from IPython.display import display
                if get_ipython() is not None and "IPKernelApp" in get_ipython().config:
                    display(part)
                    return None
            except Exception:
                pass
            print(part.to_string())
        return None

    def get(self, key):
        """Returns a column's values, or one feature.

        Parameters
        ----------
        key : str or int
            A column name: returns that column's values as a list.
            A position (0-based, negative counts from the end): returns that
            feature as a Feature.

        Raises
        ------
        ValueError
            If the column doesn't exist or the position is out of range.

        Examples
        --------
        >>> rain = stations.get("rain_mm")           # list of 600 values
        >>> max(rain)
        >>> first = stations.get(0)                  # a Feature
        >>> first.get("district")
        'North'
        >>> stations.get(-1)                         # the last feature
        """
        if isinstance(key, str):
            values = _column(self._data, key).tolist()
            return [v.item() if isinstance(v, np.generic) else v for v in values]
        if isinstance(key, (int, np.integer)) and not isinstance(key, bool):
            n = len(self._data)
            if not -n <= key < n:
                raise ValueError(f"Position {key} is out of range: this dataset has {n} features (0 to {n - 1}).")
            row = self._data.iloc[int(key)]
            return self._feature(row)
        raise TypeError("get() takes a column name (text) or a feature position (number).")

    def first(self):
        """Returns the first feature, as a Feature.

        Examples
        --------
        >>> stations.first().properties()
        """
        if len(self._data) == 0:
            raise ValueError("This dataset has no features.")
        return self.get(0)

    # ------------------------------------------------------------------ #
    #  Adding columns: map
    # ------------------------------------------------------------------ #

    def map(self, func):
        """Runs a function on every feature and builds a new dataset from the results.

        Your function receives one Feature and returns a Feature, usually made
        with ``f.set({...})`` (same geometry, new or changed properties) or
        ``vector.Feature(geometry, {...})`` (new geometry). Return None to drop
        the feature. This is how custom columns are added.

        Parameters
        ----------
        func : function
            Takes a Feature, returns a Feature (or None to drop it).

        Returns
        -------
        Vector

        Raises
        ------
        TypeError
            If the function returns something other than a Feature or None.

        Examples
        --------
        >>> def add_columns(f):
        ...     return f.set({
        ...         "rain_cm": f.get("rain_mm") / 10,
        ...         "wet": f.get("rain_mm") > 20,
        ...     })
        >>> stations2 = stations.map(add_columns)
        >>> districts.map(lambda f: f.set({"area_km2": f.area() / 1e6}))
        >>> districts.map(lambda f: vector.Feature(f.centroid(), {"district": f.get("district")}))
        """
        if not callable(func):
            raise TypeError("map() expects a function that takes a Feature and returns a Feature.")
        geometries, records = [], []
        for _, row in self._data.iterrows():
            result = func(self._feature(row))
            if result is None:
                continue
            if not isinstance(result, Feature):
                raise TypeError(
                    f"The function given to map() must return a Feature (e.g. f.set({{...}}) or "
                    f"vector.Feature(geometry, {{...}})) or None, but it returned {type(result).__name__}.")
            geometries.append(result._geometry)
            records.append(result._properties)
        table = pd.DataFrame.from_records(records) if records else pd.DataFrame(
            columns=[c for c in self.columns()])
        if any(g is not None for g in geometries) or (self.hasGeometry() and not records):
            data = gpd.GeoDataFrame(table, geometry=gpd.GeoSeries(geometries, crs=self.crs))
        else:
            data = table
        return self._derive(data)

    # ------------------------------------------------------------------ #
    #  Filtering, sorting
    # ------------------------------------------------------------------ #

    def filter(self, filter):
        """Keeps the features that pass a filter.

        Parameters
        ----------
        filter : Filter
            Built with Filter.eq / gt / inList / rangeContains / stringContains / ...
            and combined with ``&``, ``|``, ``~``.

        Returns
        -------
        Vector

        Examples
        --------
        >>> from earthforge.vector import Filter
        >>> wet = stations.filter(Filter.gt("rain_mm", 20))
        >>> stations.filter(Filter.eq("district", "North") & Filter.gte("ndvi", 0.4)).size()
        """
        if not isinstance(filter, Filter):
            raise TypeError("filter() expects a Filter, e.g. Filter.eq('district', 'North')")
        keep = filter._test(self._data).fillna(False).astype(bool)
        return self._derive(self._data[keep.values])

    def filterBounds(self, geometry):
        """Keeps the features that intersect a geometry (spatial filter).

        Parameters
        ----------
        geometry : Vector or Image
            Features touching any of its shapes are kept. For an Image, its
            extent is used. Reprojected to this dataset's CRS automatically.

        Returns
        -------
        Vector

        Examples
        --------
        >>> in_aoi = stations.filterBounds(aoi)
        >>> on_image = stations.filterBounds(img)            # inside the image's extent
        """
        if not self.hasGeometry():
            raise ValueError("This dataset is a table without geometries; it can't be filtered by location.")
        if not isinstance(geometry, Vector) and callable(getattr(geometry, "geometry", None)):
            geometry = geometry.geometry()                  # an Image: use its extent
        if not isinstance(geometry, Vector):
            raise TypeError("filterBounds() expects a Vector or an Image.")
        if self.crs is None:
            raise ValueError("This dataset has no CRS, so it can't be matched to the geometry. "
                             "Open it with a CRS, e.g. vector.Open(path, crs='EPSG:4326').")
        from shapely import union_all
        area = union_all(list(geometry._shapes_in(self.crs)))
        return self._derive(self._data[self._data.intersects(area).values])

    def sort(self, column, ascending=True):
        """Sorts the features by a column.

        Examples
        --------
        >>> stations.sort("rain_mm", ascending=False).show(5)     # 5 wettest
        """
        _column(self._data, column)
        return self._derive(self._data.sort_values(column, ascending=ascending, kind="stable"))

    def limit(self, n, column=None, ascending=True):
        """Keeps the first `n` features, optionally after sorting by a column.

        Examples
        --------
        >>> stations.limit(100)                                   # first 100
        >>> stations.limit(10, "rain_mm", ascending=False)        # 10 wettest
        """
        if not isinstance(n, int) or isinstance(n, bool) or n < 0:
            raise ValueError(f"limit() needs a number of features (0 or more), got {n!r}")
        data = self.sort(column, ascending)._data if column is not None else self._data
        return self._derive(data.head(n))

    # ------------------------------------------------------------------ #
    #  Combining datasets
    # ------------------------------------------------------------------ #

    def merge(self, other):
        """Appends the features of another dataset (stacks the rows).

        Columns missing on one side are left empty. The other dataset is
        reprojected to this dataset's CRS.

        Examples
        --------
        >>> both_years = vector.Open("survey.xlsx", layer="2023").merge(vector.Open("survey.xlsx", layer="2024"))
        """
        if not isinstance(other, Vector):
            raise TypeError("merge() expects a Vector.")
        if self.hasGeometry() != other.hasGeometry():
            raise ValueError("Can't merge a dataset with geometries and a table without geometries.")
        right = other._data
        if self.hasGeometry():
            if self.crs is not None and right.crs is not None and right.crs != self.crs:
                right = right.to_crs(self.crs)
            right = right.rename_geometry(self._data.geometry.name) \
                if right.geometry.name != self._data.geometry.name else right
            data = gpd.GeoDataFrame(pd.concat([self._data, right], ignore_index=True),
                                    geometry=self._data.geometry.name, crs=self.crs)
        else:
            data = pd.concat([self._data, right], ignore_index=True)
        return self._derive(data)

    def join(self, other, on, right_on=None, how="inner"):
        """Joins the columns of another dataset or table by matching values in a column.

        Each feature gets the columns of the matching row(s) of `other`.
        Geometries come from this dataset; the other dataset's geometry is not
        used. Columns with the same name get the suffix "_2".

        Parameters
        ----------
        other : Vector
            The dataset or table (CSV, Excel sheet, ...) to take columns from.
        on : str
            Column of this dataset to match on.
        right_on : str, optional
            Column of `other` to match on. Default: the same name as `on`.
        how : "inner" or "left", default "inner"
            "inner" keeps only features with a match; "left" keeps all features
            (unmatched ones get empty values).

        Returns
        -------
        Vector

        Examples
        --------
        >>> population = vector.Open("survey.xlsx", layer="districts")      # a table
        >>> stations.join(population, on="district").show(5)
        >>> districts.join(population, on="name", right_on="district", how="left")
        """
        if not isinstance(other, Vector):
            raise TypeError("join() expects a Vector (a dataset or a table).")
        if how not in ("inner", "left"):
            raise ValueError(f"how must be 'inner' or 'left', got {how!r}")
        right_on = right_on or on
        _column(self._data, on)
        _column(other._data, right_on)
        right = pd.DataFrame(other._data.drop(columns=other._data.geometry.name)
                             if other.hasGeometry() else other._data)
        merged = self._data.merge(right, left_on=on, right_on=right_on, how=how, suffixes=("", "_2"))
        if right_on != on and right_on in merged.columns:
            merged = merged.drop(columns=right_on)
        return self._derive(merged)

    def spatialJoin(self, other, predicate="intersects", how="inner"):
        """Joins the columns of another dataset by location.

        Each feature gets the columns of the features of `other` it touches
        (or lies within, contains, ...). A feature matching several features of
        `other` appears once per match. `other` is reprojected to this
        dataset's CRS. Columns with the same name get the suffix "_2".

        Parameters
        ----------
        other : Vector
            Dataset with geometries, e.g. district polygons.
        predicate : str, default "intersects"
            "intersects", "within", "contains", "touches", "crosses",
            "overlaps", "covers" or "covered_by".
        how : "inner" or "left", default "inner"
            "inner" keeps only features with a match; "left" keeps all.

        Returns
        -------
        Vector

        Examples
        --------
        >>> districts = vector.Open("districts.geojson")
        >>> with_zone = stations.spatialJoin(districts)              # adds the polygon's columns
        >>> stations.spatialJoin(districts, predicate="within", how="left")
        """
        predicates = ("intersects", "within", "contains", "touches", "crosses",
                      "overlaps", "covers", "covered_by")
        if not isinstance(other, Vector):
            raise TypeError("spatialJoin() expects a Vector.")
        if predicate not in predicates:
            raise ValueError(f"predicate must be one of {list(predicates)}, got {predicate!r}")
        if how not in ("inner", "left"):
            raise ValueError(f"how must be 'inner' or 'left', got {how!r}")
        if not self.hasGeometry() or not other.hasGeometry():
            raise ValueError("spatialJoin() needs geometries on both sides; use join() for tables.")
        if self.crs is None or other.crs is None:
            raise ValueError("Both datasets need a CRS for a spatial join.")
        right = other._data.to_crs(self.crs) if other.crs != self.crs else other._data
        geom = right.geometry.name
        clashes = {c: f"{c}_2" for c in right.columns if c != geom and c in self._data.columns}
        right = right.rename(columns=clashes)
        joined = gpd.sjoin(self._data, right, how=how, predicate=predicate)
        joined = joined.drop(columns=[c for c in ("index_right", "index_left") if c in joined.columns])
        return self._derive(joined.reset_index(drop=True))

    # ------------------------------------------------------------------ #
    #  Geometry operations
    # ------------------------------------------------------------------ #

    def buffer(self, distance):
        """Grows (or shrinks) every geometry by a distance in metres.

        A point becomes a circle, a line a corridor, and a polygon gets wider.
        Negative distances shrink polygons. Works in metres for
        longitude/latitude data too (computed in a local UTM projection).

        Parameters
        ----------
        distance : float
            Distance in metres (CRS units for projected data, usually metres).

        Returns
        -------
        Vector
            Same columns, buffered geometries, same CRS.

        Raises
        ------
        ValueError
            If the dataset has no geometries or no CRS.

        Examples
        --------
        >>> circle = vector.Point(73.125, 33.700).buffer(500)       # 500 m radius
        >>> corridor = roads.buffer(50)                              # 50 m each side
        >>> inner = aoi.buffer(-100)                                 # 100 m inside the boundary
        """
        if not self.hasGeometry():
            raise ValueError("This dataset is a table without geometries; there is nothing to buffer.")
        if self.crs is None:
            raise ValueError("The dataset has no CRS, so metres can't be measured. "
                             "Open it with a CRS, e.g. vector.Open(path, crs='EPSG:4326').")
        if not isinstance(distance, (int, float, np.number)) or isinstance(distance, bool):
            raise ValueError(f"distance must be a number of metres, got {distance!r}")
        data = self._data
        if self.crs.is_geographic:
            local = data.estimate_utm_crs()
            grown = data.to_crs(local).buffer(distance).to_crs(self.crs)
        else:
            grown = data.buffer(distance)
        return self._derive(data.set_geometry(grown.values))

    # ------------------------------------------------------------------ #
    #  Clustering and PCA on columns
    # ------------------------------------------------------------------ #

    def cluster(self, clusterer, outputName="cluster", columns=None, region=None):
        """Adds a column with each feature's cluster number.

        Give a trained clusterer, or an untrained one to train it on this
        dataset right away (on `columns`, default all numeric columns). Rows
        with an empty value in any input column get an empty cluster.

        Parameters
        ----------
        clusterer : cluster.TrainedClusterer or cluster.Clusterer
            A trained clusterer (trained on columns with the same names), or an
            untrained one to train on this dataset.
        outputName : str, default "cluster"
            Name of the new column.
        columns : list of str, optional
            Columns to train on, when `clusterer` is not trained yet.
        region : vector.Vector, optional
            When `clusterer` is not trained yet: train only on the features
            inside these shapes. All features still get a cluster.

        Returns
        -------
        Vector

        Raises
        ------
        ValueError
            If a column the clusterer was trained on is missing.

        Examples
        --------
        >>> trained = cluster.kMeans(4, normalize=True).train(stations, inputProperties=["ndvi", "elevation_m", "temp_c"])
        >>> grouped = stations.cluster(trained)
        >>> grouped.visualize(column="cluster")
        >>> stations.cluster(cluster.kMeans(4, normalize=True), columns=["ndvi", "elevation_m", "temp_c"])
        >>> stations.cluster(cluster.kMeans(3), columns=["ndvi", "temp_c"], region=central)
        """
        from .cluster import Clusterer, TrainedClusterer
        if isinstance(clusterer, Clusterer):
            clusterer = clusterer.train(self, inputProperties=columns, region=region)
        if not isinstance(clusterer, TrainedClusterer):
            raise TypeError("cluster() expects a clusterer, e.g. cluster.kMeans(4) or a trained one.")
        X, valid = self._model_matrix(clusterer.schema(), "clusterer")
        labels = pd.Series(pd.NA, index=self._data.index, dtype="Int64")
        labels[valid] = clusterer._predict(X[valid])
        data = self._data.copy()
        data[outputName] = labels.values
        return self._derive(data)

    def pca(self, model, columns=None, standardize=False):
        """Adds principal-component columns (PC1, PC2, ...) computed from numeric columns.

        Give a trained PCA, or a number of components to train one on this
        dataset right away (from `columns`, default all numeric columns).

        Parameters
        ----------
        model : pca.TrainedPCA, int or None
            A trained PCA, or the number of components to keep (None = all).
        columns : list of str, optional
            Columns to train on, when `model` is a number.
        standardize : bool, default False
            Scale columns to unit variance first, when `model` is a number.
            Recommended when columns have different units.

        Returns
        -------
        Vector

        Examples
        --------
        >>> with_pcs = stations.pca(2, columns=["ndvi", "elevation_m", "rain_mm", "temp_c"], standardize=True)
        >>> with_pcs.show(5, columns=["station_id", "PC1", "PC2"])
        """
        from .pca import PCA, TrainedPCA
        if model is None or (isinstance(model, (int, np.integer)) and not isinstance(model, bool)):
            model = PCA(model, standardize=standardize).train(self, inputProperties=columns)
        if not isinstance(model, TrainedPCA):
            raise TypeError("pca() expects a trained PCA (pca.PCA(2).train(...)) or a number of components.")
        X, valid = self._model_matrix(model.schema(), "PCA")
        scores = np.full((len(X), model.nComponents), np.nan)
        scores[valid] = model._apply(X[valid])
        data = self._data.copy()
        for i, name in enumerate(model.componentNames()):
            data[name] = scores[:, i]
        return self._derive(data)

    def _model_matrix(self, names, label):
        missing = [n for n in names if n not in self._data.columns]
        if missing:
            raise ValueError(f"The {label} was trained on columns {names}, but this dataset lacks {missing}.")
        X = self._data[names].apply(pd.to_numeric, errors="coerce").to_numpy(dtype="float64")
        return X, np.isfinite(X).all(axis=1)

    # ------------------------------------------------------------------ #
    #  Plotting
    # ------------------------------------------------------------------ #

    def visualize(self, column=None, palette=None, color=None, edgecolor=None, linewidth=1.0,
                  legend=True, save_chart=False, dpi=200):
        """Draws the geometries as a map chart.

        Polygons are drawn filled with an outline, lines as lines and points as
        dots. Give a `column` to color the features by an attribute: numeric
        columns get a palette and a colorbar, text columns one color per value
        and a legend. The axes show map coordinates (longitude/latitude for
        geographic CRSs). To draw a vector on top of an image, use
        ``img.visualize(overlay=vector)``.

        Parameters
        ----------
        column : str, optional
            Attribute column to color the features by.
        palette : str or list of colors, optional
            Palette for `column`: a built-in palette, a matplotlib colormap or a
            list of colors (see raster.palettes()). Default: "viridis" for numbers,
            "tab10" for text.
        color : str, optional
            Fill color when no `column` is given. Default: green.
        edgecolor : str, optional
            Outline color. Default: dark green (white when coloring by `column`).
        linewidth : float, default 1.0
            Outline / line width.
        legend : bool, default True
            Show the colorbar or legend when `column` is given.
        save_chart : bool, default False
            Also save the chart as "<file name>_<epoch time>.png" in the current folder.
        dpi : int, default 200
            Resolution of the saved PNG.

        Returns
        -------
        str or None
            The saved PNG file name when save_chart=True, otherwise None.

        Raises
        ------
        ValueError
            If the dataset has no geometries, or `column` doesn't exist.

        Examples
        --------
        >>> aoi = vector.Open("boundary.shp")
        >>> aoi.visualize()                                       # outlines and fill
        >>> aoi.visualize(column="name")                          # one color per name
        >>> vector.Open("stations.csv").visualize(column="rain_mm", palette="precipitation")
        >>> aoi.visualize(color="none", edgecolor="red", save_chart=True)
        """
        import time
        import matplotlib.pyplot as plt
        from matplotlib.colors import to_rgba
        from .raster import _make_cmap

        if not self.hasGeometry():
            raise ValueError("This dataset is a table without geometries; there is nothing to draw.")
        data = self._data[self._data.geometry.notna() & ~self._data.geometry.is_empty]
        if column is not None and column not in data.columns:
            raise ValueError(f"Column '{column}' not found. Columns: {list(self.columns())}")
        kinds = data.geometry.geom_type
        polygons = data[kinds.str.contains("Polygon")]
        lines = data[kinds.str.contains("LineString")]
        points = data[kinds.str.contains("Point")]

        fig, ax = plt.subplots(figsize=(9, 7))
        if column is not None:
            numeric = pd.api.types.is_numeric_dtype(data[column])
            cmap = _make_cmap(palette or ("viridis" if numeric else "tab10"))
            legend_kwds = ({"label": column, "shrink": 0.7} if numeric else
                           {"title": column, "loc": "upper left", "bbox_to_anchor": (1.01, 1),
                            "frameon": False})
            # white outlines separate polygons; dots get a dark edge so pale
            # palette colors (e.g. white at the low end) stay visible
            outline = edgecolor or ("white" if len(polygons) else "#333333")
            data.plot(ax=ax, column=column, cmap=cmap, categorical=not numeric, legend=legend,
                      legend_kwds=legend_kwds, edgecolor=outline,
                      linewidth=linewidth if len(polygons) else 0.8, markersize=40)
        else:
            fill, edge = color or "#2e9f78", edgecolor or "#0a5c45"
            if len(polygons):
                face = "none" if fill == "none" else to_rgba(fill, 0.35)
                polygons.plot(ax=ax, facecolor=face, edgecolor=edge, linewidth=linewidth)
            if len(lines):
                lines.plot(ax=ax, color=edge, linewidth=linewidth * 1.5)
            if len(points):
                points.plot(ax=ax, color=fill if fill != "none" else edge, edgecolor=edge,
                            linewidth=0.6, markersize=36)

        crs = self.crs
        geographic = crs is not None and crs.is_geographic
        ax.set_xlabel("Longitude" if geographic else "X")
        ax.set_ylabel("Latitude" if geographic else "Y")
        n = len(data)
        subtitle = f"{n} feature{'s' if n != 1 else ''}" + (f", colored by {column}" if column else "")
        ax.set_title(f"{Path(self.path).name}\n{subtitle}")
        ax.ticklabel_format(useOffset=False, style="plain")
        ax.margins(0.06)                                    # points at the edges stay whole

        saved = None
        if save_chart:
            saved = f"{Path(self.path).stem}_{int(time.time())}.png"
            fig.savefig(saved, dpi=dpi, bbox_inches="tight")
            print(f"Chart saved: {Path(saved).resolve()}  ({dpi} dpi)")
        plt.show()
        plt.close(fig)
        return saved

    def _draw_outline(self, ax, crs, color, linewidth=1.4):
        """Draw the geometries as outlines on an existing map (used by Image.visualize)."""
        data = self._data
        if crs is not None and data.crs is not None and data.crs != crs:
            data = data.to_crs(crs)
        data = data[data.geometry.notna() & ~data.geometry.is_empty]
        kinds = data.geometry.geom_type
        polygons = data[kinds.str.contains("Polygon")]
        lines = data[kinds.str.contains("LineString")]
        points = data[kinds.str.contains("Point")]
        if len(polygons):
            polygons.boundary.plot(ax=ax, color=color, linewidth=linewidth)
        if len(lines):
            lines.plot(ax=ax, color=color, linewidth=linewidth)
        if len(points):
            points.plot(ax=ax, color=color, edgecolor="white", linewidth=0.6, markersize=30)

    # ------------------------------------------------------------------ #
    #  Export
    # ------------------------------------------------------------------ #

    def toGeoJSON(self):
        """Returns the features as a GeoJSON dict (coordinates in EPSG:4326 longitude/latitude).

        Examples
        --------
        >>> img.geometry().toGeoJSON()["features"][0]["geometry"]["coordinates"]
        """
        if not self.hasGeometry():
            raise ValueError("This dataset is a table without geometries; it can't be converted to GeoJSON.")
        return json.loads(self._in_wgs84().to_json(drop_id=True))

    def save(self, path):
        """Saves the dataset to a file; the format is chosen from the extension.

        Supported: .geojson / .json, .shp, .gpkg, .kml, .parquet, .csv, .xlsx.
        GeoJSON and KML are always written in EPSG:4326 (longitude/latitude), as
        those formats require. In .csv and .xlsx, geometries are written as WKT
        text in a "geometry" column.

        Parameters
        ----------
        path : str
            Output file.

        Returns
        -------
        str
            The absolute path of the saved file.

        Examples
        --------
        >>> img.geometry().save("extent.geojson")
        >>> aoi.save("aoi.gpkg")
        """
        out = Path(path)
        ext = out.suffix.lower()
        tables = {".csv", ".xlsx", ".parquet"}
        if not self.hasGeometry() and ext not in tables:
            raise ValueError(f"This dataset is a table without geometries; save it as "
                             f"{', '.join(sorted(tables))}.")
        if ext in (".geojson", ".json"):
            self._in_wgs84().to_file(out, driver="GeoJSON")
        elif ext == ".kml":
            self._in_wgs84().to_file(out, driver="KML")
        elif ext == ".shp":
            self._data.to_file(out, driver="ESRI Shapefile")
        elif ext == ".gpkg":
            self._data.to_file(out, driver="GPKG")
        elif ext == ".parquet":
            self._data.to_parquet(out)
        elif ext in (".csv", ".xlsx"):
            table = self._as_wkt_table()
            if ext == ".csv":
                table.to_csv(out, index=False)
            else:
                table.to_excel(out, index=False)
        else:
            raise ValueError(f"Unsupported output format '{ext}'. Use one of: "
                             f".geojson, .json, .shp, .gpkg, .kml, .parquet, .csv, .xlsx")
        print(f"Saved: {out.resolve()}  ({len(self._data)} feature(s))")
        return str(out.resolve())

    # ------------------------------------------------------------------ #
    #  Helpers
    # ------------------------------------------------------------------ #

    def _derive(self, data):
        """A new Vector with other data but the same source information."""
        if isinstance(data, gpd.GeoDataFrame):
            data = data.reset_index(drop=True)
        else:
            data = pd.DataFrame(data).reset_index(drop=True)
        return Vector(data, self.path, self._format, layer=self._layer, layers=self._layers)

    def _feature(self, row):
        """One row -> a Feature (with this dataset's CRS, for area/length)."""
        if self.hasGeometry():
            geom_name = self._data.geometry.name
            props = {k: v for k, v in row.items() if k != geom_name}
            return Feature(row[geom_name], props, self.crs)
        return Feature(None, dict(row.items()))

    def _in_wgs84(self):
        if self._data.crs is None or CRS.from_user_input(self._data.crs).to_epsg() == 4326:
            return self._data
        return self._data.to_crs(4326)

    def _as_wkt_table(self):
        if not self.hasGeometry():
            return pd.DataFrame(self._data)
        table = pd.DataFrame(self._data.drop(columns=self._data.geometry.name))
        table["geometry"] = self._data.geometry.to_wkt()
        return table

    def _shapes_in(self, crs):
        """The geometries reprojected to `crs`, for clipping a raster."""
        if not self.hasGeometry():
            raise ValueError("This dataset is a table without geometries; it can't be used to clip.")
        if self._data.crs is None:
            raise ValueError("The vector has no CRS, so it can't be matched to the image. "
                             "Open it with a CRS, e.g. vector.Open(path, crs='EPSG:4326').")
        data = self._data if crs is None else self._data.to_crs(crs)
        shapes = tuple(g for g in data.geometry if g is not None and not g.is_empty)
        if not shapes:
            raise ValueError("The vector has no geometries to clip with.")
        return shapes

    def __len__(self):
        return len(self._data)

    def __repr__(self):
        types = ", ".join(self.geometryTypes()) or "table"
        n = len(self._data)
        head = (f"<Vector {Path(self.path).name} | {n} feature{'s' if n != 1 else ''} | "
                f"{types} | {_crs_text(self.crs)}>")
        with pd.option_context("display.max_columns", 8, "display.width", 120,
                               "display.max_colwidth", 60):
            return head + "\n" + self._data.head(5).to_string()


# ====================================================================== #
#  Opening files
# ====================================================================== #

def Open(path, layer=None, x=None, y=None, crs=None):
    """Opens a vector file or table as a Vector.

    Supported: Shapefile (.shp), GeoJSON (.geojson/.json), KML (.kml), KMZ (.kmz),
    GeoPackage (.gpkg), Parquet / GeoParquet (.parquet), CSV (.csv) and
    Excel (.xlsx/.xls, any sheet).

    Tables (CSV, Excel, plain Parquet) become point features when they have
    coordinate columns (e.g. longitude/latitude, lon/lat, x/y) or a WKT
    geometry column; otherwise they are opened as a plain table.

    Parameters
    ----------
    path : str or Path
        The file to open.
    layer : str or int, optional
        Layer of a multi-layer file (GeoPackage, KML folders), or sheet of an
        Excel file, by name or 0-based index. Default: the first layer/sheet;
        for KML/KMZ, all layers are combined. List them with vector.layers(path).
    x, y : str, optional
        Column names with point coordinates in a table. Default: detected
        automatically.
    crs : str or int, optional
        CRS of the data when the file doesn't define one, e.g. "EPSG:32643".
        Tables with longitude/latitude columns default to EPSG:4326.

    Returns
    -------
    Vector

    Raises
    ------
    VectorNotFoundError
        If the file does not exist.
    VectorReadError
        If the format is not supported or the file can't be read.

    Examples
    --------
    >>> aoi = vector.Open("boundary.shp")
    >>> pts = vector.Open("stations.csv")                       # lon/lat columns -> points
    >>> sheet = vector.Open("survey.xlsx", layer="2024")         # a specific sheet
    >>> kml = vector.Open("sites.kmz")
    """
    file = Path(path)
    if not file.is_file():
        raise VectorNotFoundError(f"Vector file not found: '{path}'\nLooked in: {file.resolve()}")
    ext = file.suffix.lower()
    if ext not in _FORMATS:
        raise VectorReadError(f"Unsupported vector format '{ext}'. Supported: "
                              f"{', '.join(sorted(_FORMATS))}")
    fmt = _FORMATS[ext]
    try:
        source = _kml_source(file) if ext == ".kmz" else file
        all_layers = _list_layers(source, ext)
        if ext in (".xlsx", ".xls"):
            sheet = _pick_layer(layer, all_layers, "sheet")
            table = pd.read_excel(file, sheet_name=sheet)
            data = _table_to_data(table, x, y, crs)
            return Vector(data, file, fmt, layer=sheet, layers=all_layers)
        if ext == ".csv":
            data = _table_to_data(pd.read_csv(file), x, y, crs)
            return Vector(data, file, fmt)
        if ext in (".parquet", ".geoparquet"):
            try:
                data = gpd.read_parquet(file)
                fmt = "GeoParquet"
            except ValueError:                  # no geo metadata: a plain table
                data = _table_to_data(pd.read_parquet(file), x, y, crs)
            return Vector(_with_crs(data, crs), file, fmt)
        if ext in (".kml", ".kmz"):
            if layer is None and len(all_layers) > 1:      # KML folders: combine them all
                parts = []
                for name in all_layers:
                    part = gpd.read_file(source, layer=name)
                    part["layer"] = name
                    parts.append(part)
                data = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), crs=parts[0].crs)
                chosen = "all"
            else:
                chosen = _pick_layer(layer, all_layers, "layer") if all_layers else None
                data = gpd.read_file(source, layer=chosen)
            return Vector(_with_crs(_tidy_kml(data), crs), file, fmt, layer=chosen, layers=all_layers)
        chosen = _pick_layer(layer, all_layers, "layer") if all_layers else None
        data = gpd.read_file(file, layer=chosen)
        return Vector(_with_crs(data, crs), file, fmt, layer=chosen, layers=all_layers)
    except (VectorReadError, VectorNotFoundError):
        raise
    except _ChoiceError as error:               # wrong layer / column choice: keep the message
        raise ValueError(str(error)) from None
    except Exception as error:
        raise VectorReadError(f"Could not read '{path}' as {fmt}: {error}") from None


# ====================================================================== #
#  Creating your own geometries
# ====================================================================== #

def _is_coordinate(value):
    return (isinstance(value, (list, tuple)) and len(value) in (2, 3)
            and all(isinstance(v, (int, float, np.number)) and not isinstance(v, bool) for v in value))


def _to_shape(item):
    """One geometry in any supported form -> a shapely geometry."""
    from shapely import wkt as _wkt
    from shapely.geometry import Polygon as _Polygon, shape
    from shapely.geometry.base import BaseGeometry
    if isinstance(item, BaseGeometry):
        return item
    if isinstance(item, dict):
        if item.get("type") == "Feature":
            return shape(item["geometry"])
        return shape(item)
    if isinstance(item, str):
        try:
            return _wkt.loads(item)
        except Exception:
            raise ValueError(f"Could not read {item[:40]!r} as WKT, e.g. 'POLYGON ((73.10 33.69, ...))'.") from None
    if isinstance(item, (list, tuple)) and len(item) >= 3 and all(_is_coordinate(c) for c in item):
        return _Polygon(item)
    raise TypeError(f"Can't make a geometry from {type(item).__name__}. Use a shapely geometry, a GeoJSON dict, "
                    "WKT text, or a list of (x, y) corners.")


def fromGeometry(geometry, crs="EPSG:4326", properties=None):
    """Creates a Vector from your own geometry or geometries.

    Accepts shapely geometries, GeoJSON (a geometry, a Feature or a whole
    FeatureCollection, e.g. drawn on geojson.io), WKT text, or a list of
    (x, y) corners for a polygon. Give a list to create several features.

    Parameters
    ----------
    geometry : geometry, dict, str, list
        One geometry, or a list of geometries.
    crs : str or int, default "EPSG:4326"
        CRS of the coordinates. EPSG:4326 means longitude/latitude in degrees.
    properties : dict or list of dict, optional
        Columns for the features: a dict of lists ({"name": ["A", "B"]}), or
        one dict per feature.

    Returns
    -------
    Vector

    Raises
    ------
    ValueError, TypeError
        If a geometry can't be read.

    Examples
    --------
    >>> aoi = vector.fromGeometry({"type": "Polygon", "coordinates": [[[73.11, 33.69], [73.14, 33.69],
    ...                                                                [73.14, 33.71], [73.11, 33.71], [73.11, 33.69]]]})
    >>> aoi = vector.fromGeometry("POLYGON ((73.11 33.69, 73.14 33.69, 73.14 33.71, 73.11 33.69))")
    >>> fields = vector.fromGeometry([field_1, field_2], properties={"name": ["north", "south"]})
    >>> img.sample(1000, region=aoi)
    """
    if isinstance(geometry, dict) and geometry.get("type") == "FeatureCollection":
        data = gpd.GeoDataFrame.from_features(geometry["features"], crs=crs)
        return Vector(data, "geometry", "Geometry")
    if isinstance(geometry, (list, tuple)) and geometry and not _is_coordinate(geometry[0]):
        items = list(geometry)
    else:
        items = [geometry]
    shapes = [_to_shape(item) for item in items]
    if properties is None:
        table = {}
    elif isinstance(properties, dict):
        table = {k: (list(v) if isinstance(v, (list, tuple)) else [v] * len(shapes)) for k, v in properties.items()}
        for k, v in table.items():
            if len(v) != len(shapes):
                raise ValueError(f"Property '{k}' has {len(v)} values for {len(shapes)} geometries.")
    elif isinstance(properties, (list, tuple)) and len(properties) == len(shapes):
        table = pd.DataFrame(list(properties))
    else:
        raise ValueError("properties must be a dict of lists, or one dict per geometry.")
    data = gpd.GeoDataFrame(table, geometry=shapes, crs=crs)
    return Vector(data, "geometry", "Geometry")


def Rectangle(xmin, ymin, xmax, ymax, crs="EPSG:4326"):
    """Creates a rectangle from its corner coordinates, as a Vector with one feature.

    Parameters
    ----------
    xmin, ymin, xmax, ymax : float
        West, south, east and north edges (longitude/latitude for EPSG:4326).
    crs : str or int, default "EPSG:4326"
        CRS of the coordinates.

    Returns
    -------
    Vector

    Raises
    ------
    ValueError
        If xmin >= xmax or ymin >= ymax.

    Examples
    --------
    >>> box = vector.Rectangle(73.11, 33.69, 73.14, 33.71)              # lon/lat
    >>> box = vector.Rectangle(330000, 3700000, 340000, 3710000, crs="EPSG:32643")   # UTM metres
    >>> img.sample(1000, region=box)
    """
    from shapely.geometry import box as _box
    if not (xmin < xmax and ymin < ymax):
        raise ValueError(f"Need xmin < xmax and ymin < ymax, got ({xmin}, {ymin}, {xmax}, {ymax}).")
    return fromGeometry(_box(xmin, ymin, xmax, ymax), crs=crs)


def Polygon(coordinates, crs="EPSG:4326"):
    """Creates a polygon from a list of (x, y) corners, as a Vector with one feature.

    The polygon is closed automatically (the last corner joins the first).

    Parameters
    ----------
    coordinates : list of (x, y)
        At least 3 corners (longitude, latitude for EPSG:4326).
    crs : str or int, default "EPSG:4326"
        CRS of the coordinates.

    Returns
    -------
    Vector

    Raises
    ------
    ValueError
        If there are fewer than 3 corners or the shape is invalid.

    Examples
    --------
    >>> lake = vector.Polygon([(73.110, 33.690), (73.145, 33.692), (73.148, 33.716), (73.118, 33.712)])
    >>> pca.PCA(3).train(img, region=lake)
    """
    from shapely.geometry import Polygon as _Polygon
    coordinates = list(coordinates)
    if len(coordinates) < 3 or not all(_is_coordinate(c) for c in coordinates):
        raise ValueError("A polygon needs at least 3 corners given as (x, y) pairs.")
    shape = _Polygon(coordinates)
    if not shape.is_valid or shape.area == 0:
        raise ValueError("These corners don't make a valid polygon (do the edges cross, or are all corners on a line?).")
    return fromGeometry(shape, crs=crs)


def Point(x, y, crs="EPSG:4326"):
    """Creates a point, as a Vector with one feature. Use .buffer(metres) to make a circle around it.

    Parameters
    ----------
    x, y : float
        Coordinates (longitude, latitude for EPSG:4326).
    crs : str or int, default "EPSG:4326"
        CRS of the coordinates.

    Returns
    -------
    Vector

    Examples
    --------
    >>> site = vector.Point(73.125, 33.700)
    >>> circle = site.buffer(500)                    # 500 m around the point
    >>> img.sample(300, region=circle)
    """
    from shapely.geometry import Point as _Point
    return fromGeometry(_Point(x, y), crs=crs)


def layers(path):
    """Lists the layers of a vector file, or the sheets of an Excel file.

    Parameters
    ----------
    path : str or Path

    Returns
    -------
    list of str

    Examples
    --------
    >>> vector.layers("survey.xlsx")
    ['2023', '2024']
    """
    file = Path(path)
    if not file.is_file():
        raise VectorNotFoundError(f"Vector file not found: '{path}'\nLooked in: {file.resolve()}")
    ext = file.suffix.lower()
    if ext not in _FORMATS:
        raise VectorReadError(f"Unsupported vector format '{ext}'. Supported: "
                              f"{', '.join(sorted(_FORMATS))}")
    return _list_layers(_kml_source(file) if ext == ".kmz" else file, ext)


# KML display settings that GDAL turns into columns; not real attributes
_KML_RENDERING = ("timestamp", "begin", "end", "altitudeMode", "tessellate", "extrude",
                  "visibility", "drawOrder", "icon")


def _kml_source(file):
    """Path GDAL can read for the KML inside a KMZ (keeps all attributes)."""
    import zipfile
    with zipfile.ZipFile(file) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".kml")]
    if not names:
        raise VectorReadError(f"'{file}' contains no .kml file.")
    inner = "doc.kml" if "doc.kml" in names else names[0]
    return f"/vsizip/{Path(file).resolve().as_posix()}/{inner}"


def _tidy_kml(data):
    """Drop KML rendering columns and empty id/description columns."""
    drop = [c for c in _KML_RENDERING if c in data.columns]
    for col in ("id", "description"):
        if col in data.columns and data[col].fillna("").astype(str).str.strip().eq("").all():
            drop.append(col)
    return data.drop(columns=drop)


def _list_layers(file, ext):
    if ext in (".xlsx", ".xls"):
        with pd.ExcelFile(file) as book:
            return [str(s) for s in book.sheet_names]
    if ext in (".csv", ".parquet", ".geoparquet"):
        return []
    return [str(row[0]) for row in pyogrio.list_layers(file)]


def _pick_layer(layer, available, word):
    if layer is None:
        return available[0] if available else None
    if isinstance(layer, int) and not isinstance(layer, bool):
        if not 0 <= layer < len(available):
            raise _ChoiceError(f"{word.capitalize()} index {layer} is out of range. Available: {available}")
        return available[layer]
    if str(layer) not in available:
        raise _ChoiceError(f"{word.capitalize()} '{layer}' not found. Available: {available}")
    return str(layer)


def _with_crs(data, crs):
    """Set the CRS when the file has none and the user gave one."""
    if crs is not None and isinstance(data, gpd.GeoDataFrame) and data.crs is None:
        data = data.set_crs(crs)
    return data


def _table_to_data(table, x=None, y=None, crs=None):
    """A pandas table -> GeoDataFrame of points / WKT geometries, or the table itself."""
    lower = {str(c).lower(): c for c in table.columns}
    if (x is None) != (y is None):
        raise _ChoiceError("Give both x and y column names, e.g. x='lon', y='lat'.")
    if x is not None:
        for name in (x, y):
            if name not in table.columns:
                raise _ChoiceError(f"Column '{name}' not found. Columns: {list(map(str, table.columns))}")
        x_col, y_col = x, y
    else:
        x_col = next((lower[n] for n in _X_NAMES if n in lower), None)
        y_col = next((lower[n] for n in _Y_NAMES if n in lower), None)
    if x_col is not None and y_col is not None:
        xs = pd.to_numeric(table[x_col], errors="coerce")
        ys = pd.to_numeric(table[y_col], errors="coerce")
        lonlat = xs.abs().le(180).all() and ys.abs().le(90).all()
        point_crs = crs if crs is not None else ("EPSG:4326" if lonlat else None)
        return gpd.GeoDataFrame(table, geometry=gpd.points_from_xy(xs, ys), crs=point_crs)
    wkt_col = next((lower[n] for n in _WKT_NAMES if n in lower), None)
    if wkt_col is not None:
        try:
            geoms = gpd.GeoSeries.from_wkt(table[wkt_col].astype(str))
            data = gpd.GeoDataFrame(table.drop(columns=wkt_col), geometry=geoms, crs=crs)
            return data
        except Exception:
            pass                                    # not WKT after all: keep as a table
    return table                                    # plain table, no geometry


def _from_geometries(geometries, crs, properties=None, source="geometry", fmt="Geometry"):
    """Build a Vector from shapely geometries (used by Image.geometry())."""
    data = gpd.GeoDataFrame(properties or {}, geometry=list(geometries), crs=crs)
    return Vector(data, source, fmt)


def _number(value):
    """Readable coordinate: 6 decimals for degrees, 2 for metres."""
    return f"{value:.2f}" if abs(value) >= 1000 else f"{value:.6f}"


def _crs_text(crs):
    if crs is None:
        return "unknown"
    crs = CRS.from_user_input(crs)
    epsg = crs.to_epsg()
    return f"EPSG:{epsg}" if epsg else crs.name


# ---- short, clean tracebacks for every public entry point ----
clean_class(Vector)
clean_class(Feature)
clean_class(Filter)
Open = clean_errors(Open)
layers = clean_errors(layers)
fromGeometry = clean_errors(fromGeometry)
Rectangle = clean_errors(Rectangle)
Polygon = clean_errors(Polygon)
Point = clean_errors(Point)
