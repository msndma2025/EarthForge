# _errors.py  -> short, clean error messages for the whole package (internal module)
#
# Errors raised by the library's own code are re-raised without the library's
# internal frames. In Jupyter / IPython only the user's code line and the
# message are shown. The errors keep their normal classes (ValueError, ...).
# Set `pyearthforge.FULL_TRACEBACKS = True` to see the internal frames when debugging.

import functools
import os
import sys
from pathlib import Path

_PACKAGE_DIR = os.path.normcase(os.path.abspath(os.path.dirname(__file__)))
_PACKAGE = __name__.rpartition(".")[0]


class LibraryError(Exception):
    """Base class for the library's own error types."""


def _in_package(filename):
    return os.path.normcase(os.path.abspath(os.path.dirname(filename))) == _PACKAGE_DIR


def _full_tracebacks():
    return bool(getattr(sys.modules.get(_PACKAGE), "FULL_TRACEBACKS", False))


def _raised_by_library(error):
    """True if the error was raised by this package's code (not numpy, rasterio, ...)."""
    tb = error.__traceback__
    if tb is None:
        return True
    while tb.tb_next is not None:
        tb = tb.tb_next
    return _in_package(tb.tb_frame.f_code.co_filename)


def _frame_label(filename, lineno):
    """'Cell In[3], line 2' for notebook cells, 'File path, line N' otherwise."""
    try:
        from IPython import get_ipython
        shell = get_ipython()
        count = shell.compile._filename_map.get(filename) if shell is not None else None
        if count is not None:
            return f"Cell In[{count}], line {lineno}"
    except Exception:
        pass
    return f"File {filename}, line {lineno}"


class _ShortTraceback:
    """Mixin for library errors: Jupyter/IPython shows only the user's code and the message.

    IPython calls `_render_traceback_()` instead of printing the full traceback.
    """

    def _render_traceback_(self):
        import traceback
        red, reset = "\x1b[31m", "\x1b[39m"
        name = type(self).__name__
        lines = ["-" * 75, f"{red}{name}{reset}" + " " * max(1, 42 - len(name))
                 + "Traceback (most recent call last)"]
        for frame in traceback.extract_tb(self.__traceback__):
            if _in_package(frame.filename):
                continue                                    # library internals
            if {"IPython", "ipykernel"} & set(Path(frame.filename).parts):
                continue                                    # notebook machinery
            lines.append(_frame_label(frame.filename, frame.lineno))
            if frame.line:
                lines.append(f"----> {frame.line}")
        lines += ["", f"{red}{name}{reset}: {self}"]
        return lines


_SHORT_TYPES = {}


def _short_type(cls):
    """A subclass of `cls` with the same name that has a short Jupyter traceback."""
    if cls not in _SHORT_TYPES:
        _SHORT_TYPES[cls] = type(cls.__name__, (_ShortTraceback, cls),
                                 {"__module__": cls.__module__, "__qualname__": cls.__qualname__})
    return _SHORT_TYPES[cls]


_CLEANED = (ValueError, TypeError, FileNotFoundError, LibraryError)


def clean_errors(func):
    """Decorator: errors raised by the library show only the user's line and the message."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        __tracebackhide__ = True  # noqa: F841  (read by pytest)
        try:
            return func(*args, **kwargs)
        except _CLEANED as error:
            if _full_tracebacks() or not _raised_by_library(error):
                raise
            if not isinstance(error, _ShortTraceback):
                error = _short_type(type(error))(*error.args)
            raise error.with_traceback(None) from None
    return wrapper


_OPERATOR_METHODS = {
    "__init__", "__bool__", "__neg__", "__abs__", "__invert__", "__len__",
    "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
    "__truediv__", "__rtruediv__", "__pow__", "__rpow__",
    "__lt__", "__le__", "__gt__", "__ge__", "__and__", "__rand__", "__or__", "__ror__",
}


def clean_class(cls):
    """Apply clean_errors to every public method (and operators) of a class."""
    for name, attr in list(vars(cls).items()):
        if name.startswith("_") and name not in _OPERATOR_METHODS:
            continue
        if isinstance(attr, staticmethod):                  # keep static methods static
            setattr(cls, name, staticmethod(clean_errors(attr.__func__)))
        elif isinstance(attr, classmethod):
            setattr(cls, name, classmethod(clean_errors(attr.__func__)))
        elif callable(attr):
            setattr(cls, name, clean_errors(attr))
    return cls
