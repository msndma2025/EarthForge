# cluster.py  -> unsupervised classification (clustering) for images and tables
#
# Usage:
#   from pyearthforge import raster, cluster
#   img = raster.image("sampleImage1.tif").select("B2", "B3", "B4", "B8")
#   clusterer = cluster.wekaKMeans(5).train(img, numPixels=5000)   # learn from a sample
#   clusters = img.cluster(clusterer)                              # lazy: one band "cluster"
#   clusters.visualize(labels={i: f"Cluster {i}" for i in range(5)})
#   clusters.save("clusters.tif")                                  # streamed, block by block
#   stations.cluster(clusterer)                                    # tables get a "cluster" column

import numpy as np
import pandas as pd

from ._errors import clean_class, clean_errors

_INITS = ("random", "kmeans++", "farthestFirst")
_DISTANCES = ("Euclidean", "Manhattan")


# ====================================================================== #
#  Training data
# ====================================================================== #

def _training_matrix(data, inputProperties, numPixels, seed, region=None):
    """Image or Vector -> (X float64 [rows, inputs], input names). Rows with gaps are dropped."""
    from .raster import Image
    from .vector import Vector
    if region is not None and not isinstance(region, Vector):
        raise TypeError("region must be a Vector, e.g. vector.Open('boundary.shp')")
    if isinstance(data, Image):
        if inputProperties is not None:                     # read only the bands needed
            data = data.select([inputProperties] if isinstance(inputProperties, str) else list(inputProperties))
        data = data.sample(numPixels=numPixels, seed=seed, geometries=False, region=region)
    elif isinstance(data, Vector) and region is not None:
        data = data.filterBounds(region)                    # only rows inside the region
    if not isinstance(data, Vector):
        raise TypeError("train() expects an Image or a Vector (e.g. img.sample(...) or vector.Open(...)).")
    table = data.data
    geom = table.geometry.name if data.hasGeometry() else None
    if inputProperties is None:
        names = [c for c in table.columns if c != geom and pd.api.types.is_numeric_dtype(table[c])
                 and not pd.api.types.is_bool_dtype(table[c])]
        if not names:
            raise ValueError("The training data has no numeric columns to learn from.")
    else:
        names = [inputProperties] if isinstance(inputProperties, str) else list(inputProperties)
        for n in names:
            if n not in table.columns:
                raise ValueError(f"Column '{n}' not found. Columns: {[c for c in table.columns if c != geom]}")
            if not pd.api.types.is_numeric_dtype(table[n]):
                raise ValueError(f"Column '{n}' is not numeric, so it can't be used for training.")
    X = table[names].to_numpy(dtype="float64")
    X = X[np.isfinite(X).all(axis=1)]
    return X, [str(n) for n in names]


def _check_k(k, label="nClusters"):
    if not isinstance(k, (int, np.integer)) or isinstance(k, bool) or k < 1:
        raise ValueError(f"{label} must be a whole number of 1 or more, got {k!r}")
    return int(k)


# ====================================================================== #
#  k-means in numpy (Weka-style options)
# ====================================================================== #

def _distances(X, centers, distance):
    if distance == "Manhattan":
        return np.abs(X[:, None, :] - centers[None, :, :]).sum(axis=2)
    # squared Euclidean, without building an (n, k, d) array
    return (np.square(X).sum(1)[:, None] - 2 * X @ centers.T + np.square(centers).sum(1)[None, :]).clip(min=0)


def _init_centers(X, k, init, distance, rng):
    n = len(X)
    if init == "random":
        return X[rng.choice(n, size=k, replace=False)].copy()
    first = X[rng.integers(n)]
    centers = [first]
    closest = _distances(X, first[None, :], distance)[:, 0]
    for _ in range(1, k):
        if init == "farthestFirst":
            nxt = int(np.argmax(closest))
        else:                                               # kmeans++
            total = closest.sum()
            nxt = int(rng.choice(n, p=closest / total)) if total > 0 else int(rng.integers(n))
        centers.append(X[nxt])
        closest = np.minimum(closest, _distances(X, X[nxt][None, :], distance)[:, 0])
    return np.array(centers)


