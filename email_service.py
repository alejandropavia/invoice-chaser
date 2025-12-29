import os
import requests

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

RESEND_API_URL = "https://api.resend.com/emails"
FROM_EMAIL = "Invoice Chaser <notifications@invoicechaserapp.com>"


def send_email(to_email: str, subject: str, html: str):
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY environment variable is not set")

    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "html": html,
        },
        timeout=10,
    )

    response.raise_for_status()
    return response.json()

