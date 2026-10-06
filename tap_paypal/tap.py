"""PayPal tap."""
# -*- coding: utf-8 -*-
import logging
from argparse import Namespace

from importlib.metadata import version
from singer import get_logger, utils
from singer.catalog import Catalog

from tap_paypal.discover import discover
from tap_paypal.paypal import PayPal
from tap_paypal.sync import sync

VERSION: str = version('tap-paypal')
LOGGER: logging.RootLogger = get_logger()
REQUIRED_CONFIG_KEYS: tuple = ('start_date', 'end_date', 'account_id', 'client_id', 'client_secret')


@utils.handle_top_exception(LOGGER)
def main() -> None:
    """Run tap."""
    # Parse command line arguments
    args: Namespace = utils.parse_args(REQUIRED_CONFIG_KEYS)

    LOGGER.info(f'>>> Running tap-paypal v{VERSION}')

    # If discover flag was passed, run discovery mode and dump output to stdout
    if args.discover:
        catalog: Catalog = discover()
        catalog.dump()
        return

    # Otherwise run in sync mode
    if args.catalog:
        # Load command line catalog
        catalog = args.catalog
    else:
        # Loadt the  catalog
        catalog = discover()

    # Initialize PayPal client
    paypal: PayPal = PayPal(
        args.config['client_id'],
        args.config['client_secret'],
        args.config.get('sandbox', False),
        account_id=args.config['account_id'],
        merchant_id=args.config.get('merchant_id'),
    )

    sync(paypal, args.state, catalog, args.config)


if __name__ == '__main__':
    main()
