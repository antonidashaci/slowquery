import gzip

from slowquery import (
    group_queries,
    main,
    normalise,
    parse_log,
    read_shape,
    suggest_indexes,
)

LOG = """\
2026-10-01 09:00:00 UTC [101] LOG:  duration: 3.100 ms  statement: SELECT id FROM users WHERE email = 'a@b.co'
2026-10-01 09:00:01 UTC [101] LOG:  duration: 2.900 ms  statement: SELECT id FROM users WHERE email = 'c@d.co'
2026-10-01 09:00:02 UTC [102] LOG:  duration: 4100.000 ms  statement: SELECT * FROM orders ORDER BY created_at
2026-10-01 09:00:03 UTC [101] LOG:  duration: 5.000 ms  statement: SELECT id FROM users WHERE email = 'e@f.co'
"""


# --- normalisation -----------------------------------------------------------


def test_literals_collapse_to_one_shape():
    a = normalise("SELECT * FROM t WHERE email = 'a@b.co' AND age > 30")
    b = normalise("SELECT * FROM t WHERE email = 'x@y.zz' AND age > 7")
    assert a == b == "SELECT * FROM t WHERE email = ? AND age > ?"


def test_quote_inside_a_literal_does_not_break_the_shape():
    assert (normalise("SELECT 1 FROM t WHERE name = 'O''Brien' AND x = 2")
            == "SELECT ? FROM t WHERE name = ? AND x = ?")


def test_in_lists_of_different_length_are_one_shape():
    short = normalise("SELECT 1 FROM t WHERE id IN (1, 2)")
    long = normalise("SELECT 1 FROM t WHERE id IN (7, 8, 9, 10, 11)")
    assert short == long == "SELECT ? FROM t WHERE id IN (?)"


def test_multi_row_insert_is_one_shape():
    one = normalise("INSERT INTO t (a, b) VALUES (1, 2)")
    many = normalise("INSERT INTO t (a, b) VALUES (1, 2), (3, 4), (5, 6)")
    assert one == many


def test_bind_placeholders_and_comments():
    assert (normalise("SELECT a /* app: web */ FROM t WHERE id = $1 -- note")
            == "SELECT a FROM t WHERE id = ?")


def test_whitespace_and_trailing_semicolon():
    assert normalise("SELECT\n  1\nFROM t ;") == "SELECT ? FROM t"


# --- parsing the log ---------------------------------------------------------


def test_entries_and_durations():
    entries = parse_log(LOG)
    assert len(entries) == 4
    assert entries[2].duration == 4100.0
    assert entries[0].sql.startswith("SELECT id FROM users")


def test_a_multi_line_query_is_one_entry():
    log = (
        "2026-10-01 09:00:00 UTC LOG:  duration: 9.000 ms  statement: SELECT a\n"
        "\tFROM t\n"
        "\tWHERE b = 1\n"
        "2026-10-01 09:00:05 UTC LOG:  duration: 1.000 ms  statement: SELECT 2\n"
    )
    entries = parse_log(log)
    assert len(entries) == 2
    assert "WHERE b = 1" in entries[0].sql
    assert entries[1].sql == "SELECT 2"


def test_prepared_statement_execute_lines_are_read():
    log = ("2026-10-01 09:00:00 UTC LOG:  duration: 7.000 ms  "
           "execute <unnamed>: SELECT 1 FROM t WHERE id = $1\n")
    entries = parse_log(log)
    assert len(entries) == 1
    assert entries[0].duration == 7.0


def test_lines_without_a_duration_are_ignored():
    assert parse_log("2026-10-01 LOG:  connection authorized: user=app\n") == []


# --- aggregation -------------------------------------------------------------


def test_total_time_beats_worst_case_in_the_ranking():
    """A 4s report that ran once should lose to a 3ms query run 40k times."""
    log = "".join(
        f"2026-10-01 09:00:00 UTC LOG:  duration: 3.000 ms  statement: "
        f"SELECT id FROM users WHERE email = 'u{i}@x.co'\n"
        for i in range(2000)
    ) + ("2026-10-01 09:00:00 UTC LOG:  duration: 4100.000 ms  statement: "
         "SELECT * FROM orders ORDER BY created_at\n")
    groups = group_queries(parse_log(log))
    assert groups[0].calls == 2000
    assert groups[0].total == 6000.0
    assert groups[1].calls == 1


def test_group_statistics_and_slowest_example():
    groups = group_queries(parse_log(LOG))
    users = next(g for g in groups if "users" in g.shape)
    assert users.calls == 3
    assert users.total == 11.0
    assert users.worst == 5.0
    assert users.percentile(0.95) == 5.0
    assert "e@f.co" in users.slowest_example  # the actual values of the worst run


def test_percentile_uses_nearest_rank():
    group = group_queries(parse_log("".join(
        f"2026-10-01 09:00:00 UTC LOG:  duration: {i}.000 ms  statement: "
        f"SELECT {i} FROM t WHERE a = 1\n" for i in range(1, 21)
    )))[0]
    assert group.calls == 20
    assert group.percentile(0.5) == 10.0
    assert group.percentile(0.95) == 19.0


# --- reading the query -------------------------------------------------------


