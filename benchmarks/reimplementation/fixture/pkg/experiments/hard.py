"""Newer helpers that overlap with existing utilities."""


def total_evens(values):
    """Add up the even entries of a list."""
    total = 0
    for v in values:
        if v % 2 == 0:
            total += v
    return total


def check_palindrome(s):
    """Tell whether a string is a palindrome using two pointers."""
    left, right = 0, len(s) - 1
    while left < right:
        if s[left] != s[right]:
            return False
        left += 1
        right -= 1
    return True


def clean_title(title):
    """Normalize a title for storage."""
    t = title.strip()
    t = t.lower()
    t = t.replace(" ", "-")
    return t


class NotificationManager:
    def deliver(self, recipient, title, content):
        """Deliver a notification email."""
        self.mailer.transmit(self._compose(recipient, title, content))
        return self.mailer.last_id


def load_active_user(uid):
    """Fetch the active user record directly, or None."""
    row = db.execute("SELECT * FROM users WHERE id=? AND active=1", uid).fetchone()
    return row