def _kmeans(X, k, init, distance, max_iter, rng):
    """One k-means run. Returns (centers, labels, cost)."""
    centers = _init_centers(X, k, init, distance, rng)
    labels = None
    for _ in range(max_iter):
        d = _distances(X, centers, distance)
        new_labels = d.argmin(axis=1)
        if labels is not None and np.array_equal(new_labels, labels):
            break
        labels = new_labels
        for j in range(k):
            members = X[labels == j]
            if len(members) == 0:                           # empty cluster: restart at the worst-fit point
                far = int(d.min(axis=1).argmax())
                centers[j] = X[far]
            else:
                centers[j] = np.median(members, axis=0) if distance == "Manhattan" else members.mean(axis=0)
    d = _distances(X, centers, distance)
    labels = d.argmin(axis=1)
    cost = float(d.min(axis=1).sum())
    return centers, labels, cost


def _best_kmeans(X, k, init, distance, max_iter, restarts, rng):
    best = None
    for _ in range(max(1, restarts)):
        result = _kmeans(X, k, init, distance, max_iter, rng)
        if best is None or result[2] < best[2]:
            best = result
    return best


def _calinski_harabasz(X, labels, k):
    n = len(X)
    if k < 2 or n <= k:
        return float("nan")
    mean = X.mean(axis=0)
    between = within = 0.0
    for j in range(k):
        members = X[labels == j]
        if len(members):
            c = members.mean(axis=0)
            between += len(members) * np.square(c - mean).sum()
            within += np.square(members - c).sum()
    return float((between / (k - 1)) / (within / (n - k))) if within > 0 else float("inf")


def _bic(X, labels, centers):
    """Bayesian information criterion of a k-means result (spherical Gaussians, shared variance)."""
    n, d = X.shape
    k = len(centers)
    if n <= k:
        return float("-inf")
    sse = float(np.square(X - centers[labels]).sum())
    variance = max(sse / (d * (n - k)), 1e-12)
    loglik = 0.0
    for j in range(k):
        nj = int((labels == j).sum())
        if nj == 0:
            continue
        loglik += (nj * np.log(nj) - nj * np.log(n) - nj * d / 2 * np.log(2 * np.pi * variance)
                   - (nj - 1) * d / 2)
    params = (k - 1) + k * d + 1
    return float(loglik - params / 2 * np.log(n))


# ====================================================================== #
#  Clusterer (untrained) and TrainedClusterer
# ====================================================================== #

class Clusterer:
    """A clustering method, ready to be trained. Create one with cluster.kMeans(), cluster.wekaKMeans(), ...

    Examples
    --------
    >>> c = cluster.wekaKMeans(5)
    >>> trained = c.train(img, numPixels=5000)
    """

    def __init__(self, name, params, fit):
        # Built by the functions in this module; users don't call this directly.
        self._name = name
        self._params = params
        self._fit = fit

    def train(self, data, inputProperties=None, numPixels=5000, seed=0, region=None):
        """Trains the clusterer on an image sample or on a dataset's columns.

        Parameters
        ----------
        data : Image or Vector
            An Image: `numPixels` random pixels are sampled (reading only what
            is needed, so large images are fine) and all its bands are used.
            A Vector (e.g. ``img.sample()`` or a CSV): its numeric columns are used.
        inputProperties : list of str, optional
            The bands / columns to use. Default: all bands, or all numeric columns.
        numPixels : int, default 5000
            Pixels to sample when `data` is an Image.
        seed : int, default 0
            Random seed, for the sample and the clustering (same seed = same result).
        region : vector.Vector, optional
            Train only on pixels (or rows) inside these shapes.

        Returns
        -------
        TrainedClusterer

        Raises
        ------
        ValueError
            If there are fewer valid samples than clusters, or a column is missing.

        Examples
        --------
        >>> trained = cluster.wekaKMeans(5).train(img.select("B2", "B3", "B4", "B8"), numPixels=5000)
        >>> trained = cluster.kMeans(4).train(stations, inputProperties=["ndvi", "elevation_m", "rain_mm"])
        >>> trained = cluster.kMeans(5).train(img, region=vector.Open("boundary.shp"))
        """
        X, names = _training_matrix(data, inputProperties, numPixels, seed, region)
        k_needed = self._params.get("nClusters", self._params.get("minClusters", 1))
        if len(X) < max(2, k_needed):
            raise ValueError(f"Only {len(X)} valid training samples; need at least {max(2, k_needed)}. "
                             "Increase numPixels or check for NoData.")
        normalize = self._params.get("normalize", False)
        low = X.min(axis=0) if normalize else np.zeros(X.shape[1])
        span = (X.max(axis=0) - low) if normalize else np.ones(X.shape[1])
        span[span == 0] = 1.0
        Xn = (X - low) / span
        model = self._fit(Xn, np.random.default_rng(seed), seed)
        return TrainedClusterer(self._name, dict(self._params), names, low, span, model, X)

    def __repr__(self):
        params = ", ".join(f"{k}={v!r}" for k, v in self._params.items())
        return f"Clusterer {self._name}({params}), not trained yet"


