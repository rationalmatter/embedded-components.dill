#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Author: Leonardo Gama (@leogama)
# Copyright (c) 2022 The Uncertainty Quantification Foundation.
# License: 3-clause BSD.  The full license text is available at:
#  - https://github.com/uqfoundation/dill/blob/master/LICENSE
"""
Logging utilities for dill.

The 'logger' object is dill's top-level logger.

The 'adapter' object wraps the logger and implements a 'trace()' method that
generates a detailed tree-style trace for the pickling call at log level
:const:`dill.logging.TRACE`, which has an intermediary value between
:const:`logging.INFO` and :const:`logging.DEGUB`.

The 'trace()' function sets and resets dill's logger log level, enabling and
disabling the pickling trace.

The trace shows a tree structure depicting the depth of each object serialized
*with dill save functions*, but not the ones that use save functions from
``pickle._Pickler.dispatch``. If the information is available, it also displays
the size in bytes that the object contributed to the pickle stream (including
its child objects).  Sample trace output:

    >>> import dill
    >>> import keyword
    >>> with dill.detect.trace():
    ...     dill.dump_module(module=keyword)
    ┬ M1: <module 'keyword' from '/usr/lib/python3.8/keyword.py'>
    ├┬ F2: <function _import_module at 0x7f4a6087b0d0>
    │└ # F2 [32 B]
    ├┬ D5: <dict object at 0x7f4a60669940>
    │├┬ T4: <class '_frozen_importlib.ModuleSpec'>
    ││└ # T4 [35 B]
    │├┬ D2: <dict object at 0x7f4a62e699c0>
    ││├┬ T4: <class '_frozen_importlib_external.SourceFileLoader'>
    │││└ # T4 [50 B]
    ││├┬ D2: <dict object at 0x7f4a62e5f280>
    │││└ # D2 [47 B]
    ││└ # D2 [280 B]
    │└ # D5 [1 KiB]
    └ # M1 [1 KiB]
"""

from __future__ import annotations

__all__ = [
    'adapter', 'logger', 'trace', 'getLogger',
    'CRITICAL', 'ERROR', 'WARNING', 'INFO', 'TRACE', 'DEBUG', 'NOTSET',
]

import codecs
import contextlib
import locale
import logging
import math
import os
from contextlib import suppress
from logging import getLogger, CRITICAL, ERROR, WARNING, INFO, DEBUG, NOTSET
from functools import partial
from typing import Optional, TextIO, Union

import dill
from ._utils import _format_bytes_size

# Intermediary logging level for tracing.
TRACE = (INFO + DEBUG) // 2

_nameOrBoolToLevel = logging._nameToLevel.copy()
_nameOrBoolToLevel['TRACE'] = TRACE
_nameOrBoolToLevel[False] = WARNING
_nameOrBoolToLevel[True] = TRACE

# Tree drawing characters: Unicode to ASCII map.
ASCII_MAP = str.maketrans({"│": "|", "├": "|", "┬": "+", "└": "`"})

## Notes about the design choices ##

# Here is some domumentation of the Standard Library's logging internals that
# can't be found completely in the official documentation.  dill's logger is
# obtained by calling logging.getLogger('dill') and therefore is an instance of
# logging.getLoggerClass() at the call time.  As this is controlled by the user,
# in order to add some functionality to it it's necessary to use a LoggerAdapter
# to wrap it, overriding some of the adapter's methods and creating new ones.
#
# Basic calling sequence
# ======================
#
# Python's logging functionality can be conceptually divided into five steps:
#   0. Check logging level -> abort if call level is greater than logger level
#   1. Gather information -> construct a LogRecord from passed arguments and context
#   2. Filter (optional) -> discard message if the record matches a filter
#   3. Format -> format message with args, then format output string with message plus record
#   4. Handle -> write the formatted string to output as defined in the handler
#
# dill.logging.logger.log ->        # or logger.info, etc.
#   Logger.log ->               \
#     Logger._log ->             }- accept 'extra' parameter for custom record entries
#       Logger.makeRecord ->    /
#         LogRecord.__init__
#       Logger.handle ->
#         Logger.callHandlers ->
#           Handler.handle ->
#             Filterer.filter ->
#               Filter.filter
#             StreamHandler.emit ->
#               Handler.format ->
#                 Formatter.format ->
#                   LogRecord.getMessage        # does: record.message = msg % args
#                   Formatter.formatMessage ->
#                     PercentStyle.format       # does: self._fmt % vars(record)
#
# NOTE: All methods from the second line on are from logging.__init__.py

