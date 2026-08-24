"""Bridge to Home Assistant's built-in external auth callback view.

The bank redirects the browser to Home Assistant's ``/auth/external/callback``
after the PSU has logged in. That view is registered unconditionally by the
``auth`` component, needs no authentication, and resumes the config flow whose
id is carried inside the (signed) ``state`` parameter — handling both the
``code`` and the ``error`` case.

Everything private to Home Assistant is isolated in this module on purpose:
``_encode_jwt`` and ``AUTH_CALLBACK_PATH`` are not public API. If a future
Home Assistant release renames them, this single import fails with a clear
message instead of the integration breaking in obscure ways.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.network import NoURLAvailableError, get_url

try:
    from homeassistant.helpers.config_entry_oauth2_flow import (  # noqa: PLC2701
        AUTH_CALLBACK_PATH,
        _encode_jwt,
    )
except ImportError as err:  # pragma: no cover - depends on HA internals
    raise ImportError(
        "This Home Assistant version no longer exposes the external auth "
        "callback helpers (_encode_jwt / AUTH_CALLBACK_PATH) that the Enable "
        "Banking integration relies on. Please report this against the "
        "integration."
    ) from err


class RedirectUriUnavailableError(Exception):
    """Raised when Home Assistant has no externally reachable URL.

    The bank must be able to redirect a browser back to this instance, so an
    internal-only URL is useless here.
    """


@callback
def async_get_redirect_uri(hass: HomeAssistant) -> str:
    """Return the redirect URI to register with the Enable Banking app."""
    try:
        base = get_url(hass, allow_internal=False)
    except NoURLAvailableError as err:
        raise RedirectUriUnavailableError from err
    return f"{base.rstrip('/')}{AUTH_CALLBACK_PATH}"


@callback
def async_encode_flow_state(hass: HomeAssistant, flow_id: str) -> str:
    """Encode the flow id the way the callback view expects to decode it.

    The secret backing this signature lives in ``hass.data`` and is recreated
    on restart — which is fine, because config flows do not survive a restart
    either.
    """
    return _encode_jwt(hass, {"flow_id": flow_id, "flow_type": "config_flow"})
