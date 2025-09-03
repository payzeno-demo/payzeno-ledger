"""``Settings`` — the one and only place this service reads the environment.

There is no ``os.environ.get`` anywhere else in ``app/``. Not in a module constant, not in
a class body, not in a worker's ``interval_seconds``. This is enforced by review and by
``ruff``'s ``TID`` rules, and the reason is operational rather than aesthetic: a value read
in a class body is frozen at import, and at 01:44 on the night of PAY-2041 the on-call
engineer needed ``RECONCILE_MAX_ATTEMPTS`` and ``RETRY_DRAIN_INTERVAL_SECONDS`` to change
without a redeploy. Everything that might ever need to move at 2am is a field here and is
read through the settings object at call time.

Every field maps to exactly one name in ``.env.example`` and every name in ``.env.example``
maps to exactly one field. ``interfaces.md`` §7 is the list; an env var nothing reads is a
dead node and a field with no env line is undocumented config.

Construction: ``get_settings()`` is memoised and is what ``create_app`` and
``app/ops/cli.py`` call. Tests build ``Settings(...)`` directly with overrides — pydantic
keeps the field defaults so a test only names what it cares about.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Typed view of the process environment.

    Field order mirrors ``.env.example`` so a diff of one is readable against the other.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # -- internal auth -------------------------------------------------------------
    #: Shared with payzeno-api's INTERNAL_API_SECRET and payzeno-billing-legacy's
    #: PAYZENO_INTERNAL_SECRET. All three must match or nothing talks to us.
    internal_api_secret: str = "dev-internal-secret-do-not-use-anywhere-real"

    # -- messaging -----------------------------------------------------------------
    sns_ledger_topic_arn: str = (
        "arn:aws:sns:eu-west-1:000000000000:payzeno-ledger-events"
    )
    #: Local only — localstack. Must be unset in staging and production so boto3
    #: resolves the real endpoint.
    aws_endpoint_url: str | None = None

    #: BREAKER_WINDOW is a request COUNT, not a duration.
    worldflow_breaker_threshold_pct: int = 50
    worldflow_breaker_window: int = 100
    nordpay_base_url: str = "http://payzeno-acquirer-sandbox:9101"
    nordpay_acquirer_account: str = "payzeno-uk-1"
    # -- retry drain ---------------------------------------------------------------
    retry_drain_interval_seconds: int = 60
    retry_drain_batch_size: int = 50
    payout_cutoff_faster_payments_utc: str = "17:30"

    # -- observability -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    @classmethod
    @property
    def alembic_database_url(self) -> str:
        """The same DSN with the sync driver, for Alembic.

        Alembic runs its own connection outside the app's engine and psycopg is the
        driver its autogenerate support is tested against.
        """
        return self.database_url.replace("+asyncpg", "").replace(
            "postgresql://", "postgresql+psycopg://", 1
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings object.

    Memoised because ``BaseSettings`` re-reads ``.env`` on every construction and the
    container, the CLI and the Alembic runner all want the same values. Tests that need
    different values construct ``Settings`` directly instead of clearing this cache.
    """
    return Settings()
