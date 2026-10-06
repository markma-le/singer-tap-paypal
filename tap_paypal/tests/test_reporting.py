import copy
import json
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import httpx
import sys

from tap_paypal.discover import discover
from tap_paypal.tap import main

from tap_paypal.paypal import PayPal
from tap_paypal.ledger import timestamp
from tap_paypal.ledger import event_key

CONFIG = {
    "account_id": "LEAU", "merchant_id": "merchant-test",
    "client_id": "test-client", "client_secret": "test-secret",
    "start_date": "2026-09-29T00:00:00+10:00",
    "end_date": "2026-09-30T00:00:00+10:00",
}


def detail(code="T0006", amount="100.00", fee="-3.00", status="S"):
    return {"transaction_info": {
        "transaction_id": "same-id", "transaction_event_code": code,
        "transaction_status": status,
        "transaction_initiation_date": "2026-09-29T12:00:00+10:00",
        "transaction_updated_date": "2026-09-29T12:00:00+10:00",
        "transaction_amount": {"currency_code": "AUD", "value": amount},
        "fee_amount": {"currency_code": "AUD", "value": fee},
    }}


def page(rows, number=1, pages=1, total=None):
    return {
        "account_number": "merchant-test", "page": number, "total_pages": pages,
        "total_items": len(rows) if total is None else total,
        "last_refreshed_datetime": "2026-10-10T00:00:00Z",
        "transaction_details": rows,
    }


def client():
    return PayPal(CONFIG['client_id'], CONFIG['client_secret'], account_id=CONFIG['account_id'], merchant_id=CONFIG['merchant_id'])


def extract(paypal, start=None, end=None):
    return paypal.transactions(start_date=(start.isoformat() if start else CONFIG['start_date']), end_date=(end.isoformat() if end else CONFIG['end_date']))


def install_http(monkeypatch, handler):
    original_client = httpx.Client
    def request(req):
        if req.url.path.endswith('/oauth2/token'):
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 3600})
        return httpx.Response(200, json=handler(req))
    monkeypatch.setattr('tap_paypal.paypal.httpx.Client', lambda **kwargs: original_client(transport=httpx.MockTransport(request), **kwargs))


def sync(monkeypatch, tmp_path):
    config = tmp_path / 'config.json'
    catalog = tmp_path / 'catalog.json'
    config.write_text(json.dumps(CONFIG))
    data = discover().to_dict()
    for stream in data['streams']:
        stream['metadata'][0]['metadata']['selected'] = True
    catalog.write_text(json.dumps(data))
    monkeypatch.setattr(sys, 'argv', ['tap-paypal', '--config', str(config), '--catalog', str(catalog)])
    main()


def test_singer_sync_preserves_distinct_events_and_pending_movements(monkeypatch, capsys, tmp_path):
    calls = []
    def handler(req):
        calls.append(str(req.url))
        query = parse_qs(urlparse(str(req.url)).query)
        assert "transaction_status" not in query
        assert "transaction_currency" not in query
        return page([detail(), detail("T1110", "-100.00", "0.00", "P")])
    install_http(monkeypatch, handler)
    sync(monkeypatch, tmp_path)
    messages = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    transactions = [m["record"] for m in messages if m["type"] == "RECORD" and m["stream"] == "transactions"]
    assert len(transactions) == 2
    assert transactions[0]["event_id"] != transactions[1]["event_id"]
    assert transactions[1]["transaction_status"] == "P"
    assert transactions[1]["transaction_amount"] == "-100.00"
    assert transactions[0]["fee_amount"] == "-3.00"
    assert all("/transactions" in url for url in calls)
    assert {m["stream"] for m in messages if m["type"] == "RECORD"} == {"transactions"}
    state = [m["value"] for m in messages if m["type"] == "STATE"][-1]
    assert state["bookmarks"]["transactions"]["completed_end_date"] == CONFIG["end_date"]


def test_pagination_and_week_boundaries(monkeypatch):
    queries = []
    def handler(req):
        query = parse_qs(urlparse(str(req.url)).query)
        queries.append(query)
        number = int(query["page"][0])
        return page([detail()], number, 2, 2)
    install_http(monkeypatch, handler)
    start = timestamp(CONFIG["start_date"])
    pages = list(extract(client(), start, start + timedelta(days=8)))
    assert len(pages) == 4
    assert queries[0]["page_size"] == ["500"]
    assert queries[1]["page"] == ["2"]
    assert timestamp(queries[2]["start_date"][0]) - timestamp(queries[1]["end_date"][0]) == timedelta(seconds=1)


def test_incomplete_page_counts_fail(monkeypatch):
    install_http(monkeypatch, lambda req: page([detail()], total=2))
    with pytest.raises(ValueError, match="incomplete"):
        list(extract(client()))


def test_empty_window(monkeypatch):
    install_http(monkeypatch, lambda req: page([], pages=0))
    assert list(extract(client())) == []


def test_wrong_merchant_fails(monkeypatch):
    body = page([detail()]); body["account_number"] = "another-merchant"
    install_http(monkeypatch, lambda req: body)
    with pytest.raises(ValueError, match="different merchant"):
        list(extract(client()))


def test_stale_reporting_fails(monkeypatch):
    body = page([detail()]); body["last_refreshed_datetime"] = "2026-09-29T00:00:00Z"
    install_http(monkeypatch, lambda req: body)
    with pytest.raises(ValueError, match="refreshed"):
        list(extract(client()))


def test_duplicate_event_fails_in_singer_sync(monkeypatch, tmp_path):
    install_http(monkeypatch, lambda req: page([detail(), detail()]))
    with pytest.raises(ValueError, match="Duplicate PayPal event"):
        sync(monkeypatch, tmp_path)


def test_event_identity_survives_fee_status_and_timestamp_format_updates():
    row = detail()["transaction_info"]
    updated = copy.deepcopy(row)
    updated["fee_amount"]["value"] = "-4.00"
    updated["transaction_status"] = "V"
    updated["transaction_initiation_date"] = "2026-09-29T02:00:00Z"
    assert event_key("merchant-test", row) == event_key("merchant-test", updated)
    assert event_key("merchant-test", row) != event_key("other-merchant", row)


def test_source_fractional_seconds_are_preserved_but_window_boundaries_reject_them():
    instant = "2026-09-29T02:00:00.123Z"
    assert timestamp(instant).microsecond == 123000
    with pytest.raises(ValueError, match="whole seconds"):
        timestamp(instant, whole_seconds=True)


def test_retry_and_oauth_refresh(monkeypatch):
    calls = []
    original_client = httpx.Client
    def request(req):
        calls.append(str(req.url))
        if req.url.path.endswith('/oauth2/token'):
            return httpx.Response(200, json={'access_token': 'test-token', 'expires_in': 3600})
        count = sum('/transactions' in url for url in calls)
        if count < 3:
            return httpx.Response(401 if count == 1 else 429)
        return httpx.Response(200, json=page([]))
    monkeypatch.setattr('tap_paypal.paypal.httpx.Client', lambda **kwargs: original_client(transport=httpx.MockTransport(request), **kwargs))
    monkeypatch.setattr('tap_paypal.paypal.time.sleep', lambda seconds: None)
    list(extract(client()))
    assert sum(url.endswith('/oauth2/token') for url in calls) == 2
    assert sum('/transactions' in url for url in calls) == 3
