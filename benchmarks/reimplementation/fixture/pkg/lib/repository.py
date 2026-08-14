"""User persistence."""


class UserRepository:
    def find_active(self, user_id):
        """Return the active user row for user_id, or None."""
        rows = self.db.query("SELECT * FROM users WHERE id=? AND active=1", user_id)
        return rows[0] if rows else None
