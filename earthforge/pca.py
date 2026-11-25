# pca.py  -> principal component analysis for images and tables
#
# Usage:
#   from earthforge import raster, pca
#   img = raster.image("sampleImage1.tif").select("B2", "B3", "B4", "B8", "B11", "B12")
#   model = pca.PCA(3).train(img, numPixels=10000)
#   model.info()                       # explained variance per component
#   pcs = img.pca(model)               # lazy image: bands PC1, PC2, PC3
#   pcs.visualize(bands=["PC1", "PC2", "PC3"])
#   stations.pca(pca.PCA(2).train(stations, inputProperties=[...]))   # tables too

import numpy as np
import pandas as pd

from ._errors import clean_class
from .cluster import _training_matrix


class PCA:
    """Principal component analysis, ready to be trained.

    PCA turns many correlated bands (or columns) into a few uncorrelated
    "principal components". PC1 holds the most variation, PC2 the most of what
    is left, and so on, so the first few components usually summarize the data.

    Parameters
    ----------
    nComponents : int, optional
        Number of components to keep. Default: as many as there are inputs.
    standardize : bool, default False
        Scale every input to unit variance first (correlation PCA). Use it
        when inputs have very different units or ranges, e.g. NDVI and
        elevation; leave it off for bands of one sensor.

    Examples
    --------
    >>> model = pca.PCA(3).train(img, numPixels=10000)
    >>> pcs = img.pca(model)
    >>> pca.PCA(2, standardize=True).train(stations, inputProperties=["ndvi", "elevation_m", "temp_c"])
    """

    def __init__(self, nComponents=None, standardize=False):
        if nComponents is not None and (not isinstance(nComponents, (int, np.integer))
                                        or isinstance(nComponents, bool) or nComponents < 1):
            raise ValueError(f"nComponents must be a whole number of 1 or more, got {nComponents!r}")
        self._k = nComponents
        self._standardize = bool(standardize)

    def train(self, data, inputProperties=None, numPixels=10000, seed=0, region=None):
        """Learns the principal components from an image sample or a dataset's columns.

        Parameters
        ----------
        data : Image or Vector
            An Image: `numPixels` random pixels are sampled (reading only what
            is needed) and all its bands are used. A Vector: its numeric
            columns are used.
        inputProperties : list of str, optional
            The bands / columns to use. Default: all bands, or all numeric columns.
        numPixels : int, default 10000
            Pixels to sample when `data` is an Image.
        seed : int, default 0
            Random seed for the sample.
        region : vector.Vector, optional
            Learn only from pixels (or rows) inside these shapes, e.g. your
            study area. The components can then be applied to the whole image.

        Returns
        -------
        TrainedPCA

        Raises
        ------
        ValueError
            If there are fewer valid samples than inputs, or nComponents is
            larger than the number of inputs.

        Examples
        --------
        >>> model = pca.PCA(3).train(img.select("B2", "B3", "B4", "B8", "B11", "B12"))
        >>> model = pca.PCA().train(stations, inputProperties=["ndvi", "elevation_m", "rain_mm", "temp_c"])
        >>> model = pca.PCA(3).train(img, region=vector.Open("boundary.shp"))      # only inside the boundary
        """
        X, names = _training_matrix(data, inputProperties, numPixels, seed, region)
        p = X.shape[1]
        k = p if self._k is None else self._k
        if k > p:
            raise ValueError(f"nComponents={k} is more than the {p} inputs {names}.")
        if len(X) <= p:
            raise ValueError(f"Only {len(X)} valid samples for {p} inputs; increase numPixels.")
        mean = X.mean(axis=0)
        scale = X.std(axis=0, ddof=1) if self._standardize else np.ones(p)
        scale[scale == 0] = 1.0
        Z = (X - mean) / scale
        values, vectors = np.linalg.eigh(np.cov(Z, rowvar=False))
        order = np.argsort(values)[::-1]
        values, vectors = values[order].clip(min=0), vectors[:, order]
        for j in range(p):                                  # fixed signs: largest loading positive
            if vectors[np.abs(vectors[:, j]).argmax(), j] < 0:
                vectors[:, j] *= -1
        return TrainedPCA(names, mean, scale, vectors[:, :k], values, self._standardize, len(X))

    def __repr__(self):
        return f"PCA(nComponents={self._k}, standardize={self._standardize}), not trained yet"


