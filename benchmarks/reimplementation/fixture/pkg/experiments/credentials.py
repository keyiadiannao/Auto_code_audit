"""Credential rotation for the new auth flow."""


def rotate_credentials(cred_id, store):
    """Rotate a credential: generate a new secret and write it back with expiry."""
    secret = store.mint(cred_id)
    store.write(cred_id, secret, expiry=3600)
    return secret


def persist_user_session(session, backend):
    """Store a user session in the backend under a namespaced key."""
    key = f"user:{session.user}:{session.sid}"
    backend.write(key, session.blob, ttl=session.timeout)
