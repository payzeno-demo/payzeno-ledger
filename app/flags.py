"""The flag registry — four flags, one accessor, no ``os.environ`` anywhere else.

``interfaces.md`` §8 is the table. Flags live here and are read through
:class:`~app.ports.FeatureFlags`; nothing branches on a raw env var.

======================================  ==================================  ========
Flag                                    Env                                 Default
======================================  ==================================  ========
``duplicate_settlement_alarm``          ``FLAG_DUPLICATE_SETTLEMENT_ALARM``  on
``payout_same_day_ach``                 ``FLAG_PAYOUT_SAME_DAY_ACH``         off
``redact_pan_in_logs``                  ``FLAG_REDACT_PAN_IN_LOGS``          on
======================================  ==================================  ========

Three, not four, at HEAD. ``reconcile_batch_lock_on_retry`` was added by PAY-2043 as a
no-deploy kill switch for the batch advisory lock on the retry path and **deleted by
PAY-2056 one week later**, when the lock became unconditional. It is listed in §8 and it
is intentionally absent from this file, from ``Settings`` and from ``.env.example``. If
you are reading this because you found a reference to it in a commit message or in
``docs/postmortems/2041-duplicate-settlement.md``, that is why.

Two things that are **not** flags and must never become flags:

* ``capture_at_settlement`` — a per-merchant column
  (``merchant_projection.capture_at_settlement`` / ``settlement_charge.capture_at_settlement``).
  ``SettlementPoster`` reads it off the **charge** row, never off the merchant row, and
  never through here.
* the ``settlement.duplicate_detected`` publish. ``duplicate_settlement_alarm`` gates the
  CloudWatch custom metric only. A flag in front of the event would silently disable the
  alarm the whole of PAY-2055 exists to produce.

Layering: L-1. Imports ``app.config`` and ``app.ports`` and nothing else.
"""

from __future__ import annotations

from typing import Final, Mapping

from app.config import Settings
from app.ports import FeatureFlags

__all__ = ["FLAG_NAMES", "EnvFeatureFlags", "StaticFeatureFlags", "env_name_for"]

#: Every flag this service knows about. An ``enabled()`` call for anything not in here
#: returns False and logs nothing — a typo'd flag name should fail closed, not crash a
#: settlement, and it should not be able to hide behind a default-true.
FLAG_NAMES: Final[frozenset[str]] = frozenset(
    {
        "duplicate_settlement_alarm",
        "payout_same_day_ach",
        "redact_pan_in_logs",
    }
)

#: Names accepted from an older deployment shape. PAY-2043's flag was rolled out with an
#: un-prefixed name on two tasks before the ``FLAG_`` convention was settled; the alias
#: table is kept because the convention has changed once and will change again.
ENV_ALIASES: Final[Mapping[str, str]] = {
    "reconcile_batch_lock_on_retry": "RECONCILE_RETRY_BATCH_LOCK",
}


def env_name_for(flag: str) -> str:
    """``duplicate_settlement_alarm`` -> ``FLAG_DUPLICATE_SETTLEMENT_ALARM``.

    The ``FLAG_`` prefix is load-bearing: it is how an operator greps a task definition
    for "what can I turn off right now" and gets an answer that is not the whole config.
    """
    return f"FLAG_{flag.upper()}"


class EnvFeatureFlags(FeatureFlags):
    """The production :class:`~app.ports.FeatureFlags`, backed by :class:`Settings`.

    It reads the settings *object*, not the environment, so there is still exactly one
    env reader in the service. The field name is derived from the flag name — flag
    ``payout_same_day_ach`` is ``Settings.flag_payout_same_day_ach`` from
    ``FLAG_PAYOUT_SAME_DAY_ACH`` — which is why adding a flag is one field plus one entry
    in :data:`FLAG_NAMES` and no branching anywhere.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def enabled(self, flag: str, *, merchant_id: str | None = None) -> bool:
        """True when ``flag`` is on.

        ``merchant_id`` is accepted and currently unused: none of the three live flags is
        per-merchant. It stays in the signature because taking it out is a change at
        every call site and putting it back is the same change again.
        """
        if flag not in FLAG_NAMES:
            return False
        return bool(getattr(self._settings, f"flag_{flag}", False))

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance only
        on = sorted(name for name in FLAG_NAMES if self.enabled(name))
        return f"EnvFeatureFlags(on={on})"


class StaticFeatureFlags(FeatureFlags):
    """A fixed mapping. The second implementation, and it is wired in production.

    ``app/ops/cli.py`` builds one so an operator command cannot accidentally pick up a
    task's flag state, and every service-layer test constructs one directly. Unknown
    flags are False, matching :class:`EnvFeatureFlags` — a test that passes ``{}`` gets
    the same fail-closed behaviour production has.
    """

    def enabled(self, flag: str, *, merchant_id: str | None = None) -> bool:
        """True when ``flag`` was explicitly set to a truthy value."""
        return bool(self._values.get(flag, False))

    def set(self, flag: str, value: bool) -> None:
        """Flip a flag in place. Used by the ops CLI's ``--flag name=on`` argument."""
        self._values[flag] = value

    def __repr__(self) -> str:  # pragma: no cover - debugging affordance only
        return f"StaticFeatureFlags({self._values!r})"
