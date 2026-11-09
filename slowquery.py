#!/usr/bin/env python3
"""Turn a Postgres slow query log into the two queries worth fixing.

The slowest query in the log is almost never the expensive one. A 4-second
report that runs twice a day costs 8 seconds; a 3 ms lookup that runs 40,000
times costs two minutes and holds a connection the whole time. Sorting by
duration shows you the first one. Sorting by *total* time shows you the bill.

So: group identical queries by shape, rank by total time, and read the WHERE
clause to propose the composite index -- in the column order that actually
works, which is equality, then sort, then range.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# `duration: 12.345 ms  statement: SELECT ...` / `... execute <unnamed>: SELECT`
DURATION = re.compile(
    r"duration:\s*([0-9.]+)\s*ms\s+(?:statement|execute\s+[^:]*|parse\s+[^:]*):\s*",
    re.I,
)
# A new log entry starts with a timestamp; anything else continues the SQL.
ENTRY_START = re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}")

STRING_LITERAL = re.compile(r"'(?:[^']|'')*'")
NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
BIND = re.compile(r"\$\d+")
IN_LIST = re.compile(r"\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)", re.I)
VALUES_LIST = re.compile(r"\bVALUES\s*(\(\s*\?(?:\s*,\s*\?)*\s*\)\s*,?\s*)+", re.I)


def normalise(sql: str) -> str:
    """Collapse a query to its shape, so two calls with different values match."""
    sql = STRING_LITERAL.sub("?", sql)
    sql = BIND.sub("?", sql)
    sql = NUMBER.sub("?", sql)
    sql = IN_LIST.sub("IN (?)", sql)
    sql = VALUES_LIST.sub("VALUES (?)", sql)
    sql = re.sub(r"--[^\n]*", " ", sql)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"\s+", " ", sql).strip().rstrip(";").strip()
    return sql


@dataclass
class Entry:
    duration: float
    sql: str


def parse_log(text: str) -> list[Entry]:
    """Pull `duration: N ms statement: ...` entries out of a Postgres log.

    Handles the multi-line case: a query that spans lines continues until the
    next line that starts with a timestamp, which is how Postgres writes it.
    """
    entries: list[Entry] = []
    pending: Entry | None = None
    for line in text.splitlines():
        match = DURATION.search(line)
        if match:
            if pending:
                entries.append(pending)
            pending = Entry(float(match.group(1)), line[match.end():])
            continue
        if pending is not None:
            if ENTRY_START.match(line):
                entries.append(pending)
                pending = None
            else:
                pending.sql += "\n" + line
    if pending:
        entries.append(pending)
    for entry in entries:
        entry.sql = entry.sql.strip()
    return entries


# --- aggregation -------------------------------------------------------------


@dataclass
class Group:
    shape: str
    durations: list[float] = field(default_factory=list)
    slowest_example: str = ""

    @property
    def calls(self) -> int:
        return len(self.durations)

    @property
    def total(self) -> float:
        return sum(self.durations)

    @property
    def mean(self) -> float:
        return self.total / self.calls if self.calls else 0.0

    @property
    def worst(self) -> float:
        return max(self.durations, default=0.0)

    def percentile(self, fraction: float) -> float:
        """Nearest-rank percentile: with 20 samples, p95 is the 19th.

        `round()` would give the 20th here, because round(19.5) is 20 -- the
        maximum masquerading as a percentile.
        """
        if not self.durations:
            return 0.0
        ordered = sorted(self.durations)
        rank = max(1, min(len(ordered), math.ceil(fraction * len(ordered))))
        return ordered[rank - 1]


def group_queries(entries: list[Entry]) -> list[Group]:
    groups: dict[str, Group] = {}
    for entry in entries:
        shape = normalise(entry.sql)
        group = groups.setdefault(shape, Group(shape))
        if entry.duration >= group.worst:
            group.slowest_example = entry.sql
        group.durations.append(entry.duration)
    return sorted(groups.values(), key=lambda g: -g.total)


# --- reading the WHERE clause ------------------------------------------------

EQUALITY_OPS = ("=", "IN", "IS")
RANGE_OPS = (">", "<", ">=", "<=", "BETWEEN")


def _split_top_level(text: str, token: str) -> list[str]:
    """Split on `token` outside parentheses, so a subquery stays whole."""
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    i = 0
    pattern = re.compile(rf"\b{token}\b", re.I)
    while i < len(text):
        char = text[i]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if depth == 0:
            match = pattern.match(text, i)
            if match:
                parts.append("".join(buf))
                buf = []
                i = match.end()
                continue
        buf.append(char)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


@dataclass
class Shape:
    tables: list[str] = field(default_factory=list)
    equality: list[str] = field(default_factory=list)
    ranges: list[str] = field(default_factory=list)
    order_by: list[str] = field(default_factory=list)
    joins: list[tuple[str, str]] = field(default_factory=list)  # (table, column)
    notes: list[str] = field(default_factory=list)
    # Column -> table, for columns written with a qualifier (`k.tarih`).
    # Unqualified columns are absent and belong to the first table.
    # ponytail: keyed by bare column name, so `a.x = ? AND b.x = ?` keeps only
    # the last owner. Key by (table, column) if that shape ever shows up.
    owner: dict[str, str] = field(default_factory=dict)


_NOT_ALIAS = (r"(?!(?:WHERE|JOIN|INNER|LEFT|RIGHT|FULL|CROSS|NATURAL|ON|USING|GROUP"
              r"|ORDER|LIMIT|OFFSET|FOR|UNION)\b)")


def read_shape(sql: str) -> Shape:
    """What the query asks of the planner: tables, filters, sort, hazards."""
    shape = Shape()
    upper = sql.upper()
    aliases: dict[str, str] = {}

    def own(qualifier: str | None, column: str) -> None:
        if qualifier:
            shape.owner[column] = aliases.get(qualifier.lower(), qualifier)

    for match in re.finditer(
        rf"\bFROM\s+([a-z_][\w.]*)(?:\s+(?:AS\s+)?{_NOT_ALIAS}([a-z_]\w*))?", sql, re.I
    ):
        shape.tables.append(match.group(1))
        aliases[(match.group(2) or match.group(1).split(".")[-1]).lower()] = match.group(1)
    for match in re.finditer(
        rf"\bJOIN\s+([a-z_][\w.]*)(?:\s+(?:AS\s+)?{_NOT_ALIAS}([a-z_]\w*))?", sql, re.I
    ):
        aliases[(match.group(2) or match.group(1).split(".")[-1]).lower()] = match.group(1)
    for match in re.finditer(
        r"\bJOIN\s+([a-z_][\w.]*)(?:\s+(?:AS\s+)?([a-z_]\w*))?\s+ON\s+(.+?)"
        r"(?=\bJOIN\b|\bWHERE\b|\bGROUP\b|\bORDER\b|\bLIMIT\b|$)",
        sql, re.I | re.S,
    ):
        table, alias, condition = match.group(1), match.group(2), match.group(3)
        shape.tables.append(table)
        prefix = (alias or table).split(".")[-1]
        for side in re.findall(rf"\b{re.escape(prefix)}\.(\w+)", condition):
            shape.joins.append((table, side))

    where = re.search(
        r"\bWHERE\b(.+?)(?=\bGROUP\s+BY\b|\bORDER\s+BY\b|\bLIMIT\b|\bOFFSET\b"
        r"|\bFOR\s+UPDATE\b|$)",
        sql, re.I | re.S,
    )
    if where:
        for predicate in _split_top_level(where.group(1), "AND"):
            # Only a top-level OR defeats a composite index; one inside a
            # subquery or a parenthesised group is that query's own problem.
            if len(_split_top_level(predicate, "OR")) > 1:
                shape.notes.append(
                    "an OR in the WHERE clause: one index per branch, or rewrite "
                    "it as UNION ALL -- a single composite index will not serve it"
                )
                continue
            column = re.match(r"\(?\s*([a-z_][\w]*)\s*\.\s*(\w+)|\(?\s*([a-z_]\w*)",
                              predicate, re.I)
            if func := re.match(r"\(?\s*(\w+)\s*\(\s*([\w.]+)\s*\)\s*(=|<|>)",
                                predicate, re.I):
                shape.notes.append(
                    f"{func.group(1)}({func.group(2)}) in the WHERE clause cannot "
                    f"use a plain index -- CREATE INDEX ... (({func.group(1)}"
                    f"({func.group(2)})))"
                )
                continue
            if not column:
                continue
            name = column.group(2) or column.group(3)
            own(column.group(1) if column.group(2) else None, name)
            rest = predicate[column.end():].strip()
            if re.match(r"(=|IN\b|IS\b)", rest, re.I):
                if name not in shape.equality:
                    shape.equality.append(name)
            elif re.match(r"(>=|<=|>|<|BETWEEN\b)", rest, re.I):
                if name not in shape.ranges:
                    shape.ranges.append(name)
            elif re.match(r"(NOT\s+)?LIKE\s*\?", rest, re.I):
                shape.notes.append(
                    f"LIKE on {name}: only indexable without a leading wildcard, "
                    "and only with a text_pattern_ops index (or pg_trgm)"
                )

    order = re.search(r"\bORDER\s+BY\b(.+?)(?=\bLIMIT\b|\bOFFSET\b|$)", sql, re.I | re.S)
    if order:
        for item in order.group(1).split(","):
            name = re.match(r"\s*(?:([a-z_]\w*)\s*\.\s*)?(\w+)", item, re.I)
            if name:
                shape.order_by.append(name.group(2))
                own(name.group(1), name.group(2))
        if "LIMIT" not in upper:
            shape.notes.append(
                "ORDER BY with no LIMIT: the whole result set is sorted before "
                "the first row is returned"
            )
    if re.match(r"\s*SELECT\s+\*", sql, re.I):
        shape.notes.append(
            "SELECT * blocks an index-only scan and ships columns nobody read"
        )
    if re.search(r"\bOFFSET\s+\?", sql, re.I):
        shape.notes.append(
            "OFFSET still reads and discards every skipped row -- keyset "
            "pagination (WHERE id > ?) does not"
        )
    return shape


def suggest_indexes(sql: str) -> list[str]:
    """Composite index candidates, in the one column order that works.

    Equality columns first -- they narrow the scan. Then the ORDER BY columns,
    so the sort disappears. Range columns last, because the first range column
    ends the usable prefix: nothing after it can be used for lookup.
    """
    shape = read_shape(sql)
    if not shape.tables:
        return []
    table = shape.tables[0]
    columns = (
        shape.equality
        + [c for c in shape.order_by if c not in shape.equality]
        + [c for c in shape.ranges if c not in shape.equality and c not in shape.order_by]
    )
    out: list[str] = []
    # One composite per table: a column filtered through an alias belongs to
    # the table behind that alias, not to whichever table came first.
    per_table: dict[str, list[str]] = {}
    for column in columns:
        per_table.setdefault(shape.owner.get(column, table), []).append(column)
    for owner, cols in per_table.items():
        name = f"idx_{owner.split('.')[-1]}_{'_'.join(cols)}"[:63]
        out.append(
            f"CREATE INDEX CONCURRENTLY {name} ON {owner} ({', '.join(cols)});"
        )
    seen = {(owner, cols[0]) for owner, cols in per_table.items()}
    for joined, column in shape.joins:
        # `id` is the primary key in every schema this would help, and a PK is
        # already indexed. Suggesting it would just be noise.
        if (joined, column) in seen or joined == table or column == "id":
            continue
        seen.add((joined, column))
        short = joined.split(".")[-1]
        out.append(
            f"CREATE INDEX CONCURRENTLY idx_{short}_{column} ON {joined} ({column});"
        )
    return out


# --- CLI ---------------------------------------------------------------------


def _read(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    if path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    return Path(path).read_text(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="slowquery",
        description="Group a Postgres slow query log by query shape, rank by "
                    "total time, and suggest the index.",
    )
    parser.add_argument("logs", nargs="+", help="log files, .gz, or - for stdin")
    parser.add_argument("--top", type=int, default=5, metavar="N",
                        help="how many query shapes to report (default 5)")
    parser.add_argument("--min-calls", type=int, default=1, metavar="N")
    parser.add_argument("--no-suggest", action="store_true",
                        help="skip the index suggestions")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    entries: list[Entry] = []
    for path in args.logs:
        try:
            entries.extend(parse_log(_read(path)))
        except OSError as exc:
            print(f"slowquery: {exc}", file=sys.stderr)
            return 2
    if not entries:
        print("slowquery: no `duration: ... ms  statement:` lines found. Is "
              "log_min_duration_statement set?", file=sys.stderr)
        return 1

    groups = [g for g in group_queries(entries) if g.calls >= args.min_calls]
    total = sum(g.total for g in groups) or 1.0

    if args.json:
        print(json.dumps([{
            "shape": g.shape,
            "calls": g.calls,
            "total_ms": round(g.total, 2),
            "mean_ms": round(g.mean, 2),
            "p95_ms": round(g.percentile(0.95), 2),
            "max_ms": round(g.worst, 2),
            "share": round(g.total / total, 4),
            "suggestions": [] if args.no_suggest else suggest_indexes(g.shape),
        } for g in groups[:args.top]], indent=2))
        return 0

    print(f"{len(entries)} slow statements, {len(groups)} distinct shapes, "
          f"{total / 1000:.1f}s total\n")
    for position, group in enumerate(groups[:args.top], 1):
        print(f"#{position}  {group.total / 1000:.2f}s total "
              f"({group.total / total:.0%})  {group.calls} "
              f"call{'' if group.calls == 1 else 's'}  "
              f"mean {group.mean:.1f}ms  p95 {group.percentile(0.95):.1f}ms  "
              f"max {group.worst:.1f}ms")
        print(f"    {_wrap(group.shape)}")
        if not args.no_suggest:
            for note in read_shape(group.shape).notes:
                print(f"    ! {note}")
            for suggestion in suggest_indexes(group.shape):
                print(f"    + {suggestion}")
        print()
    return 0


def _wrap(sql: str, width: int = 96) -> str:
    return sql if len(sql) <= width else sql[: width - 3] + "..."


if __name__ == "__main__":
    sys.exit(main())
