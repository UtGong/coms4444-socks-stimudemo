"""Print progress and coverage for a recorded SQLite dataset."""

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, nargs='?',
                        default=Path('datasets/socks_counterfactual.sqlite'))
    args = parser.parse_args()
    if not args.database.exists():
        raise SystemExit(f'dataset does not exist: {args.database}')
    connection = sqlite3.connect(f'file:{args.database}?mode=ro', uri=True)
    tables = ('runs', 'decision_points', 'action_choices', 'rollouts')
    counts = {
        table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table in tables
    }
    complete, running = connection.execute(
        "SELECT SUM(status='complete'), SUM(status!='complete') FROM runs"
    ).fetchone()
    coverage = {
        'roommates': [row[0] for row in connection.execute(
            'SELECT DISTINCT roommates FROM runs ORDER BY roommates')],
        'capacities': [row[0] for row in connection.execute(
            'SELECT DISTINCT capacity FROM runs ORDER BY capacity')],
        'policies': [row[0] for row in connection.execute(
            'SELECT DISTINCT policy FROM runs ORDER BY policy')],
        'seeds': [row[0] for row in connection.execute(
            'SELECT DISTINCT seed FROM runs ORDER BY seed')],
        'budgets': [row[0] for row in connection.execute(
            'SELECT DISTINCT budget FROM runs ORDER BY budget')],
    }
    connection.close()
    print(json.dumps({
        'database': str(args.database),
        'size_mb': round(args.database.stat().st_size / 1024 / 1024, 2),
        **counts,
        'complete_runs': complete or 0,
        'incomplete_runs': running or 0,
        'coverage': coverage,
    }, indent=2))


if __name__ == '__main__':
    main()
