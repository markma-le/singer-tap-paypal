"""PayPal API Client."""  # noqa: WPS226
# -*- coding: utf-8 -*-

import logging
import time
from datetime import datetime, timedelta, timezone
from types import MappingProxyType
from typing import Generator, Optional

import httpx
import singer
from dateutil.rrule import WEEKLY, rrule

from tap_paypal.ledger import timestamp, transaction_record

# Todo: Sadbox
API_SCHEME: str = 'https://'
API_BASE_URL: str = 'api-m.paypal.com'
API_BASE_URL_SANDBOX: str = 'api-m.sandbox.paypal.com'
API_VERSION: str = 'v1'

API_PATH_OAUTH: str = 'oauth2/token'
API_PATH_TRANSACTIONS: str = 'reporting/transactions'

HEADERS: MappingProxyType = MappingProxyType({  # Frozen dictionary
    'Content-Type': 'application/json',
    'Authorization': 'Bearer :accesstoken:',
})


class PayPal(object):  # noqa: WPS230
    """PayPal API Client."""

    def __init__(
        self,
        client_id: str,
        secret: str,
        sandbox=False,
        account_id=None,
        merchant_id=None,
    ) -> None:
        """Initialize client.

        Arguments:
            client_id {str} -- PayPal client id
            secret {str} -- PayPal secret
            sandbox {bool} -- Whether to use the sandbox or live environment
        """
        self.client_id: str = client_id
        self.secret: str = secret
        self.sandbox: bool = sandbox
        self.account_id = account_id
        self.merchant_id = merchant_id
        self.http = httpx.Client(http2=True, timeout=60)
        self.base: str = API_BASE_URL_SANDBOX if sandbox else API_BASE_URL
        self.logger: logging.Logger = singer.get_logger()

        self.token: Optional[str] = None
        self.token_expires: Optional[datetime] = None

        if sandbox:
            self.logger.info('Running in Sandbox environment')
        else:
            self.logger.info('Running in Live environment')

        # Perform authentication during initialising
        self._authenticate()

    def transactions(  # noqa: WPS210, WPS213
        self,
        **kwargs: dict,
    ) -> Generator[dict, None, None]:
        """Paypal transaction history.

        Raises:
            ValueError: When the parameter start_date is missing

        Yields:
            Generator[dict] -- Yields PayPal transactions
        """
        self.logger.info('Stream PayPal transactions')

        # Validate the start_date value exists
        start_date_input: str = str(kwargs.get('start_date', ''))

        if not start_date_input:
            raise ValueError('The parameter start_date is required.')

        # Set start date and end date
        start_date: datetime = timestamp(start_date_input, whole_seconds=True)
        end_date: datetime = timestamp(kwargs.pop('end_date'), whole_seconds=True)
        if start_date >= end_date:
            raise ValueError('end_date must be later than start_date')

        self.logger.info(
            f'Retrieving transactions from {start_date} to {end_date}',
        )
        # Extra kwargs will be converted to parameters in the API requests
        # start_date is parsed into batches, thus we remove it from the kwargs
        kwargs.pop('start_date', None)

        # The difference between start_date and end_date can max be 31 days
        # Split up the requests into weekly batches
        batches: rrule = rrule(
            WEEKLY,
            dtstart=start_date,
            until=end_date - timedelta(seconds=1),
        )

        total_batches: int = len(list(batches))
        self.logger.info(f'Total weekly batches: {total_batches}')

        current_batch: int = 0

        # Batches contain all start_dates, the end_date is 6 days 23:59 later
        # E.g. 2021-01-01T00:00:00+0000 <--> 2021-01-07T23:59:59+0000
        for start_date_batch in batches:
            # Determine the end_date
            end_date_batch: datetime = (
                start_date_batch + timedelta(days=7, seconds=-1)
            )

            # Prevent the end_date from going into the future
            if end_date_batch >= end_date:
                end_date_batch = end_date - timedelta(seconds=1)

            # Convert the datetimes to datetime formats the api expects
            start_date_str: str = self._date_to_paypal_format(start_date_batch)
            end_date_str: str = self._date_to_paypal_format(end_date_batch)

            current_batch += 1

            self.logger.info(
                f'Parsing batch {current_batch}: {start_date_str} <--> '
                f'{end_date_str}',
            )

            # Default initial parameters send with each request
            fixed_params: dict = {
                'fields': 'transaction_info',
                'page_size': 500,
                'page': 1,  # Is updated in further requests
                'start_date': start_date_str,
                'end_date': end_date_str,
            }
            # Kwargs can be used to add aditional parameters to each requests
            http_params: dict = {**fixed_params, **kwargs}

            # Start of pagination
            page: int = 0
            total_pages: int = 1
            expected_total = None
            seen = 0
            url: str = (
                f'{API_SCHEME}{self.base}/'
                f'{API_VERSION}/{API_PATH_TRANSACTIONS}'
            )

            # Request more pages if there are available
            while page < total_pages:
                # Update current page
                page += 1
                http_params['page'] = page

                # Request data from the API
                response: httpx.Response = self._request(  # noqa: WPS437
                    url,
                    headers=self.headers,
                    params=http_params,
                )

                # Raise error on 4xx and 5xxx
                response.raise_for_status()

                response_data: dict = response.json()

                # Retrieve the current page details
                if int(response_data['page']) != page:
                    raise ValueError('Unexpected transaction page')
                merchant_id = response_data['account_number']
                if self.merchant_id is not None and merchant_id != self.merchant_id:
                    raise ValueError('Reporting response belongs to a different merchant account')
                self.merchant_id = merchant_id
                if timestamp(response_data['last_refreshed_datetime']) < end_date_batch:
                    raise ValueError('Transactions have not refreshed through requested end time')
                total = int(response_data['total_items'])
                if expected_total is not None and total != expected_total:
                    raise ValueError('Transaction count changed during pagination; rerun')
                expected_total = total
                total_pages = int(response_data['total_pages'])

                percentage_page: float = round((page / max(total_pages, 1)) * 100, 2)
                percentage_batch: float = round(
                    (current_batch / total_batches) * 100, 2,
                )

                self.logger.info(
                    f'Batch: {current_batch} of '  # noqa: WPS221
                    f'{total_batches} '
                    f'({percentage_batch}%), '
                    f'page: {page} of '
                    f'{total_pages} '
                    f'({percentage_page}%)',
                )

                # Yield every transaction in the response
                transactions: list = response_data.get(
                    'transaction_details',
                    [],
                )
                seen += len(transactions)
                yield from (
                    transaction_record(self.account_id, merchant_id, transaction)
                    for transaction in transactions
                )
            if seen != expected_total:
                raise ValueError('Transaction pages are incomplete')
        self.logger.info('Finished: paypal_transactions')

    def _request(self, url, **kwargs):
        for attempt in range(5):
            if datetime.now(timezone.utc) >= self.token_expires:
                self._authenticate()
                kwargs['headers'] = self.headers
            try:
                response = self.http.get(url, **kwargs)
                if response.status_code == 401 and attempt < 4:
                    self._authenticate()
                    kwargs['headers'] = self.headers
                    continue
                if (response.status_code == 429 or response.status_code >= 500) and attempt < 4:
                    time.sleep(2 ** attempt)
                    continue
                response.raise_for_status()
                return response
            except httpx.RequestError:
                if attempt == 4:
                    raise RuntimeError('PayPal network request failed') from None
                time.sleep(2 ** attempt)

    def _authenticate(self) -> None:  # noqa: WPS210
        """Generate a bearer access token."""
        url: str = (
            f'{API_SCHEME}{self.base}/'
            f'{API_VERSION}/{API_PATH_OAUTH}'
        )
        headers: dict = {
            'Accept': 'application/json',
            'Accept-Language': 'en_US',
        }
        post_data: dict = {'grant_type': 'client_credentials'}

        now: datetime = datetime.now(timezone.utc)

        response: httpx.Response = self.http.post(  # noqa: WPS437
            url,
            headers=headers,
            data=post_data,
            auth=(self.client_id, self.secret),
        )

        # Raise error on 4xx and 5xxx
        response.raise_for_status()

        response_data: dict = response.json()

        # Save the token
        self.token = response_data.get('access_token')

        # Set experation date for token
        expires_in: int = response_data.get('expires_in', 0)
        self.token_expires = now + timedelta(seconds=max(0, expires_in - 60))

        # Set up headers to use in API requests
        self._create_headers()

        appid: Optional[str] = response_data.get('app_id')

        self.logger.info(
            'Authentication succesfull '
            f'(appid: {appid})',
        )

    def _create_headers(self) -> None:
        """Create authenticationn headers for requests."""
        headers: dict = dict(HEADERS)
        headers['Authorization'] = headers['Authorization'].replace(
            ':accesstoken:',
            self.token,
        )
        self.headers = headers

    def _date_to_paypal_format(self, input_datetime: datetime) -> str:
        """Convert a datetime to the format that the PayPal api expects.

        Arguments:
            input_datetime {datetime} -- Input e.g. 2021-01-01 00:00:00+00:00

        Returns:
            str -- Converted datetime: 2021-01-01T00:00:00+0000
        """
        return input_datetime.isoformat()
