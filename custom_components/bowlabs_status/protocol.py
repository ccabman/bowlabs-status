"""In-memory, status-only protocol for the owned loopback lab. No device APIs."""

import hashlib
import hmac
import json
import secrets
import time

SCOPE = "status.report.synthetic"
POLICY = {"protocol": 1, "role": "mac_status_lab", "capabilities": [SCOPE]}
MODULES = {"photos", "calendar", "music", "album"}


class Refused(Exception):
    def __init__(self, status=400):
        self.status = status


def digest(secret):
    return hashlib.sha256(secret.encode()).hexdigest()


def decode(body):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Refused()
            result[key] = value
        return result

    try:
        return json.loads(
            body,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(Refused()),
        )
    except (ValueError, TypeError, UnicodeError):
        raise Refused() from None


def validate(report, now):
    if not isinstance(report, dict) or set(report) != {
        "protocol",
        "source",
        "sequence",
        "reportedAt",
        "modules",
    }:
        raise Refused()
    if (
        type(report["protocol"]) is not int
        or report["protocol"] != 1
        or report["source"] != "synthetic"
    ):
        raise Refused()
    if (
        type(report["sequence"]) is not int
        or not 1 <= report["sequence"] <= 1_000_000_000
    ):
        raise Refused()
    if type(report["reportedAt"]) is not int or abs(report["reportedAt"] - now) > 30:
        raise Refused()
    modules = report["modules"]
    if not isinstance(modules, dict) or set(modules) != MODULES:
        raise Refused()
    allowed = {
        "photos": {
            ("checked", "full"),
            ("permission_required", "denied"),
            ("not_checked", "unknown"),
        },
        "calendar": {
            ("checked", "full"),
            ("permission_required", "denied"),
            ("not_checked", "unknown"),
        },
        "music": {
            ("permission_only", "granted"),
            ("permission_required", "denied"),
            ("not_checked", "unknown"),
        },
        "album": {
            ("sample_loaded", "full"),
            ("partial", "full"),
            ("permission_required", "denied"),
            ("not_checked", "unknown"),
        },
    }
    for name, row in modules.items():
        fields = {"state", "permission"} | (
            {"loaded", "needs_download", "failed", "timed_out"}
            if name == "album"
            else set()
        )
        if (
            not isinstance(row, dict)
            or set(row) != fields
            or type(row["state"]) is not str
            or type(row["permission"]) is not str
        ):
            raise Refused()
        if (row["state"], row["permission"]) not in allowed[name]:
            raise Refused()
        if name == "album":
            counts = [
                row[key] for key in ("loaded", "needs_download", "failed", "timed_out")
            ]
            if (
                any(type(n) is not int or not 0 <= n <= 3 for n in counts)
                or sum(counts) > 3
            ):
                raise Refused()
            if row["state"] == "sample_loaded" and (
                counts[0] == 0 or sum(counts[1:]) != 0
            ):
                raise Refused()
            if row["state"] == "partial" and not (
                counts[0] > 0 and sum(counts[1:]) > 0
            ):
                raise Refused()
            if (
                row["state"] in {"permission_required", "not_checked"}
                and sum(counts) != 0
            ):
                raise Refused()
    return report


class Link:
    def __init__(self, changed, clock=time.time, monotonic=time.monotonic):
        self.changed = changed
        self.clock = clock
        self.monotonic = monotonic
        self.pending = None
        self.active = None
        self.starts = []
        self.attempts = 0

    def start(self, body):
        if (
            not isinstance(body, dict)
            or body != POLICY
            or type(body.get("protocol")) is not int
        ):
            raise Refused()
        now = self.monotonic()
        self.starts = [stamp for stamp in self.starts if now - stamp < 60]
        if len(self.starts) >= 5:
            raise Refused(429)
        self.starts.append(now)
        if self.active and self.active["expires"] > now:
            raise Refused(409)
        secret = secrets.token_urlsafe(36)
        code = f"{secrets.randbelow(100000000):08d}"
        self.pending = {
            "hash": digest(secret),
            "code": code,
            "expires": now + 120,
            "approved": False,
        }
        self.attempts = 0
        return {**POLICY, "secret": secret, "code": code, "expiresIn": 120}

    def claim(self, secret):
        if not self.pending or self.pending["expires"] <= self.monotonic():
            self.pending = None
            raise Refused(410)
        if not hmac.compare_digest(self.pending["hash"], digest(secret)):
            raise Refused(401)
        return self.pending

    def approve(self, code):
        if not self.pending or self.pending["expires"] <= self.monotonic():
            raise Refused(410)
        if self.attempts >= 5:
            raise Refused(429)
        self.attempts += 1
        if type(code) is not str or not hmac.compare_digest(self.pending["code"], code):
            raise Refused(403)
        self.pending["approved"] = True

    def status(self, secret):
        claim = self.claim(secret)
        return {**POLICY, "status": "approved" if claim["approved"] else "waiting"}

    def confirm(self, secret):
        claim = self.claim(secret)
        if not claim["approved"]:
            raise Refused(403)
        self.active = {
            "hash": claim["hash"],
            "expires": self.monotonic() + 600,
            "sequence": 0,
            "reports": [],
            "seen": None,
        }
        self.pending = None
        self.changed("connected", None)
        return {**POLICY, "expiresIn": 600}

    def authorized(self, secret):
        if self.active and self.active["expires"] <= self.monotonic():
            self.revoke("expired")
        if not self.active or not hmac.compare_digest(
            self.active["hash"], digest(secret)
        ):
            raise Refused(401)
        return self.active

    def report(self, secret, body):
        active = self.authorized(secret)
        report = validate(body, self.clock())
        if report["sequence"] <= active["sequence"]:
            raise Refused(409)
        now = self.monotonic()
        active["reports"] = [stamp for stamp in active["reports"] if now - stamp < 60]
        if len(active["reports"]) >= 60:
            raise Refused(429)
        active["reports"].append(now)
        active["sequence"] = report["sequence"]
        active["seen"] = now
        self.changed("connected", report)
        return {"accepted": True, "sequence": report["sequence"]}

    def cancel(self, secret):
        if self.pending and hmac.compare_digest(self.pending["hash"], digest(secret)):
            self.pending = None
        elif self.active and hmac.compare_digest(self.active["hash"], digest(secret)):
            self.revoke("disconnected")
        else:
            raise Refused(401)
        return {"ok": True}

    def revoke(self, reason="revoked"):
        self.pending = None
        self.active = None
        self.changed(reason, None)

    def tick(self):
        if self.pending and self.pending["expires"] <= self.monotonic():
            self.pending = None
        if self.active:
            if self.active["expires"] <= self.monotonic():
                self.revoke("expired")
            elif (
                self.active["seen"] is not None
                and self.monotonic() - self.active["seen"] > 90
            ):
                self.changed("disconnected", None)
