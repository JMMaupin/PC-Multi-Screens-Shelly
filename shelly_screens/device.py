"""JSON-RPC client for Shelly Gen2+ devices (here a Power Strip 4 Gen4).

The protocol is a simplified JSON-RPC 2.0 exposed as POST on /rpc:

    {"id": 1, "method": "Switch.Set", "params": {"id": 0, "on": true}}

The response carries either "result" or "error". Authentication, when it is
enabled on the device, is HTTP Digest SHA-256 with the user "admin" -- we
handle it here so we don't get locked out if it is enabled later.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT = 4.0
# The device rate-limits and answers 429 when pushed too hard. This is not
# a failure: we simply need to let a moment pass.
RATE_LIMIT_STATUS = 429
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_PAUSE_S = 0.6
AUTH_USERNAME = "admin"  # imposed by the Shelly Gen2+ firmware
# Method and path of RPC calls. They go into the digest computation, hence
# declaring them here rather than hard-coding them in two places.
RPC_METHOD = "POST"
RPC_URI = "/rpc"


class ShellyError(RuntimeError):
    """Application error returned by the device ("error" field of the JSON-RPC)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Shelly error {code}: {message}")
        self.code = code
        self.message = message


class ShellyUnreachable(RuntimeError):
    """The device did not answer: network down, wrong address, timeout."""


class ProtectedOutlet(RuntimeError):
    """Attempt to switch off an output declared untouchable.

    The safeguard lives here, as close as possible to the network call, and
    not in the layers above: a protection that relies on a list being built
    correctly gives way as soon as a list is built wrong. Here, no function
    can switch off the PC's output, whatever the path.
    """

    def __init__(self, host: str, switch_id: int) -> None:
        super().__init__(
            f"{host}: output {switch_id} is protected and must never be switched off"
        )
        self.host = host
        self.switch_id = switch_id


class AuthenticationFailed(RuntimeError):
    """The device requires authentication that we cannot provide.

    Deliberately distinct from `ShellyUnreachable`: the device answers
    perfectly well, it is the password that is missing or wrong. Once it
    has been lost, only a reset via the buttons gets out of it, and the
    interface must be able to say so.
    """

    def __init__(self, host: str, has_password: bool) -> None:
        reason = "wrong password" if has_password else "password required"
        super().__init__(f"{host}: {reason}")
        self.host = host
        self.has_password = has_password


@dataclass(frozen=True)
class SwitchState:
    """Instantaneous state of an outlet, as returned by Switch.GetStatus."""

    id: int
    output: bool
    apower: float  # active power, W
    voltage: float  # voltage, V
    current: float  # current, A
    energy_total: float  # cumulative energy, Wh
    source: str  # what caused the last change (SHC, HTTP, button...)

    @classmethod
    def from_rpc(cls, payload: dict[str, Any]) -> "SwitchState":
        aenergy = payload.get("aenergy") or {}
        return cls(
            id=int(payload.get("id", 0)),
            output=bool(payload.get("output", False)),
            apower=float(payload.get("apower") or 0.0),
            voltage=float(payload.get("voltage") or 0.0),
            current=float(payload.get("current") or 0.0),
            energy_total=float(aenergy.get("total") or 0.0),
            source=str(payload.get("source") or ""),
        )