class TraceAdapter(logging.LoggerAdapter):
    """
    Tracks object tree depth and calculates pickled object size.

    A single instance of this wraps the module's logger, as the logging API
    doesn't allow setting it directly with a custom Logger subclass.  The added
    'trace()' method receives a pickle instance as the first argument and
    creates extra values to be added in the LogRecord from it, then calls
    'info()'.

    Examples:

    In the first call to `trace()`, before pickling an object, it must be passed
    to `trace()` as the last positional argument or as the keyword argument
    `obj`.  Note how, in the second example, the object is not passed as a
    positional argument, and therefore won't be substituted in the message:

        >>> from dill.logger import adapter as logger  #NOTE: not dill.logger.logger
        >>> ...
        >>> def save_atype(pickler, obj):
        >>>     logger.trace(pickler, "X: Message with %s and %r placeholders", 'text', obj)
        >>>     ...
        >>>     logger.trace(pickler, "# X")
        >>> def save_weakproxy(pickler, obj)
        >>>     trace_message = "W: This works even with a broken weakproxy: %r" % obj
        >>>     logger.trace(pickler, trace_message, obj=obj)
        >>>     ...
        >>>     logger.trace(pickler, "# W")
    """
    def __init__(self, logger):
        self.logger = logger
    def addHandler(self, handler):
        formatter = TraceFormatter("%(prefix)s%(message)s%(suffix)s", handler=handler)
        handler.setFormatter(formatter)
        self.logger.addHandler(handler)
    def removeHandler(self, handler):
        self.logger.removeHandler(handler)
    def process(self, msg, kwargs):
        # A no-op override, as we don't have self.extra.
        return msg, kwargs
    def trace_setup(self, pickler):
        # Called by Pickler.dump().
        if not dill._dill.is_dill(pickler, child=False):
            return
        elif self.isEnabledFor(TRACE):
            pickler._trace_stack = []
            pickler._size_stack = []
        else:
            pickler._trace_stack = None
    def trace(self, pickler, msg, *args, obj=None, **kwargs):
        if not hasattr(pickler, '_trace_stack'):
            logger.info(msg, *args, **kwargs)
            return
        elif pickler._trace_stack is None:
            return
        extra = kwargs.get('extra', {})
        pushed_obj = msg.startswith('#')
        if not pushed_obj:
            if obj is None and (not args or type(args[-1]) is str):
                raise TypeError(
                    "the pickled object must be passed as the last positional "
                    "argument (being substituted in the message) or as the "
                    "'obj' keyword argument."
                )
            if obj is None:
                obj = args[-1]
            pickler._trace_stack.append(id(obj))
        size = None
        with suppress(AttributeError, TypeError):
            # Streams are not required to be tellable.
            size = pickler._file_tell()
            frame = pickler.framer.current_frame
            try:
                size += frame.tell()
            except AttributeError:
                # PyPy may use a BytesBuilder as frame
                size += len(frame)
        if size is not None:
            if not pushed_obj:
                pickler._size_stack.append(size)
                if len(pickler._size_stack) == 3:  # module > dict > variable
                    with suppress(AttributeError, KeyError):
                        extra['varname'] = pickler._id_to_name.pop(id(obj))
            else:
                size -= pickler._size_stack.pop()
                extra['size'] = size
        extra['depth'] = len(pickler._trace_stack)
        kwargs['extra'] = extra
        self.info(msg, *args, **kwargs)
        if pushed_obj:
            pickler._trace_stack.pop()
    def roll_back(self, pickler, obj):
        if pickler._trace_stack and id(obj) == pickler._trace_stack[-1]:
            pickler._trace_stack.pop()
            pickler._size_stack.pop()

