import hashlib
import json

from datetime import datetime, timezone


def timestamp(value, whole_seconds=False):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Dates require a timezone")
    if whole_seconds and result.microsecond:
        raise ValueError("Window boundaries require whole seconds")
    return result.astimezone(timezone.utc)


def event_key(merchant_id, row):
    identity = [
        merchant_id,
        row["transaction_id"],
        row["transaction_event_code"],
        timestamp(row["transaction_initiation_date"]).isoformat(),
        row["transaction_amount"]["currency_code"],
    ]
    return hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()


def transaction_record(account_id, merchant_id, detail):
    info = detail["transaction_info"]
    record = {
        "account_id": account_id,
        "merchant_id": merchant_id,
        "event_id": event_key(merchant_id, info),
        "transaction_info_json": json.dumps(info, sort_keys=True, separators=(",", ":")),
    }
    for field in [
        "transaction_id", "transaction_event_code", "transaction_status",
        "transaction_initiation_date", "transaction_updated_date",
        "paypal_reference_id", "paypal_reference_id_type", "bank_reference_id",
        "invoice_id", "custom_field",
    ]:
        record[field] = info.get(field)
    for field in ["transaction_amount", "fee_amount", "ending_balance", "available_balance"]:
        record[field] = info.get(field, {}).get("value")
        record[field + "_currency"] = info.get(field, {}).get("currency_code")
    return record
