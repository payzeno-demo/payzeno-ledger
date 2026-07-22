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

    # -- database ------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://payzeno:payzeno@localhost:5432/payzeno_ledger",
        description="asyncpg DSN. Alembic rewrites it to the sync driver itself.",
    )
    #: Read the comment in .env.example before lowering this. `reconcile_batch` holds
    #: three sessions in one pass and `RetryDrainJob` takes a fourth, so the floor is
    #: 3 x (concurrent sweeps) + drains. Production is 20 across four tasks.
    database_pool_size: int = 20
    database_pool_max_overflow: int = 10
    #: SQL echo. Never on outside local compose — it logs bound parameters, and bound
    #: parameters on this service include merchant identifiers.
    database_echo: bool = False

    # -- internal auth -------------------------------------------------------------
    #: Shared with payzeno-api's INTERNAL_API_SECRET and payzeno-billing-legacy's
    #: PAYZENO_INTERNAL_SECRET. All three must match or nothing talks to us.
    internal_api_secret: str = "dev-internal-secret-do-not-use-anywhere-real"

    # -- messaging -----------------------------------------------------------------
    sns_ledger_topic_arn: str = (
        "arn:aws:sns:eu-west-1:000000000000:payzeno-ledger-events"
    )
    sqs_payments_queue_url: str = ""
    sqs_merchants_queue_url: str = ""
    aws_region: str = "eu-west-1"
    #: Local only — localstack. Must be unset in staging and production so boto3
    #: resolves the real endpoint.
    aws_endpoint_url: str | None = None

    # -- acquirers -----------------------------------------------------------------
    worldflow_base_url: str = "http://payzeno-acquirer-sandbox:9100"
    worldflow_api_key: str = "sandbox_wf_0000000000000000"
    #: Sent as `Payzeno-Acquirer-Account`. Worldflow routes settlement files by it and
    #: a wrong value fails open into another Payzeno account's file, so it is explicit
    #: config rather than something derived from the base URL.
    worldflow_acquirer_account: str = "payzeno-eu-1"
    #: BREAKER_WINDOW is a request COUNT, not a duration.
    worldflow_breaker_threshold_pct: int = 50
    worldflow_breaker_window: int = 100
    worldflow_breaker_reset_seconds: int = 30

    nordpay_base_url: str = "http://payzeno-acquirer-sandbox:9101"
    nordpay_api_key: str = "sandbox_np_0000000000000000"
    nordpay_acquirer_account: str = "payzeno-uk-1"
    nordpay_breaker_threshold_pct: int = 50
    nordpay_breaker_window: int = 100
    # -- reconciliation ------------------------------------------------------------
    reconcile_sweep_interval_seconds: int = 900
    #: Wall-clock budget for one sweep pass, added by PR #171. The guard transaction is
    #: committed and reopened at this cadence so the batch advisory lock is never held
    #: for more than half a minute; a pass that runs over finishes on the next tick.
    #: Without it a 5,000-item pass at 200-900ms per acquirer call held the lock for
    #: over an hour and starved the drain completely.
    reconcile_sweep_wall_budget_seconds: int = 30
    reconcile_max_items_per_run: int = 500
    #: The live ceiling `RetryScheduler` reads. `constants.MAX_ATTEMPTS` is only the
    #: default value baked into the module — it is not the read path, and collapsing the
    #: two would make this env var dead and the knob unturnable at 01:44.
    reconcile_max_attempts: int = 5
    reconcile_retry_backoff_base_seconds: int = 30

    # -- retry drain ---------------------------------------------------------------
    retry_drain_interval_seconds: int = 60
    retry_drain_batch_size: int = 50
    #: Staged-rollout leftover from PAY-1688. Defaults false and was never widened; on
    #: the night of PAY-2041 it was true on exactly ONE of four ledger tasks, which is
    #: why a 4,113-item backlog took twenty-one minutes to drain and why the drain
    #: overlapped the sweep at all. See docs/postmortems/2041-duplicate-settlement.md.
    retry_drain_enabled: bool = False

    payout_cutoff_same_day_ach_utc: str = "16:45"
    payout_cutoff_sepa_utc: str = "14:00"
    payout_cutoff_faster_payments_utc: str = "17:30"

    # -- periodic jobs that are not always on --------------------------------------
    ledger_audit_enabled: bool = True
    settlement_import_enabled: bool = True
    funding_match_tolerance_bps: int = 5

    # -- feature flags (read through app/flags.py::EnvFeatureFlags, never directly) --
    #: Gates the CloudWatch custom metric only. The `settlement.duplicate_detected`
    #: event is published unconditionally — a flag in front of the publish would
    #: silently disable the alarm the whole of PAY-2055 exists to produce.
    flag_duplicate_settlement_alarm: bool = True
    flag_payout_same_day_ach: bool = False
    #: arc PCI. Non-removable: compliance signed off on the assumption it is always on.
    flag_redact_pan_in_logs: bool = True

    # -- observability -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    otel_exporter_otlp_endpoint: str | None = None

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper(cls, value: object) -> object:
        """Accept ``info`` as well as ``INFO``.

        The ECS task definition and the compose file disagree about case and have since
        month two. Normalising here is cheaper than making them agree.
        """
        return value.upper() if isinstance(value, str) else value

    @field_validator("aws_endpoint_url", "otel_exporter_otlp_endpoint", mode="before")
    @classmethod
    def _blank_is_none(cls, value: object) -> object:
        """An empty string in the environment means "unset", not "endpoint ''".

        ECS renders an absent SSM parameter as an empty string rather than omitting the
        variable, and boto3 treats ``endpoint_url=""`` as a hard failure at first call
        rather than at startup.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def payout_cutoffs(self) -> dict[str, str]:
        """The four rail cutoffs keyed by ``payout_method``.

        Built here rather than in ``PayoutService`` so the rails and the scheduler read
        the same mapping and a new rail is one field plus one key.
        """
        return {
            "ach": self.payout_cutoff_ach_utc,
            "same_day_ach": self.payout_cutoff_same_day_ach_utc,
            "sepa": self.payout_cutoff_sepa_utc,
            "faster_payments": self.payout_cutoff_faster_payments_utc,
        }

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
