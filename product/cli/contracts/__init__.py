#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""``mote.product.cli.contracts`` — the display framework's shared contract layer.

Cross-cutting contracts shared across the display framework's hosts (terminal /
Web / IM / machine): structural ``interface`` Protocols, subclassable ``base``
classes, and the human ``view`` union. The ``interface`` Protocols are leaf
modules importing only stdlib typing, so any host can depend on them without
risking an import cycle.
"""
