"""Preview or export raw rows from the SQLite research dataset."""

import argparse
import csv
import json
import sqlite3
import sys
from pathlib import Path


TABLES = ('runs', 'decision_points', 'action_choices', 'rollouts',
          'rollout_dataset', 'action_outcomes')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database', type=Path, nargs='?',
                        default=Path('datasets/socks_counterfactual.sqlite'))
    parser.add_argument('--table', choices=TABLES, default='rollout_dataset')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--format', choices=('table', 'json', 'csv'), default='table')
    parser.add_argument('--output', type=Path,
                        help='write JSON/CSV to a file instead of stdout')
    parser.add_argument('--sql', help='read-only SELECT query; overrides --table and --limit')
    args = parser.parse_args()
    if args.limit < 1:
        parser.error('--limit must be positive')
    if not args.database.exists():
        raise SystemExit(f'dataset does not exist: {args.database}')

    connection = sqlite3.connect(f'file:{args.database}?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    query = args.sql or f'SELECT * FROM {args.table} LIMIT ?'
    parameters = () if args.sql else (args.limit,)
    try:
        cursor = connection.execute(query, parameters)
        rows = [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as error:
        raise SystemExit(f'query failed: {error}') from error
    finally:
        connection.close()

    stream = args.output.open('w', newline='', encoding='utf-8') if args.output else sys.stdout
    try:
        if args.format == 'json':
            json.dump(rows, stream, indent=2)
            stream.write('\n')
        elif args.format == 'csv':
            if rows:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        elif rows:
            columns = list(rows[0])
            widths = {
                column: min(36, max(len(column), *(len(str(row[column])) for row in rows)))
                for column in columns
            }
            print(' | '.join(column[:widths[column]].ljust(widths[column])
                             for column in columns), file=stream)
            print('-+-'.join('-' * widths[column] for column in columns), file=stream)
            for row in rows:
                print(' | '.join(str(row[column])[:widths[column]].ljust(widths[column])
                                 for column in columns), file=stream)
        else:
            print('(no rows)', file=stream)
    finally:
        if args.output:
            stream.close()
            print(args.output.resolve())


if __name__ == '__main__':
    main()
