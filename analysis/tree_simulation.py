"""Build a bounded sequential multi-roommate sock decision tree in SQLite.

Each roommate draws and acts in random order. Kept leftovers return immediately,
so an earlier choice changes the drawer from which later roommates draw. The
explorer covers baseline-context individual actions, samples sequential action
interactions and chance outcomes, merges equivalent next-day states, and keeps
a diverse state frontier.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import random
import sqlite3
import time
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from core.engine import (
    EMBARRASSMENT_THRESHOLD,
    HOLE_PROBABILITY,
    PACK_COST,
    PACK_SIZE,
    SOCKLESS_PENALTY,
)
from models.sock import (
    BLACK_CEILING,
    BLACK_FADE,
    BLACK_START,
    WHITE_FADE,
    WHITE_FLOOR,
    WHITE_START,
)


Sock = tuple[str, int]
MAX_LOG_WEIGHT = math.log(1e300)


@dataclass(frozen=True)
class Choice:
    wear: tuple[int, ...]
    discard: tuple[int, ...]


@dataclass(frozen=True)
class State:
    day: int
    drawer: tuple[Sock, ...]
    pending_white: int
    pending_black: int
    spent: int
    scores: tuple[int, ...]
    sockless: tuple[int, ...]

    @property
    def signature(self) -> str:
        payload = (
            self.day,
            self.drawer,
            self.pending_white,
            self.pending_black,
            self.spent,
            self.scores,
            self.sockless,
        )
        return hashlib.sha256(repr(payload).encode()).hexdigest()


@dataclass(frozen=True)
class Scenario:
    roommates: int
    capacity: int
    sock_level: float
    requested_socks_per_person: float
    budget: int | None
    budget_level: float
    seed: int
    days: int
    unit: int
    chance_samples: int
    max_trajectories: int
    max_states_per_day: int

    @property
    def signature(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


@dataclass
class Candidate:
    state: State
    log_path_weight: float = -math.inf
    inbound_transitions: int = 0

    def add(self, parent_log_weight: float) -> None:
        self.log_path_weight = logaddexp(self.log_path_weight, parent_log_weight)
        self.inbound_transitions += 1


@dataclass(frozen=True)
class TransitionMetrics:
    immediate_scores: tuple[int, ...]
    main_discard_count: int
    household_discard_count: int
    main_holes: int
    household_holes: int
    packs_bought: int
    spent_today: int


@dataclass(frozen=True)
class TrajectoryResult:
    state: State
    metrics: TransitionMetrics
    order: tuple[int, ...]
    hands: tuple[tuple[Sock, ...], ...]
    choices: tuple[Choice, ...]
    action_indices: tuple[int, ...]


PROFILE_DEFAULTS = {
    "small": {
        "roommates": (1, 5, 10),
        "day_values": (1, 10, 100, 1000),
        "sock_levels": (0.0, 0.5, 1.0),
        "budget_levels": (0.0, 0.5, 1.0),
        "full_grid": False,
        "seeds": (1,),
        "chance_samples": 1,
        "max_trajectories": 256,
        "state_caps": ((30, 20), (100, 5), (360, 2), (1000, 1)),
    },
    "research": {
        "roommates": tuple(range(1, 11)),
        "day_values": (1, 10, 30, 100, 360, 1000),
        "sock_levels": (0.0, 0.25, 0.5, 0.75, 1.0),
        "budget_levels": (0.0, 0.25, 0.5, 0.75, 1.0),
        "full_grid": False,
        "seeds": (1,),
        "chance_samples": 1,
        "max_trajectories": 256,
        "state_caps": ((30, 30), (100, 5), (360, 2), (1000, 1)),
    },
    "large": {
        "roommates": tuple(range(1, 11)),
        "day_values": (1, 10, 30, 100, 360, 1000),
        "sock_levels": (0.0, 0.25, 0.5, 0.75, 1.0),
        "budget_levels": (0.0, 0.25, 0.5, 0.75, 1.0),
        "full_grid": True,
        "seeds": (1, 2),
        "chance_samples": 2,
        "max_trajectories": 384,
        "state_caps": ((30, 50), (100, 10), (360, 4), (1000, 2)),
    },
}


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY,
    signature TEXT NOT NULL UNIQUE,
    profile TEXT NOT NULL,
    roommates INTEGER NOT NULL,
    capacity INTEGER NOT NULL,
    sock_level REAL NOT NULL,
    requested_socks_per_person REAL NOT NULL,
    actual_socks_per_person REAL NOT NULL,
    budget INTEGER,
    budget_per_person REAL,
    budget_level REAL NOT NULL,
    budget_per_player_day REAL NOT NULL,
    seed INTEGER NOT NULL,
    days INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    chance_samples INTEGER NOT NULL,
    max_trajectories INTEGER NOT NULL,
    max_states_per_day INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    started_at TEXT,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS layers (
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_states INTEGER NOT NULL DEFAULT 0,
    chance_realizations INTEGER NOT NULL DEFAULT 0,
    theoretical_trajectories_log10 REAL NOT NULL DEFAULT 0,
    explored_trajectories INTEGER NOT NULL DEFAULT 0,
    transition_rows INTEGER NOT NULL DEFAULT 0,
    unique_candidates INTEGER NOT NULL DEFAULT 0,
    retained_states INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    PRIMARY KEY (scenario_id, day)
);
CREATE TABLE IF NOT EXISTS states (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    depth INTEGER NOT NULL,
    signature TEXT NOT NULL,
    drawer_json TEXT NOT NULL,
    drawer_size INTEGER NOT NULL,
    white_count INTEGER NOT NULL,
    black_count INTEGER NOT NULL,
    pairable_fraction REAL NOT NULL,
    pending_white INTEGER NOT NULL,
    pending_black INTEGER NOT NULL,
    spent INTEGER NOT NULL,
    scores_json TEXT NOT NULL,
    main_score INTEGER NOT NULL,
    other_mean_score REAL NOT NULL,
    score_spread REAL NOT NULL,
    sockless_json TEXT NOT NULL,
    total_sockless INTEGER NOT NULL,
    log_path_weight REAL NOT NULL,
    inbound_transitions INTEGER NOT NULL,
    UNIQUE (scenario_id, depth, signature)
);
CREATE TABLE IF NOT EXISTS chance_contexts (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    parent_state_id INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
    chance_index INTEGER NOT NULL,
    trajectories_theoretical_upper INTEGER NOT NULL,
    sequential_pairs_possible INTEGER NOT NULL,
    sequential_pairs_explored INTEGER NOT NULL,
    trajectories_explored INTEGER NOT NULL,
    order_json TEXT NOT NULL,
    hands_json TEXT NOT NULL,
    UNIQUE (scenario_id, day, parent_state_id, chance_index)
);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY,
    scenario_id INTEGER NOT NULL REFERENCES scenarios(id) ON DELETE CASCADE,
    day INTEGER NOT NULL,
    parent_state_id INTEGER NOT NULL REFERENCES states(id) ON DELETE CASCADE,
    child_signature TEXT NOT NULL,
    child_state_id INTEGER REFERENCES states(id) ON DELETE SET NULL,
    retained INTEGER NOT NULL DEFAULT 0,
    context_id INTEGER NOT NULL REFERENCES chance_contexts(id) ON DELETE CASCADE,
    trajectory_index INTEGER NOT NULL,
    action_indices_json TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    immediate_scores_json TEXT NOT NULL,
    main_immediate INTEGER NOT NULL,
    others_immediate_mean REAL NOT NULL,
    main_discard_count INTEGER NOT NULL,
    household_discard_count INTEGER NOT NULL,
    main_holes INTEGER NOT NULL,
    household_holes INTEGER NOT NULL,
    packs_bought INTEGER NOT NULL,
    spent_today INTEGER NOT NULL,
    next_drawer_size INTEGER NOT NULL,
    next_pending_white INTEGER NOT NULL,
    next_pending_black INTEGER NOT NULL,
    next_main_score INTEGER NOT NULL,
    next_other_mean_score REAL NOT NULL,
    next_total_sockless INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_states_scenario_depth ON states(scenario_id, depth);
CREATE INDEX IF NOT EXISTS idx_contexts_scenario_day ON chance_contexts(scenario_id, day);
CREATE INDEX IF NOT EXISTS idx_transitions_scenario_day ON transitions(scenario_id, day);
CREATE INDEX IF NOT EXISTS idx_transitions_child ON transitions(scenario_id, day, child_signature);
"""


