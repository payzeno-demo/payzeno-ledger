# Runbook — settlement import

Owner: nmigration · Pager: `#payzeno-alerts`

## The path

`SettlementImportJob` (3600s, gated by `SETTLEMENT_IMPORT_ENABLED`) →
`SettlementImportService.import_file` →
`ProcessorClient.fetch_settlement_file(acquirer, date)` → parser → `open_batch` →
`match_items` → items in `pending`.

Two parsers, one interface, and no plan to converge them:

- **Worldflow** files are CSV → `WorldflowCsvParser`
- **Nordpay** files are fixed-width → `LegacyFixedWidthParser`

There is also `POST /internal/v1/settlement-imports`, which payzeno-billing-legacy pushes
to (`import_legacy_records`). It predates `SettlementImportService` and duplicates about
forty lines of its matching logic. Files arriving that way do not go through the job.

## Matching

Strategies run in order and the first confident match wins:

1. `ExactReferenceMatch` — `processor_reference`
2. `NetworkTransactionMatch` — `network_transaction_id`
3. `HeuristicAmountWindowMatch` — amount + merchant + time window

Anything unmatched gets `charge_id = NULL` and settles into `OrphanedItemError` when the
poster reaches it. That is a real state, not a failure: fix it with
`POST /internal/v1/ops/items/{item_id}/match`, which takes the item advisory lock and
attaches a charge by hand. It refuses an already-`settled` item, because re-matching one
would orphan its transaction.

`ix_reconciliation_item_charge_id` is **non-unique on purpose**. A charge legitimately
appears in two batches — a representment after a chargeback reversal is the common case —
and a unique index there would reject correct files. Do not add one.

## "The batch total does not match the file"

`expected_total_minor` comes from the file's trailer; `posted_total_minor` accumulates as
items settle. `posted > expected` means duplicates and should be impossible since `0020` —
go to the reconciliation runbook's duplicate query. `posted < expected` means unsettled
items; list them:

```sql
SELECT status, count(*), sum(net_minor)
FROM   reconciliation_item
WHERE  batch_id = :batch_id
GROUP  BY status ORDER BY 2 DESC;
```

## Re-importing

`uq_settlement_batch_file` on `(acquirer, file_reference)` means a re-import of the same
file is refused rather than duplicated. To genuinely re-import, the batch has to be
cancelled first — there is no "force" flag and there should not be one.

## Levers

- `SETTLEMENT_IMPORT_ENABLED=false` — stops the job. Files accumulate at the acquirer;
  they are not lost.
- Batches only become reconcilable in `closed` or `partially_reconciled`
  (`RECONCILABLE_BATCH_STATUSES`). `BatchCloseJob` (3600s) does the transition;
  `POST /internal/v1/settlement-batches/{id}/close` does it by hand.
