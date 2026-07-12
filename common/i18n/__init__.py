#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Human display-layer i18n: the ``t()`` translation seam + locale runtime.

The public surface for localising the CLI's human-facing text (``cli/view`` +
``cli/consumers``). Import :func:`t` and the message-id constants from
:mod:`mote.common.i18n.keys` to render a string under the active locale::

    from mote.common.i18n import t
    from mote.common.i18n import keys as K
    t(K.SUMMARY_READ_LINES, count=1024)

Importing this package registers the bundled zh + en catalogs as a side effect.
Model-facing text is deliberately NOT localised here — see ``common/text/*``.
"""
from __future__ import annotations

# Registers the bundled catalogs (zh + en) into the runtime as an import side effect.
from mote.common.i18n import catalog as _catalog  # noqa: E402,F401  (side-effecting)
from mote.common.i18n.locale import Locale, available_locales, get_locale, negotiate
from mote.common.i18n.runtime import (
    BASE_LOCALE,
    DEFAULT_LOCALE,
    current_locale,
    locales,
    negotiate_and_set,
    register_catalog,
    set_locale,
    t,
    use_locale,
)

__all__ = [
    "t",
    "set_locale",
    "use_locale",
    "current_locale",
    "negotiate_and_set",
    "register_catalog",
    "locales",
    "available_locales",
    "get_locale",
    "negotiate",
    "Locale",
    "DEFAULT_LOCALE",
    "BASE_LOCALE",
]
