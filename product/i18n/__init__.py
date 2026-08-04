#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Human display-layer i18n: the ``t()`` translation seam + locale runtime.

The public surface for localising the CLI's human-facing text (``cli/view`` +
``cli/consumers``). Import :func:`t` and the message-id constants from
:mod:`mote.product.i18n.keys` to render a string under the active locale::

    from mote.product.i18n import t
    from mote.product.i18n import keys as K
    t(K.SUMMARY_READ_LINES, count=1024)

Model-facing text is deliberately not localised by this Product presentation package.
"""

from __future__ import annotations

from mote.product.i18n.locale import Locale, available_locales, get_locale, negotiate
from mote.product.i18n.runtime import (
    BASE_LOCALE,
    DEFAULT_LOCALE,
    current_locale,
    locales,
    negotiate_and_set,
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
    "locales",
    "available_locales",
    "get_locale",
    "negotiate",
    "Locale",
    "DEFAULT_LOCALE",
    "BASE_LOCALE",
]
