"""Client JSON-RPC pour les appareils Shelly Gen2+ (ici une Power Strip 4 Gen4).

Le protocole est un JSON-RPC 2.0 simplifie expose en POST sur /rpc :

    {"id": 1, "method": "Switch.Set", "params": {"id": 0, "on": true}}

La reponse porte soit "result", soit "error". L'authentification, quand elle
est activee sur l'appareil, est un HTTP Digest SHA-256 avec l'utilisateur
"admin" -- on la gere ici pour ne pas etre bloque si elle est activee plus tard.
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
# L'appareil limite son debit et repond 429 quand on le presse trop. Ce
# n'est pas une panne : il faut simplement laisser passer un instant.
RATE_LIMIT_STATUS = 429
RATE_LIMIT_RETRIES = 3
RATE_LIMIT_PAUSE_S = 0.6
AUTH_USERNAME = "admin"  # impose par le firmware Shelly Gen2+
# Methode et chemin des appels RPC. Ils entrent dans le calcul du digest,
# d'ou leur declaration ici plutot qu'en dur a deux endroits.
RPC_METHOD = "POST"
RPC_URI = "/rpc"


class ShellyError(RuntimeError):
    """Erreur applicative renvoyee par l'appareil (champ "error" du JSON-RPC)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(f"Shelly error {code}: {message}")
        self.code = code
        self.message = message


class ShellyUnreachable(RuntimeError):
    """L'appareil n'a pas repondu : reseau coupe, mauvaise adresse, timeout."""


class ProtectedOutlet(RuntimeError):
    """Tentative de couper une sortie declaree intouchable.

    Le garde-fou vit ici, au plus pres de l'appel reseau, et non dans les
    couches au-dessus : une protection qui repose sur la bonne construction
    d'une liste cede des qu'une liste est mal construite. Ici, aucune
    fonction ne peut couper la sortie du PC, quel que soit le chemin.
    """

    def __init__(self, host: str, switch_id: int) -> None:
        super().__init__(
            f"{host}: output {switch_id} is protected and must never be switched off"
        )
        self.host = host
        self.switch_id = switch_id


class AuthenticationFailed(RuntimeError):
    """L'appareil exige une authentification que l'on ne sait pas fournir.

    Distincte de `ShellyUnreachable` a dessein : l'appareil repond
    parfaitement, c'est le mot de passe qui manque ou qui est faux. Seule
    une reinitialisation par les boutons permet d'en sortir quand il a ete
    perdu, et l'interface doit pouvoir le dire.
    """

    def __init__(self, host: str, has_password: bool) -> None:
        reason = "wrong password" if has_password else "password required"
        super().__init__(f"{host}: {reason}")
        self.host = host
        self.has_password = has_password


