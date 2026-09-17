"""Build a resumable SQLite counterfactual dataset for pattern discovery."""

import argparse
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from itertools import product
from pathlib import Path

from analysis.sock_counterfactual import (
    Point,
    Simulator,
    actions,
    metric_delta,
    policy_choice,
)
from core.engine import EMBARRASSMENT_THRESHOLD, SOCKLESS_PENALTY
from models.player import Selection
from models.sock import Color


POLICIES = ('greedy', 'conserve', 'aggressive', 'random')


@dataclass(frozen=True)
class Scenario:
    capacity: int
    roommates: int
    unit: int
    days: int
    budget: float | None
    seed: int
    policy: str
    horizon: int
    repeats: int
    sample_every: int

    @property
    def signature(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


def minimum_capacity(roommates: int, unit: int = 4) -> int:
    floor = unit * roommates + 10
    return 4 * (floor // 4 + 1)


def capacities(roommates: int, levels: int) -> list[int]:
    base = minimum_capacity(roommates)
    return [base + 16 * level for level in range(levels)]


def scenarios(profile: str) -> list[Scenario]:
    """Curated grids avoid an infeasible blind Cartesian product."""
    if profile == 'smoke':
        spec = {
            'roommates': (1, 4), 'capacity_levels': 1,
            'budget_per_person': (0.0, 100.0, None),
            'seeds': (1, 2), 'policies': ('greedy', 'conserve'),
            'days': 60, 'horizon': 3, 'repeats': 2, 'sample_every': 10,
        }
    elif profile == 'full':
        spec = {
            'roommates': (1, 2, 4, 6), 'capacity_levels': 2,
            'budget_per_person': (0.0, 25.0, 100.0, 300.0, None),
            'seeds': tuple(range(1, 6)), 'policies': POLICIES,
            'days': 360, 'horizon': 7, 'repeats': 4, 'sample_every': 14,
        }
    elif profile == 'large':
        spec = {
            'roommates': (1, 2, 4, 6, 8), 'capacity_levels': 3,
            'budget_per_person': (0.0, 10.0, 25.0, 75.0, 150.0, 300.0, None),
            'seeds': tuple(range(1, 11)), 'policies': POLICIES,
            'days': 720, 'horizon': 14, 'repeats': 8, 'sample_every': 14,
        }
    else:
        raise ValueError(profile)

    result = []
    for roommates, budget_pp, seed, policy in product(
        spec['roommates'], spec['budget_per_person'], spec['seeds'], spec['policies']
    ):
        for capacity in capacities(roommates, spec['capacity_levels']):
            budget = None if budget_pp is None else budget_pp * roommates
            result.append(Scenario(
                capacity=capacity, roommates=roommates, unit=4,
                days=spec['days'], budget=budget, seed=seed, policy=policy,
                horizon=spec['horizon'], repeats=spec['repeats'],
                sample_every=spec['sample_every'],
            ))
    return result


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL,
    capacity INTEGER NOT NULL,
    roommates INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    days INTEGER NOT NULL,
    budget REAL,
    seed INTEGER NOT NULL,
    policy TEXT NOT NULL,
    horizon INTEGER NOT NULL,
    repeats INTEGER NOT NULL,
    sample_every INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    baseline_spent REAL,
    baseline_embarrassment REAL,
    baseline_sockless INTEGER,
    baseline_end_drawer INTEGER,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS decision_points (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    offered_json TEXT NOT NULL,
    offered_count INTEGER NOT NULL,
    drawer_size INTEGER NOT NULL,
    drawer_json TEXT NOT NULL,
    returning_json TEXT NOT NULL,
    pending_white INTEGER NOT NULL,
    pending_black INTEGER NOT NULL,
    spent_so_far REAL NOT NULL,
    budget_remaining REAL,
    household_embarrassment_so_far REAL NOT NULL,
    focal_embarrassment_so_far REAL NOT NULL,
    household_sockless_so_far INTEGER NOT NULL,
    effective_horizon INTEGER NOT NULL,
    baseline_wear_json TEXT,
    baseline_discard_json TEXT,
    legal_actions INTEGER NOT NULL,
    is_sockless INTEGER NOT NULL,
    UNIQUE(run_id, day)
);
CREATE TABLE IF NOT EXISTS action_choices (
    id INTEGER PRIMARY KEY,
    decision_id INTEGER NOT NULL REFERENCES decision_points(id) ON DELETE CASCADE,
    action_index INTEGER NOT NULL,
    wear_json TEXT NOT NULL,
    discard_json TEXT NOT NULL,
    wear_shades_json TEXT NOT NULL,
    discard_shades_json TEXT NOT NULL,
    discard_white INTEGER NOT NULL,
    discard_black INTEGER NOT NULL,
    immediate_embarrassment REAL NOT NULL,
    UNIQUE(decision_id, action_index)
);
CREATE TABLE IF NOT EXISTS rollouts (
    id INTEGER PRIMARY KEY,
    action_id INTEGER NOT NULL REFERENCES action_choices(id) ON DELETE CASCADE,
    repeat_index INTEGER NOT NULL,
    rollout_seed INTEGER NOT NULL,
    effective_horizon INTEGER NOT NULL,
    household_embarrassment REAL NOT NULL,
    focal_embarrassment REAL NOT NULL,
    household_spend REAL NOT NULL,
    household_sockless INTEGER NOT NULL,
    end_drawer_size INTEGER NOT NULL,
    end_pending_white INTEGER NOT NULL,
    end_pending_black INTEGER NOT NULL,
    new_discards_white INTEGER NOT NULL,
    new_discards_black INTEGER NOT NULL,
    new_holes_white INTEGER NOT NULL,
    new_holes_black INTEGER NOT NULL,
    UNIQUE(action_id, repeat_index)
);
CREATE INDEX IF NOT EXISTS idx_decisions_run_day ON decision_points(run_id, day);
CREATE INDEX IF NOT EXISTS idx_actions_decision ON action_choices(decision_id);
CREATE INDEX IF NOT EXISTS idx_rollouts_action ON rollouts(action_id);
CREATE INDEX IF NOT EXISTS idx_runs_parameters
    ON runs(roommates, capacity, budget, seed, policy);
CREATE VIEW IF NOT EXISTS rollout_dataset AS
SELECT
    r.id AS run_id, r.profile, r.capacity, r.roommates, r.unit, r.days,
    r.budget, r.seed AS baseline_seed, r.policy, r.horizon, r.repeats,
    d.id AS decision_id, d.day, d.offered_json, d.offered_count,
    d.drawer_size, d.drawer_json, d.returning_json, d.pending_white,
    d.pending_black, d.spent_so_far, d.budget_remaining,
    d.household_embarrassment_so_far, d.focal_embarrassment_so_far,
    d.household_sockless_so_far, d.effective_horizon, d.is_sockless,
    a.id AS action_id, a.action_index, a.wear_json, a.discard_json,
    a.wear_shades_json, a.discard_shades_json, a.discard_white,
    a.discard_black, a.immediate_embarrassment,
    o.repeat_index, o.rollout_seed, o.household_embarrassment,
    o.focal_embarrassment, o.household_spend, o.household_sockless,
    o.end_drawer_size, o.end_pending_white, o.end_pending_black,
    o.new_discards_white, o.new_discards_black,
    o.new_holes_white, o.new_holes_black
FROM rollouts o
JOIN action_choices a ON a.id = o.action_id
JOIN decision_points d ON d.id = a.decision_id
JOIN runs r ON r.id = d.run_id;
CREATE VIEW IF NOT EXISTS action_outcomes AS
SELECT
    action_id, COUNT(*) AS rollout_count,
    AVG(household_embarrassment) AS mean_household_embarrassment,
    AVG(focal_embarrassment) AS mean_focal_embarrassment,
    AVG(household_spend) AS mean_household_spend,
    AVG(household_sockless) AS mean_household_sockless,
    AVG(end_drawer_size) AS mean_end_drawer_size
FROM rollouts
GROUP BY action_id;
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.execute('PRAGMA synchronous=NORMAL')
    connection.executescript(SCHEMA)
    connection.execute(
        'INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)',
        ('schema_version', '1'),
    )
    connection.commit()
    return connection


def json_socks(socks) -> str:
    return json.dumps([[sock.color.value, sock.shade] for sock in socks], separators=(',', ':'))


def ensure_run(connection: sqlite3.Connection, scenario: Scenario, profile: str) -> tuple[int, str]:
    connection.execute(
        '''INSERT OR IGNORE INTO runs(
               signature, profile, capacity, roommates, unit, days, budget, seed,
               policy, horizon, repeats, sample_every
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (scenario.signature, profile, scenario.capacity, scenario.roommates, scenario.unit,
         scenario.days, scenario.budget, scenario.seed, scenario.policy,
         scenario.horizon, scenario.repeats, scenario.sample_every),
    )
    row = connection.execute(
        'SELECT id, status FROM runs WHERE signature=?', (scenario.signature,)
    ).fetchone()
    connection.commit()
    return int(row[0]), str(row[1])


def insert_decision(connection: sqlite3.Connection, run_id: int, scenario: Scenario,
                    baseline: Simulator, point: Point, baseline_choice: Selection | None) -> int:
    offered = point.offered
    horizon = min(scenario.horizon, scenario.days - baseline.day + 1)
    budget_remaining = None if math.isinf(baseline.budget_remaining) else baseline.budget_remaining
    cursor = connection.execute(
        '''INSERT INTO decision_points(
               run_id, day, offered_json, offered_count, drawer_size, drawer_json,
               returning_json, pending_white, pending_black, spent_so_far,
               budget_remaining, household_embarrassment_so_far,
               focal_embarrassment_so_far, household_sockless_so_far,
               effective_horizon, baseline_wear_json, baseline_discard_json,
               legal_actions, is_sockless
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
        (run_id, baseline.day, json.dumps([sock.shade for sock in offered]), len(offered),
         len(baseline.drawer), json_socks(baseline.drawer), json_socks(point.returning),
         baseline.pending[Color.WHITE], baseline.pending[Color.BLACK], baseline.spent,
         budget_remaining, sum(baseline.scores), baseline.scores[0], sum(baseline.sockless),
         horizon, None if baseline_choice is None else json.dumps(baseline_choice.wear),
         None if baseline_choice is None else json.dumps(baseline_choice.discard),
         len(actions(len(offered))), int(len(offered) < 2)),
    )
    return int(cursor.lastrowid)


def evaluate_decision(connection: sqlite3.Connection, decision_id: int,
                      scenario: Scenario, baseline: Simulator, point: Point) -> None:
    before = baseline.clone()
    offered = point.offered
    effective_horizon = min(scenario.horizon, scenario.days - baseline.day + 1)
    for action_index, choice in enumerate(actions(len(offered))):
        shades = [sock.shade for sock in offered]
        difference = abs(shades[choice.wear[0]] - shades[choice.wear[1]])
        immediate = difference if difference > EMBARRASSMENT_THRESHOLD else 0
        discarded = [offered[i] for i in choice.discard]
        cursor = connection.execute(
            '''INSERT INTO action_choices(
                   decision_id, action_index, wear_json, discard_json,
                   wear_shades_json, discard_shades_json, discard_white,
                   discard_black, immediate_embarrassment
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (decision_id, action_index, json.dumps(choice.wear), json.dumps(choice.discard),
             json.dumps([shades[i] for i in choice.wear]),
             json.dumps([sock.shade for sock in discarded]),
             sum(sock.color is Color.WHITE for sock in discarded),
             sum(sock.color is Color.BLACK for sock in discarded), immediate),
        )
        action_id = int(cursor.lastrowid)
        for repeat in range(scenario.repeats):
            rollout_seed = (
                scenario.seed * 1_000_003 + baseline.day * 10_009
                + action_index * 101 + repeat
            )
            trial = before.clone()
            trial.rng.seed(rollout_seed)
            branch = Point(trial, list(point.offered), point.roommate,
                           list(point.remaining_order), list(point.returning))
            trial.resume(branch, choice)
            for _ in range(effective_horizon - 1):
                trial.step()
            outcome = metric_delta(before, trial)
            connection.execute(
                '''INSERT INTO rollouts(
                       action_id, repeat_index, rollout_seed, effective_horizon,
                       household_embarrassment, focal_embarrassment, household_spend,
                       household_sockless, end_drawer_size, end_pending_white,
                       end_pending_black, new_discards_white, new_discards_black,
                       new_holes_white, new_holes_black
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (action_id, repeat, rollout_seed, effective_horizon,
                 outcome['household_embarrassment'], outcome['focal_embarrassment'],
                 outcome['spend'], outcome['sockless_days'], len(trial.drawer),
                 trial.pending[Color.WHITE], trial.pending[Color.BLACK],
                 trial.discards[Color.WHITE] - before.discards[Color.WHITE],
                 trial.discards[Color.BLACK] - before.discards[Color.BLACK],
                 trial.holes[Color.WHITE] - before.holes[Color.WHITE],
                 trial.holes[Color.BLACK] - before.holes[Color.BLACK]),
            )


def run_scenario(connection: sqlite3.Connection, scenario: Scenario,
                 profile: str) -> tuple[bool, int]:
    run_id, status = ensure_run(connection, scenario, profile)
    if status == 'complete':
        return False, 0
    completed_days = {
        row[0] for row in connection.execute(
            'SELECT day FROM decision_points WHERE run_id=?', (run_id,)
        )
    }
    baseline = Simulator(
        scenario.capacity, scenario.roommates, scenario.unit, scenario.days,
        scenario.seed, scenario.budget, scenario.policy,
    )
    decisions_added = 0
    for day in range(1, scenario.days + 1):
        point = baseline.pause_at(0)
        has_pair = len(point.offered) >= 2
        baseline_choice = (
            policy_choice(scenario.policy, tuple(sock.shade for sock in point.offered),
                          baseline.budget_remaining, baseline.rng)
            if has_pair else None
        )
        should_sample = day == 1 or day == scenario.days or day % scenario.sample_every == 0
        if should_sample and day not in completed_days:
            try:
                decision_id = insert_decision(
                    connection, run_id, scenario, baseline, point, baseline_choice
                )
                if has_pair:
                    evaluate_decision(connection, decision_id, scenario, baseline, point)
                connection.commit()
                decisions_added += 1
            except Exception:
                connection.rollback()
                raise
        marker = baseline_choice or Selection(wear=(), discard=())
        baseline.resume(point, marker)
    connection.execute(
        '''UPDATE runs SET status='complete', baseline_spent=?, baseline_embarrassment=?,
               baseline_sockless=?, baseline_end_drawer=?, completed_at=CURRENT_TIMESTAMP
           WHERE id=?''',
        (baseline.spent, sum(baseline.scores), sum(baseline.sockless),
         len(baseline.drawer), run_id),
    )
    connection.commit()
    return True, decisions_added


def estimate(items: list[Scenario]) -> dict:
    decisions = sum(math.ceil(item.days / item.sample_every) + 1 for item in items)
    actions_per_decision = 24  # full four-sock hand; depletion can lower this
    rollouts = sum((math.ceil(item.days / item.sample_every) + 1)
                   * actions_per_decision * item.repeats for item in items)
    return {'scenarios': len(items), 'decision_points_upper': decisions,
            'rollouts_upper': rollouts}


def counts(connection: sqlite3.Connection) -> dict:
    return {
        table: connection.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
        for table in ('runs', 'decision_points', 'action_choices', 'rollouts')
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=('smoke', 'full', 'large'), default='smoke')
    parser.add_argument('--output', type=Path, default=Path('datasets/socks_counterfactual.sqlite'))
    parser.add_argument('--limit-scenarios', type=int,
                        help='run only the first N scenarios; useful for testing')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--progress-every', type=int, default=10)
    args = parser.parse_args()

    items = scenarios(args.profile)
    if args.limit_scenarios is not None:
        items = items[:args.limit_scenarios]
    plan = estimate(items)
    print(json.dumps({'profile': args.profile, 'output': str(args.output), **plan}, indent=2))
    if args.dry_run:
        return

    connection = connect(args.output)
    connection.execute(
        'INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)',
        ('last_profile', args.profile),
    )
    connection.commit()
    started = time.monotonic()
    executed = 0
    try:
        for index, item in enumerate(items, 1):
            ran, added = run_scenario(connection, item, args.profile)
            executed += int(ran)
            if index % args.progress_every == 0 or index == len(items):
                elapsed = time.monotonic() - started
                print(
                    f'[{index}/{len(items)}] executed={executed} '
                    f'new_decisions={added} elapsed={elapsed / 60:.1f}m counts={counts(connection)}',
                    file=sys.stderr, flush=True,
                )
    finally:
        final = counts(connection)
        connection.close()
    print(json.dumps({'complete': True, 'executed_scenarios': executed, **final}, indent=2))


if __name__ == '__main__':
    main()
