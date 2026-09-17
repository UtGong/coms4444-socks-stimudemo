"""Enumerate a hand's decisions, then compare short stochastic futures.

This is an offline research tool. Its drawer state is an oracle used to evaluate
counterfactuals, not information passed to a tournament player.
"""

import argparse
import copy
import json
import math
import random
import statistics
from pathlib import Path
from dataclasses import dataclass
from itertools import combinations

from core.engine import (
    EMBARRASSMENT_THRESHOLD,
    HOLE_PROBABILITY,
    PACK_COST,
    PACK_SIZE,
    SOCKLESS_PENALTY,
)
from models.player import Selection
from models.sock import Color, Sock, pristine

from analysis.viz import write_action_html


def actions(count: int) -> list[Selection]:
    """All legal actions for a full hand; 24 for four, 80 for five."""
    if count < 2:
        return []
    result = []
    for wear in combinations(range(count), 2):
        remaining = [i for i in range(count) if i not in wear]
        for mask in range(1 << len(remaining)):
            discard = tuple(i for bit, i in enumerate(remaining) if mask & (1 << bit))
            result.append(Selection(wear=wear, discard=discard))
    return result


def greedy(shades: tuple[int, ...], budget_remaining: float) -> Selection:
    """Match players.greedy_player, including its one-leftover discard rule."""
    wear = min(combinations(range(len(shades)), 2), key=lambda p: abs(shades[p[0]] - shades[p[1]]))
    discard = ()
    if budget_remaining >= PACK_COST:
        average = (shades[wear[0]] + shades[wear[1]]) / 2
        remaining = [i for i in range(len(shades)) if i not in wear]
        if remaining:
            worst = max(remaining, key=lambda i: abs(shades[i] - average))
            if abs(shades[worst] - average) > EMBARRASSMENT_THRESHOLD:
                discard = (worst,)
    return Selection(wear=wear, discard=discard)


def policy_choice(name: str, shades: tuple[int, ...], budget_remaining: float,
                  rng: random.Random) -> Selection:
    """Background policies used to reach a wider variety of drawer states."""
    if name == 'greedy':
        return greedy(shades, budget_remaining)
    if name == 'conserve':
        wear = min(combinations(range(len(shades)), 2),
                   key=lambda p: abs(shades[p[0]] - shades[p[1]]))
        return Selection(wear=wear, discard=())
    if name == 'aggressive':
        wear = min(combinations(range(len(shades)), 2),
                   key=lambda p: abs(shades[p[0]] - shades[p[1]]))
        return Selection(wear=wear,
                         discard=tuple(i for i in range(len(shades)) if i not in wear))
    if name == 'random':
        wear = tuple(rng.sample(range(len(shades)), 2))
        return Selection(wear=wear, discard=())
    raise ValueError(f'unknown background policy: {name}')


@dataclass
class Point:
    sim: 'Simulator'
    offered: list[Sock]
    roommate: int
    remaining_order: list[int]
    returning: list[Sock]


