#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Base-class decorator logging: :class:`LoggedMixin` (and :func:`log_class`).

Subclassing :class:`LoggedMixin` auto-wraps the subclass's own public methods
with :func:`~mote.common.logs.decorator.log_call`. It uses
``__init_subclass__`` (not a metaclass) so it composes cleanly with the
``Singleton`` (ABCMeta) metaclass and pydantic's ``ModelMetaclass`` without a
metaclass conflict. Only methods defined directly on the subclass are wrapped;
private/dunder, descriptors (property/staticmethod/classmethod), generator
functions, already-wrapped and ``@no_log`` methods are skipped.
"""

from __future__ import annotations

import inspect
from typing import Iterable, Optional

from mote.common.logs.decorator import _LOGGED_MARKER, log_call

_NO_LOG_MARKER = "_no_log"


def no_log(func):
    """Mark a method so :class:`LoggedMixin` / :func:`log_class` skips it."""
    setattr(func, _NO_LOG_MARKER, True)
    return func


def _should_wrap(name: str, attr) -> bool:
    if name.startswith("_"):
        return False
    if not inspect.isfunction(attr):
        # Skips property / staticmethod / classmethod descriptors.
        return False
    if inspect.isgeneratorfunction(attr) or inspect.isasyncgenfunction(attr):
        return False
    if getattr(attr, _LOGGED_MARKER, False):
        return False
    if getattr(attr, _NO_LOG_MARKER, False):
        return False
    return True


def _wrap_class_methods(
    cls,
    *,
    level: str,
    exclude: Optional[Iterable[str]],
    log_args: bool,
    log_result: bool,
) -> None:
    names = set(exclude or ())
    names |= set(getattr(cls, "__log_exclude__", ()) or ())
    # Only the class's own dict — never re-wrap inherited (already wrapped) methods.
    for name, attr in list(cls.__dict__.items()):
        if name in names or not _should_wrap(name, attr):
            continue
        setattr(cls, name, log_call(attr, level=level, log_args=log_args, log_result=log_result))


class LoggedMixin:
    """Mixin that auto-logs a subclass's own public methods.

    Class-level knobs (override on the subclass)::

        class MyRole(LoggedMixin):
            __log_level__ = "DEBUG"
            __log_exclude__ = {"hot_loop"}
            __log_args__ = True
            __log_result__ = True
    """

    __log_level__: str = "INFO"
    __log_exclude__: set = set()
    __log_args__: bool = True
    __log_result__: bool = True

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        _wrap_class_methods(
            cls,
            level=getattr(cls, "__log_level__", "INFO"),
            exclude=getattr(cls, "__log_exclude__", set()),
            log_args=getattr(cls, "__log_args__", True),
            log_result=getattr(cls, "__log_result__", True),
        )


def log_class(
    _cls=None,
    *,
    level: str = "INFO",
    exclude: Optional[Iterable[str]] = None,
    log_args: bool = True,
    log_result: bool = True,
):
    """Class decorator equivalent of :class:`LoggedMixin`.

    Use when changing the inheritance chain is inconvenient::

        @log_class(level="DEBUG", exclude={"hot_loop"})
        class MyService: ...
    """

    def deco(cls):
        _wrap_class_methods(cls, level=level, exclude=exclude, log_args=log_args, log_result=log_result)
        return cls

    return deco(_cls) if _cls else deco
