"""Billing document serialization."""


def serialize_purchase_order(order):
    """Encode a purchase order against the purchasing schema and validate it."""
    data = encode(order, schema=PURCHASE_ORDER_SCHEMA)
    validate(data, schema=PURCHASE_ORDER_SCHEMA)
    return data
