"""Build a bounded multi-roommate sock decision tree in SQLite.

The literal tree is intractable: a four-roommate day can have 24**4 joint
actions before chance outcomes.  This explorer enumerates every individual
legal action, samples joint interactions, samples random orders/draws/holes,
merges equivalent next-day states, and retains a diverse state frontier.
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
    requested_socks_per_person: float
    budget: int | None
    seed: int
    days: int
    unit: int
    chance_samples: int
    max_joint_profiles: int
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


PROFILE_DEFAULTS = {
    "small": {
        "roommates": (2, 4),
        "sock_ratios": (8.0, 12.0),
        "budgets_per_person": (0, 100, None),
        "seeds": (1,),
        "days": 10,
        "chance_samples": 2,
        "max_joint_profiles": 128,
        "max_states": 50,
    },
    "research": {
        "roommates": (2, 4, 6),
        "sock_ratios": (8.0, 12.0, 16.0),
        "budgets_per_person": (0, 25, 100, 300, None),
        "seeds": (1,),
        "days": 10,
        "chance_samples": 3,
        "max_joint_profiles": 192,
        "max_states": 200,
    },
    "large": {
        "roommates": (2, 4, 6),
        "sock_ratios": (8.0, 12.0, 16.0),
        "budgets_per_person": (0, 25, 100, 300, None),
        "seeds": (1, 2),
        "days": 10,
        "chance_samples": 4,
        "max_joint_profiles": 384,
        "max_states": 400,
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
    requested_socks_per_person REAL NOT NULL,
    actual_socks_per_person REAL NOT NULL,
    budget INTEGER,
    budget_per_person REAL,
    seed INTEGER NOT NULL,
    days INTEGER NOT NULL,
    unit INTEGER NOT NULL,
    chance_samples INTEGER NOT NULL,
    max_joint_profiles INTEGER NOT NULL,
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
    theoretical_joint_profiles_log10 REAL NOT NULL DEFAULT 0,
    explored_joint_profiles INTEGER NOT NULL DEFAULT 0,
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
    joint_profiles_theoretical INTEGER NOT NULL,
    pairwise_profiles_possible INTEGER NOT NULL,
    pairwise_profiles_explored INTEGER NOT NULL,
    profiles_explored INTEGER NOT NULL,
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
    profile_index INTEGER NOT NULL,
    profile_indices_json TEXT NOT NULL,
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


def round_capacity(roommates: int, ratio: float, unit: int = 4) -> int:
    requested = math.ceil(roommates * ratio)
    minimum = unit * roommates + 11
    capacity = max(requested, minimum)
    return 4 * math.ceil(capacity / 4)


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


def joint_profiles(
    hands: list[list[Sock]], max_profiles: int, seed: int,
    action_lists: list[list[Choice]] | None = None,
) -> tuple[list[tuple[int, ...]], int, int, int]:
    """Cover every marginal action, then sample pairwise interactions."""
    action_lists = action_lists or [choices(len(hand)) for hand in hands]
    sizes = [len(items) for items in action_lists]
    total = math.prod(sizes)
    if total <= max_profiles:
        profiles = list(itertools.product(*(range(size) for size in sizes)))
        pairwise = sum(sizes[a] * sizes[b] for a in range(len(sizes))
                       for b in range(a + 1, len(sizes)))
        return profiles, total, pairwise, pairwise

    baseline = tuple(
        baseline_choice_index(hand, available)
        for hand, available in zip(hands, action_lists, strict=True)
    )
    profiles = {baseline}
    for player, size in enumerate(sizes):
        for action_index in range(size):
            profile = list(baseline)
            profile[player] = action_index
            profiles.add(tuple(profile))
    if len(profiles) > max_profiles:
        raise ValueError(
            f"max_joint_profiles={max_profiles} cannot cover all {len(profiles)} "
            "individual actions; increase the limit"
        )

    pair_candidates = []
    for left in range(len(sizes)):
        for right in range(left + 1, len(sizes)):
            for left_action in range(sizes[left]):
                for right_action in range(sizes[right]):
                    pair_candidates.append((left, right, left_action, right_action))
    pairwise_possible = len(pair_candidates)
    rng = random.Random(seed)
    rng.shuffle(pair_candidates)
    explored_pairwise = 0
    for left, right, left_action, right_action in pair_candidates:
        if len(profiles) >= max_profiles:
            break
        profile = list(baseline)
        profile[left] = left_action
        profile[right] = right_action
        previous = len(profiles)
        profiles.add(tuple(profile))
        explored_pairwise += int(len(profiles) > previous)

    return sorted(profiles), total, pairwise_possible, explored_pairwise


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


def draw_day(
    state: State, scenario: Scenario, chance_index: int
) -> tuple[list[int], list[list[Sock]], list[Sock]]:
    rng = random.Random(stable_seed(scenario.seed, state.signature, state.day, chance_index, "draw"))
    order = list(range(scenario.roommates))
    rng.shuffle(order)
    drawer = list(state.drawer)
    hands: list[list[Sock]] = [[] for _ in range(scenario.roommates)]
    for player in order:
        picked = rng.sample(range(len(drawer)), min(scenario.unit, len(drawer)))
        picked.sort(reverse=True)
        hands[player] = [drawer.pop(index) for index in picked]
    return order, hands, drawer


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


def apply_profile(
    state: State,
    scenario: Scenario,
    chance_index: int,
    order: list[int],
    hands: list[list[Sock]],
    leftover_drawer: list[Sock],
    profile: tuple[int, ...],
    action_lists: list[list[Choice]] | None = None,
) -> tuple[State, TransitionMetrics, list[Choice]]:
    action_lists = action_lists or [choices(len(hand)) for hand in hands]
    selected = [available[index] for available, index in zip(action_lists, profile, strict=True)]
    returning: list[Sock] = []
    pending = {"white": state.pending_white, "black": state.pending_black}
    scores = list(state.scores)
    sockless = list(state.sockless)
    immediate = [0] * scenario.roommates
    household_discards = 0
    household_holes = 0
    main_holes = 0

    for player in order:
        hand = hands[player]
        action = selected[player]
        if len(hand) < 2:
            scores[player] += int(SOCKLESS_PENALTY)
            sockless[player] += 1
            immediate[player] = int(SOCKLESS_PENALTY)
            returning.extend(hand)
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
                returning.append(washed(sock))

        for hand_index, sock in enumerate(hand):
            if hand_index in action.wear:
                continue
            if hand_index in action.discard:
                pending[sock[0]] += 1
                household_discards += 1
            else:
                returning.append(sock)

    next_drawer = list(leftover_drawer) + returning
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
    return next_state, metrics, selected


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
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.executescript(SCHEMA)
    connection.execute(
        "INSERT OR REPLACE INTO metadata(key,value) VALUES('schema_version','3')"
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
    connection.execute(
        """
        INSERT OR IGNORE INTO scenarios(
            signature,profile,roommates,capacity,requested_socks_per_person,
            actual_socks_per_person,budget,budget_per_person,seed,days,unit,
            chance_samples,max_joint_profiles,max_states_per_day
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            scenario.signature, profile, scenario.roommates, scenario.capacity,
            scenario.requested_socks_per_person, scenario.capacity / scenario.roommates,
            scenario.budget, budget_pp, scenario.seed, scenario.days, scenario.unit,
            scenario.chance_samples, scenario.max_joint_profiles,
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
    scenario_id,day,parent_state_id,child_signature,context_id,profile_index,
    profile_indices_json,actions_json,immediate_scores_json,main_immediate,
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
            theoretical_joint_profiles_log10=0,explored_joint_profiles=0,
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
            order, hands, leftover = draw_day(state, scenario, chance_index)
            action_lists = [choices(len(hand)) for hand in hands]
            profiles, theoretical, pair_possible, pair_explored = joint_profiles(
                hands,
                scenario.max_joint_profiles,
                stable_seed(scenario.seed, state.signature, depth, chance_index, "profiles"),
                action_lists,
            )
            chance_realizations += 1
            explored_profiles += len(profiles)
            theoretical_profiles_total += theoretical
            context_cursor = connection.execute(
                """
                INSERT INTO chance_contexts(
                    scenario_id,day,parent_state_id,chance_index,
                    joint_profiles_theoretical,pairwise_profiles_possible,
                    pairwise_profiles_explored,profiles_explored,order_json,hands_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    scenario_id, day, int(source_row["id"]), chance_index,
                    theoretical, pair_possible, pair_explored, len(profiles),
                    json.dumps(order, separators=(",", ":")),
                    json.dumps(hands, separators=(",", ":")),
                ),
            )
            context_id = int(context_cursor.lastrowid)
            for profile_index, profile in enumerate(profiles):
                child, metrics, selected = apply_profile(
                    state, scenario, chance_index, order, hands, leftover, profile,
                    action_lists,
                )
                candidate = candidates.setdefault(child.signature, Candidate(child))
                candidate.add(parent_log_weight)
                other_immediate = (
                    sum(metrics.immediate_scores[1:]) / max(1, scenario.roommates - 1)
                )
                next_other = sum(child.scores[1:]) / max(1, scenario.roommates - 1)
                action_json = json.dumps([
                    {"wear": action.wear, "discard": action.discard}
                    for action in selected
                ], separators=(",", ":"))
                batch.append((
                    scenario_id, day, int(source_row["id"]), child.signature,
                    context_id, profile_index,
                    json.dumps(profile, separators=(",", ":")), action_json,
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
            theoretical_joint_profiles_log10=?,explored_joint_profiles=?,transition_rows=?,
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


def parse_budgets(values: list[str] | None, defaults: tuple[int | None, ...]) -> tuple[int | None, ...]:
    if values is None:
        return defaults
    result = []
    for value in values:
        result.append(None if value.lower() in {"none", "inf", "unlimited"} else int(value))
    return tuple(result)


def build_scenarios(args: argparse.Namespace) -> list[Scenario]:
    defaults = PROFILE_DEFAULTS[args.profile]
    roommate_values = tuple(args.roommates or defaults["roommates"])
    ratios = tuple(args.sock_ratios or defaults["sock_ratios"])
    budgets_pp = parse_budgets(args.budgets_per_person, defaults["budgets_per_person"])
    seeds = tuple(args.seeds or defaults["seeds"])
    days = args.days or defaults["days"]
    chance_samples = args.chance_samples or defaults["chance_samples"]
    max_profiles = args.max_joint_profiles or defaults["max_joint_profiles"]
    max_states = args.max_states or defaults["max_states"]
    # it should be different for 4-sock-senario and 5-sock-senario, then we need to keep the `unit`
    actions_per_hand = len(choices(args.unit))
    result = []
    for roommates, ratio, budget_pp, seed in itertools.product(
        roommate_values, ratios, budgets_pp, seeds
    ):
        capacity = round_capacity(roommates, ratio, args.unit)
        budget = None if budget_pp is None else budget_pp * roommates
        result.append(Scenario(
            roommates=roommates,
            capacity=capacity,
            requested_socks_per_person=ratio,
            budget=budget,
            seed=seed,
            days=days,
            unit=args.unit,
            chance_samples=chance_samples,
            # cover the baseline plus every player's individual deviations
            max_joint_profiles=max(max_profiles, 1 + roommates * (actions_per_hand - 1)),
            max_states_per_day=max_states,
        ))
    return result


def workload(items: list[Scenario]) -> dict[str, float | int | str]:
    transitions = sum(
        (1 + (item.days - 1) * item.max_states_per_day)
        * item.chance_samples
        * item.max_joint_profiles
        for item in items
    )
    minimum_hours = transitions / 5000 / 3600
    maximum_hours = transitions / 1000 / 3600
    low_storage = transitions * 300 / 1024**3
    high_storage = transitions * 900 / 1024**3
    full_action_log10 = max(
        (item.days * item.roommates * math.log10(len(choices(item.unit))) for item in items),
        default=0,
    )
    return {
        "scenarios": len(items),
        "transition_rows_upper_bound": transitions,
        "runtime_estimate": f"{minimum_hours:.1f}-{maximum_hours:.1f} hours",
        "database_estimate": f"{low_storage:.1f}-{high_storage:.1f} GiB",
        "largest_action_only_full_tree": f"10^{full_action_log10:.1f} paths",
        "note": "Runtime and storage are planning estimates; state merging can reduce work.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=tuple(PROFILE_DEFAULTS), default="small")
    parser.add_argument("--output", type=Path, default=Path("datasets/sock_tree.sqlite"))
    parser.add_argument("--roommates", nargs="+", type=int)
    parser.add_argument("--sock-ratios", nargs="+", type=float)
    parser.add_argument("--budgets-per-person", nargs="+")
    parser.add_argument("--seeds", nargs="+", type=int)
    parser.add_argument("--days", type=int)
    parser.add_argument("--unit", type=int, choices=(4, 5), default=4)
    parser.add_argument("--chance-samples", type=int)
    parser.add_argument("--max-joint-profiles", type=int)
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
