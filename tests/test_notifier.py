from __future__ import annotations

import smtplib
from dataclasses import replace

from bike_scout.notifier import EmailNotifier, build_message, is_good_deal, should_notify


def test_is_good_deal(listing_factory):
    assert is_good_deal(listing_factory(price=1500, market_price=2500), 0.8)
    assert not is_good_deal(listing_factory(price=2000, market_price=2500), 0.8)  # exactly 80%
    assert not is_good_deal(listing_factory(price=None), 0.8)
    assert not is_good_deal(listing_factory(market_price=None), 0.8)


def test_should_notify_filters(settings, listing_factory):
    email = settings.email
    assert should_notify(listing_factory(), email)
    assert not should_notify(listing_factory(sold=True), email)
    assert not should_notify(listing_factory(price=2400), email)  # not a good deal
    assert not should_notify(listing_factory(), replace(email, enabled=False))

    hot_only = replace(email, only_hot_deals=True, only_good_deals=False)
    assert not should_notify(listing_factory(hot_deal=False), hot_only)
    assert should_notify(listing_factory(hot_deal=True, price=2400), hot_only)


def test_build_message_escapes_html(listing_factory):
    listing = listing_factory(title='<script>alert("x")</script>', url='https://x/?a="b"')
    msg = build_message(listing, "a@example.com", "b@example.com")
    html = msg.get_body(("html",)).get_content()
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert 'href="https://x/?a=&quot;b&quot;"' in html
    assert "40% below market" in msg.get_body(("plain",)).get_content()


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.sent = host, port, []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        self.login_args = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_notify_sends_via_smtp(settings, listing_factory):
    FakeSMTP.instances.clear()
    assert EmailNotifier(settings.email, smtp_factory=FakeSMTP).notify(listing_factory()) is True
    (smtp,) = FakeSMTP.instances
    assert smtp.login_args == ("me@example.com", "x")
    assert len(smtp.sent) == 1


def test_notify_survives_smtp_errors(settings, listing_factory):
    def failing(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    assert EmailNotifier(settings.email, smtp_factory=failing).notify(listing_factory()) is False
