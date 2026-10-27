"""Web Push (PWA notifications, issue #7).

Sends browser push notifications via VAPID. The keypair is generated once and
persisted in the control store (never in config); the public *application server
key* is handed to the client so its ``PushManager`` subscription is bound to this
server. Subscriptions the push service reports as gone (404/410) are pruned on
send.

This module owns only the backend foundation (issue #11): key management, the
subscription store, and the send path. The service worker + opt-in UI (#12) build
on top; what is sent is the top bar's new Needs you list (``work_view``, #3348).
"""

from __future__ import annotations

import base64
import json
import logging

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid
from pywebpush import WebPushException, webpush

from .config import PushConfig

log = logging.getLogger(__name__)


class PushService:
    def __init__(self, config: PushConfig, store) -> None:
        self.config = config
        self.store = store
        self._vapid: Vapid | None = None  # signing key, passed to webpush as-is
        self._public_key: str | None = None  # base64url application server key

    @property
    def enabled(self) -> bool:
        """Configured on *and* a usable keypair is loaded."""
        return bool(self.config.enabled and self._public_key and self._vapid)

    @property
    def public_key(self) -> str | None:
        return self._public_key

    def start(self) -> None:
        """Load the VAPID keypair, generating + persisting one on first run.
        Idempotent and a no-op when push is disabled."""
        if not self.config.enabled:
            return
        keys = self.store.load_vapid_keys()
        if keys is None:
            keys = self._generate_keys()
            if keys is not None:
                self.store.save_vapid_keys(*keys)
        if keys is None:
            return
        public_key, private_pem = keys
        # pywebpush's `vapid_private_key=<PEM string>` path routes through
        # Vapid.from_string, which expects a *raw* key, not a PEM — it fails with
        # "could not deserialize key data". Passing a Vapid instance is the
        # supported path, so build it once here.
        try:
            self._vapid = Vapid.from_pem(private_pem.encode())
            self._public_key = public_key
        except Exception as exc:  # noqa: BLE001 -- a bad stored key disables push, never crashes
            log.warning("loading VAPID key failed: %s", exc)
            self._vapid = None

    @staticmethod
    def _generate_keys() -> tuple[str, str] | None:
        """Return (application_server_key_b64url, private_pem) or None on error."""
        try:
            vapid = Vapid()
            vapid.generate_keys()
            private_pem = vapid.private_pem().decode()
            raw = vapid.public_key.public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint,
            )
            public_key = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
            return public_key, private_pem
        except Exception as exc:  # noqa: BLE001 -- a crypto failure disables push, never crashes
            log.warning("VAPID key generation failed: %s", exc)
            return None

    def subscribe(self, subscription: object) -> bool:
        """Persist a browser PushSubscription (as sent by ``PushManager``)."""
        if not isinstance(subscription, dict):
            return False
        endpoint = subscription.get("endpoint")
        keys = subscription.get("keys")
        if not (isinstance(endpoint, str) and endpoint and isinstance(keys, dict)):
            return False
        p256dh, auth = keys.get("p256dh"), keys.get("auth")
        if not (isinstance(p256dh, str) and isinstance(auth, str)):
            return False
        self.store.add_push_subscription(endpoint, p256dh, auth)
        return True

    def unsubscribe(self, endpoint: str) -> None:
        self.store.delete_push_subscription(endpoint)

    def has_subscriptions(self) -> bool:
        """A device would receive a push now."""
        return self.enabled and bool(self.store.load_push_subscriptions())

    def send_to_all(self, title: str, body: str = "", url: str = "/", tag: str = "") -> int:
        """Push one notification to every subscription; prune gone ones. Returns
        the number successfully delivered. A device shows one notification per ``tag``
        (else per ``url``): a newer one with the same tag replaces it."""
        message = {"title": title, "body": body, "url": url}
        if tag:
            message["tag"] = tag
        return self._send_all(message)

    def clear_all(self, tags: list[str]) -> int:
        """Close the notifications with these tags on every device (#3365). The service
        worker shows nothing for it, so it is sent only for notifications really sent."""
        if not tags:
            return 0
        return self._send_all({"clear": list(tags)})

    def _send_all(self, message: dict) -> int:
        if not self.enabled:
            return 0
        payload = json.dumps(message)
        subject = self.config.subject or "mailto:agentdeck@localhost"
        sent = 0
        for sub in self.store.load_push_subscriptions():
            if self._send_one(sub, payload, subject):
                sent += 1
        return sent

    def _send_one(self, sub: dict, payload: str, subject: str) -> bool:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=self._vapid,
                vapid_claims={"sub": subject},
            )
            return True
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                self.store.delete_push_subscription(sub.get("endpoint", ""))
                log.info("pruned expired push subscription (%s)", status)
            else:
                log.warning("web push failed (%s): %s", status, exc)
            return False
        except Exception as exc:  # noqa: BLE001 -- one bad endpoint must not stop the rest
            log.warning("web push error: %s", exc)
            return False
