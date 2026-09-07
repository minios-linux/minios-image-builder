#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared UI utilities for MiniOS Image Builder.

Provides application-specific presentation helpers not supplied by
``minios_gui``.

Copyright (C) 2026 MiniOS Linux
Author: crims0n <crims0n@minios.dev>
"""

def human_size(num_bytes):
    """Return a human readable representation of a size in bytes."""
    try:
        size = float(num_bytes)
    except (TypeError, ValueError):
        return ''
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if size < 1024.0 or unit == 'TiB':
            if unit == 'B':
                return '%d %s' % (int(size), unit)
            return '%.1f %s' % (size, unit)
        size /= 1024.0
    return '%.1f TiB' % size