def logaddexp(left: float, right: float) -> float:
    if left == -math.inf:
        return right
    if right == -math.inf:
        return left
    high, low = max(left, right), min(left, right)
    if high > MAX_LOG_WEIGHT:
        return high
    return high + math.log1p(math.exp(low - high))


def stable_seed(*parts: object) -> int:
    digest = hashlib.blake2b(repr(parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def capacity_bounds(roommates: int, unit: int = 4) -> tuple[int, int]:
    """Return legal multiples of four inside the requested sock interval."""
    requested_floor = 4 * roommates + 10
    engine_floor = unit * roommates + 10
    strict_floor = max(requested_floor, engine_floor)
    minimum = 4 * (strict_floor // 4 + 1)
    maximum = 20 * roommates
    if minimum > maximum:
        raise ValueError(
            f"no legal capacity for n={roommates}, unit={unit} inside "
            f"[{requested_floor}, {maximum}]"
        )
    return minimum, maximum


def capacity_from_level(roommates: int, level: float, unit: int = 4) -> tuple[int, float]:
    if not 0 <= level <= 1:
        raise ValueError(f"sock level must lie in [0,1], got {level}")
    minimum, maximum = capacity_bounds(roommates, unit)
    target = minimum + level * (maximum - minimum)
    capacity = min(maximum, max(minimum, 4 * round(target / 4)))
    return capacity, target / roommates


def budget_from_level(roommates: int, days: int, level: float) -> int:
    if not 0 <= level <= 1:
        raise ValueError(f"budget level must lie in [0,1], got {level}")
    return round(level * 4 * roommates * days)


def parameter_pairs(
    sock_levels: tuple[float, ...],
    budget_levels: tuple[float, ...],
    full_grid: bool,
) -> tuple[tuple[float, float], ...]:
    """Return a full grid or a boundary-aware space-filling cross design."""
    if full_grid:
        return tuple(itertools.product(sock_levels, budget_levels))
    if not sock_levels or not budget_levels:
        return tuple()

    pairs: set[tuple[float, float]] = set()
    sock_last = len(sock_levels) - 1
    budget_last = len(budget_levels) - 1
    for index, sock_level in enumerate(sock_levels):
        quantile = 0 if sock_last == 0 else index / sock_last
        low_index = round(quantile * budget_last)
        high_index = round((1 - quantile) * budget_last)
        pairs.add((sock_level, budget_levels[low_index]))
        pairs.add((sock_level, budget_levels[high_index]))

    middle_sock = sock_levels[len(sock_levels) // 2]
    middle_budget = budget_levels[len(budget_levels) // 2]
    pairs.add((middle_sock, budget_levels[0]))
    pairs.add((middle_sock, budget_levels[-1]))
    pairs.add((sock_levels[0], middle_budget))
    pairs.add((sock_levels[-1], middle_budget))
    return tuple(sorted(pairs))


def state_cap_for_days(
    state_caps: tuple[tuple[int, int], ...], days: int, override: int | None
) -> int:
    if override is not None:
        return override
    for maximum_days, cap in state_caps:
        if days <= maximum_days:
            return cap
    return state_caps[-1][1]


def choices(hand_size: int) -> list[Choice]:
    if hand_size < 2:
        return [Choice((), ())]
    result = []
    for wear in itertools.combinations(range(hand_size), 2):
        leftovers = [index for index in range(hand_size) if index not in wear]
        for mask in range(1 << len(leftovers)):
            discard = tuple(
                index for bit, index in enumerate(leftovers) if mask & (1 << bit)
            )
            result.append(Choice(wear, discard))
    return result


def baseline_choice_index(hand: list[Sock], available: list[Choice]) -> int:
    if len(hand) < 2:
        return 0
    return min(
        range(len(available)),
        key=lambda index: (
            abs(hand[available[index].wear[0]][1] - hand[available[index].wear[1]][1]),
            len(available[index].discard),
            available[index].wear,
        ),
    )


def initial_state(scenario: Scenario) -> State:
    half = scenario.capacity // 2
    drawer = tuple(sorted(
        [("white", WHITE_START)] * half + [("black", BLACK_START)] * half
    ))
    return State(
        day=0,
        drawer=drawer,
        pending_white=0,
        pending_black=0,
        spent=0,
        scores=(0,) * scenario.roommates,
        sockless=(0,) * scenario.roommates,
    )


def roommate_order(state: State, scenario: Scenario, chance_index: int) -> tuple[int, ...]:
    rng = random.Random(stable_seed(
        scenario.seed, state.signature, state.day, chance_index, "order"
    ))
    order = list(range(scenario.roommates))
    rng.shuffle(order)
    return tuple(order)


def draw_hand(
    drawer: list[Sock],
    state: State,
    scenario: Scenario,
    chance_index: int,
    turn_position: int,
) -> list[Sock]:
    """Draw uniformly after all immediate returns from earlier turns.

    Sorting is safe because socks have no identity in the analysis state. It
    also makes common random numbers depend on the drawer multiset rather than
    an arbitrary append order.
    """
    drawer.sort()
    rng = random.Random(stable_seed(
        scenario.seed, state.signature, state.day, chance_index, turn_position, "draw"
    ))
    picked = rng.sample(range(len(drawer)), min(scenario.unit, len(drawer)))
    picked.sort(reverse=True)
    return [drawer.pop(index) for index in picked]


def worn_out(sock: Sock) -> bool:
    color, shade = sock
    return shade <= WHITE_FLOOR if color == "white" else shade >= BLACK_CEILING


def washed(sock: Sock) -> Sock:
    color, shade = sock
    if color == "white":
        return color, max(WHITE_FLOOR, shade - WHITE_FADE)
    return color, min(BLACK_CEILING, shade + BLACK_FADE)


def hole_occurs(
    scenario: Scenario,
    state: State,
    chance_index: int,
    player: int,
    hand_index: int,
) -> bool:
    rng = random.Random(stable_seed(
        scenario.seed, state.signature, state.day, chance_index, player, hand_index, "hole"
    ))
    return rng.random() < HOLE_PROBABILITY


def run_trajectory(
    state: State,
    scenario: Scenario,
    chance_index: int,
    order: tuple[int, ...],
    forced_by_turn: tuple[int | None, ...],
) -> TrajectoryResult:
    """Run one sequential day under optional action overrides by turn position."""
    drawer = list(state.drawer)
    worn_returning: list[Sock] = []
    pending = {"white": state.pending_white, "black": state.pending_black}
    scores = list(state.scores)
    sockless = list(state.sockless)
    immediate = [0] * scenario.roommates
    hands: list[tuple[Sock, ...]] = [tuple() for _ in range(scenario.roommates)]
    selected: list[Choice] = [Choice((), ()) for _ in range(scenario.roommates)]
    action_indices = [0] * scenario.roommates
    household_discards = 0
    household_holes = 0
    main_holes = 0

    for turn_position, player in enumerate(order):
        hand = draw_hand(drawer, state, scenario, chance_index, turn_position)
        hands[player] = tuple(hand)
        available = choices(len(hand))
        forced = forced_by_turn[turn_position]
        action_index = (
            baseline_choice_index(hand, available)
            if forced is None
            else forced % len(available)
        )
        action = available[action_index]
        selected[player] = action
        action_indices[player] = action_index
        if len(hand) < 2:
            scores[player] += int(SOCKLESS_PENALTY)
            sockless[player] += 1
            immediate[player] = int(SOCKLESS_PENALTY)
            drawer.extend(hand)
            continue

        first, second = action.wear
        difference = abs(hand[first][1] - hand[second][1])
        score = difference if difference > EMBARRASSMENT_THRESHOLD else 0
        scores[player] += score
        immediate[player] = score

        for hand_index in action.wear:
            sock = hand[hand_index]
            if worn_out(sock) and hole_occurs(
                scenario, state, chance_index, player, hand_index
            ):
                pending[sock[0]] += 1
                household_holes += 1
                main_holes += int(player == 0)
            else:
                worn_returning.append(washed(sock))

        for hand_index, sock in enumerate(hand):
            if hand_index in action.wear:
                continue
            if hand_index in action.discard:
                pending[sock[0]] += 1
                household_discards += 1
            else:
                # Kept leftovers are immediately available to the next roommate.
                drawer.append(sock)

    next_drawer = drawer + worn_returning
    spent = state.spent
    packs_bought = 0
    for color_name in ("white", "black"):
        packs = pending[color_name] // PACK_SIZE
        if scenario.budget is not None:
            packs = min(packs, max(0, (scenario.budget - spent) // int(PACK_COST)))
        if packs:
            count = packs * PACK_SIZE
            pending[color_name] -= count
            shade = WHITE_START if color_name == "white" else BLACK_START
            next_drawer.extend([(color_name, shade)] * count)
            spent += packs * int(PACK_COST)
            packs_bought += packs

    next_state = State(
        day=state.day + 1,
        drawer=tuple(sorted(next_drawer)),
        pending_white=pending["white"],
        pending_black=pending["black"],
        spent=spent,
        scores=tuple(scores),
        sockless=tuple(sockless),
    )
    metrics = TransitionMetrics(
        immediate_scores=tuple(immediate),
        main_discard_count=len(selected[0].discard),
        household_discard_count=household_discards,
        main_holes=main_holes,
        household_holes=household_holes,
        packs_bought=packs_bought,
        spent_today=spent - state.spent,
    )
    return TrajectoryResult(
        state=next_state,
        metrics=metrics,
        order=order,
        hands=tuple(hands),
        choices=tuple(selected),
        action_indices=tuple(action_indices),
    )


def sequential_trajectories(
    state: State,
    scenario: Scenario,
    chance_index: int,
) -> tuple[list[TrajectoryResult], int, int, int]:
    """Cover marginal actions, then sample two-turn sequential interactions.

    Marginal coverage changes one roommate's action from the all-baseline path.
    Pairwise coverage changes an earlier action, redraws later hands from the
    resulting drawer, and then changes a later action. Thus every stored hand is
    consistent with the choices that preceded it.
    """
    order = roommate_order(state, scenario, chance_index)
    empty = (None,) * scenario.roommates
    baseline = run_trajectory(state, scenario, chance_index, order, empty)
    results: dict[tuple[int, ...], TrajectoryResult] = {
        baseline.action_indices: baseline
    }

    marginal_specs: list[tuple[int | None, ...]] = []
    for turn_position, player in enumerate(order):
        for action_index in range(len(choices(len(baseline.hands[player])))):
            forced = [None] * scenario.roommates
            forced[turn_position] = action_index
            marginal_specs.append(tuple(forced))
    required = 1 + sum(
        max(0, len(choices(len(baseline.hands[player]))) - 1) for player in order
    )
    if required > scenario.max_trajectories:
        raise ValueError(
            f"max_trajectories={scenario.max_trajectories} cannot cover all "
            f"{required} baseline-context individual actions; increase the limit"
        )
    for forced in marginal_specs:
        result = run_trajectory(state, scenario, chance_index, order, forced)
        results.setdefault(result.action_indices, result)

    pair_specs: list[tuple[int | None, ...]] = []
    for left in range(scenario.roommates):
        left_player = order[left]
        left_count = len(choices(len(baseline.hands[left_player])))
        for left_action in range(left_count):
            left_forced = [None] * scenario.roommates
            left_forced[left] = left_action
            left_result = run_trajectory(
                state, scenario, chance_index, order, tuple(left_forced)
            )
            for right in range(left + 1, scenario.roommates):
                right_player = order[right]
                right_count = len(choices(len(left_result.hands[right_player])))
                for right_action in range(right_count):
                    forced = left_forced.copy()
                    forced[right] = right_action
                    pair_specs.append(tuple(forced))
    pairwise_possible = len(pair_specs)
    rng = random.Random(stable_seed(
        scenario.seed, state.signature, state.day, chance_index, "sequential-pairs"
    ))
    rng.shuffle(pair_specs)
    pairwise_explored = 0
    for forced in pair_specs:
        if len(results) >= scenario.max_trajectories:
            break
        result = run_trajectory(state, scenario, chance_index, order, forced)
        before = len(results)
        results.setdefault(result.action_indices, result)
        pairwise_explored += int(len(results) > before)

    theoretical_upper = len(choices(scenario.unit)) ** scenario.roommates
    return list(results.values()), theoretical_upper, pairwise_possible, pairwise_explored


def pairable_fraction(drawer: tuple[Sock, ...]) -> float:
    if not drawer:
        return 0.0
    pairable = 0
    for index, (color_name, shade) in enumerate(drawer):
        if any(
            other_color == color_name and abs(other_shade - shade) <= EMBARRASSMENT_THRESHOLD
            for other_index, (other_color, other_shade) in enumerate(drawer)
            if other_index != index
        ):
            pairable += 1
    return pairable / len(drawer)


def state_summary(state: State) -> tuple[int, int, float, float, float]:
    white = sum(color_name == "white" for color_name, _ in state.drawer)
    black = len(state.drawer) - white
    other_mean = sum(state.scores[1:]) / max(1, len(state.scores) - 1)
    spread = other_mean - state.scores[0]
    return white, black, pairable_fraction(state.drawer), other_mean, spread


def state_stratum(state: State, scenario: Scenario) -> tuple[int, ...]:
    white, black, pairability, _, spread = state_summary(state)
    socks_per_person = len(state.drawer) / scenario.roommates
    stock_bucket = min(5, int(socks_per_person // 2))
    balance_bucket = min(5, int(6 * abs(white - black) / max(1, len(state.drawer))))
    pair_bucket = min(4, int(pairability * 5))
    budget_left = math.inf if scenario.budget is None else scenario.budget - state.spent
    budget_bucket = 5 if math.isinf(budget_left) else min(4, int(max(0, budget_left) // 25))
    spread_bucket = 0 if spread < -100 else 1 if spread < 0 else 2 if spread < 100 else 3
    sockless_bucket = min(4, sum(state.sockless))
    return stock_bucket, balance_bucket, pair_bucket, budget_bucket, spread_bucket, sockless_bucket


def retain_candidates(
    candidates: dict[str, Candidate], limit: int, scenario: Scenario
) -> list[Candidate]:
    if len(candidates) <= limit:
        return sorted(candidates.values(), key=lambda item: item.state.signature)
    groups: defaultdict[tuple[int, ...], list[Candidate]] = defaultdict(list)
    for candidate in candidates.values():
        groups[state_stratum(candidate.state, scenario)].append(candidate)
    for items in groups.values():
        items.sort(key=lambda item: (-item.log_path_weight, item.state.signature))

    retained = []
    group_order = sorted(
        groups,
        key=lambda key: (-groups[key][0].log_path_weight, key),
    )
    for key in group_order[:limit]:
        retained.append(groups[key].pop(0))
    while len(retained) < limit:
        changed = False
        for key in group_order:
            if groups[key] and len(retained) < limit:
                retained.append(groups[key].pop(0))
                changed = True
        if not changed:
            break
    return retained


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    existing = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'"
    ).fetchone()
    if existing:
        version = connection.execute(
            "SELECT value FROM metadata WHERE key='schema_version'"
        ).fetchone()
        if version and version[0] != "5":
            connection.close()
            raise RuntimeError(
                f"{path} uses tree schema {version[0]}; choose a new --output path "
                "for the sequential-return simulator"
            )
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','5')"
    )
    connection.commit()
    return connection


def encode_socks(socks: tuple[Sock, ...] | list[Sock]) -> str:
    return json.dumps(socks, separators=(",", ":"))


def insert_state(
    connection: sqlite3.Connection,
    scenario_id: int,
    state: State,
    log_path_weight: float,
    inbound_transitions: int,
) -> int:
    white, black, pairability, other_mean, spread = state_summary(state)
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO states(
            scenario_id,depth,signature,drawer_json,drawer_size,white_count,black_count,
            pairable_fraction,pending_white,pending_black,spent,scores_json,main_score,
            other_mean_score,score_spread,sockless_json,total_sockless,log_path_weight,
            inbound_transitions
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scenario_id, state.day, state.signature, encode_socks(state.drawer), len(state.drawer),
            white, black, pairability, state.pending_white, state.pending_black, state.spent,
            json.dumps(state.scores), state.scores[0], other_mean, spread,
            json.dumps(state.sockless), sum(state.sockless), log_path_weight,
            inbound_transitions,
        ),
    )
    if cursor.lastrowid:
        return int(cursor.lastrowid)
    row = connection.execute(
        "SELECT id FROM states WHERE scenario_id=? AND depth=? AND signature=?",
        (scenario_id, state.day, state.signature),
    ).fetchone()
    return int(row[0])


def decode_state(row: sqlite3.Row) -> State:
    return State(
        day=int(row["depth"]),
        drawer=tuple((str(color_name), int(shade)) for color_name, shade in json.loads(row["drawer_json"])),
        pending_white=int(row["pending_white"]),
        pending_black=int(row["pending_black"]),
        spent=int(row["spent"]),
        scores=tuple(int(value) for value in json.loads(row["scores_json"])),
        sockless=tuple(int(value) for value in json.loads(row["sockless_json"])),
    )


def ensure_scenario(
    connection: sqlite3.Connection, scenario: Scenario, profile: str
) -> tuple[int, str]:
    budget_pp = None if scenario.budget is None else scenario.budget / scenario.roommates
    budget_ppd = 0 if scenario.budget is None else budget_pp / scenario.days
    connection.execute(
        """
        INSERT OR IGNORE INTO scenarios(
            signature,profile,roommates,capacity,sock_level,requested_socks_per_person,
            actual_socks_per_person,budget,budget_per_person,budget_level,
            budget_per_player_day,seed,days,unit,
            chance_samples,max_trajectories,max_states_per_day
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scenario.signature, profile, scenario.roommates, scenario.capacity,
            scenario.sock_level, scenario.requested_socks_per_person,
            scenario.capacity / scenario.roommates, scenario.budget, budget_pp,
            scenario.budget_level, budget_ppd, scenario.seed, scenario.days, scenario.unit,
            scenario.chance_samples, scenario.max_trajectories,
            scenario.max_states_per_day,
        ),
    )
    row = connection.execute(
        "SELECT id,status FROM scenarios WHERE signature=?", (scenario.signature,)
    ).fetchone()
    scenario_id, status = int(row[0]), str(row[1])
    if not connection.execute(
        "SELECT 1 FROM states WHERE scenario_id=? AND depth=0", (scenario_id,)
    ).fetchone():
        insert_state(connection, scenario_id, initial_state(scenario), 0.0, 1)
    connection.commit()
    return scenario_id, status


TRANSITION_INSERT = """
INSERT INTO transitions(
    scenario_id,day,parent_state_id,child_signature,context_id,trajectory_index,
    action_indices_json,actions_json,immediate_scores_json,main_immediate,
    others_immediate_mean,main_discard_count,household_discard_count,main_holes,
    household_holes,packs_bought,spent_today,next_drawer_size,next_pending_white,
    next_pending_black,next_main_score,next_other_mean_score,next_total_sockless
) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def expand_layer(
    connection: sqlite3.Connection,
    scenario_id: int,
    scenario: Scenario,
    depth: int,
) -> dict[str, int | float]:
    day = depth + 1
    prior = connection.execute(
        "SELECT status FROM layers WHERE scenario_id=? AND day=?", (scenario_id, day)
    ).fetchone()
    if prior and prior[0] == "complete":
        return {"skipped": 1}

    connection.execute("DELETE FROM transitions WHERE scenario_id=? AND day=?", (scenario_id, day))
    connection.execute("DELETE FROM chance_contexts WHERE scenario_id=? AND day=?", (scenario_id, day))
    connection.execute("DELETE FROM states WHERE scenario_id=? AND depth=?", (scenario_id, day))
    connection.execute(
        """
        INSERT INTO layers(scenario_id,day,status) VALUES(?,?,'running')
        ON CONFLICT(scenario_id,day) DO UPDATE SET
            status='running',source_states=0,chance_realizations=0,
            theoretical_trajectories_log10=0,explored_trajectories=0,
            transition_rows=0,unique_candidates=0,retained_states=0,
            started_at=CURRENT_TIMESTAMP,completed_at=NULL
        """,
        (scenario_id, day),
    )
    connection.commit()

    connection.row_factory = sqlite3.Row
    source_rows = connection.execute(
        "SELECT * FROM states WHERE scenario_id=? AND depth=? ORDER BY id",
        (scenario_id, depth),
    ).fetchall()
    candidates: dict[str, Candidate] = {}
    batch = []
    chance_realizations = 0
    explored_profiles = 0
    theoretical_profiles_total = 0

    for source_row in source_rows:
        state = decode_state(source_row)
        parent_log_weight = float(source_row["log_path_weight"])
        for chance_index in range(scenario.chance_samples):
            trajectories, theoretical, pair_possible, pair_explored = sequential_trajectories(
                state, scenario, chance_index
            )
            chance_realizations += 1
            explored_profiles += len(trajectories)
            theoretical_profiles_total += theoretical
            baseline = trajectories[0]
            context_cursor = connection.execute(
                """
                INSERT INTO chance_contexts(
                    scenario_id,day,parent_state_id,chance_index,
                    trajectories_theoretical_upper,sequential_pairs_possible,
                    sequential_pairs_explored,trajectories_explored,order_json,hands_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scenario_id, day, int(source_row["id"]), chance_index,
                    theoretical, pair_possible, pair_explored, len(trajectories),
                    json.dumps(baseline.order, separators=(",", ":")),
                    json.dumps(baseline.hands, separators=(",", ":")),
                ),
            )
            context_id = int(context_cursor.lastrowid)
            for trajectory_index, trajectory in enumerate(trajectories):
                child = trajectory.state
                metrics = trajectory.metrics
                candidate = candidates.setdefault(child.signature, Candidate(child))
                candidate.add(parent_log_weight)
                other_immediate = (
                    sum(metrics.immediate_scores[1:]) / max(1, scenario.roommates - 1)
                )
                next_other = sum(child.scores[1:]) / max(1, scenario.roommates - 1)
                action_json = json.dumps([
                    {
                        "player": player,
                        "offered": trajectory.hands[player],
                        "wear": trajectory.choices[player].wear,
                        "discard": trajectory.choices[player].discard,
                    }
                    for player in trajectory.order
                ], separators=(",", ":"))
                batch.append((
                    scenario_id, day, int(source_row["id"]), child.signature,
                    context_id, trajectory_index,
                    json.dumps(trajectory.action_indices, separators=(",", ":")), action_json,
                    json.dumps(metrics.immediate_scores, separators=(",", ":")),
                    metrics.immediate_scores[0], other_immediate,
                    metrics.main_discard_count, metrics.household_discard_count,
                    metrics.main_holes, metrics.household_holes, metrics.packs_bought,
                    metrics.spent_today, len(child.drawer), child.pending_white,
                    child.pending_black, child.scores[0], next_other, sum(child.sockless),
                ))
                if len(batch) >= 5000:
                    connection.executemany(TRANSITION_INSERT, batch)
                    connection.commit()
                    batch.clear()
    if batch:
        connection.executemany(TRANSITION_INSERT, batch)
        connection.commit()

    retained = retain_candidates(candidates, scenario.max_states_per_day, scenario)
    for candidate in retained:
        insert_state(
            connection, scenario_id, candidate.state, candidate.log_path_weight,
            candidate.inbound_transitions,
        )
    connection.execute(
        """
        UPDATE transitions
        SET retained=1,
            child_state_id=(
                SELECT s.id FROM states s
                WHERE s.scenario_id=transitions.scenario_id
                  AND s.depth=transitions.day
                  AND s.signature=transitions.child_signature
            )
        WHERE scenario_id=? AND day=? AND EXISTS(
            SELECT 1 FROM states s
            WHERE s.scenario_id=transitions.scenario_id
              AND s.depth=transitions.day
              AND s.signature=transitions.child_signature
        )
        """,
        (scenario_id, day),
    )
    transition_rows = connection.execute(
        "SELECT COUNT(*) FROM transitions WHERE scenario_id=? AND day=?",
        (scenario_id, day),
    ).fetchone()[0]
    connection.execute(
        """
        UPDATE layers SET status='complete',source_states=?,chance_realizations=?,
            theoretical_trajectories_log10=?,explored_trajectories=?,transition_rows=?,
            unique_candidates=?,retained_states=?,completed_at=CURRENT_TIMESTAMP
        WHERE scenario_id=? AND day=?
        """,
        (
            len(source_rows), chance_realizations,
            math.log10(max(1, theoretical_profiles_total)),
            explored_profiles, transition_rows, len(candidates), len(retained),
            scenario_id, day,
        ),
    )
    connection.commit()
    return {
        "source_states": len(source_rows),
        "transitions": transition_rows,
        "candidates": len(candidates),
        "retained": len(retained),
    }


def run_scenario(
    connection: sqlite3.Connection, scenario: Scenario, profile: str, progress: bool = True
) -> bool:
    scenario_id, status = ensure_scenario(connection, scenario, profile)
    if status == "complete":
        return False
    connection.execute(
        "UPDATE scenarios SET status='running',started_at=COALESCE(started_at,CURRENT_TIMESTAMP) "
        "WHERE id=?",
        (scenario_id,),
    )
    connection.commit()
    for depth in range(scenario.days):
        stats = expand_layer(connection, scenario_id, scenario, depth)
        if progress and "skipped" not in stats:
            print(
                f"scenario={scenario_id} day={depth + 1}/{scenario.days} "
                f"sources={stats['source_states']} transitions={stats['transitions']} "
                f"unique={stats['candidates']} retained={stats['retained']}",
                flush=True,
            )
    connection.execute(
        "UPDATE scenarios SET status='complete',completed_at=CURRENT_TIMESTAMP WHERE id=?",
        (scenario_id,),
    )
    connection.commit()
    return True


def build_scenarios(args: argparse.Namespace) -> list[Scenario]:
    defaults = PROFILE_DEFAULTS[args.profile]
    roommate_values = tuple(args.roommates or defaults["roommates"])
    day_values = tuple(args.days or defaults["day_values"])
    sock_levels = tuple(args.sock_levels or defaults["sock_levels"])
    budget_levels = tuple(args.budget_levels or defaults["budget_levels"])
    seeds = tuple(args.seeds or defaults["seeds"])
    chance_samples = args.chance_samples or defaults["chance_samples"]
    max_trajectories = args.max_trajectories or defaults["max_trajectories"]
    full_grid = args.full_grid or defaults["full_grid"]
    pairs = parameter_pairs(sock_levels, budget_levels, full_grid)
    result = []
    seen_conditions: set[tuple[int, int, int, int, int]] = set()
    for roommates, days, pair, seed in itertools.product(
        roommate_values, day_values, pairs, seeds
    ):
        sock_level, budget_level = pair
        capacity, requested_ratio = capacity_from_level(roommates, sock_level, args.unit)
        budget = budget_from_level(roommates, days, budget_level)
        max_states = state_cap_for_days(defaults["state_caps"], days, args.max_states)
        condition = (roommates, days, capacity, budget, seed)
        if condition in seen_conditions:
            continue
        seen_conditions.add(condition)
        result.append(Scenario(
            roommates=roommates,
            capacity=capacity,
            sock_level=sock_level,
            requested_socks_per_person=requested_ratio,
            budget=budget,
            budget_level=budget_level,
            seed=seed,
            days=days,
            unit=args.unit,
            chance_samples=chance_samples,
            max_trajectories=max_trajectories,
            max_states_per_day=max_states,
        ))
    return result


def workload(items: list[Scenario]) -> dict[str, object]:
    transitions = sum(
        (1 + (item.days - 1) * item.max_states_per_day)
        * item.chance_samples
        * item.max_trajectories
        for item in items
    )
    minimum_hours = transitions / 5000 / 3600
    maximum_hours = transitions / 1000 / 3600
    low_storage = transitions * 300 / 1024**3
    high_storage = transitions * 900 / 1024**3
    full_action_log10 = max(
        (item.days * item.roommates * math.log10(24) for item in items),
        default=0,
    )
    return {
        "scenarios": len(items),
        "roommate_values": sorted({item.roommates for item in items}),
        "day_values": sorted({item.days for item in items}),
        "capacity_range": (
            f"{min((item.capacity for item in items), default=0)}-"
            f"{max((item.capacity for item in items), default=0)} socks"
        ),
        "budget_range": (
            f"${min((item.budget or 0 for item in items), default=0)}-"
            f"${max((item.budget or 0 for item in items), default=0)}"
        ),
        "transition_rows_upper_bound": transitions,
        "runtime_estimate": f"{minimum_hours:.1f}-{maximum_hours:.1f} hours",
        "database_estimate": f"{low_storage:.1f}-{high_storage:.1f} GiB",
        "largest_action_only_full_tree": f"10^{full_action_log10:.1f} paths",
        "note": "Runtime and storage are planning estimates; state merging can reduce work.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILE_DEFAULTS), default="small")
    parser.add_argument(
        "--output", type=Path, default=Path("datasets/sock_tree_space.sqlite")
    )
    parser.add_argument("--roommates", nargs="+", type=int, choices=range(1, 11))
    parser.add_argument(
        "--days", nargs="+", type=int, choices=range(1, 1001),
        help="one or more game lengths from 1 through 1000",
    )
    parser.add_argument(
        "--sock-levels", nargs="+", type=float,
        help="normalized positions in the legal sock interval [0,1]",
    )
    parser.add_argument(
        "--budget-levels", nargs="+", type=float,
        help="normalized positions in the budget interval [0,1]",
    )
    parser.add_argument(
        "--full-grid", action="store_true",
        help="cross every sock level with every budget level",
    )
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--unit", type=int, choices=(4, 5), default=4)
    parser.add_argument("--chance-samples", type=int)
    parser.add_argument(
        "--max-trajectories", "--max-joint-profiles",
        dest="max_trajectories", type=int,
        help="maximum sequential day trajectories per state and chance sample",
    )
    parser.add_argument("--max-states", type=int)
    parser.add_argument("--limit-scenarios", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    items = build_scenarios(args)
    if args.limit_scenarios is not None:
        items = items[:args.limit_scenarios]
    plan = workload(items)
    print(json.dumps({"profile": args.profile, "output": str(args.output), **plan}, indent=2))
    if args.dry_run:
        return

    connection = connect(args.output)
    started = time.monotonic()
    executed = 0
    try:
        for index, scenario in enumerate(items, 1):
            print(
                f"[{index}/{len(items)}] n={scenario.roommates} C={scenario.capacity} "
                f"budget={scenario.budget} seed={scenario.seed}",
                flush=True,
            )
            executed += int(run_scenario(connection, scenario, args.profile))
    finally:
        connection.close()
    print(json.dumps({
        "complete": True,
        "executed_scenarios": executed,
        "elapsed_hours": (time.monotonic() - started) / 3600,
    }, indent=2))


if __name__ == "__main__":
    main()