class ShellyDevice:
    """RPC access to a Shelly. Safe to use from several threads."""

    def __init__(
        self,
        host: str,
        password: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        protected: set[int] | None = None,
    ) -> None:
        self.host = host
        self.password = password
        self.timeout = timeout
        # Outputs that no call can switch off: typically the one that
        # powers the tower. Cutting a running PC loses its work in progress
        # and can damage its file system.
        self.protected: set[int] = set(protected or ())
        self._lock = threading.Lock()
        self._request_id = 0
        # Digest challenge parameters, remembered between calls to avoid a
        # systematic 401 round trip.
        self._auth_challenge: dict[str, str] | None = None
        self._nonce_count = 0

    # ------------------------------------------------------------------ RPC

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Call an RPC method and return its "result"."""
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            challenge = self._auth_challenge

        body = {"id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        raw = json.dumps(body).encode("utf-8")
        # Target the IPv4 address: the mDNS name may resolve to an IPv6
        # link-local address, on which the connection fails.
        from .discovery import ipv4_host

        url = f"http://{ipv4_host(self.host)}{RPC_URI}"

        auth_header = self._build_auth_header(challenge) if challenge else None
        try:
            payload = self._post_with_backoff(url, raw, auth_header)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise ShellyUnreachable(f"{self.host}: HTTP {exc.code}") from exc
            # The remembered nonce may have expired: renegotiate the
            # challenge, then replay exactly once.
            challenge = _parse_digest_challenge(exc.headers.get("WWW-Authenticate", ""))
            if not challenge or not self.password:
                raise AuthenticationFailed(self.host, bool(self.password)) from exc
            with self._lock:
                self._auth_challenge = challenge
                self._nonce_count = 0
            try:
                payload = self._post_with_backoff(
                    url, raw, self._build_auth_header(challenge)
                )
            except urllib.error.HTTPError as retry_exc:
                if retry_exc.code == 401:
                    # Replay refused with a fresh challenge: the password is
                    # wrong, no point insisting.
                    with self._lock:
                        self._auth_challenge = None
                    raise AuthenticationFailed(self.host, True) from retry_exc
                raise ShellyUnreachable(
                    f"{self.host}: HTTP {retry_exc.code}"
                ) from retry_exc
            except (urllib.error.URLError, OSError) as retry_exc:
                raise ShellyUnreachable(f"{self.host}: {retry_exc}") from retry_exc
        except (urllib.error.URLError, OSError) as exc:
            raise ShellyUnreachable(f"{self.host}: {exc}") from exc

        if "error" in payload:
            err = payload["error"]
            raise ShellyError(int(err.get("code", 0)), str(err.get("message", "")))
        return payload.get("result")

    def _post_with_backoff(
        self, url: str, raw: bytes, auth_header: str | None
    ) -> dict[str, Any]:
        """Send the request, waiting whenever the device asks for a breather.

        A 429 is not a failure: the device is rate-limiting. Insisting right
        away would only make it dig in, and reporting it as unreachable would
        trigger a pointless network resolution.
        """
        for attempt in range(RATE_LIMIT_RETRIES):
            try:
                return self._post(url, raw, auth_header)
            except urllib.error.HTTPError as exc:
                if exc.code != RATE_LIMIT_STATUS or attempt == RATE_LIMIT_RETRIES - 1:
                    raise
                time.sleep(RATE_LIMIT_PAUSE_S * (attempt + 1))
        raise ShellyUnreachable(f"{self.host}: rate limited")

    def _post(self, url: str, raw: bytes, auth_header: str | None) -> dict[str, Any]:
        request = urllib.request.Request(url, data=raw, method="POST")
        request.add_header("Content-Type", "application/json")
        if auth_header:
            request.add_header("Authorization", auth_header)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _build_auth_header(self, challenge: dict[str, str]) -> str:
        """Build the Digest SHA-256 header expected by the firmware.

        This is the standard RFC 7616 computation, with `ha2` derived from
        the method and the URI. Shelly's documentation describes, for other
        firmwares, a constant `ha2` computed over the string
        `dummy_method:dummy_uri`; this firmware rejects it -- checked on the
        device, only the standard computation is accepted.

        The difference is not cosmetic: with a constant `ha2`, the response
        does not depend on the request, and a captured header could be
        replayed to trigger an entirely different command while the nonce
        remains valid. Here, the response is bound to the method and URI.
        """
        with self._lock:
            self._nonce_count += 1
            nonce_count = self._nonce_count
        realm = challenge.get("realm", "")
        nonce = challenge.get("nonce", "")
        cnonce = os.urandom(8).hex()
        nc = f"{nonce_count:08x}"

        def sha256(text: str) -> str:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()

        ha1 = sha256(f"{AUTH_USERNAME}:{realm}:{self.password}")
        ha2 = sha256(f"{RPC_METHOD}:{RPC_URI}")
        response = sha256(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        return (
            f'Digest username="{AUTH_USERNAME}", realm="{realm}", nonce="{nonce}", '
            f'uri="{RPC_URI}", response="{response}", algorithm=SHA-256, '
            f"qop=auth, nc={nc}, cnonce=\"{cnonce}\""
        )

    # --------------------------------------------------------------- methods

    def get_status(self) -> dict[str, Any]:
        return self.call("Shelly.GetStatus")

    def get_all_switches(self) -> dict[int, SwitchState]:
        """Read every outlet in a single Shelly.GetStatus call."""
        status = self.get_status()
        states: dict[int, SwitchState] = {}
        for key, value in status.items():
            if key.startswith("switch:") and isinstance(value, dict):
                state = SwitchState.from_rpc(value)
                states[state.id] = state
        return states

    def set_switch(self, switch_id: int, on: bool) -> bool:
        """Change an outlet's state; return the previous state.

        A protected output cannot be switched off: the request is refused
        before anything is sent over the network.
        """
        if not on and switch_id in self.protected:
            raise ProtectedOutlet(self.host, switch_id)
        result = self.call("Switch.Set", {"id": switch_id, "on": bool(on)})
        return bool((result or {}).get("was_on", False))

    def protect(self, switch_ids: set[int]) -> None:
        """Declare the outputs that must never be switched off."""
        self.protected = set(switch_ids)

    def count_switches(self) -> int:
        return len(self.get_all_switches())

    # -------------------------------------------------------- authentication

    def set_password(self, realm: str, password: str) -> None:
        """Enable the device's authentication, or remove it.

        The firmware does not accept the password itself but its `ha1`
        digest, and imposes the user `admin`. The realm is the device
        identifier. Passing an empty string removes authentication.
        """
        if password:
            digest = hashlib.sha256(
                f"{AUTH_USERNAME}:{realm}:{password}".encode("utf-8")
            ).hexdigest()
            params: dict[str, Any] = {
                "user": AUTH_USERNAME,
                "realm": realm,
                "ha1": digest,
            }
        else:
            params = {"user": AUTH_USERNAME, "realm": realm, "ha1": None}
        self.call("Shelly.SetAuth", params)
        # The device has just changed its secret: the remembered challenge
        # is now worthless.
        with self._lock:
            self._auth_challenge = None
            self._nonce_count = 0
        self.password = password or None


def _parse_digest_challenge(header: str) -> dict[str, str]:
    """Extract the parameters of a WWW-Authenticate Digest header."""
    if not header.lower().startswith("digest"):
        return {}
    params: dict[str, str] = {}
    for part in header[len("digest") :].split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip().lower()] = value.strip().strip('"')
    return params
