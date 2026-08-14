"""Outbound email delivery."""


class EmailService:
    def send(self, to, subject, body):
        """Send an email via SMTP and return its message id."""
        message = self._build_message(to, subject, body)
        self.smtp.send(message)
        return message.id