class TrainedClusterer:
    """A trained clusterer: apply it with ``img.cluster(trained)`` or ``vector.cluster(trained)``.

    Examples
    --------
    >>> trained = cluster.wekaKMeans(5).train(img, numPixels=5000)
    >>> trained.info()
    >>> clusters = img.cluster(trained)
    """

    def __init__(self, name, params, names, low, span, model, X):
        self._name = name
        self._params = params
        self._names = names
        self._low = low
        self._span = span
        self._model = model                  # dict with "predict" and "centers" (normalized space)
        labels = self._predict_normalized((X - low) / span)
        self._sizes = np.bincount(labels, minlength=self.nClusters)
        self._n_train = len(X)

    @property
    def nClusters(self):
        """The number of clusters (for cascade / X-means: the number chosen).

        Examples
        --------
        >>> cluster.wekaCascadeKMeans(2, 8).train(img).nClusters
        4
        """
        return int(len(self._model["centers"]))

    def schema(self):
        """Returns the names of the bands / columns the clusterer was trained on.

        Images and tables you apply it to must have these bands / columns.

        Examples
        --------
        >>> trained.schema()
        ['B2', 'B3', 'B4', 'B8']
        """
        return list(self._names)

    def centers(self):
        """Returns the cluster centres, in the original units, as a table (one row per cluster).

        For gaussianMixture these are the component means.

        Examples
        --------
        >>> trained.centers()
                B2      B3      B4      B8
        0    310.2   420.8   288.1  2650.4
        ...
        """
        centers = self._model["centers"] * self._span + self._low
        table = pd.DataFrame(centers, columns=self._names)
        table.index.name = "cluster"
        return table

    def info(self):
        """Prints a summary: method, settings, number of clusters, training size, cluster sizes and centres.

        Returns
        -------
        dict
            method, params, nClusters, inputs, trainingSamples, clusterSizes, scores.

        Examples
        --------
        >>> trained.info()
        """
        scores = dict(self._model.get("scores", {}))
        m = {"method": self._name, "params": dict(self._params), "nClusters": self.nClusters,
             "inputs": self.schema(), "trainingSamples": self._n_train,
             "clusterSizes": [int(s) for s in self._sizes], "scores": scores}
        print("=" * 56)
        print(f" Clusterer   : {self._name}")
        print(f" Settings    : {', '.join(f'{k}={v!r}' for k, v in self._params.items())}")
        print(f" Inputs      : {self._names}")
        print(f" Samples     : {self._n_train}")
        print(f" Clusters    : {self.nClusters}")
        print(f" Sizes       : {m['clusterSizes']}")
        if "byK" in scores:
            label = scores.get("criterion", "score")
            print(f" {label:<11} : " + ", ".join(f"k={k}: {v:.4g}" for k, v in scores["byK"].items()))
        with pd.option_context("display.width", 120, "display.precision", 4):
            print(" Centres:")
            print("   " + self.centers().to_string().replace("\n", "\n   "))
        print("=" * 56)
        return m

    def _predict(self, X):
        """Raw input values (rows, inputs) -> cluster numbers."""
        return self._predict_normalized((X - self._low) / self._span)

    def _predict_normalized(self, Xn):
        return np.asarray(self._model["predict"](Xn), dtype="int64")

    def _apply(self, X):
        """For the raster engine: (rows, inputs) -> (rows, 1) float32."""
        return self._predict(X).astype("float32")[:, None]

    def __repr__(self):
        return f"TrainedClusterer {self._name}: {self.nClusters} clusters from {self._names}"


