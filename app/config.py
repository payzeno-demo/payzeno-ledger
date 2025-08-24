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

    # -- messaging -----------------------------------------------------------------
    sns_ledger_topic_arn: str = (
        "arn:aws:sns:eu-west-1:000000000000:payzeno-ledger-events"
    )
    #: Local only — localstack. Must be unset in staging and production so boto3
    #: resolves the real endpoint.
    aws_endpoint_url: str | None = None

    worldflow_breaker_window: int = 100
    nordpay_base_url: str = "http://payzeno-acquirer-sandbox:9101"
    nordpay_acquirer_account: str = "payzeno-uk-1"
    retry_drain_batch_size: int = 50
    # -- observability -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    @classmethod
    @property
