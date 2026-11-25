"""E-mail alerts for interesting new listings."""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from html import escape

from .config import EmailSettings
from .models import Listing

log = logging.getLogger(__name__)


def is_good_deal(listing: Listing, ratio: float) -> bool:
    """True when the asking price is below ``ratio`` x the market price."""
    if listing.price is None or not listing.market_price:
        return False
    return listing.price < listing.market_price * ratio


def should_notify(listing: Listing, settings: EmailSettings) -> bool:
    if not settings.enabled or listing.sold:
        return False
    if settings.only_hot_deals and not listing.hot_deal:
        return False
    if settings.only_good_deals and not is_good_deal(listing, settings.good_deal_ratio):
        return False
    return True


def build_message(listing: Listing, sender: str, recipient: str) -> EmailMessage:
    tag = "Hot deal" if listing.hot_deal else "Good deal"
    msg = EmailMessage()
    msg["Subject"] = f"[Bike Scout] {tag}: {listing.title} ({listing.size or 'n/a'}) at {listing.price_label}"
    msg["From"] = sender
    msg["To"] = recipient

    discount = f"{listing.discount:.0%} below market" if listing.discount is not None else "n/a"
    fields = [
        ("Model", listing.title),
        ("Size", listing.size or "n/a"),
        ("Price", listing.price_label),
        ("Market price", f"€ {listing.market_price:,.0f}" if listing.market_price else "n/a"),
        ("Discount", discount),
        ("Groupset", listing.groupset or "n/a"),
        ("Country", listing.country or "n/a"),
    ]
    msg.set_content("\n".join(f"{k}: {v}" for k, v in fields) + f"\n\n{listing.url}\n")

    rows = "".join(
        f'<tr><td style="padding:6px 12px;font-weight:bold">{escape(k)}</td>'
        f'<td style="padding:6px 12px">{escape(str(v))}</td></tr>'
        for k, v in fields
    )
    image = (
        f'<img src="{escape(listing.image_url, quote=True)}" alt="" style="max-width:100%;border-radius:6px">'
        if listing.image_url
        else ""
    )
    msg.add_alternative(
        f"""<html><body style="font-family:Arial,sans-serif;color:#333">
<div style="max-width:600px;margin:0 auto;padding:20px;border:1px solid #e0e0e0;border-radius:8px">
<h2 style="color:#e67e22;margin-top:0">{escape(tag)}: {escape(listing.title)}</h2>
{image}
<table style="border-collapse:collapse;margin:16px 0">{rows}</table>
<p style="text-align:center"><a href="{escape(listing.url, quote=True)}"
 style="background:#e67e22;color:#fff;padding:12px 24px;text-decoration:none;border-radius:4px">View listing</a></p>
</div></body></html>""",
        subtype="html",
    )
    return msg


class EmailNotifier:
    def __init__(self, settings: EmailSettings, smtp_factory=smtplib.SMTP) -> None:
        self.settings = settings
        self._smtp_factory = smtp_factory

    def notify(self, listing: Listing) -> bool:
        """Send an alert if ``listing`` matches the filters. Returns ``True`` if an e-mail was sent."""
        if not should_notify(listing, self.settings):
            return False
        s = self.settings
        msg = build_message(listing, s.sender, s.recipient)
        try:
            with self._smtp_factory(s.smtp_server, s.smtp_port, timeout=30) as smtp:
                smtp.starttls()
                smtp.login(s.sender, s.app_password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("Could not send e-mail for %s: %s", listing.url, exc)
            return False
        log.info("Alert sent for %s", listing.title)
        return True
