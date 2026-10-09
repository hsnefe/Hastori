# ADR 0001: TimescaleDB, not InfluxDB, for measurements

Status: accepted (day 1)

## Context

Seven devices publish four metrics every two seconds: about 14 readings a second, 1.2 million
rows a day. The API must answer per-device time series, a daily kWh per site in the site's time
zone, and it must never show one tenant's data to another. Alarms, rules, users and the audit log
live in a relational database anyway.

## Decision

Measurements go into a TimescaleDB hypertable (one-day chunks) in the same PostgreSQL as the
rest, with a one-minute continuous aggregate (real-time, so the latest minutes are included),
a 7-day retention policy on raw rows and 90 days on the aggregate.

## Why

- **One database, one transaction.** The outbox row that tells the alarm service about a reading
  is written in the same transaction as the reading itself. With a separate time-series store
  that guarantee needs a second mechanism.
- **Tenant isolation is a SQL `WHERE`.** Site scoping, joins to `devices` and `sites`, and the
  audit trail all use the tools and tests the rest of the API already has. InfluxDB would put a
  second query language and a second authorisation model next to it.
- **Plain SQL for the odd questions.** "A day of the site's time zone" is a join and a range, not
  a Flux task.
- At this volume (a few hundred megabytes a week) neither engine is stressed; the choice is
  about simplicity of the whole system, not raw ingest speed.

## Consequences

- Retention and the aggregate interact: refreshing the aggregate over a window older than the raw
  data deletes the materialised minutes (risk D1). Every refresh in this repository has both
  bounds and says so.
- Compression is off; with it on, `ON CONFLICT DO NOTHING` on `uq_measurements` slows down on
  compressed chunks (risk D3).
- Scaling out writes means Timescale multi-node or a different store; for this load it does not
  come up.
