"""Checks that every public function, method and property has a description and an example.

    python tools/check_docstrings.py

Exits with an error (and lists the problems) if any public name has no
docstring, a one-line docstring only, or no "Examples" section.
"""

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import earthforge  # noqa: E402
from earthforge import cluster, pca, raster, vector  # noqa: E402

TARGETS = {
    "earthforge": [earthforge.add, earthforge.subtract],
    "raster": [raster.image, raster.mosaic, raster.stack, raster.palettes, raster.showPalettes],
    "vector": [vector.Open, vector.layers, vector.fromGeometry, vector.Rectangle, vector.Polygon, vector.Point],
    "cluster": [cluster.kMeans, cluster.wekaKMeans, cluster.wekaCascadeKMeans, cluster.wekaXMeans,
                cluster.miniBatchKMeans, cluster.bisectingKMeans, cluster.gaussianMixture],
}
CLASSES = [raster.Image, raster.BandList, vector.Vector, vector.ColumnList, vector.Feature, vector.Filter,
           cluster.Clusterer, cluster.TrainedClusterer, pca.PCA, pca.TrainedPCA]


def problems_of(name, obj):
    doc = inspect.getdoc(obj) or ""
    found = []
    if not doc.strip():
        found.append("no docstring")
    else:
        if len([line for line in doc.splitlines() if line.strip()]) < 2:
            found.append("only a one-line description")
        if "Examples" not in doc and ">>>" not in doc:
            found.append("no example")
    return [f"{name}: {p}" for p in found]


def docs_code():
    """All code blocks of the user-guide pages, as one text."""
    import re
    text = ""
    for page in (ROOT / "docs").glob("*.md"):
        if page.name in ("api.md", "changelog.md"):
            continue
        text += "\n".join(re.findall(r"```python\n(.*?)```", page.read_text(encoding="utf-8"), re.S))
    return text


def main():
    problems, checked = [], 0
    code = docs_code()
    for module, funcs in TARGETS.items():
        for func in funcs:
            problems += problems_of(f"{module}.{func.__name__}", func)
            if f"{func.__name__}(" not in code:
                problems.append(f"{module}.{func.__name__}: no example in the docs pages")
            checked += 1
    for cls in CLASSES:
        seen = set()
        for name, attr in vars(cls).items():
            if name.startswith("_"):
                continue
            target = attr.fget if isinstance(attr, property) else attr
            target = getattr(target, "__func__", target)
            if not callable(target):
                continue
            original = getattr(target, "__wrapped__", target)
            if original in seen:            # aliases (Expression, and_, or_, not_) share one docstring
                continue
            seen.add(original)
            problems += problems_of(f"{cls.__name__}.{name}", attr)
            usage = f".{name}" if isinstance(attr, property) else f".{name}("
            if usage not in code:
                problems.append(f"{cls.__name__}.{name}: no example in the docs pages")
            checked += 1
    print(f"Checked {checked} public functions, methods and properties.")
    if problems:
        print(f"{len(problems)} problem(s):")
        for p in problems:
            print("  -", p)
        sys.exit(1)
    print("All have a description and an example.")


if __name__ == "__main__":
    main()