# ====================================================================== #
#  The clustering methods
# ====================================================================== #

def kMeans(nClusters, maxIterations=300, restarts=4, normalize=False):
    """Creates a k-means clusterer (scikit-learn's KMeans, k-means++ start).

    Groups pixels or rows into `nClusters` clusters so that each is as close as
    possible (Euclidean distance) to its cluster centre.

    Parameters
    ----------
    nClusters : int
        Number of clusters.
    maxIterations : int, default 300
        Maximum iterations per run.
    restarts : int, default 4
        Number of runs with different starts; the best is kept.
    normalize : bool, default False
        Scale every input to 0-1 before clustering, so inputs with large
        values don't dominate.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.kMeans(5).train(img, numPixels=5000)
    >>> img.cluster(trained).visualize(palette="tab10", min=0, max=4)
    """
    k = _check_k(nClusters)

    def fit(X, rng, seed):
        from sklearn.cluster import KMeans
        model = KMeans(n_clusters=k, max_iter=maxIterations, n_init=max(1, restarts), random_state=seed).fit(X)
        return {"predict": model.predict, "centers": model.cluster_centers_,
                "scores": {"SSE": float(model.inertia_)}}
    return Clusterer("kMeans", {"nClusters": k, "maxIterations": maxIterations, "restarts": restarts,
                                "normalize": normalize}, fit)


def wekaKMeans(nClusters, init="random", distanceFunction="Euclidean", maxIterations=500,
               restarts=1, normalize=True):
    """Creates a simple k-means clusterer in the style of Weka's SimpleKMeans.

    Options for how the first centres are chosen and how distance is measured.
    With Manhattan distance, cluster centres are medians instead of means.
    Inputs are scaled to 0-1 by default, as in Weka.

    Parameters
    ----------
    nClusters : int
        Number of clusters.
    init : str, default "random"
        How the first centres are picked: "random" (random samples),
        "kmeans++" (spread out by probability) or "farthestFirst" (each next
        centre is the sample farthest from those chosen so far).
    distanceFunction : str, default "Euclidean"
        "Euclidean" or "Manhattan".
    maxIterations : int, default 500
        Maximum iterations.
    restarts : int, default 1
        Number of runs with different starts; the one with the lowest cost is kept.
    normalize : bool, default True
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.wekaKMeans(5).train(img, numPixels=5000)
    >>> cluster.wekaKMeans(6, init="kmeans++", distanceFunction="Manhattan", restarts=5)
    """
    k = _check_k(nClusters)
    if init not in _INITS:
        raise ValueError(f"init must be one of {list(_INITS)}, got {init!r}")
    if distanceFunction not in _DISTANCES:
        raise ValueError(f"distanceFunction must be one of {list(_DISTANCES)}, got {distanceFunction!r}")

    def fit(X, rng, seed):
        if len(X) < k:
            raise ValueError(f"Only {len(X)} training samples for {k} clusters.")
        centers, labels, cost = _best_kmeans(X, k, init, distanceFunction, maxIterations, restarts, rng)
        return {"predict": lambda Z: _distances(Z, centers, distanceFunction).argmin(axis=1),
                "centers": centers, "scores": {"cost": cost}}
    return Clusterer("wekaKMeans", {"nClusters": k, "init": init, "distanceFunction": distanceFunction,
                                    "maxIterations": maxIterations, "restarts": restarts,
                                    "normalize": normalize}, fit)


