"""Streams metadata."""
# -*- coding: utf-8 -*-
from types import MappingProxyType

# Streams metadata
STREAMS: MappingProxyType = MappingProxyType({
    'transactions': {
        'key_properties': ['event_id'],
        'replication_method': 'FULL_TABLE',
        'replication_key': None,
        'bookmark': 'start_date',
    },
})
