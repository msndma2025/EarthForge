def add(a, b):
    """Returns the sum of a and b.

    Works with anything that supports ``+``: numbers, NumPy arrays and Images.

    Parameters
    ----------
    a, b : number, numpy.ndarray or Image

    Returns
    -------
    The same type as the inputs: a + b.

    Examples
    --------
    >>> from pyearthforge import add
    >>> add(3, 4)
    7
    >>> add(img.select("B4").data, 100)      # NumPy array + number
    """
    return a + b
