"""Generate a compact HTML overview directly from the full SQLite dataset."""

import argparse
import html
import sqlite3
from pathlib import Path

from analysis.viz import STYLE


def rows(connection: sqlite3.Connection, query: str) -> list[dict]:
    connection.row_factory = sqlite3.Row
    return [dict(row) for row in connection.execute(query)]


def fmt(value) -> str:
    if value is None:
        return '—'
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return f'{value:,}'
    return f'{value:,.3f}' if abs(value) < 100 else f'{value:,.1f}'


def table(data: list[dict], columns: list[tuple[str, str]]) -> str:
    head = ''.join(f'<th>{html.escape(label)}</th>' for _, label in columns)
    body = ''.join(
        '<tr>' + ''.join(f'<td>{html.escape(fmt(row[key]))}</td>' for key, _ in columns) + '</tr>'
        for row in data
    )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def bars(data: list[dict], key: str, label_key: str, title: str) -> str:
    values = [float(row[key] or 0) for row in data]
    maximum = max(values, default=1) or 1
    items = ''.join(
        f'<div class="barrow"><span>{html.escape(str(row[label_key]))}</span>'
        f'<div class="track"><i style="width:{100 * float(row[key] or 0) / maximum:.2f}%"></i></div>'
        f'<b>{fmt(row[key])}</b></div>'
        for row in data
    )
    return f'<section class="card"><h2>{html.escape(title)}</h2><div class="bars">{items}</div></section>'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, nargs='?',
                        default=Path('datasets/socks_counterfactual.sqlite'))
    parser.add_argument('--output', type=Path, default=Path('results/dataset_overview.html'))
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f'dataset does not exist: {args.database}')
    connection = sqlite3.connect(f'file:{args.database}?mode=ro', uri=True)

    counts = {
        name: connection.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0]
        for name in ('runs', 'decision_points', 'action_choices', 'rollouts')
    }
    policy = rows(connection, '''
        SELECT r.policy AS label, COUNT(*) AS rollouts,
               AVG(o.household_embarrassment * 1.0 /
                   (r.roommates * o.effective_horizon)) AS embarrassment,
               AVG(o.household_spend * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_spend,
               AVG(o.household_sockless * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_sockless
        FROM rollouts o
        JOIN action_choices a ON a.id=o.action_id
        JOIN decision_points d ON d.id=a.decision_id
        JOIN runs r ON r.id=d.run_id
        GROUP BY r.policy ORDER BY r.policy
    ''')
    roommates = rows(connection, '''
        SELECT CAST(r.roommates AS TEXT) AS label, COUNT(*) AS rollouts,
               AVG(o.household_embarrassment * 1.0 /
                   (r.roommates * o.effective_horizon)) AS embarrassment,
               AVG(o.household_spend * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_spend,
               AVG(o.household_sockless * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_sockless
        FROM rollouts o
        JOIN action_choices a ON a.id=o.action_id
        JOIN decision_points d ON d.id=a.decision_id
        JOIN runs r ON r.id=d.run_id
        GROUP BY r.roommates ORDER BY r.roommates
    ''')
    budgets = rows(connection, '''
        SELECT CASE WHEN r.budget IS NULL THEN 'unlimited'
                    ELSE printf('$%g/person', r.budget/r.roommates) END AS label,
               CASE WHEN r.budget IS NULL THEN 1e30 ELSE r.budget/r.roommates END AS ordering,
               COUNT(*) AS rollouts,
               AVG(o.household_embarrassment * 1.0 /
                   (r.roommates * o.effective_horizon)) AS embarrassment,
               AVG(o.household_spend * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_spend,
               AVG(o.household_sockless * 360.0 /
                   (r.roommates * o.effective_horizon)) AS annual_sockless
        FROM rollouts o
        JOIN action_choices a ON a.id=o.action_id
        JOIN decision_points d ON d.id=a.decision_id
        JOIN runs r ON r.id=d.run_id
        GROUP BY label ORDER BY ordering
    ''')
    discards = rows(connection, '''
        WITH means AS (
            SELECT a.id, a.decision_id, a.wear_json,
                   json_array_length(a.discard_json) AS discard_count,
                   AVG(o.household_embarrassment) AS embarrassment,
                   AVG(o.household_spend) AS spend,
                   AVG(o.household_sockless) AS sockless
            FROM action_choices a JOIN rollouts o ON o.action_id=a.id GROUP BY a.id
        )
        SELECT m.discard_count AS label, COUNT(*) AS comparisons,
               AVG((m.embarrassment-n.embarrassment) * 1.0 /
                   (r.roommates*d.effective_horizon)) AS embarrassment_delta,
               AVG((m.spend-n.spend) * 360.0 /
                   (r.roommates*d.effective_horizon)) AS spend_delta,
               AVG((m.sockless-n.sockless) * 360.0 /
                   (r.roommates*d.effective_horizon)) AS sockless_delta
        FROM means m
        JOIN means n ON n.decision_id=m.decision_id
                    AND n.wear_json=m.wear_json AND n.discard_count=0
        JOIN decision_points d ON d.id=m.decision_id
        JOIN runs r ON r.id=d.run_id
        WHERE m.discard_count>0
        GROUP BY m.discard_count ORDER BY m.discard_count
    ''')
    coverage = rows(connection, '''
        SELECT roommates, capacity, policy,
               CASE WHEN budget IS NULL THEN 'unlimited'
                    ELSE printf('$%g', budget) END AS budget,
               COUNT(*) AS runs
        FROM runs GROUP BY roommates, capacity, policy, budget
        ORDER BY roommates, capacity, policy, budget
    ''')
    connection.close()

    cards = ''.join(
        f'<div class="stat"><small>{label}</small><b>{counts[key]:,}</b></div>'
        for key, label in (
            ('runs', 'Scenarios'), ('decision_points', 'Decision states'),
            ('action_choices', 'Legal actions'), ('rollouts', 'Recorded rollouts')
        )
    )
    outcome_columns = [
        ('label', 'Group'), ('rollouts', 'Rollouts'),
        ('embarrassment', 'Embarrassment/person/day'),
        ('annual_spend', 'Spend/person/360d'), ('annual_sockless', 'Sockless/person/360d')
    ]
    page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Full sock dataset overview</title><style>{STYLE}