class Simulator:
    """Minimal serial engine: daily no-replacement draw, delayed returns, packs."""

    def __init__(self, capacity: int, roommates: int, unit: int, days: int,
                 seed: int, budget: float | None = None,
                 background_policy: str = 'greedy'):
        if roommates < 1 or unit not in (4, 5) or days < 1:
            raise ValueError('roommates and days must be positive; unit must be 4 or 5')
        if capacity % 4 or capacity <= unit * roommates + 10:
            raise ValueError('C must be a multiple of 4 and exceed unit * n + 10')
        if budget is not None and budget < 0:
            raise ValueError('budget must be nonnegative')
        if background_policy not in ('greedy', 'conserve', 'aggressive', 'random'):
            raise ValueError(f'unknown background policy: {background_policy}')
        self.capacity, self.roommates, self.unit, self.days = capacity, roommates, unit, days
        self.budget = budget
        self.background_policy = background_policy
        self.rng = random.Random(seed)
        self.drawer = [pristine(Color.WHITE) for _ in range(capacity // 2)]
        self.drawer += [pristine(Color.BLACK) for _ in range(capacity // 2)]
        self.pending = {Color.WHITE: 0, Color.BLACK: 0}
        self.spent = 0.0
        self.day = 0
        self.scores = [0.0] * roommates
        self.sockless = [0] * roommates
        self.holes = {Color.WHITE: 0, Color.BLACK: 0}
        self.discards = {Color.WHITE: 0, Color.BLACK: 0}
        self.last_offered: dict[int, tuple[int, ...]] = {}
        self.last_actions: dict[int, Selection] = {}
        self.last_daily_scores: dict[int, float] = {}

    @property
    def budget_remaining(self) -> float:
        return float('inf') if self.budget is None else self.budget - self.spent

    def clone(self) -> 'Simulator':
        return copy.deepcopy(self)

    def _draw(self) -> list[Sock]:
        picked = self.rng.sample(range(len(self.drawer)), min(self.unit, len(self.drawer)))
        picked.sort(reverse=True)
        return [self.drawer.pop(i) for i in picked]

    def _discard(self, sock: Sock, hole: bool = False) -> None:
        self.pending[sock.color] += 1
        self.discards[sock.color] += 1
        if hole:
            self.holes[sock.color] += 1

    def _dress(self, roommate: int, offered: list[Sock], returning: list[Sock],
               choice: Selection | None = None) -> None:
        self.last_offered[roommate] = tuple(sock.shade for sock in offered)
        if len(offered) < 2:
            self.scores[roommate] += SOCKLESS_PENALTY
            self.sockless[roommate] += 1
            self.last_daily_scores[roommate] = SOCKLESS_PENALTY
            returning.extend(offered)
            return
        shades = tuple(sock.shade for sock in offered)
        choice = choice or policy_choice(
            self.background_policy, shades, self.budget_remaining, self.rng
        )
        wear = choice.wear
        if len(wear) != 2 or wear[0] == wear[1] or any(i not in range(len(offered)) for i in wear):
            raise ValueError('illegal wear indices')
        if any(i not in range(len(offered)) or i in wear for i in choice.discard):
            raise ValueError('illegal discard indices')
        difference = abs(shades[wear[0]] - shades[wear[1]])
        score = difference if difference > EMBARRASSMENT_THRESHOLD else 0
        self.scores[roommate] += score
        self.last_daily_scores[roommate] = score
        self.last_actions[roommate] = choice
        for i in wear:
            sock = offered[i]
            if sock.worn_out and self.rng.random() < HOLE_PROBABILITY:
                self._discard(sock, hole=True)
            else:
                returning.append(sock.washed())
        for i, sock in enumerate(offered):
            if i in wear:
                continue
            if i in choice.discard:
                self._discard(sock)
            else:
                returning.append(sock)

    def _finish(self, returning: list[Sock]) -> None:
        self.drawer.extend(returning)
        for color in (Color.WHITE, Color.BLACK):
            packs = self.pending[color] // PACK_SIZE
            if self.budget is not None:
                packs = min(packs, int(self.budget_remaining // PACK_COST))
            if packs:
                self.pending[color] -= packs * PACK_SIZE
                self.drawer.extend(pristine(color) for _ in range(packs * PACK_SIZE))
                self.spent += packs * PACK_COST

    def step(self) -> None:
        if self.day >= self.days:
            raise ValueError('simulation has ended')
        self.day += 1
        self.last_offered, self.last_actions, self.last_daily_scores = {}, {}, {}
        order = list(range(self.roommates))
        self.rng.shuffle(order)
        returning: list[Sock] = []
        for roommate in order:
            self._dress(roommate, self._draw(), returning)
        self._finish(returning)

    def pause_at(self, focal: int) -> Point:
        """Begin a day and pause after focal's hand is removed from drawer."""
        if self.day >= self.days or focal not in range(self.roommates):
            raise ValueError('invalid focal roommate or ended simulation')
        self.day += 1
        self.last_offered, self.last_actions, self.last_daily_scores = {}, {}, {}
        order = list(range(self.roommates))
        self.rng.shuffle(order)
        returning: list[Sock] = []
        for position, roommate in enumerate(order):
            offered = self._draw()
            if roommate == focal:
                return Point(self, offered, focal, order[position + 1:], returning)
            self._dress(roommate, offered, returning)
        raise AssertionError('focal roommate not scheduled')

    def resume(self, point: Point, choice: Selection) -> None:
        self._dress(point.roommate, point.offered, point.returning, choice)
        for roommate in point.remaining_order:
            self._dress(roommate, self._draw(), point.returning)
        self._finish(point.returning)


def metric_delta(before: Simulator, after: Simulator) -> dict:
    return {
        'household_embarrassment': sum(after.scores) - sum(before.scores),
        'focal_embarrassment': after.scores[0] - before.scores[0],
        'spend': after.spent - before.spent,
        'sockless_days': sum(after.sockless) - sum(before.sockless),
    }


def dominates(left: dict, right: dict) -> bool:
    """Three-objective Pareto dominance: embarrassment, dollars, sockless."""
    keys = ('household_embarrassment', 'spend', 'sockless_days')
    a, b = left['mean'], right['mean']
    return all(a[key] <= b[key] for key in keys) and any(a[key] < b[key] for key in keys)


def analyze(capacity: int, roommates: int, unit: int, days: int, seed: int,
            budget: float | None, samples: int, repeats: int, horizon: int,
            every_day: bool = False) -> dict:
    """Evaluate all hand choices at evenly spaced baseline days.

    Future RNG streams share a repeat seed across actions. Their *events* can
    still diverge as the actions change the drawer and hole RNG consumption.
    """
    if min(samples, repeats, horizon) < 1:
        raise ValueError('samples, repeats, and horizon must be positive')
    baseline = Simulator(capacity, roommates, unit, days, seed, budget)
    if every_day:
        # Near the end of the run, effective_horizon becomes shorter because
        # no simulated days exist beyond the configured game length.
        chosen_days = list(range(1, days + 1))
    else:
        last_sample_day = max(1, days - horizon + 1)
        chosen_days = sorted({1 + round((last_sample_day - 1) * i / max(1, samples - 1))
                              for i in range(samples)})
    rows = []
    for day in range(1, days + 1):
        point = baseline.pause_at(0)
        has_pair = len(point.offered) >= 2
        # _dress handles a short hand before reading the selection. This empty
        # marker lets the baseline continue without pretending a legal action
        # exists on a sockless turn.
        base_choice = (
            greedy(tuple(s.shade for s in point.offered), baseline.budget_remaining)
            if has_pair else Selection(wear=(), discard=())
        )
        if day in chosen_days and has_pair:
            before = baseline.clone()
            entries = []
            for choice in actions(len(point.offered)):
                trial_values = {'household_embarrassment': [], 'focal_embarrassment': [],
                                'spend': [], 'sockless_days': []}
                for repeat in range(repeats):
                    trial = before.clone()
                    trial.rng.seed(seed * 1000003 + day * 1009 + repeat)
                    branch = Point(trial, list(point.offered), point.roommate,
                                   list(point.remaining_order), list(point.returning))
                    trial.resume(branch, choice)
                    for _ in range(min(horizon - 1, days - trial.day)):
                        trial.step()
                    outcome = metric_delta(before, trial)
                    for name, value in outcome.items():
                        trial_values[name].append(value)
                entries.append({
                    'wear': choice.wear, 'discard': choice.discard,
                    'immediate_embarrassment': (lambda d: d if d > 6 else 0)(
                        abs(point.offered[choice.wear[0]].shade - point.offered[choice.wear[1]].shade)),
                    'mean': {name: statistics.mean(values)
                             for name, values in trial_values.items()},
                    'standard_error': {name: statistics.stdev(values) / math.sqrt(repeats)
                                       if repeats > 1 else None
                                       for name, values in trial_values.items()},
                })
            entries.sort(key=lambda row: (row['mean']['household_embarrassment'],
                                          row['mean']['spend']))
            frontier = [row for row in entries if not any(
                dominates(other, row) for other in entries if other is not row)]
            baseline_entry = next(row for row in entries if tuple(row['wear']) == base_choice.wear
                                  and tuple(row['discard']) == base_choice.discard)
            rows.append({
                'day': day, 'offered': [sock.shade for sock in point.offered],
                'spent_so_far': before.spent, 'drawer_size': len(before.drawer),
                'pending_discards': {color.value: before.pending[color] for color in Color},
                'effective_horizon': min(horizon, days - day + 1),
                'baseline_action': baseline_entry,
                'choices_evaluated': len(entries), 'frontier': frontier, 'choices': entries,
            })
        elif day in chosen_days:
            before = baseline.clone()
            rows.append({
                'day': day, 'offered': [sock.shade for sock in point.offered],
                'spent_so_far': before.spent, 'drawer_size': len(before.drawer),
                'pending_discards': {color.value: before.pending[color] for color in Color},
                'effective_horizon': min(horizon, days - day + 1),
                'baseline_action': None, 'choices_evaluated': 0,
                'frontier': [], 'choices': [],
                'sockless_reason': 'Fewer than two socks were offered; no legal choice exists.',
            })
        baseline.resume(point, base_choice)
    return {
        'parameters': {'capacity': capacity, 'roommates': roommates, 'unit': unit,
                       'days': days, 'seed': seed, 'budget': budget,
                       'sampled_days': 'every day' if every_day else samples,
                       'repeats': repeats, 'horizon': horizon},
        'note': 'Full drawer and pending counts are oracle diagnostics, unavailable to players. '
                'Action means are short-horizon outcomes, not globally optimal values.',
        'sampled_turns': rows,
        'baseline_end': {'spent': baseline.spent, 'household_embarrassment': sum(baseline.scores),
                         'sockless_days': sum(baseline.sockless), 'drawer_size': len(baseline.drawer)},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capacity', '-C', type=int, default=40)
    parser.add_argument('--roommates', '-n', type=int, default=4)
    parser.add_argument('--unit', type=int, choices=(4, 5), default=4)
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--budget', type=float)
    parser.add_argument('--samples', type=int, default=6)
    parser.add_argument('--every-day', action='store_true',
                        help='analyze every day instead of evenly spaced sampled days')
    parser.add_argument('--repeats', type=int, default=8)
    parser.add_argument('--horizon', type=int, default=14)
    parser.add_argument('--top', type=int, default=5,
                        help='number of frontier entries shown in HTML; all actions are plotted')
    parser.add_argument('--output', type=Path, default=Path('results/socks_choices.html'))
    parser.add_argument('--json', type=Path, help='optional raw data export')
    args = parser.parse_args()
    if args.top < 1:
        parser.error('--top must be positive')
    report = analyze(args.capacity, args.roommates, args.unit, args.days, args.seed,
                     args.budget, args.samples, args.repeats, args.horizon, args.every_day)
    for row in report['sampled_turns']:
        row['frontier'] = row['frontier'][:args.top]
    write_action_html(report, args.output)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(args.output.resolve())


if __name__ == '__main__':
    main()
