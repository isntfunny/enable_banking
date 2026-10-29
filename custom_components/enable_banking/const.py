"""Constants for the Enable Banking integration."""

DOMAIN = "enable_banking"

# --- Config entry keys ---
CONF_APPLICATION_ID = "application_id"
CONF_PRIVATE_KEY = "private_key"  # PEM content (not a file path!) stored in entry
CONF_ASPSP_NAME = "aspsp_name"
CONF_ASPSP_COUNTRY = "aspsp_country"
CONF_PSU_TYPE = "psu_type"
CONF_PSU_IP = "psu_ip"
CONF_SESSION_ID = "session_id"
CONF_ACCOUNTS = "accounts"  # list[str] of account uids, filled in after auth
CONF_VALID_DAYS = "valid_days"
CONF_VALID_UNTIL = "valid_until"  # RFC3339, from access.valid_until of the session
CONF_ACCOUNT_NAMES = "account_names"  # dict[uid, friendly name override]

DEFAULT_PSU_TYPE = "personal"
DEFAULT_VALID_DAYS = 180  # matches maximum_consent_validity of most ASPSPs (15552000s)

# How long before the consent expires we start nagging via a repairs issue.
# PSD2 requires a fresh SCA to renew, so there is nothing to automate here —
# the user needs enough lead time to actually sit down and do the bank login.
RENEWAL_WARNING_DAYS = 14

# Six hours = four scheduled refresh cycles per day. Startup/reloads and
# bank-specific counting of endpoints/pages can consume additional quota.
# Scheduled polls never set PSU headers.
DEFAULT_UPDATE_INTERVAL = 360  # minutes
TRANSACTION_LOOKBACK_DAYS = 30

# --- Enable Banking API ---
API_BASE = "https://api.enablebanking.com"
JWT_ISSUER = "enablebanking.com"
JWT_AUDIENCE = "api.enablebanking.com"
JWT_TTL_SECONDS = 3600  # max allowed by the API is 86400 (24h)

# --- Entity attribute keys ---
ATTR_BANK = "bank"
ATTR_ACCOUNT_TYPE = "account_type"
ATTR_IBAN = "iban"
ATTR_BIC = "bic"
ATTR_PRODUCT = "product"

ACCOUNT_TYPE_BALANCE = "balance"
ACCOUNT_TYPE_CONSENT = "consent"

# Preferred balance_type order when multiple balances are returned for one
# account (Berlin Group BalanceStatus codes). First match wins as the
# "primary" balance sensor state; all returned balances are exposed as
# attributes regardless.
BALANCE_TYPE_PRIORITY = ("CLBD", "XPCD", "ITAV", "CLAV", "OTHR")

EVENT_NEW_TRANSACTION = "new_transaction"

ERROR_EXPIRED_SESSION = "EXPIRED_SESSION"

ISSUE_CONSENT_EXPIRING = "consent_expiring"
