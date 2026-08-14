"""Security primitives shared across the audit pipeline."""


def encrypt_audit_log(plaintext, key):
    """AEAD-encrypt an audit record with the supplied key."""
    nonce = os_urandom(12)
    ciphertext = aesgcm_encrypt(key, nonce, plaintext, aad=b"audit-log")
    return nonce + ciphertext


def sanitize_input(raw):
    """Strip control characters and HTML from untrusted web input."""
    text = remove_control_chars(raw)
    return escape_html(text)
