# slowquery

Group a Postgres slow query log by query shape, rank it by **total** time, and
propose the index.

```bash
python slowquery.py /var/log/postgresql/postgresql.log --top 3
```

```
1803 slow statements, 3 distinct shapes, 11.3s total

#1  6.12s total (54%)  1801 calls  mean 3.4ms  p95 3.8ms  max 3.8ms
    SELECT id, email FROM users WHERE tenant_id = ? AND email = ?
    + CREATE INDEX CONCURRENTLY idx_users_tenant_id_email ON users (tenant_id, email);

#2  4.21s total (37%)  1 call  mean 4210.0ms  p95 4210.0ms  max 4210.0ms
    SELECT * FROM orders o JOIN users u ON u.id = o.user_id WHERE o.status = ? AND o.created_at >...
    ! ORDER BY with no LIMIT: the whole result set is sorted before the first row is returned
    ! SELECT * blocks an index-only scan and ships columns nobody read
    + CREATE INDEX CONCURRENTLY idx_orders_status_created_at ON orders (status, created_at);

#3  0.98s total (9%)  1 call  mean 980.0ms  p95 980.0ms  max 980.0ms
    SELECT count(*) FROM events WHERE lower(source) = ?
    ! lower(source) in the WHERE clause cannot use a plain index -- CREATE INDEX ... ((lower(source)))
```

The slowest query in the log is almost never the expensive one. A 4-second
report that runs twice a day costs 8 seconds. A 3 ms lookup that runs 40,000
times costs two minutes and holds a connection for every one of them. Sorting
by duration — which is what reading the log does — shows you the first.
Sorting by total time shows you the bill.

## Install

```bash
curl -O https://raw.githubusercontent.com/antonidashaci/slowquery/main/slowquery.py
```

Python 3.9+. No dependencies, no database connection. Reads plain logs, `.gz`,
and stdin.

```bash
zcat postgresql-*.log.gz | python slowquery.py -
```

Needs `log_min_duration_statement` set to something (`200ms` is a reasonable
place to start). Reads both `statement:` and `execute <unnamed>:` lines, so
prepared statements from a connection pool are included.

## The index column order

This is the part worth getting right. For

```sql
SELECT id FROM orders WHERE tenant_id = ? AND created_at > ? ORDER BY created_at LIMIT ?
```

the suggestion is `(tenant_id, created_at)`, in that order, because:

1. **Equality columns first.** They narrow the scan to a contiguous range.
2. **Then the `ORDER BY` columns**, so the rows come out sorted and the sort
   node disappears — which is what makes `LIMIT` cheap instead of
   "sort 2M rows, return 20".
3. **Range columns last.** The first range column ends the usable prefix:
   nothing after it can be used for lookup, only as a filter.

Put the range column first and the index stops working for the equality — the
same columns, in the wrong order, is a different index.

## What else it reads out of the query

- `lower(email) = ?` — no plain index applies; it needs an expression index.
- A **top-level** `OR` — one composite index can't serve both branches. An `OR`
  inside a subquery is left alone, because it isn't this query's plan problem.
- `LIKE` — indexable only without a leading wildcard, and only with
  `text_pattern_ops` or `pg_trgm`.
- `ORDER BY` with no `LIMIT`, `SELECT *`, `OFFSET ?` pagination.
- Join columns get their own index — except `id`, which is already the primary
  key.

## Normalisation

Two calls with different values are one query. String literals, numbers, `$1`
binds and comments are replaced, and these all collapse to the same shape:

```sql
WHERE id IN (1, 2)                   -- }
WHERE id IN (7, 8, 9, 10, 11)        -- } one shape
INSERT INTO t VALUES (1,2),(3,4)     -- same as a single-row insert
WHERE name = 'O''Brien'              -- the escaped quote doesn't break it
```

A query spanning several lines is one entry: the SQL continues until the next
line that starts with a timestamp, which is how Postgres writes it.

`p95` is nearest-rank — with 20 samples it is the 19th, not the 20th.
`round(19.5)` would have given you the maximum wearing a percentile's name.

## As a library

```python
from slowquery import parse_log, group_queries, suggest_indexes, read_shape

for group in group_queries(parse_log(open("pg.log").read()))[:5]:
    print(group.total, group.calls, group.shape)
    print(group.slowest_example)       # the real values from the worst run
    print(suggest_indexes(group.shape), read_shape(group.shape).notes)
```

## Limits

It reads text, not your schema: it doesn't know which indexes already exist,
so a suggestion may already be in place, and it doesn't know row counts, so it
can't tell you whether an index is worth it. `--json` is there to feed
something that does.

## Tests

```bash
pytest
```

## License

MIT