@dataclass(frozen=True)
class SwitchState:
    """Etat instantane d'une prise, tel que renvoye par Switch.GetStatus."""

    id: int
    output: bool
    apower: float  # puissance active, W
    voltage: float  # tension, V
    current: float  # courant, A
    energy_total: float  # energie cumulee, Wh
    source: str  # qui a provoque le dernier changement (SHC, HTTP, button...)

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
    """Acces RPC a un Shelly. Utilisable depuis plusieurs threads."""

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
        # Sorties qu'aucun appel ne pourra couper : typiquement celle qui
        # alimente l'unite centrale. Couper le PC en marche lui fait perdre
        # son travail en cours et peut abimer son systeme de fichiers.
        self.protected: set[int] = set(protected or ())
        self._lock = threading.Lock()
        self._request_id = 0
        # Parametres du challenge digest, memorises entre deux appels pour
        # eviter un aller-retour 401 systematique.
        self._auth_challenge: dict[str, str] | None = None
        self._nonce_count = 0

    # ------------------------------------------------------------------ RPC

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Appelle une methode RPC et renvoie son "result"."""
        with self._lock:
            self._request_id += 1
            request_id = self._request_id
            challenge = self._auth_challenge

        body = {"id": request_id, "method": method}
        if params is not None:
            body["params"] = params
        raw = json.dumps(body).encode("utf-8")
        # On vise l'adresse IPv4 : le nom mDNS peut resoudre vers une
        # adresse lien-local IPv6, sur laquelle la connexion echoue.
        from .discovery import ipv4_host

        url = f"http://{ipv4_host(self.host)}{RPC_URI}"

        auth_header = self._build_auth_header(challenge) if challenge else None
        try:
            payload = self._post_with_backoff(url, raw, auth_header)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise ShellyUnreachable(f"{self.host}: HTTP {exc.code}") from exc
            # Le nonce memorise a peut-etre expire : on renegocie le
            # challenge, puis on rejoue une seule fois.
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
                    # Rejeu refuse avec un challenge frais : le mot de passe
                    # est faux, inutile d'insister.
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
        """Envoie la requete, en patientant si l'appareil demande a souffler.

        Un 429 n'est pas une panne : l'appareil limite son debit. Insister
        aussitot ne ferait que le braquer, et le signaler comme injoignable
        declencherait une resolution reseau inutile.
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
        """Construit l'en-tete Digest SHA-256 attendu par le firmware.

        C'est le calcul standard du RFC 7616, avec `ha2` derive de la methode
        et de l'URI. La documentation de Shelly decrit pour d'autres
        firmwares un `ha2` constant, calcule sur la chaine
        `dummy_method:dummy_uri` ; ce firmware-ci le refuse -- verifie sur
        l'appareil, seul le calcul standard est accepte.

        La difference n'est pas cosmetique : avec un `ha2` constant, la
        reponse ne depend pas de la requete, et un en-tete capture peut etre
        rejoue pour declencher une tout autre commande tant que le nonce
        vaut. Ici, la reponse est liee a la methode et a l'URI.
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

    # -------------------------------------------------------------- methodes

    def get_device_info(self) -> dict[str, Any]:
        return self.call("Shelly.GetDeviceInfo")

    def get_status(self) -> dict[str, Any]:
        return self.call("Shelly.GetStatus")

    def get_switch(self, switch_id: int) -> SwitchState:
        return SwitchState.from_rpc(self.call("Switch.GetStatus", {"id": switch_id}))

    def get_all_switches(self) -> dict[int, SwitchState]:
        """Lit toutes les prises en un seul appel Shelly.GetStatus."""
        status = self.get_status()
        states: dict[int, SwitchState] = {}
        for key, value in status.items():
            if key.startswith("switch:") and isinstance(value, dict):
                state = SwitchState.from_rpc(value)
                states[state.id] = state
        return states

    def set_switch(self, switch_id: int, on: bool) -> bool:
        """Change l'etat d'une prise ; renvoie l'etat precedent.

        Une sortie protegee ne peut pas etre coupee : la demande est
        refusee avant tout envoi sur le reseau.
        """
        if not on and switch_id in self.protected:
            raise ProtectedOutlet(self.host, switch_id)
        result = self.call("Switch.Set", {"id": switch_id, "on": bool(on)})
        return bool((result or {}).get("was_on", False))

    def protect(self, switch_ids: set[int]) -> None:
        """Declare les sorties qu'il ne faut jamais couper."""
        self.protected = set(switch_ids)

    def count_switches(self) -> int:
        return len(self.get_all_switches())

    # ------------------------------------------------------ authentification

    def set_password(self, realm: str, password: str) -> None:
        """Active l'authentification de l'appareil, ou la retire.

        Le firmware n'accepte pas le mot de passe lui-meme mais son
        condensat `ha1`, et impose l'utilisateur `admin`. Le realm est
        l'identifiant de l'appareil. Passer une chaine vide retire
        l'authentification.
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
        # L'appareil vient de changer de secret : le challenge memorise ne
        # vaut plus rien.
        with self._lock:
            self._auth_challenge = None
            self._nonce_count = 0
        self.password = password or None


def _parse_digest_challenge(header: str) -> dict[str, str]:
    """Extrait les parametres d'un en-tete WWW-Authenticate Digest."""
    if not header.lower().startswith("digest"):
        return {}
    params: dict[str, str] = {}
    for part in header[len("digest") :].split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        params[key.strip().lower()] = value.strip().strip('"')
    return params
