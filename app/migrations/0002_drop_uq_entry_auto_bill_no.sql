-- 0002_drop_uq_entry_auto_bill_no.sql
--
-- Drops the partial unique index on entry.auto_bill_no.
-- In AMS, a multi-item GRN or multi-item receiving transaction creates one
-- inventory ledger row (Entry) per line item, all tagged with the same GRN
-- auto_bill_no. Enforcing uniqueness on entry(auto_bill_no) caused multi-item
-- GRNs to fail with IntegrityError (HTTP 500) and only save a single item.
--
-- Lookups on entry(auto_bill_no) are served by a regular non-unique index.

DROP INDEX IF EXISTS uq_entry_auto_bill_no;

CREATE INDEX IF NOT EXISTS ix_entry_auto_bill_no
    ON entry(auto_bill_no)
    WHERE auto_bill_no IS NOT NULL AND TRIM(auto_bill_no) <> '';
