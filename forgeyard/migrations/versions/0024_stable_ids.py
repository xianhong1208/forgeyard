"""Ids that history refers to are never handed out again (1.6.1, issue #190). SQLite only; nothing on PostgreSQL.

Without AUTOINCREMENT, SQLite gives a new row the highest id in the table + 1, so after the newest row is deleted the
next one gets its id again. Rows that outlive what they name, without a foreign key, then mix two things up:
`gpu_usage_daily` summed a deleted deployment's GPU time and a new one's into one row, a new key took a deleted key's
usage, a pinned deployment would follow a removed machine's id to the next machine that joined. PostgreSQL's sequences
never go back, so it was never affected.

The tables rebuilt are those whose rows can be deleted while their id is kept somewhere without a foreign key:

- users: usage_daily.user_id, video_jobs.user_id, audit_events.actor_id, budget alerts (scope person), audit detail.
- teams: usage_daily.team_id, gpu_usage_daily.team_id, budget alerts (scope team), audit detail.
- api_keys: usage_daily.key_id, video_jobs.key_id (who may read a video job), budget alerts (scope key), snoozed
  Setup health findings (a person's preferences).
- nodes: gpu_usage_daily.node_id, join_tokens.node_id, workloads.spec.machine_id (a pinned deployment), audit detail,
  snoozed findings.
- workloads, models, model_downloads: audit detail (model_downloads also the cancel link /api/downloads/{id}).
- deployments: usage_daily.deployment_id, gpu_usage_daily.deployment_id, audit detail, snoozed findings.
- providers: budget alerts (scope provider), audit detail, snoozed findings.
- provider_models: usage_daily.target ("pm:<id>", which prices the usage), snoozed findings.
- gateway_routes: audit detail, snoozed findings.

Not rebuilt: tables whose rows are never deleted or whose ids nothing keeps (engine_trials are never deleted; the
history tables themselves are referred to by nobody).

Each table is rebuilt with SQLite's own procedure (https://sqlite.org/lang_altertable.html#otheralter): foreign keys
off, one transaction, a new table from the stored CREATE TABLE text with only `PRIMARY KEY (id)` moved onto the id
column as `PRIMARY KEY AUTOINCREMENT` (the rows are stored the same way: the id stays the rowid), every row copied, the
old table dropped, the new one renamed, its indexes created again from their stored text, then `foreign_key_check`
before the commit. Foreign keys must be off: with them on, dropping the old `deployments` would run its ON DELETE
CASCADE and delete every gateway route. Every column, foreign key, cascade rule, index and constraint is the same text
as before.

Then `sqlite_sequence` is set, per table, to the highest id ever seen: the table's own and every place above that
keeps one, so ids used by rows deleted before the upgrade are not given out either. History recorded before the
upgrade that already summed two things under one id cannot be split.

Going down rebuilds the same tables without AUTOINCREMENT (the same text as before the upgrade); ids may then be
reused again.

Revision ID: 0024_stable_ids
Revises: 0023_gpu_usage
Create Date: 2026-11-26 23:00:00.000000
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager

from alembic import op


revision = '0024_stable_ids'
down_revision = '0023_gpu_usage'
branch_labels = None
depends_on = None

TABLES = ('users', 'teams', 'api_keys', 'nodes', 'workloads', 'models', 'model_downloads', 'deployments',
          'providers', 'provider_models', 'gateway_routes')

_PLAIN_ID = '(\n\tid INTEGER NOT NULL, '
_AUTO_ID = '(\n\tid INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, '
_FIRST_CONSTRAINT = re.compile(r'\n\t(?:FOREIGN KEY|UNIQUE|CONSTRAINT|CHECK)\b')


def _with_autoincrement(sql: str) -> str:
    if _PLAIN_ID not in sql or sql.count('PRIMARY KEY (id)') != 1:
        raise RuntimeError(f"unexpected table definition: {sql[:80]!r}")
    sql = sql.replace(_PLAIN_ID, _AUTO_ID, 1)
    if '\n\tPRIMARY KEY (id), ' in sql:
        return sql.replace('\n\tPRIMARY KEY (id), ', '', 1)
    if sql.endswith(', \n\tPRIMARY KEY (id)\n)'):
        return sql[:-len(', \n\tPRIMARY KEY (id)\n)')] + '\n)'
    raise RuntimeError(f"unexpected primary key clause: {sql[:80]!r}")


def _without_autoincrement(sql: str) -> str:
    if _AUTO_ID not in sql:
        raise RuntimeError(f"unexpected table definition: {sql[:80]!r}")
    sql = sql.replace(_AUTO_ID, _PLAIN_ID, 1)
    m = _FIRST_CONSTRAINT.search(sql)
    if m:
        return sql[:m.start()] + '\n\tPRIMARY KEY (id), ' + sql[m.start():]
    return sql[:-len('\n)')] + ', \n\tPRIMARY KEY (id)\n)'


@contextmanager
def _foreign_keys_off(bind):
    """SQLite's rule for rebuilding a table others refer to: foreign keys off (outside any transaction), the rebuild in
    one transaction, foreign_key_check, commit, foreign keys back as they were. The check refuses a reference the
    rebuild broke; one that was broken before (a database that ran without foreign keys once) is left as it was."""
    raw = bind.connection.dbapi_connection
    if raw.in_transaction:      # the migrations before this one: done, so committed now (PRAGMA cannot run inside)
        raw.commit()
    was_on = raw.execute('PRAGMA foreign_keys').fetchone()[0]
    raw.execute('PRAGMA foreign_keys=OFF')
    try:
        raw.execute('BEGIN')
        try:
            before = set(raw.execute('PRAGMA foreign_key_check').fetchall())
            yield raw
            broken = set(raw.execute('PRAGMA foreign_key_check').fetchall()) - before
            if broken:
                raise RuntimeError(f"the rebuild broke references: {sorted(broken)[:5]}")
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
    finally:
        raw.execute(f"PRAGMA foreign_keys={'ON' if was_on else 'OFF'}")


def _rebuild(raw, table: str, transform) -> None:
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    new_sql = transform(sql)
    head = re.match(r'CREATE TABLE ("?)' + re.escape(table) + r'\1 \(', new_sql)
    if head is None:
        raise RuntimeError(f"unexpected table definition: {sql[:80]!r}")
    tmp = f'_fy_rebuild_{table}'
    indexes = [r[0] for r in raw.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL ORDER BY name",
        (table,))]
    cols = ', '.join(f'"{r[1]}"' for r in raw.execute(f'PRAGMA table_info("{table}")'))
    raw.execute(f'CREATE TABLE "{tmp}" (' + new_sql[head.end():])
    raw.execute(f'INSERT INTO "{tmp}" ({cols}) SELECT {cols} FROM "{table}"')
    raw.execute(f'DROP TABLE "{table}"')
    raw.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')   # SQLite stores the name quoted: "teams"
    for s in indexes:
        raw.execute(s)


def _max(raw, sql: str) -> int:
    v = raw.execute(sql).fetchone()[0]
    return int(v) if isinstance(v, (int, float)) else 0


def _audit_ids(raw, *keys: str) -> int:
    parts = [f"max(CASE WHEN json_type(detail, '$.{k}') = 'integer' THEN json_extract(detail, '$.{k}') END)"
             for k in keys]
    row = raw.execute(f"SELECT {', '.join(parts)} FROM audit_events").fetchone()
    return max([int(v) for v in row if v is not None] or [0])


def _snoozed_ids(raw) -> dict[str, int]:
    """Highest id per scope kind in the Setup health findings people snoozed: `<finding>@deployment:15`."""
    out: dict[str, int] = {}
    for (prefs,) in raw.execute('SELECT preferences FROM users'):
        try:
            snoozed = (json.loads(prefs) if isinstance(prefs, str) else prefs or {}).get('advisor_snoozed') or {}
        except (TypeError, ValueError, AttributeError):
            continue
        for key in snoozed if isinstance(snoozed, dict) else ():
            m = re.search(r'@([a-z-]+):(\d+)', str(key))
            if m:
                out[m.group(1)] = max(out.get(m.group(1), 0), int(m.group(2)))
    return out


def highest_ids(raw) -> dict[str, int]:
    """The highest id each table has ever used: its own rows and every place that keeps one without a foreign key."""
    snoozed = _snoozed_ids(raw)

    def own(t):
        return _max(raw, f'SELECT max(id) FROM {t}')

    def alert(scope):
        return _max(raw, f"SELECT max(scope_id) FROM budget_alerts WHERE scope = '{scope}'")

    return {
        'users': max(own('users'), _max(raw, 'SELECT max(user_id) FROM usage_daily'),
                     _max(raw, 'SELECT max(user_id) FROM video_jobs'),
                     _max(raw, 'SELECT max(actor_id) FROM audit_events'), alert('person'), _audit_ids(raw, 'user_id')),
        'teams': max(own('teams'), _max(raw, 'SELECT max(team_id) FROM usage_daily'),
                     _max(raw, 'SELECT max(team_id) FROM gpu_usage_daily'), alert('team'), _audit_ids(raw, 'team_id')),
        'api_keys': max(own('api_keys'), _max(raw, 'SELECT max(key_id) FROM usage_daily'),
                        _max(raw, 'SELECT max(key_id) FROM video_jobs'), alert('key'), snoozed.get('key', 0)),
        'nodes': max(own('nodes'), _max(raw, 'SELECT max(node_id) FROM gpu_usage_daily'),
                     _max(raw, 'SELECT max(node_id) FROM join_tokens'),
                     _max(raw, "SELECT max(json_extract(spec, '$.machine_id')) FROM workloads "
                               "WHERE json_type(spec, '$.machine_id') = 'integer'"),
                     _audit_ids(raw, 'machine_id', 'from_machine_id'), snoozed.get('machine', 0),
                     snoozed.get('gpu', 0)),
        'workloads': max(own('workloads'), _audit_ids(raw, 'workload_id')),
        'models': max(own('models'), _audit_ids(raw, 'model_id')),
        'model_downloads': max(own('model_downloads'), _audit_ids(raw, 'download_id')),
        'deployments': max(own('deployments'), _max(raw, 'SELECT max(deployment_id) FROM usage_daily'),
                           _max(raw, 'SELECT max(deployment_id) FROM gpu_usage_daily'),
                           _audit_ids(raw, 'deployment_id'), snoozed.get('deployment', 0)),
        'providers': max(own('providers'), alert('provider'), _audit_ids(raw, 'provider_id'),
                         snoozed.get('provider', 0)),
        'provider_models': max(own('provider_models'),
                               _max(raw, "SELECT max(CAST(substr(target, 4) AS INTEGER)) FROM usage_daily "
                                         "WHERE target LIKE 'pm:%'"),
                               snoozed.get('provider-model', 0)),
        'gateway_routes': max(own('gateway_routes'), _audit_ids(raw, 'route_id'), snoozed.get('route', 0)),
    }


def _is_autoincrement(raw, table: str) -> bool:
    sql = raw.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()[0]
    return 'AUTOINCREMENT' in sql


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        return
    with _foreign_keys_off(bind) as raw:
        for table in TABLES:
            if not _is_autoincrement(raw, table):
                _rebuild(raw, table, _with_autoincrement)
        for table, seq in highest_ids(raw).items():
            current = _max(raw, f"SELECT max(seq) FROM sqlite_sequence WHERE name = '{table}'")
            raw.execute('DELETE FROM sqlite_sequence WHERE name = ?', (table,))
            raw.execute('INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)', (table, max(seq, current)))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != 'sqlite':
        return
    with _foreign_keys_off(bind) as raw:
        for table in TABLES:
            if _is_autoincrement(raw, table):
                _rebuild(raw, table, _without_autoincrement)
