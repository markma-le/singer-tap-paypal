"""Sync data."""
# -*- coding: utf-8 -*-
import logging
from datetime import datetime, timezone

import singer
from singer.catalog import Catalog, CatalogEntry

from tap_paypal import tools
from tap_paypal.paypal import PayPal

LOGGER: logging.RootLogger = singer.get_logger()


def sync(
    paypal: PayPal,
    state: dict,
    catalog: Catalog,
    config: dict,
) -> None:
    """Sync data from tap source.

    Arguments:
        paypal {PayPal} -- PayPal client
        state {dict} -- Tap state
        catalog {Catalog} -- Stream catalog
    """
    # For every stream in the catalog
    LOGGER.info('Sync')
    LOGGER.debug('Current state:\n{state}')

    # Only selected streams are synced, whether a stream is selected is
    # determined by whether the key-value: "selected": true is in the schema
    # file.
    for stream in catalog.get_selected_streams(state):
        LOGGER.info(f'Syncing stream: {stream.tap_stream_id}')

        # Update the current stream as active syncing in the state
        singer.set_currently_syncing(state, stream.tap_stream_id)

        # Write the schema
        singer.write_schema(
            stream_name=stream.tap_stream_id,
            schema=stream.schema.to_dict(),
            key_properties=stream.key_properties,
        )

        identities = set()
        for row in paypal.transactions(
            start_date=config['start_date'],
            end_date=config['end_date'],
        ):
            if row['event_id'] in identities:
                raise ValueError('Duplicate PayPal event identity; investigate source grain')
            identities.add(row['event_id'])
            sync_record(stream, row, state)
        singer.write_bookmark(state, stream.tap_stream_id, 'completed_start_date', config['start_date'])
        singer.write_bookmark(state, stream.tap_stream_id, 'completed_end_date', config['end_date'])
        tools.clear_currently_syncing(state)
        singer.write_state(state)


def sync_record(stream: CatalogEntry, row: dict, state: dict) -> None:
    """Sync the record.

    Arguments:
        stream {CatalogEntry} -- Stream catalog
        row {dict} -- Record
        state {dict} -- State
    """
    # Write a row to the stream
    singer.write_record(
        stream.tap_stream_id,
        row,
        time_extracted=datetime.now(timezone.utc),
    )