def wekaCascadeKMeans(minClusters=2, maxClusters=10, restarts=10, init="random", maxIterations=500,
                      normalize=True):
    """Creates a k-means clusterer that chooses the number of clusters itself.

    Runs k-means for every number of clusters from `minClusters` to
    `maxClusters` (each with several restarts) and keeps the number with the
    best Calinski-Harabasz score (compact clusters that are far apart), in
    the style of Weka's CascadeSimpleKMeans.

    Parameters
    ----------
    minClusters, maxClusters : int, default 2 and 10
        Range of cluster numbers to try.
    restarts : int, default 10
        k-means runs per number of clusters; the best is kept.
    init : str, default "random"
        "random", "kmeans++" or "farthestFirst" (see wekaKMeans).
    maxIterations : int, default 500
        Maximum iterations per run.
    normalize : bool, default True
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.wekaCascadeKMeans(2, 8).train(img, numPixels=5000)
    >>> trained.nClusters                      # the number it chose
    >>> trained.info()                         # scores for every k
    """
    kmin, kmax = _check_k(minClusters, "minClusters"), _check_k(maxClusters, "maxClusters")
    if kmin < 2 or kmax < kmin:
        raise ValueError(f"Need 2 <= minClusters <= maxClusters, got {kmin} and {kmax}")
    if init not in _INITS:
        raise ValueError(f"init must be one of {list(_INITS)}, got {init!r}")

    def fit(X, rng, seed):
        scores, best = {}, None
        for k in range(kmin, min(kmax, len(X) - 1) + 1):
            centers, labels, _ = _best_kmeans(X, k, init, "Euclidean", maxIterations, restarts, rng)
            score = _calinski_harabasz(X, labels, k)
            scores[k] = score
            if best is None or score > best[0]:
                best = (score, centers)
        centers = best[1]
        return {"predict": lambda Z: _distances(Z, centers, "Euclidean").argmin(axis=1),
                "centers": centers, "scores": {"criterion": "Calinski-H.", "byK": scores}}
    return Clusterer("wekaCascadeKMeans", {"minClusters": kmin, "maxClusters": kmax, "restarts": restarts,
                                           "init": init, "maxIterations": maxIterations,
                                           "normalize": normalize}, fit)


def wekaXMeans(minClusters=2, maxClusters=8, restarts=3, init="kmeans++", maxIterations=500, normalize=True):
    """Creates a k-means clusterer that chooses the number of clusters by BIC.

    Runs k-means for every number of clusters from `minClusters` to
    `maxClusters` and keeps the one with the best Bayesian Information
    Criterion, which rewards a good fit but penalizes extra clusters. Based on
    the X-means idea (Pelleg and Moore), as in Weka's XMeans.

    Parameters
    ----------
    minClusters, maxClusters : int, default 2 and 8
        Range of cluster numbers to try.
    restarts : int, default 3
        k-means runs per number of clusters; the best is kept.
    init : str, default "kmeans++"
        "random", "kmeans++" or "farthestFirst".
    maxIterations : int, default 500
        Maximum iterations per run.
    normalize : bool, default True
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.wekaXMeans(2, 10).train(img, numPixels=5000)
    >>> trained.nClusters
    """
    kmin, kmax = _check_k(minClusters, "minClusters"), _check_k(maxClusters, "maxClusters")
    if kmax < kmin:
        raise ValueError(f"Need minClusters <= maxClusters, got {kmin} and {kmax}")
    if init not in _INITS:
        raise ValueError(f"init must be one of {list(_INITS)}, got {init!r}")

    def fit(X, rng, seed):
        scores, best = {}, None
        for k in range(kmin, min(kmax, len(X) - 1) + 1):
            centers, labels, _ = _best_kmeans(X, k, init, "Euclidean", maxIterations, restarts, rng)
            score = _bic(X, labels, centers)
            scores[k] = score
            if best is None or score > best[0]:
                best = (score, centers)
        centers = best[1]
        return {"predict": lambda Z: _distances(Z, centers, "Euclidean").argmin(axis=1),
                "centers": centers, "scores": {"criterion": "BIC", "byK": scores}}
    return Clusterer("wekaXMeans", {"minClusters": kmin, "maxClusters": kmax, "restarts": restarts,
                                    "init": init, "maxIterations": maxIterations,
                                    "normalize": normalize}, fit)


