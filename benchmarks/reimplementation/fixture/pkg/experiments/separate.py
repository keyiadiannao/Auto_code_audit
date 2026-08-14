"""Reporting and admin utilities (deliberately separate from audit code)."""


def encrypt_report(text, key):
    """Encrypt a report using AEAD with a report-specific context."""
    nonce = random_bytes(12)
    cipher = aead_encrypt(key, nonce, text, aad=b"report")
    return nonce + cipher


def serialize_invoice(inv):
    """Encode an invoice against the invoicing schema and validate it."""
    data = encode(inv, schema=INVOICE_SCHEMA)
    validate(data, schema=INVOICE_SCHEMA)
    return data


def sanitize_user_input(value):
    """Strip control chars and HTML from admin-entered input."""
    clean = strip_control(value)
    return html_escape(clean)
