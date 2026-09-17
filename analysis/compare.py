"""Matched, multi-seed four/five and pooled/separate comparison dashboards."""

import argparse
import json
from pathlib import Path

from analysis.sock_counterfactual import Simulator
from analysis.viz import write_comparison_html


def run_game(capacity: int, roommates: int, unit: int, days: int,
             seed: int, budget: float | None) -> dict:
    game = Simulator(capacity, roommates, unit, days, seed, budget)
    for _ in range(days):
        game.step()
    return {
        'daily_embarrassment': sum(game.scores) / (roommates * days),
        'annual_spend': game.spent / roommates * 360 / days,
        'annual_sockless': sum(game.sockless) / roommates * 360 / days,
        'end_drawer_fraction': len(game.drawer) / capacity,
    }


def compare_units(capacity: int, roommates: int, days: int,
                  budget: float | None, seeds: list[int]) -> dict:
    # Validate both units before doing any work.
    Simulator(capacity, roommates, 5, days, seeds[0], budget)
    runs = []
    for seed in seeds:
        runs.append({'seed': seed,
                     'a': run_game(capacity, roommates, 4, days, seed, budget),
                     'b': run_game(capacity, roommates, 5, days, seed, budget)})
    return {
        'kind': 'units', 'title': 'Four socks versus five',
        'labels': ['four', 'five'],
        'parameters': {'capacity': capacity, 'roommates': roommates, 'days': days,
                       'budget': 'unlimited' if budget is None else budget,
                       'seeds': len(seeds)},
        'note': 'Capacity, roommates, days, budget and seed set are held fixed. '
                'The hand size changes. Identical seed numbers do not imply identical '
                'draws after the two processes diverge; inspect the distribution across seeds.',
        'runs': runs,
    }


def compare_pooling(capacity: int, roommates: int, unit: int, days: int,
                    budget: float | None, seeds: list[int]) -> dict:
    if capacity % roommates:
        raise ValueError('pooled capacity must divide evenly across roommates')
    solo_capacity = capacity // roommates
    solo_budget = None if budget is None else budget / roommates
    Simulator(capacity, roommates, unit, days, seeds[0], budget)
    Simulator(solo_capacity, 1, unit, days, seeds[0], solo_budget)
    runs = []
    for seed in seeds:
        pooled = run_game(capacity, roommates, unit, days, seed, budget)
        solos = [run_game(solo_capacity, 1, unit, days,
                          seed * 1000003 + person, solo_budget)
                 for person in range(roommates)]
        separate = {key: sum(s[key] for s in solos) / roommates for key in pooled}
        runs.append({'seed': seed, 'a': pooled, 'b': separate})
    return {
        'kind': 'pooling', 'title': 'Shared drawer versus separate drawers',
        'labels': ['pooled', 'separate'],
        'parameters': {'pooled_capacity': capacity, 'solo_capacity': solo_capacity,
                       'roommates': roommates, 'unit': unit, 'days': days,
                       'pooled_budget': 'unlimited' if budget is None else budget,
                       'solo_budget': 'unlimited' if solo_budget is None else solo_budget,
                       'seeds': len(seeds)},
        'note': 'Socks and budget per person are matched. Each separate result averages '
                'n independent solo drawers; solo pack purchases remain discrete $10 events. '
                'The solo policy here is the same greedy policy, so this estimates a drawer '
                'effect under that policy, not the value of a solo player tracking its drawer.',
        'runs': runs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('units', 'pooling'))
    parser.add_argument('--capacity', '-C', type=int)
    parser.add_argument('--roommates', '-n', type=int, default=4)
    parser.add_argument('--unit', type=int, choices=(4, 5), default=4,
                        help='used for pooling; unit comparison always runs both')
    parser.add_argument('--days', type=int, default=360)
    parser.add_argument('--budget', type=float)
    parser.add_argument('--seeds', type=int, nargs='+', default=list(range(1, 11)))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--json', type=Path, help='optional raw data export')
    args = parser.parse_args()
    capacity = args.capacity or (40 if args.kind == 'units' else 64)
    if args.kind == 'units':
        report = compare_units(capacity, args.roommates, args.days, args.budget, args.seeds)
    else:
        report = compare_pooling(capacity, args.roommates, args.unit, args.days,
                                 args.budget, args.seeds)
    output = args.output or Path('results') / f'socks_compare_{args.kind}.html'
    write_comparison_html(report, output)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(output.resolve())


if __name__ == '__main__':
    main()