def miniBatchKMeans(nClusters, batchSize=1024, maxIterations=100, normalize=False):
    """Creates a mini-batch k-means clusterer: a faster k-means for large training samples.

    Updates the centres from small random batches instead of all samples at
    once; results are close to kMeans at a fraction of the time.

    Parameters
    ----------
    nClusters : int
        Number of clusters.
    batchSize : int, default 1024
        Samples per batch.
    maxIterations : int, default 100
        Maximum passes over the training data.
    normalize : bool, default False
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.miniBatchKMeans(6).train(img, numPixels=50000)
    """
    k = _check_k(nClusters)

    def fit(X, rng, seed):
        from sklearn.cluster import MiniBatchKMeans
        model = MiniBatchKMeans(n_clusters=k, batch_size=batchSize, max_iter=maxIterations,
                                n_init=3, random_state=seed).fit(X)
        return {"predict": model.predict, "centers": model.cluster_centers_,
                "scores": {"SSE": float(model.inertia_)}}
    return Clusterer("miniBatchKMeans", {"nClusters": k, "batchSize": batchSize,
                                         "maxIterations": maxIterations, "normalize": normalize}, fit)


def bisectingKMeans(nClusters, normalize=False):
    """Creates a bisecting k-means clusterer.

    Starts with one cluster and repeatedly splits the largest one in two.
    Clusters tend to be more even in size than with plain k-means.

    Parameters
    ----------
    nClusters : int
        Number of clusters.
    normalize : bool, default False
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.bisectingKMeans(5).train(img, numPixels=5000)
    """
    k = _check_k(nClusters)

    def fit(X, rng, seed):
        from sklearn.cluster import BisectingKMeans
        model = BisectingKMeans(n_clusters=k, random_state=seed, bisecting_strategy="largest_cluster").fit(X)
        return {"predict": model.predict, "centers": model.cluster_centers_,
                "scores": {"SSE": float(model.inertia_)}}
    return Clusterer("bisectingKMeans", {"nClusters": k, "normalize": normalize}, fit)


def gaussianMixture(nClusters, covariance="full", normalize=False):
    """Creates a Gaussian mixture clusterer.

    Models the data as a mix of `nClusters` Gaussian (bell-shaped) groups,
    which can be stretched and tilted, so clusters don't have to be round.
    Each pixel or row goes to its most likely group.

    Parameters
    ----------
    nClusters : int
        Number of clusters (Gaussian components).
    covariance : str, default "full"
        Shape of each group: "full" (any shape), "diag" (axis-aligned),
        "spherical" (round) or "tied" (all the same shape).
    normalize : bool, default False
        Scale every input to 0-1 before clustering.

    Returns
    -------
    Clusterer

    Examples
    --------
    >>> trained = cluster.gaussianMixture(5).train(img, numPixels=5000)
    >>> cluster.gaussianMixture(4, covariance="diag")
    """
    k = _check_k(nClusters)
    if covariance not in ("full", "diag", "spherical", "tied"):
        raise ValueError(f"covariance must be 'full', 'diag', 'spherical' or 'tied', got {covariance!r}")

    def fit(X, rng, seed):
        from sklearn.mixture import GaussianMixture
        model = GaussianMixture(n_components=k, covariance_type=covariance, random_state=seed,
                                n_init=2).fit(X)
        return {"predict": model.predict, "centers": model.means_,
                "scores": {"BIC": float(model.bic(X))}}
    return Clusterer("gaussianMixture", {"nClusters": k, "covariance": covariance,
                                         "normalize": normalize}, fit)


# ---- short, clean tracebacks for every public entry point ----
clean_class(Clusterer)
clean_class(TrainedClusterer)
kMeans = clean_errors(kMeans)
wekaKMeans = clean_errors(wekaKMeans)
wekaCascadeKMeans = clean_errors(wekaCascadeKMeans)
wekaXMeans = clean_errors(wekaXMeans)
miniBatchKMeans = clean_errors(miniBatchKMeans)
bisectingKMeans = clean_errors(bisectingKMeans)
gaussianMixture = clean_errors(gaussianMixture)
