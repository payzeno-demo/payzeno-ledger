"""The 19 concrete :class:`~app.domain.postings.PostingRule` implementations.

One class per canonical posting in `domain-model.md` §7, plus one per non-sale acquirer
line type in §8. They are re-exported through ``app.domain.postings`` — import them from
there, not from here, so ``POSTING_RULE_BY_LINE_TYPE`` stays the single dispatch point.

This module deliberately contains **no imports**. ``app.domain.postings`` imports these
submodules at the bottom of its own body, and an import here would close that cycle.
"""

__all__ = [
    "corrections",
    "disputes",
    "fees",
    "payouts",
    "sale",
]
