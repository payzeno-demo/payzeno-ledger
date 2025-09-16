"""SNS publisher. Reached by ``OutboxDrainJob`` and by nothing else.

This class does not take a session and does not participate in a transaction — it is
the far side of the outbox, not an alternative to it. Publishing straight to SNS from a
business path is a review rejection; see ``docs/adr/0011-lock-ordering-in-the-money-path.md``
for why "the effect and its record must commit together" is a house rule here.
"""

from __future__ import annotations

import json
from typing import Any

import aioboto3

from payzeno_contracts.events import EVENT_SCHEMA_VERSIONS

from app.config import Settings
from app.errors import BusPublishError
from app.logging import get_logger
from app.ports import EventPublisher
from app.publishers.envelope import build_envelope, topic_for

logger = get_logger(__name__)


class SnsPublisher(EventPublisher):
    """Publishes a rendered envelope onto ``payzeno-ledger-events``.

    ``MessageAttributes.event_type`` is what the subscription filters key on, so it is
    set on every publish. Losing it does not fail the publish — it silently stops
    payzeno-api's notification queue receiving anything, which is much worse.
    """

    def __init__(self, settings: Settings, session_factory: Any | None = None) -> None:
        self._settings = settings
        self._topic_arn = settings.sns_ledger_topic_arn
        self._aws = session_factory or aioboto3.Session()

    async def publish(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        merchant_id: str | None,
        correlation_id: str,
        causation_id: str | None = None,
        livemode: bool = True,
        **_ignored: Any,
    ) -> str:
        envelope = build_envelope(
            event_type,
            payload,
            merchant_id=merchant_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            livemode=livemode,
            version=EVENT_SCHEMA_VERSIONS.get(event_type, 1),
        )
        body = json.dumps(_as_dict(envelope), default=str)
        await self.publish_raw(
            event_type=event_type,
            body=body,
            merchant_id=merchant_id,
            correlation_id=correlation_id,
        )
        return envelope.id

    async def publish_raw(
        self,
        *,
        event_type: str,
        body: str,
        merchant_id: str | None,
        correlation_id: str,
    ) -> str:
        """Publish an already-rendered outbox row.

        The drain must not rebuild the envelope: ``occurred_at`` was stamped when the
        fact became true, and re-stamping it at drain time breaks every consumer's
        ordering guard.
        """
        topic = topic_for(event_type)
        attributes = {
            "event_type": {"DataType": "String", "StringValue": event_type},
            "correlation_id": {"DataType": "String", "StringValue": correlation_id},
        }
        if merchant_id is not None:
            attributes["merchant_id"] = {"DataType": "String", "StringValue": merchant_id}

        try:
            async with self._aws.client(
                "sns",
                region_name=self._settings.aws_region,
                endpoint_url=self._settings.aws_endpoint_url or None,
            ) as sns:
                response = await sns.publish(
                    TopicArn=self._topic_arn,
                    Message=body,
                    MessageAttributes=attributes,
                )
        except Exception as exc:  # aioboto3 raises botocore ClientError subclasses
            logger.error("sns_publish_failed", event_type=event_type, error=str(exc))
            raise BusPublishError(
                f"failed to publish {event_type} to {topic}",
                code="internal_error",
                event_type=event_type,
                topic=topic,
            ) from exc

        message_id = str(response.get("MessageId", ""))
        logger.info(
            "sns_published",
            event_type=event_type,
            topic=topic,
            message_id=message_id,
            correlation_id=correlation_id,
        )
        return message_id


def _as_dict(envelope: Any) -> dict[str, Any]:
    dumper = getattr(envelope, "model_dump", None)
    if callable(dumper):
        return dict(dumper(mode="json"))
    return json.loads(json.dumps(envelope, default=str))