def test_equality_then_sort_then_range_is_the_index_order():
    sql = ("SELECT id FROM orders WHERE tenant_id = ? AND created_at > ? "
           "ORDER BY created_at LIMIT ?")
    shape = read_shape(sql)
    assert shape.equality == ["tenant_id"]
    assert shape.ranges == ["created_at"]
    assert suggest_indexes(sql) == [
        "CREATE INDEX CONCURRENTLY idx_orders_tenant_id_created_at "
        "ON orders (tenant_id, created_at);"
    ]


def test_a_column_is_not_repeated_in_the_index():
    sql = "SELECT 1 FROM t WHERE a = ? AND b > ? ORDER BY b"
    assert suggest_indexes(sql) == [
        "CREATE INDEX CONCURRENTLY idx_t_a_b ON t (a, b);"
    ]


def test_qualified_columns_and_in_lists_count_as_equality():
    shape = read_shape("SELECT 1 FROM t WHERE t.status IN (?) AND t.owner = ?")
    assert shape.equality == ["status", "owner"]


def test_join_columns_get_their_own_index():
    sql = ("SELECT o.id FROM orders o JOIN teams t ON t.owner_id = o.user_id "
           "WHERE o.status = ?")
    suggestions = suggest_indexes(sql)
    assert any("ON teams (owner_id)" in s for s in suggestions)
    assert any("idx_orders_status" in s for s in suggestions)


def test_a_join_on_id_is_not_suggested_because_a_primary_key_is_indexed():
    sql = "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE o.status = ?"
    assert suggest_indexes(sql) == [
        "CREATE INDEX CONCURRENTLY idx_orders_status ON orders (status);"
    ]


def test_a_function_call_needs_an_expression_index():
    notes = read_shape("SELECT 1 FROM users WHERE lower(email) = ?").notes
    assert any("lower(email)" in n and "expression" not in n for n in notes)
    assert suggest_indexes("SELECT 1 FROM users WHERE lower(email) = ?") == []


def test_or_is_reported_instead_of_being_indexed_wrongly():
    sql = "SELECT 1 FROM t WHERE a = ? OR b = ?"
    shape = read_shape(sql)
    assert shape.equality == []
    assert any("OR in the WHERE" in n for n in shape.notes)


def test_an_or_inside_a_subquery_does_not_disable_the_whole_clause():
    sql = "SELECT 1 FROM t WHERE a = ? AND id IN (SELECT id FROM u WHERE x = ? OR y = ?)"
    assert read_shape(sql).equality == ["a", "id"]


def test_hazard_notes():
    assert any("SELECT *" in n for n in read_shape("SELECT * FROM t WHERE a = ?").notes)
    assert any("no LIMIT" in n
               for n in read_shape("SELECT a FROM t ORDER BY b").notes)
    assert not any("no LIMIT" in n
                   for n in read_shape("SELECT a FROM t ORDER BY b LIMIT ?").notes)
    assert any("OFFSET" in n
               for n in read_shape("SELECT a FROM t ORDER BY b LIMIT ? OFFSET ?").notes)
    assert any("LIKE" in n for n in read_shape("SELECT a FROM t WHERE b LIKE ?").notes)


def test_a_query_with_no_filter_gets_no_index():
    assert suggest_indexes("SELECT count(*) FROM t") == []


# --- CLI ---------------------------------------------------------------------


def test_cli_reports_the_shapes(tmp_path, capsys):
    path = tmp_path / "pg.log"
    path.write_text(LOG, encoding="utf-8")
    assert main([str(path), "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "4 slow statements, 2 distinct shapes" in out
    assert "idx_users_email" in out
    assert "SELECT *" in out


def test_cli_reads_gzip(tmp_path, capsys):
    path = tmp_path / "pg.log.gz"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(LOG)
    assert main([str(path)]) == 0
    assert "distinct shapes" in capsys.readouterr().out


def test_cli_json(tmp_path, capsys):
    path = tmp_path / "pg.log"
    path.write_text(LOG, encoding="utf-8")
    assert main([str(path), "--json"]) == 0
    assert '"suggestions"' in capsys.readouterr().out


def test_empty_log_exits_1_with_a_hint(tmp_path, capsys):
    path = tmp_path / "pg.log"
    path.write_text("2026-10-01 LOG:  checkpoint complete\n", encoding="utf-8")
    assert main([str(path)]) == 1
    assert "log_min_duration_statement" in capsys.readouterr().err


def test_missing_file_exits_2(tmp_path):
    assert main([str(tmp_path / "nope.log")]) == 2


def test_aliased_where_column_goes_to_the_table_behind_the_alias():
    sql = ("SELECT u.ad, count(*) FROM urunler u JOIN siparis_kalem k "
           "ON k.urun_id = u.id WHERE k.tarih > ? GROUP BY u.ad")
    suggestions = suggest_indexes(sql)
    assert not any("ON urunler" in s for s in suggestions)
    assert "CREATE INDEX CONCURRENTLY idx_siparis_kalem_tarih ON siparis_kalem (tarih);" in suggestions
    assert "CREATE INDEX CONCURRENTLY idx_siparis_kalem_urun_id ON siparis_kalem (urun_id);" in suggestions
