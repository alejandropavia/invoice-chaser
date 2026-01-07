import os
import requests
from typing import Optional

RESEND_API_URL = "https://api.resend.com/emails"
# Use custom domain - replace with your verified domain
CUSTOM_DOMAIN = os.getenv("CUSTOM_DOMAIN", "invoicechaserapp.com")
FROM_EMAIL = f"Invoice Chaser <notifications@{CUSTOM_DOMAIN}>"


def send_email(to_email: str, subject: str, html: str, reply_to: Optional[str] = None):
    """Send email via Resend API. Does not crash on import if key is missing."""
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        raise RuntimeError("RESEND_API_KEY environment variable is not set")

    payload = {
        "from": FROM_EMAIL,
        "to": [to_email],
        "subject": subject,
        "html": html,
    }
    
    if reply_to:
        payload["reply_to"] = [reply_to]

    response = requests.post(
        RESEND_API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )

    response.raise_for_status()
    return response.json()