class TraceFormatter(logging.Formatter):
    """
    Generates message prefix and suffix from record.

    This Formatter adds prefix and suffix strings to the log message in trace
    mode (an also provides empty string defaults for normal logs).
    """
    def __init__(self, *args, handler=None, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            encoding = handler.stream.encoding
            if encoding is None:
                raise AttributeError
        except AttributeError:
            encoding = locale.getpreferredencoding()
        try:
            encoding = codecs.lookup(encoding).name
        except LookupError:
            self.is_utf8 = False
        else:
            self.is_utf8 = (encoding == codecs.lookup('utf-8').name)
    def format(self, record):
        fields = {'prefix': "", 'suffix': ""}
        if getattr(record, 'depth', 0) > 0:
            if record.msg.startswith("#"):
                prefix = (record.depth - 1)*"│" + "└"
            elif record.depth == 1:
                prefix = "┬"
            else:
                prefix = (record.depth - 2)*"│" + "├┬"
            if not self.is_utf8:
                prefix = prefix.translate(ASCII_MAP) + "-"
            fields['prefix'] = prefix + " "
        if hasattr(record, 'varname'):
            fields['suffix'] = " as %r" % record.varname
        elif hasattr(record, 'size'):
            fields['suffix'] = " [%d %s]" % _format_bytes_size(record.size)
        vars(record).update(fields)
        return super().format(record)

logger = getLogger('dill')
logger.propagate = False
adapter = TraceAdapter(logger)
stderr_handler = logging._StderrHandler()
adapter.addHandler(stderr_handler)

def trace(
        arg: Union[bool, str, TextIO, os.PathLike] = None, *, mode: str = 'a'
    ) -> Optional[TraceManager]:
    """print a trace through the stack when pickling; useful for debugging

    With a single boolean argument, enable or disable the tracing. Or, with a
    logging level name (not ``int``), set the logging level of the dill logger.

    Example usage:

        >>> import dill
        >>> dill.detect.trace(True)
        >>> dill.dump_session()

    Alternatively, ``trace()`` can be used as a context manager. With no
    arguments, it just takes care of restoring the tracing state on exit.
    Either a file handle, or a file name and a file mode (optional) may be
    specified to redirect the tracing output in the ``with`` block.  A ``log()``
    function is yielded by the manager so the user can write extra information
    to the file.

    Example usage:

        >>> from dill import detect
        >>> D = {'a': 42, 'b': {'x': None}}
        >>> with detect.trace():
        >>>     dumps(D)
        ┬ D2: <dict object at 0x7f2721804800>
        ├┬ D2: <dict object at 0x7f27217f5c40>
        │└ # D2 [8 B]
        └ # D2 [22 B]
        >>> squared = lambda x: x**2
        >>> with detect.trace('output.txt', mode='w') as log:
        >>>     log("> D = %r", D)
        >>>     dumps(D)
        >>>     log("> squared = %r", squared)
        >>>     dumps(squared)

    Parameters:
        arg: a boolean value, the name of a logging level (including "TRACE")
            or an optional file-like or path-like object for the context manager
        mode: mode string for ``open()`` if a file name is passed as the first
            argument
    """
    level = _nameOrBoolToLevel.get(arg) if isinstance(arg, (bool, str)) else None
    if level is not None:
        logger.setLevel(level)
        return
    else:
        return TraceManager(file=arg, mode=mode)

class TraceManager(contextlib.AbstractContextManager):
    """context manager version of trace(); can redirect the trace to a file"""
    def __init__(self, file, mode):
        self.file = file
        self.mode = mode
        self.redirect = file is not None
        self.file_is_stream = hasattr(file, 'write')
    def __enter__(self):
        if self.redirect:
            stderr_handler.flush()
            if self.file_is_stream:
                self.handler = logging.StreamHandler(self.file)
            else:
                self.handler = logging.FileHandler(self.file, self.mode)
            adapter.removeHandler(stderr_handler)
            adapter.addHandler(self.handler)
        self.old_level = adapter.getEffectiveLevel()
        adapter.setLevel(TRACE)
        return adapter.info
    def __exit__(self, *exc_info):
        adapter.setLevel(self.old_level)
        if self.redirect:
            adapter.removeHandler(self.handler)
            adapter.addHandler(stderr_handler)
            if not self.file_is_stream:
                self.handler.close()