"""Generate the complete daily-choice and comparison dashboard set."""

import argparse
from pathlib import Path

from analysis.compare import compare_pooling
from analysis.sock_counterfactual import analyze
from analysis.viz import write_action_html, write_comparison_html, write_index_html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--budget', type=float, default=600)
    parser.add_argument('--roommates', type=int, default=4)
    parser.add_argument('--capacity', type=int, default=40,
                        help='capacity used for daily and four/five dashboards')
    parser.add_argument('--pool-capacity', type=int, default=64)
    parser.add_argument('--seed', type=int, default=1,
                        help='baseline seed for daily choice dashboards')
    parser.add_argument('--seeds', type=int, nargs='+', default=list(range(1, 11)),
                        help='seed set for comparison dashboards')
    parser.add_argument('--repeats', type=int, default=4)
    parser.add_argument('--horizon', type=int, default=7)
    parser.add_argument('--output-dir', type=Path, default=Path('results'))
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = analyze(args.capacity, args.roommates, 4, args.days, args.seed,
                     args.budget, samples=1, repeats=args.repeats,
                     horizon=args.horizon, every_day=True)
    write_action_html(report, args.output_dir / 'socks_choices.html')
    pooled = compare_pooling(args.pool_capacity, args.roommates, 4, args.days,
                             args.budget, args.seeds)
    write_comparison_html(pooled, args.output_dir / 'socks_compare_pooling.html')
    index = write_index_html(args.output_dir / 'index.html')
    print(index.resolve())


if __name__ == '__main__':
    main()
