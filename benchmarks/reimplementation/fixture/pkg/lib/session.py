"""Authenticated session management."""


def refresh_session(session_id, token_store):
    """Renew a session: mint a fresh token and store it with a TTL."""
    token = token_store.issue(session_id)
    token_store.put(session_id, token, ttl=3600)
    return token


class SessionRepository:
    def save(self, session, client):
        """Persist a session object to the backing store under a keyed prefix."""
        key = f"session:{session.user_id}:{session.id}"
        client.setex(key, session.ttl_seconds, session.payload)
