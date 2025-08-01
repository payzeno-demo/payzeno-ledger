"""SQS consumer tests.

`BaseConsumer._claim_event` is insert-first by contract (ADR 0011: a dedupe guard around a
side effect is a single atomic write or it is nothing). These tests assert that shape
directly rather than asserting on the handler's observable effects, because a
check-then-act implementation passes the latter and fails the former.
"""
