"""User pseudonymisation for the log (design section 8.2).

The subject identifier from the connector token is never written. Every event
carries an HMAC of it keyed with a per-tenant secret that lives in the server's
secret store and never in the warehouse. A reader of the log can tell that the
same person asked twelve questions, not who they are. Re-identification for a
support case means running the same HMAC over the client's directory with
their data owner in the loop.
"""

from __future__ import annotations

import hashlib
import hmac

HASH_LENGTH = 32


def user_hash(subject: str, secret: str) -> str:
    """HMAC-SHA256 of `subject` keyed with `secret`, hex, truncated to 32 chars.

    Stable for the same subject and secret, unrelated across secrets, so two
    tenants (or a rotated secret) never share a hash space.
    """
    digest = hmac.new(secret.encode("utf-8"), subject.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()[:HASH_LENGTH]