.stats.four{{grid-template-columns:repeat(4,1fr)}}.charts{{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}}.bars{{display:grid;gap:10px}}.barrow{{display:grid;grid-template-columns:90px 1fr 70px;gap:10px;align-items:center;font-size:13px}}.track{{height:12px;background:#e7edef;border-radius:999px;overflow:hidden}}.track i{{display:block;height:100%;background:#187a72;border-radius:999px}}.barrow b{{text-align:right}}@media(max-width:850px){{.charts{{display:block}}.stats.four{{grid-template-columns:1fr 1fr}}}}
</style><main><h1>Full recorded dataset</h1><p class="intro muted">This page summarizes the complete SQLite dataset. Values are descriptive averages across all recorded legal actions and stochastic rollouts, not recommendations.</p>
<div class="stats four">{cards}</div>
<div class="explain"><strong>Normalization:</strong> embarrassment is per person per simulated day. Spending and sockless counts are divided by people and scaled to 360 days. Every legal action receives equal representation within each recorded decision state.</div>
<div class="charts">{bars(policy, 'embarrassment', 'label', 'Embarrassment by background policy')}{bars(policy, 'annual_spend', 'label', 'Replacement spend by policy')}{bars(policy, 'annual_sockless', 'label', 'Sockless exposure by policy')}</div>
<section class="card"><h2>Outcomes by background policy</h2>{table(policy, outcome_columns)}</section>
<section class="card"><h2>Outcomes by roommate count</h2>{table(roommates, outcome_columns)}</section>
<section class="card"><h2>Outcomes by budget per person</h2>{table(budgets, outcome_columns)}</section>
<section class="card"><h2>Matched effect of discarding</h2><p class="muted">Each discard action is compared with keeping the leftovers while wearing the same pair in the same state. Negative embarrassment is beneficial; positive spend or sockless values are costs.</p>{table(discards, [('label','Socks discarded'),('comparisons','Matched comparisons'),('embarrassment_delta','Δ embarrassment/person/day'),('spend_delta','Δ spend/person/360d'),('sockless_delta','Δ sockless/person/360d')])}</section>
<section class="card"><h2>Scenario coverage</h2>{table(coverage, [('roommates','Roommates'),('capacity','Capacity'),('policy','Policy'),('budget','Household budget'),('runs','Runs')])}</section>
<p class="note">Use the raw SQLite database for conditional analysis. Aggregating unlike states can hide causal relationships; matched comparisons and held-out validation are needed before turning a pattern into a strategy.</p></main></html>'''
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(page, encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
