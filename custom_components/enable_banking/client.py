"""Async Enable Banking API client.

Field names and response shapes below are taken verbatim from real
requests/responses recorded against the production API (api.enablebanking.com)
while integrating a C24 Bank (DE) account — not just from the docs. Notable
quirks encoded here on purpose:

- ``POST /sessions`` returns ``accounts`` as a list of *account objects*
  (each containing a ``uid`` field), while ``GET /sessions/{id}`` returns
  ``accounts`` as a plain list of *uid strings* (with account objects moved
  to a separate ``accounts_data`` list, which does NOT carry iban/product).
  ``exchange_code()`` normalises both shapes to a flat list of uid strings.
- ``transaction_amount.amount`` is an unsigned magnitude string; sign must be
  derived from ``credit_debit_indicator`` ("CRDT" / "DBIT").
- ``balance_amount.amount`` is likewise a string, e.g. ``"75.00"``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any
import uuid

import jwt

from homeassistant.core import HomeAssistant

from .const import API_BASE, JWT_AUDIENCE, JWT_ISSUER, JWT_TTL_SECONDS

_LOGGER = logging.getLogger(__name__)


class EnableBankingError(Exception):
    """Base error for the Enable Banking API."""


class EnableBankingAuthError(EnableBankingError):
    """Raised on 401/403 — invalid JWT / application not active."""


class EnableBankingSessionExpiredError(EnableBankingError):
    """Raised when the AIS session/consent needs to be re-authorised."""


class EnableBankingRateLimitError(EnableBankingError):
    """Raised on 429 — ASPSP_RATE_LIMIT_EXCEEDED (background access cap)."""


# ---------------------------------------------------------------------------
# Response data models (mirroring the exact API field names)
# ---------------------------------------------------------------------------


@dataclass
class Amount:
    """``{"currency": "EUR", "amount": "75.00"}``."""

    currency: str
    amount: str

    @property
    def as_float(self) -> float:
        return float(self.amount)

    @classmethod
    def from_api(cls, data: dict[str, Any] | None) -> "Amount | None":
        if not data:
            return None
        return cls(currency=data.get("currency", ""), amount=str(data.get("amount", "0")))


@dataclass
class AccountBalance:
    """One entry of ``GET /accounts/{id}/balances`` -> ``balances[]``."""

    name: str | None
    balance_amount: Amount
    balance_type: str
    last_change_date_time: str | None = None
    reference_date: str | None = None
    last_committed_transaction: str | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AccountBalance":
        return cls(
            name=data.get("name"),
            balance_amount=Amount.from_api(data.get("balance_amount")) or Amount("EUR", "0"),
            balance_type=data.get("balance_type", "OTHR"),
            last_change_date_time=data.get("last_change_date_time"),
            reference_date=data.get("reference_date"),
            last_committed_transaction=data.get("last_committed_transaction"),
        )


@dataclass
class SessionInfo:
    """``GET /sessions/{id}`` — the parts we act on."""

    status: str
    account_uids: list[str]
    valid_until: str | None


@dataclass
class AccountDetails:
    """``GET /accounts/{id}/details`` response (flat, not wrapped)."""

    uid: str
    iban: str | None
    name: str | None  # account holder name
    product: str | None
    details: str | None
    currency: str | None
    usage: str | None
    cash_account_type: str | None
    bic: str | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "AccountDetails":
        account_id = data.get("account_id") or {}
        servicer = data.get("account_servicer") or {}
        return cls(
            uid=data["uid"],
            iban=account_id.get("iban"),
            name=data.get("name"),
            product=data.get("product"),
            details=data.get("details"),
            currency=data.get("currency"),
            usage=data.get("usage"),
            cash_account_type=data.get("cash_account_type"),
            bic=servicer.get("bic_fi"),
        )


@dataclass
class Transaction:
    """One entry of ``GET /accounts/{id}/transactions`` -> ``transactions[]``."""

    entry_reference: str | None
    transaction_id: str | None
    status: str  # "BOOK" | "PDNG"
    credit_debit_indicator: str  # "CRDT" | "DBIT"
    transaction_amount: Amount
    booking_date: str | None
    value_date: str | None
    remittance_information: list[str] = field(default_factory=list)
    creditor_name: str | None = None
    debtor_name: str | None = None
    bank_transaction_code: str | None = None

    @property
    def signed_amount(self) -> float:
        """Amount with sign applied (positive=incoming, negative=outgoing)."""
        value = self.transaction_amount.as_float
        return value if self.credit_debit_indicator == "CRDT" else -value

    @property
    def dedup_key(self) -> str:
        """Stable-ish id for deduplication between polls.

        Prefers ``entry_reference`` (unique per Enable Banking FAQ for BOOK
        status on most ASPSPs). Falls back to a composite of date/amount/
        remittance info for the rare ASPSP that omits it.
        """
        if self.entry_reference:
            return f"ref:{self.entry_reference}"
        parts = [
            self.booking_date or "",
            self.transaction_amount.amount,
            self.transaction_amount.currency,
            self.credit_debit_indicator,
            "|".join(self.remittance_information),
        ]
        return "composite:" + "|".join(parts)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Transaction":
        creditor = data.get("creditor") or {}
        debtor = data.get("debtor") or {}
        btc = data.get("bank_transaction_code") or {}
        btc_str = "/".join(
            filter(None, [btc.get("code"), btc.get("sub_code")])
        ) or None
        return cls(
            entry_reference=data.get("entry_reference"),
            transaction_id=data.get("transaction_id"),
            status=data.get("status", "BOOK"),
            credit_debit_indicator=data.get("credit_debit_indicator", "CRDT"),
            transaction_amount=Amount.from_api(data.get("transaction_amount"))
            or Amount("EUR", "0"),
            booking_date=data.get("booking_date"),
            value_date=data.get("value_date"),
            remittance_information=list(data.get("remittance_information") or []),
            creditor_name=creditor.get("name"),
            debtor_name=debtor.get("name"),
            bank_transaction_code=btc_str,
        )


@dataclass
class Aspsp:
    """One entry of ``GET /aspsps`` -> ``aspsps[]`` (only fields we use)."""

    name: str
    country: str
    bic: str | None
    psu_types: list[str] = field(default_factory=list)
    maximum_consent_validity: int | None = None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "Aspsp":
        return cls(
            name=data["name"],
            country=data["country"],
            bic=data.get("bic"),
            psu_types=list(data.get("psu_types") or []),
            maximum_consent_validity=data.get("maximum_consent_validity"),
        )


class EnableBankingClient:
    """Thin async wrapper around the Enable Banking REST API.

    One instance per config entry. JWT is (re-)signed per request (cheap,
    RS256 sign of a ~150 byte payload) so we never have to worry about
    expiry — this mirrors what api/reference recommends ("for each request a
    new token can be generated").
    """

    def __init__(
        self,
        hass: HomeAssistant,
        application_id: str,
        private_key_pem: str,
    ) -> None:
        self._hass = hass
        self._application_id = application_id
        self._private_key_pem = private_key_pem
        self._session = None  # set lazily via aiohttp_client helper

    @property
    def _client_session(self):
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        return async_get_clientsession(self._hass)

    def _make_jwt_sync(self) -> str:
        now = int(time.time())
        headers = {"typ": "JWT", "alg": "RS256", "kid": self._application_id}
        payload = {
            "iss": JWT_ISSUER,
            "aud": JWT_AUDIENCE,
            "iat": now,
            "exp": now + JWT_TTL_SECONDS,
        }
        return jwt.encode(
            payload, self._private_key_pem, algorithm="RS256", headers=headers
        )

    async def _auth_header(self) -> dict[str, str]:
        token = await self._hass.async_add_executor_job(self._make_jwt_sync)
        return {"Authorization": f"Bearer {token}"}

    @staticmethod
    def _psu_headers(psu_ip: str | None, psu_user_agent: str | None) -> dict[str, str]:
        """Headers that mark a request as "PSU actively present".

        Per finAPI's consent-frequency-validation docs (the XS2A backend
        behind C24 and many other German banks): requests WITHOUT any
        Psu-Ip-Address header count against the ASPSP's background-access
        limit (PSD2 RTS Art. 36(6), typically 4/day). Requests WITH it are
        exempt. Only set these when a human is actually triggering the call
        right now (e.g. a manual "reload" from the UI) — never from the
        regular polling coordinator.
        """
        headers: dict[str, str] = {}
        if psu_ip:
            headers["Psu-Ip-Address"] = psu_ip
        if psu_user_agent:
            headers["Psu-User-Agent"] = psu_user_agent
        return headers

    async def _get(
        self, path: str, *, params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {**await self._auth_header(), **(extra_headers or {})}
        async with self._client_session.get(
            f"{API_BASE}{path}", headers=headers, params=params, timeout=30
        ) as resp:
            return await self._handle_response(resp)

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {**await self._auth_header(), "Content-Type": "application/json"}
        async with self._client_session.post(
            f"{API_BASE}{path}", headers=headers, json=body, timeout=30
        ) as resp:
            return await self._handle_response(resp)

    @staticmethod
    async def _handle_response(resp) -> dict[str, Any]:
        if resp.status == 429:
            body = await resp.text()
            raise EnableBankingRateLimitError(body)
        if resp.status in (401, 403):
            body = await resp.text()
            raise EnableBankingAuthError(body)
        if resp.status >= 400:
            body = await resp.text()
            if "EXPIRED_SESSION" in body:
                raise EnableBankingSessionExpiredError(body)
            raise EnableBankingError(f"HTTP {resp.status}: {body}")
        return await resp.json()

    # ------------------------------------------------------------------
    # Application / ASPSP discovery
    # ------------------------------------------------------------------

    async def get_application(self) -> dict[str, Any]:
        """``GET /application`` — verifies the app is active, returns redirect_urls."""
        return await self._get("/application")

    async def list_aspsps(self, country: str) -> list[Aspsp]:
        data = await self._get("/aspsps", params={"country": country})
        return [Aspsp.from_api(a) for a in data.get("aspsps", [])]

    # ------------------------------------------------------------------
    # AIS auth flow
    # ------------------------------------------------------------------

    async def start_auth(
        self,
        *,
        aspsp_name: str,
        aspsp_country: str,
        redirect_url: str,
        state: str | None = None,
        psu_type: str = "personal",
        valid_days: int = 180,
        auth_method: str | None = None,
    ) -> tuple[str, str]:
        """``POST /auth`` — returns (auth_url, state) for the user to open.

        ``state`` is echoed back verbatim by the ASPSP on the redirect. The
        caller passes the value it needs to recognise the callback (Home
        Assistant encodes the config flow id in there); a random UUID is only
        used as a fallback when the caller does not care.

        ``auth_method`` picks one of the ASPSP's ``auth_methods`` (e.g.
        ``SMS_OTP``) when the bank offers several. Left unset, Enable Banking
        decides — which is the right default for most banks.
        """
        state = state or str(uuid.uuid4())
        valid_until = (
            datetime.now(timezone.utc) + timedelta(days=valid_days)
        ).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "access": {"valid_until": valid_until},
            "aspsp": {"name": aspsp_name, "country": aspsp_country},
            "state": state,
            "redirect_url": redirect_url,
            "psu_type": psu_type,
        }
        if auth_method:
            body["auth_method"] = auth_method
        data = await self._post("/auth", body)
        return data["url"], state

    async def exchange_code(self, code: str) -> tuple[str, list[str]]:
        """``POST /sessions`` — exchanges the callback ``code`` for a session.

        Returns (session_id, account_uids). Normalises the fact that this
        endpoint returns full account *objects* (unlike GET /sessions/{id},
        which returns plain uid strings) — see module docstring.
        """
        data = await self._post("/sessions", {"code": code})
        raw_accounts = data.get("accounts", [])
        uids = [a["uid"] if isinstance(a, dict) else a for a in raw_accounts]
        return data["session_id"], uids

    async def get_session(self, session_id: str) -> SessionInfo:
        """``GET /sessions/{id}`` — status, linked accounts and expiry.

        status is e.g. "AUTHORIZED". Raises EnableBankingSessionExpiredError
        via _handle_response if the consent has expired.

        ``valid_until`` comes from ``access.valid_until`` and is the moment the
        PSD2 consent lapses; it is the only warning we get, since there is no
        refresh mechanism.
        """
        data = await self._get(f"/sessions/{session_id}")
        access = data.get("access") or {}
        return SessionInfo(
            status=data.get("status", ""),
            account_uids=list(data.get("accounts", [])),
            valid_until=access.get("valid_until"),
        )

    # ------------------------------------------------------------------
    # Account data
    # ------------------------------------------------------------------

    async def get_account_details(
        self,
        account_id: str,
        *,
        psu_ip: str | None = None,
        psu_user_agent: str | None = None,
    ) -> AccountDetails:
        data = await self._get(
            f"/accounts/{account_id}/details",
            extra_headers=self._psu_headers(psu_ip, psu_user_agent),
        )
        return AccountDetails.from_api(data)

    async def get_balances(
        self,
        account_id: str,
        *,
        psu_ip: str | None = None,
        psu_user_agent: str | None = None,
    ) -> list[AccountBalance]:
        data = await self._get(
            f"/accounts/{account_id}/balances",
            extra_headers=self._psu_headers(psu_ip, psu_user_agent),
        )
        return [AccountBalance.from_api(b) for b in data.get("balances", [])]

    async def get_transactions(
        self,
        account_id: str,
        *,
        date_from: str,
        strategy: str = "default",
        psu_ip: str | None = None,
        psu_user_agent: str | None = None,
    ) -> list[Transaction]:
        """Fetches all pages via ``continuation_key`` (see Enable Banking FAQ)."""
        transactions: list[Transaction] = []
        params: dict[str, Any] = {"date_from": date_from, "strategy": strategy}
        headers = self._psu_headers(psu_ip, psu_user_agent)

        while True:
            data = await self._get(
                f"/accounts/{account_id}/transactions",
                params=params,
                extra_headers=headers,
            )
            transactions.extend(
                Transaction.from_api(t) for t in data.get("transactions", [])
            )
            continuation_key = data.get("continuation_key")
            if not continuation_key:
                break
            params = {**params, "continuation_key": continuation_key}

        return transactions