class TrainedPCA:
    """A trained PCA: apply it with ``img.pca(model)`` or ``vector.pca(model)``.

    Examples
    --------
    >>> model = pca.PCA(3).train(img)
    >>> model.info()
    >>> pcs = img.pca(model)
    """

    def __init__(self, names, mean, scale, components, values, standardize, n):
        self._names = names
        self._mean = mean
        self._scale = scale
        self._components = components                  # (inputs, k)
        self._values = values                          # all eigenvalues
        self._standardize = standardize
        self._n = n

    @property
    def nComponents(self):
        """The number of components produced.

        Examples
        --------
        >>> pca.PCA(3).train(img).nComponents
        3
        """
        return int(self._components.shape[1])

    def schema(self):
        """Returns the names of the bands / columns the PCA was trained on.

        Examples
        --------
        >>> model.schema()
        ['B2', 'B3', 'B4', 'B8', 'B11', 'B12']
        """
        return list(self._names)

    def componentNames(self):
        """Returns the names of the output bands / columns: PC1, PC2, ...

        Examples
        --------
        >>> model.componentNames()
        ['PC1', 'PC2', 'PC3']
        """
        return [f"PC{i + 1}" for i in range(self.nComponents)]

    def explainedVariance(self):
        """Returns the share of the total variance each kept component explains (0-1).

        Examples
        --------
        >>> model.explainedVariance()
        [0.912, 0.061, 0.018]
        >>> sum(model.explainedVariance())          # share kept by all of them
        """
        total = self._values.sum()
        ratios = self._values[:self.nComponents] / total if total > 0 else np.zeros(self.nComponents)
        return [round(float(r), 6) for r in ratios]

    def loadings(self):
        """Returns how much each input contributes to each component, as a table.

        Rows are inputs, columns are components. Large positive or negative
        values mean the input drives that component.

        Examples
        --------
        >>> model.loadings()
                 PC1     PC2     PC3
        B2     0.21   -0.44    0.10
        ...
        """
        return pd.DataFrame(self._components, index=self._names, columns=self.componentNames())

    def info(self):
        """Prints the explained variance per component and the loadings.

        Returns
        -------
        dict
            inputs, nComponents, standardize, trainingSamples, explainedVariance.

        Examples
        --------
        >>> model.info()
        """
        ratios = self.explainedVariance()
        m = {"inputs": self.schema(), "nComponents": self.nComponents, "standardize": self._standardize,
             "trainingSamples": self._n, "explainedVariance": ratios}
        print("=" * 56)
        print(f" PCA of      : {self._names}")
        print(f" Samples     : {self._n}   standardized: {self._standardize}")
        cumulative = 0.0
        for name, r in zip(self.componentNames(), ratios):
            cumulative += r
            print(f"   {name:<5} {r * 100:6.2f} %   cumulative {cumulative * 100:6.2f} %")
        with pd.option_context("display.width", 120, "display.precision", 3):
            print(" Loadings:")
            print("   " + self.loadings().to_string().replace("\n", "\n   "))
        print("=" * 56)
        return m

    def _apply(self, X):
        """(rows, inputs) -> (rows, k) float32 component scores."""
        return (((X - self._mean) / self._scale) @ self._components).astype("float32")

    def __repr__(self):
        return f"TrainedPCA: {self.nComponents} components from {self._names}"


clean_class(PCA)
clean_class(TrainedPCA)
