"""Enable Banking config flow.

The bank redirects the browser back to Home Assistant's built-in
``/auth/external/callback`` view, which resumes this flow (see ``callback.py``
for why that view and not ``AbstractOAuth2FlowHandler``). No copying of URLs
by hand:

  1. async_step_user   — application_id / private key / ASPSP, and a check
                          that the credentials actually work
  2. async_step_auth   — external step: the browser goes to the bank, the
                          redirect comes back into this flow
  3. async_step_finish — exchange the code for a session, create the entry

Re-authorisation (the PSD2 consent lapses after at most 180 days, and there is
no refresh mechanism — a fresh SCA is mandatory) reuses steps 2+3 via
``async_step_reauth_confirm`` without asking for the credentials again.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .callback import (
    RedirectUriUnavailableError,
    async_encode_flow_state,
    async_get_redirect_uri,
)
from .client import (
    EnableBankingAuthError,
    EnableBankingClient,
    EnableBankingError,
)
from .const import (
    CONF_ACCOUNTS,
    CONF_APPLICATION_ID,
    CONF_ASPSP_COUNTRY,
    CONF_ASPSP_NAME,
    CONF_PRIVATE_KEY,
    CONF_PSU_TYPE,
    CONF_SESSION_ID,
    CONF_VALID_DAYS,
    CONF_VALID_UNTIL,
    DEFAULT_PSU_TYPE,
    DEFAULT_VALID_DAYS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_APPLICATION_ID, default=defaults.get(CONF_APPLICATION_ID, "")
            ): str,
            vol.Required(CONF_PRIVATE_KEY, default=defaults.get(CONF_PRIVATE_KEY, "")): (
                selector.TextSelector(selector.TextSelectorConfig(multiline=True))
            ),
            vol.Required(
                CONF_ASPSP_NAME, default=defaults.get(CONF_ASPSP_NAME, "C24")
            ): str,
            vol.Required(
                CONF_ASPSP_COUNTRY, default=defaults.get(CONF_ASPSP_COUNTRY, "DE")
            ): str,
            vol.Required(
                CONF_PSU_TYPE, default=defaults.get(CONF_PSU_TYPE, DEFAULT_PSU_TYPE)
            ): vol.In(["personal", "business"]),
            vol.Optional(
                CONF_VALID_DAYS,
                default=defaults.get(CONF_VALID_DAYS, DEFAULT_VALID_DAYS),
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=180)),
        }
    )


class EnableBankingConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the Enable Banking config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._user_input: dict[str, Any] = {}
        self._reauth_entry: config_entries.ConfigEntry | None = None
        self._external_data: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Step 1: credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            client = EnableBankingClient(
                self.hass,
                user_input[CONF_APPLICATION_ID],
                user_input[CONF_PRIVATE_KEY],
            )
            try:
                # Fails fast on a bad key or an inactive application, before
                # we send the user off to their bank.
                await client.get_application()
            except EnableBankingAuthError:
                errors["base"] = "invalid_auth"
            except EnableBankingError as err:
                _LOGGER.warning("Enable Banking application check failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                self._user_input = user_input
                return await self.async_step_auth()

        try:
            redirect_uri = async_get_redirect_uri(self.hass)
        except RedirectUriUnavailableError:
            return self.async_abort(reason="no_external_url")

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
            description_placeholders={"redirect_uri": redirect_uri},
        )

    # ------------------------------------------------------------------
    # Step 2: bank login, via Home Assistant's external callback view
    # ------------------------------------------------------------------

    async def async_step_auth(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            # Reached through /auth/external/callback. Do as little as
            # possible here — the browser is waiting on this request.
            self._external_data = user_input
            return self.async_external_step_done(next_step_id="finish")

        try:
            redirect_uri = async_get_redirect_uri(self.hass)
        except RedirectUriUnavailableError:
            return self.async_abort(reason="no_external_url")

        client = self._client()
        try:
            auth_url, _state = await client.start_auth(
                aspsp_name=self._user_input[CONF_ASPSP_NAME],
                aspsp_country=self._user_input[CONF_ASPSP_COUNTRY],
                redirect_url=redirect_uri,
                state=async_encode_flow_state(self.hass, self.flow_id),
                psu_type=self._user_input[CONF_PSU_TYPE],
                valid_days=self._user_input.get(CONF_VALID_DAYS, DEFAULT_VALID_DAYS),
            )
        except EnableBankingAuthError:
            return self._retry("invalid_auth")
        except EnableBankingError as err:
            _LOGGER.warning("Could not start Enable Banking auth: %s", err)
            return self._retry("cannot_connect")

        return self.async_external_step(step_id="auth", url=auth_url)

    # ------------------------------------------------------------------
    # Step 3: exchange the code, create or update the entry
    # ------------------------------------------------------------------

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if error := self._external_data.get("error"):
            # The ASPSP aborted the authorisation. Banks rarely send anything
            # beyond a bare code such as "server_error", so surface it as-is
            # rather than guessing at a cause.
            _LOGGER.warning("Bank aborted the authorisation: %s", error)
            return self._retry("bank_error", placeholders={"error": error})

        code = self._external_data.get("code")
        if not code:
            return self._retry("no_code")

        client = self._client()
        try:
            session_id, account_uids = await client.exchange_code(code)
        except EnableBankingError as err:
            _LOGGER.warning("Could not exchange code: %s", err)
            return self._retry("cannot_connect")

        if not account_uids:
            return self._retry("no_accounts")

        valid_until: str | None = None
        try:
            valid_until = (await client.get_session(session_id)).valid_until
        except EnableBankingError as err:
            # Not fatal: the entry works without it, we just lose the
            # expiry warning until the next successful setup.
            _LOGGER.warning("Could not read consent expiry: %s", err)

        entry_data = {
            **self._user_input,
            CONF_SESSION_ID: session_id,
            CONF_ACCOUNTS: account_uids,
            CONF_VALID_UNTIL: valid_until,
        }

        if self._reauth_entry:
            return self.async_update_reload_and_abort(
                self._reauth_entry, data=entry_data, reason="reauth_successful"
            )

        await self.async_set_unique_id(
            f"{self._user_input[CONF_APPLICATION_ID]}-{self._user_input[CONF_ASPSP_NAME]}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"{self._user_input[CONF_ASPSP_NAME]} ({len(account_uids)} accounts)",
            data=entry_data,
        )

    # ------------------------------------------------------------------
    # Re-authorisation (consent expired or about to)
    # ------------------------------------------------------------------

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        self._user_input = dict(entry_data)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is None:
            # A plain confirmation first: the external step opens the bank
            # immediately, which is jarring without a heads-up.
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "aspsp": self._user_input.get(CONF_ASPSP_NAME, "")
                },
            )
        return await self.async_step_auth()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _client(self) -> EnableBankingClient:
        return EnableBankingClient(
            self.hass,
            self._user_input[CONF_APPLICATION_ID],
            self._user_input[CONF_PRIVATE_KEY],
        )

    def _retry(
        self, error: str, placeholders: dict[str, str] | None = None
    ) -> FlowResult:
        """Send the user back to a form they can act on.

        During reauth there is no credentials form to return to — the entry
        already holds them — so retry from the confirmation step instead.
        """
        if self._reauth_entry:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
                errors={"base": error},
                description_placeholders={
                    "aspsp": self._user_input.get(CONF_ASPSP_NAME, ""),
                    **(placeholders or {}),
                },
            )
        try:
            redirect_uri = async_get_redirect_uri(self.hass)
        except RedirectUriUnavailableError:
            return self.async_abort(reason="no_external_url")

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(self._user_input),
            errors={"base": error},
            description_placeholders={
                "redirect_uri": redirect_uri,
                **(placeholders or {}),
            },
        )
