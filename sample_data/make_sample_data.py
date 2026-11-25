"""Creates the example datasets used in the documentation.

    python sample_data/make_sample_data.py

Writes into sample_data/:
- stations.csv       600 weather stations (points as longitude/latitude columns)
- survey.xlsx        sheets "2023" and "2024" (survey plots), "districts" (a table
                     without coordinates) and "notes"
- districts.geojson  5 district polygons covering the sample image

Locations fall inside sampleImage1.tif, and each station's NDVI and land cover
come from that image, so the tables match what you see on the map. The data is
random but reproducible (fixed seed).
"""

from datetime import date, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
from shapely.geometry import Polygon, box

HERE = Path(__file__).resolve().parent
IMAGE = HERE.parent / "sampleImage1.tif"
rng = np.random.default_rng(42)

with rasterio.open(IMAGE) as src:
    left, bottom, right, top = src.bounds
    names = list(src.descriptions)
    b8, b4 = names.index("B8") + 1, names.index("B4") + 1

# ---- districts: a central block and four surrounding districts ----
cx, cy = (left + right) / 2, (bottom + top) / 2
w, h = right - left, top - bottom
central = box(cx - 0.22 * w, cy - 0.22 * h, cx + 0.22 * w, cy + 0.22 * h)
outer = box(left, bottom, right, top).difference(central)
corners = {"North": [(left, top), (right, top)], "East": [(right, top), (right, bottom)],
           "South": [(right, bottom), (left, bottom)], "West": [(left, bottom), (left, top)]}
shapes = {"Central": central}
for name, (a, b) in corners.items():
    shapes[name] = outer.intersection(Polygon([(cx, cy), a, b]))
districts = gpd.GeoDataFrame(
    {"district": list(shapes),
     "code": ["C", "N", "E", "S", "W"],
     "zone_type": ["urban", "residential", "mixed", "rural", "residential"]},
    geometry=list(shapes.values()), crs="EPSG:4326")
districts.to_file(HERE / "districts.geojson", driver="GeoJSON")


def district_of(lon, lat):
    points = gpd.GeoDataFrame(geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")
    joined = gpd.sjoin(points, districts, how="left", predicate="within")
    return joined.groupby(level=0)["district"].first().fillna("Central").to_numpy()


def ndvi_at(lon, lat):
    with rasterio.open(IMAGE) as src:
        values = np.array(list(src.sample(zip(lon, lat), indexes=[b8, b4])), dtype="float64")
    return np.round((values[:, 0] - values[:, 1]) / (values[:, 0] + values[:, 1]), 3)


def land_cover(ndvi):
    return np.select([ndvi < 0.0, ndvi < 0.2, ndvi < 0.45], ["water", "built-up", "grassland"], "forest")


def random_points(n):
    margin = 0.01
    lon = rng.uniform(left + margin * w, right - margin * w, n).round(6)
    lat = rng.uniform(bottom + margin * h, top - margin * h, n).round(6)
    return lon, lat


# ---- stations.csv (600 records) ----
n = 600
lon, lat = random_points(n)
ndvi = ndvi_at(lon, lat)
district = district_of(lon, lat)
places = ["Lakeside", "Hill", "Park", "Market", "Garden", "Bridge", "Valley", "Ridge", "Square", "Grove"]
elevation = (520 + 140 * (lat - bottom) / h + rng.normal(0, 12, n)).round(1)
rain = rng.gamma(2.2, 7.5, n).round(1)
rain[rng.random(n) < 0.03] = np.nan                         # ~3% missing readings
stations = pd.DataFrame({
    "station_id": [f"ST{i:04d}" for i in range(1, n + 1)],
    "name": [f"{district[i]} {places[i % len(places)]} {i + 1}" for i in range(n)],
    "district": district,
    "land_cover": land_cover(ndvi),
    "ndvi": ndvi,
    "longitude": lon,
    "latitude": lat,
    "elevation_m": elevation,
    "rain_mm": rain,
    "temp_c": (24 - 0.0065 * (elevation - 500) + rng.normal(0, 1.2, n)).round(1),
    "install_date": [date(2015, 1, 1) + timedelta(days=int(d)) for d in rng.integers(0, 3650, n)],
    "active": rng.random(n) > 0.12,
})
stations.to_csv(HERE / "stations.csv", index=False)


# ---- survey.xlsx (several sheets) ----
def survey(year, n):
    lon, lat = random_points(n)
    ndvi = ndvi_at(lon, lat)
    cover = np.clip(ndvi * 120 + rng.normal(0, 8, n), 0, 100).round(0)
    return pd.DataFrame({
        "plot_id": [f"{year}-{i:03d}" for i in range(1, n + 1)],
        "district": district_of(lon, lat),
        "lon": lon,
        "lat": lat,
        "canopy_cover_pct": cover,
        "tree_count": np.maximum(0, (cover / 4 + rng.normal(0, 3, n))).round(0).astype(int),
        "surveyor": rng.choice(["Ayesha", "Bilal", "Chen", "Dana"], n),
        "visit_date": [date(year, 3, 1) + timedelta(days=int(d)) for d in rng.integers(0, 200, n)],
    })


population = pd.DataFrame({
    "district": ["Central", "North", "East", "South", "West"],
    "population": [182_400, 96_300, 74_800, 51_200, 88_900],
    "households": [31_200, 16_900, 12_700, 8_800, 15_100],
    "area_km2": districts.to_crs(32643).area.div(1e6).round(2).to_numpy(),
})
notes = pd.DataFrame({"note": [
    "Synthetic data for testing and documentation.",
    "Sheets 2023 and 2024 hold survey plots (lon/lat columns).",
    "Sheet 'districts' is a table without coordinates; join it on 'district'.",
]})
with pd.ExcelWriter(HERE / "survey.xlsx") as book:
    survey(2023, 220).to_excel(book, sheet_name="2023", index=False)
    survey(2024, 240).to_excel(book, sheet_name="2024", index=False)
    population.to_excel(book, sheet_name="districts", index=False)
    notes.to_excel(book, sheet_name="notes", index=False)

print(f"Wrote {len(stations)} stations, 220 + 240 survey plots and {len(districts)} districts to {HERE}")
