#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Ship + register the bundled locale catalogs (zh + en).

Importing this package is the single side effect that populates the runtime
registry: each ``{msg-id: pattern}`` dict is merged in under its locale code.
Adding a locale = a new ``<code>.py`` module here + one ``register_catalog``
line below (data, not call-site code).
"""
from __future__ import annotations

from mote.product.i18n.catalog import en, zh
from mote.product.i18n.runtime import register_catalog

register_catalog("zh", zh.CATALOG)
register_catalog("en", en.CATALOG)

__all__ = ["zh", "en"]
