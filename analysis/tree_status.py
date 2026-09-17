"""Print progress for a resumable decision-tree database."""

import argparse
import json
import sqlite3
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "database", nargs="?", type=Path,
        default=Path("datasets/sock_tree_sequential.sqlite"),
    )
    args = parser.parse_args()
    connection = sqlite3.connect(f"file:{args.database}?mode=ro", uri=True)
    statuses = dict(connection.execute(
        "SELECT status,COUNT(*) FROM scenarios GROUP BY status"
    ))
    counts = {
        table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("scenarios", "layers", "states", "chance_contexts", "transitions")
    }
    last_layers = [
        dict(zip(
            ("scenario_id", "last_complete_day", "transitions"), row, strict=True
        ))
        for row in connection.execute(
            """
            SELECT scenario_id,MAX(CASE WHEN status='complete' THEN day END),
                   SUM(transition_rows) FROM layers GROUP BY scenario_id ORDER BY scenario_id
            """
        )
    ]
    connection.close()
    print(json.dumps({
        "database": str(args.database.resolve()),
        "size_gib": args.database.stat().st_size / 1024**3,
        "statuses": statuses,
        "counts": counts,
        "scenario_progress": last_layers,
    }, indent=2))


if __name__ == "__main__":
    main()
