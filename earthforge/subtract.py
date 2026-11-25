def subtract(a, b):
    """Returns a minus b.

    Works with anything that supports ``-``: numbers, NumPy arrays and Images.

    Parameters
    ----------
    a, b : number, numpy.ndarray or Image

    Returns
    -------
    The same type as the inputs: a - b.

    Examples
    --------
    >>> from earthforge import subtract
    >>> subtract(10, 5)
    5
    >>> diff = subtract(img.select("B8").data, img.select("B4").data)   # NIR - Red as arrays
    """
    return a - b
