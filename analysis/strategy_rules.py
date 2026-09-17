"""Derive conditional strategy evidence from the recorded SQLite dataset.

The report deliberately distinguishes player-observable rules from oracle-only
diagnostics.  It does not run new simulations.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from core.engine import EMBARRASSMENT_THRESHOLD, PACK_COST, PACK_SIZE, SOCKLESS_PENALTY


@dataclass
class Action:
    wear: str
    discard: tuple[int, ...]
    discard_shades: tuple[int, ...]
    immediate: float
    focal: float
    household: float
    spend: float
    household_sockless: float
    focal_sockless: float
    train_focal: float
    train_household: float
    train_spend: float
    train_household_sockless: float
    train_focal_sockless: float


class Summary:
    def __init__(self) -> None:
        self.count = 0
        self.values: defaultdict[str, float] = defaultdict(float)
        self.counts: Counter[str] = Counter()

    def add(self, **values: float) -> None:
        self.count += 1
        for key, value in values.items():
            self.values[key] += value

    def mark(self, key: str, condition: bool = True) -> None:
        if condition:
            self.counts[key] += 1

    def mean(self, key: str) -> float:
        return self.values[key] / self.count if self.count else 0.0

    def pct(self, key: str) -> float:
        return 100.0 * self.counts[key] / self.count if self.count else 0.0


def budget_label(budget: float | None, roommates: int) -> str:
    return "unlimited" if budget is None else f"${budget / roommates:g}"


def remaining_budget_bucket(remaining: float | None, roommates: int) -> str:
    if remaining is None:
        return "unlimited"
    replacement_socks_per_person = math.floor(remaining / PACK_COST) * PACK_SIZE / roommates
    if replacement_socks_per_person == 0:
        return "0 replacement socks/person"
    if replacement_socks_per_person < 2:
        return "<2 replacement socks/person"
    if replacement_socks_per_person < 6:
        return "2-<6 replacement socks/person"
    return "6+ replacement socks/person"


def stock_bucket(active_socks: int, roommates: int) -> str:
    ratio = active_socks / roommates
    if ratio < 2:
        return "<2 socks/person"
    if ratio < 4:
        return "2-<4 socks/person"
    if ratio < 6:
        return "4-<6 socks/person"
    if ratio < 10:
        return "6-<10 socks/person"
    return "10+ socks/person"


def phase(day: int, days: int) -> str:
    fraction = day / days
    if fraction <= 1 / 3:
        return "early"
    if fraction <= 2 / 3:
        return "middle"
    return "late"


def color(shade: int) -> str:
    # Legal shades leave a wide gap: black stops at 64 and white stops at 127.
    return "black" if shade <= 64 else "white"


def state_socks(drawer_json: str, offered_json: str, returning_json: str) -> list[int]:
    drawer = [int(item[1]) for item in json.loads(drawer_json)]
    offered = [int(item) for item in json.loads(offered_json)]
    returning = [int(item[1]) for item in json.loads(returning_json)]
    return drawer + offered + returning


def distribution_features(shades: list[int]) -> dict[str, float]:
    by_color = {
        name: [shade for shade in shades if color(shade) == name]
        for name in ("white", "black")
    }
    partnerable = 0
    for group in by_color.values():
        for index, shade in enumerate(group):
            if any(abs(shade - other) <= EMBARRASSMENT_THRESHOLD
                   for j, other in enumerate(group) if j != index):
                partnerable += 1
    total = len(shades)
    return {
        "white": len(by_color["white"]),
        "black": len(by_color["black"]),
        "imbalance": abs(len(by_color["white"]) - len(by_color["black"])) / total
        if total else 1.0,
        "partnerable": partnerable / total if total else 0.0,
    }


def discard_quality(discarded: tuple[int, ...], state: list[int]) -> tuple[str, str]:
    if not discarded:
        return "keep", "neutral"
    isolated = []
    for shade in discarded:
        peers = [other for other in state if color(other) == color(shade)]
        try:
            peers.remove(shade)
        except ValueError:
            pass
        isolated.append(not any(abs(shade - other) <= EMBARRASSMENT_THRESHOLD for other in peers))

    before = distribution_features(state)["imbalance"]
    remaining = list(state)
    for shade in discarded:
        try:
            remaining.remove(shade)
        except ValueError:
            pass
    after = distribution_features(remaining)["imbalance"]
    balance = "improves balance" if after < before else "worsens balance" if after > before else "neutral"
    quality = "isolated only" if all(isolated) else "includes pairable sock"
    return quality, balance


def direct_pack_trigger(action: Action, pending_white: int, pending_black: int,
                        budget_remaining: float | None) -> bool:
    if budget_remaining is not None and budget_remaining < PACK_COST:
        return False
    white = sum(color(shade) == "white" for shade in action.discard_shades)
    black = len(action.discard_shades) - white
    return ((pending_white % PACK_SIZE) + white >= PACK_SIZE
            or (pending_black % PACK_SIZE) + black >= PACK_SIZE)


def fmt(value: float, digits: int = 2) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def cell_class(value: float, good_positive: bool = True) -> str:
    beneficial = value > 0 if good_positive else value < 0
    harmful = value < 0 if good_positive else value > 0
    return "good" if beneficial else "bad" if harmful else "neutral"


def table(headers: list[str], rows: list[list[str]], classes: list[list[str]] | None = None) -> str:
    head = "".join(f"<th>{html.escape(item)}</th>" for item in headers)
    body = []
    for row_index, row in enumerate(rows):
        cells = []
        for column, item in enumerate(row):
            cls = "" if classes is None else f' class="{classes[row_index][column]}"'
            cells.append(f"<td{cls}>{html.escape(str(item))}</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def analyze(database: Path) -> dict:
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    completed = connection.execute(
        "SELECT COUNT(*) FROM runs WHERE status='complete'"
    ).fetchone()[0]
    if completed == 0:
        raise ValueError("database has no completed runs")

    policy_rows = connection.execute(
        """
        SELECT policy,
               CASE WHEN budget IS NULL THEN 'unlimited'
                    ELSE printf('$%g', budget/roommates) END AS budget_pp,
               COUNT(*) AS runs,
               AVG(baseline_embarrassment/roommates) AS embarrassment_pp,
               AVG(1.0*baseline_sockless/roommates) AS sockless_pp,
               AVG(baseline_spent/roommates) AS spend_pp
        FROM runs
        GROUP BY policy, budget_pp
        """
    ).fetchall()

    sql = """
        SELECT d.id AS decision_id, r.roommates, r.capacity, r.days, r.budget,
               r.policy, d.day, d.drawer_size, d.offered_count, d.drawer_json,
               d.offered_json, d.returning_json, d.pending_white, d.pending_black,
               d.budget_remaining, d.effective_horizon,
               d.baseline_wear_json, d.baseline_discard_json,
               a.wear_json, a.discard_json, a.discard_shades_json,
               a.immediate_embarrassment,
               AVG(CASE WHEN o.repeat_index >= 2 THEN o.focal_embarrassment END) AS focal,
               AVG(CASE WHEN o.repeat_index >= 2 THEN o.household_embarrassment END) AS household,
               AVG(CASE WHEN o.repeat_index >= 2 THEN o.household_spend END) AS spend,
               AVG(CASE WHEN o.repeat_index >= 2 THEN o.household_sockless END) AS household_sockless,
               AVG(CASE WHEN o.repeat_index >= 2
                        THEN CAST(o.focal_embarrassment / ? AS INTEGER) END) AS focal_sockless,
               AVG(CASE WHEN o.repeat_index < 2 THEN o.focal_embarrassment END) AS train_focal,
               AVG(CASE WHEN o.repeat_index < 2 THEN o.household_embarrassment END) AS train_household,
               AVG(CASE WHEN o.repeat_index < 2 THEN o.household_spend END) AS train_spend,
               AVG(CASE WHEN o.repeat_index < 2 THEN o.household_sockless END) AS train_household_sockless,
               AVG(CASE WHEN o.repeat_index < 2
                        THEN CAST(o.focal_embarrassment / ? AS INTEGER) END) AS train_focal_sockless
        FROM decision_points d
        JOIN runs r ON r.id=d.run_id
        JOIN action_choices a ON a.decision_id=d.id
        JOIN rollouts o ON o.action_id=a.id
        WHERE r.policy='greedy' AND r.roommates > 1
        GROUP BY a.id
        ORDER BY d.id, a.id
    """

    matrix: defaultdict[tuple, Summary] = defaultdict(Summary)
    operational: defaultdict[tuple, Summary] = defaultdict(Summary)
    conserve_rule: defaultdict[tuple, Summary] = defaultdict(Summary)
    timing: defaultdict[tuple, Summary] = defaultdict(Summary)
    distribution: defaultdict[tuple, Summary] = defaultdict(Summary)
    closest = Summary()
    limitations = Counter()

    current_id = None
    current_rows: list[sqlite3.Row] = []

    def process(rows: list[sqlite3.Row]) -> None:
        if not rows:
            return
        first = rows[0]
        n = int(first["roommates"])
        horizon = int(first["effective_horizon"])
        active = (int(first["drawer_size"]) + int(first["offered_count"])
                  + len(json.loads(first["returning_json"])))
        stock = stock_bucket(active, n)
        remaining = remaining_budget_bucket(first["budget_remaining"], n)
        budget = budget_label(first["budget"], n)
        game_phase = phase(int(first["day"]), int(first["days"]))
        state = state_socks(first["drawer_json"], first["offered_json"], first["returning_json"])
        state_distribution = distribution_features(state)

        actions = [
            Action(
                wear=row["wear_json"],
                discard=tuple(json.loads(row["discard_json"])),
                discard_shades=tuple(json.loads(row["discard_shades_json"])),
                immediate=float(row["immediate_embarrassment"]),
                focal=float(row["focal"]),
                household=float(row["household"]),
                spend=float(row["spend"]),
                household_sockless=float(row["household_sockless"]),
                focal_sockless=float(row["focal_sockless"]),
                train_focal=float(row["train_focal"]),
                train_household=float(row["train_household"]),
                train_spend=float(row["train_spend"]),
                train_household_sockless=float(row["train_household_sockless"]),
                train_focal_sockless=float(row["train_focal_sockless"]),
            )
            for row in rows
        ]
        greedy = next(
            (action for action in actions
             if action.wear == first["baseline_wear_json"]
             and json.dumps(action.discard) == first["baseline_discard_json"]),
            None,
        )
        if greedy is None:
            limitations["missing greedy reference"] += 1
            return

        def other(action: Action) -> float:
            return (action.household - action.focal) / (n - 1)

        def train_other(action: Action) -> float:
            return (action.train_household - action.train_focal) / (n - 1)

        def full_focal(action: Action) -> float:
            return (action.train_focal + action.focal) / 2

        def full_household(action: Action) -> float:
            return (action.train_household + action.household) / 2

        def full_other(action: Action) -> float:
            return (full_household(action) - full_focal(action)) / (n - 1)

        def full_focal_sockless(action: Action) -> float:
            return (action.train_focal_sockless + action.focal_sockless) / 2

        def full_household_sockless(action: Action) -> float:
            return (action.train_household_sockless + action.household_sockless) / 2

        def full_spend(action: Action) -> float:
            return (action.train_spend + action.spend) / 2

        min_immediate = min(action.immediate for action in actions)
        defensive = min(actions, key=lambda action: (
            action.train_focal_sockless, action.train_focal, action.train_spend,
            -train_other(action)
        ))
        safe = [
            action for action in actions
            if action.train_focal_sockless <= greedy.train_focal_sockless + 1e-12
            and action.train_focal <= greedy.train_focal + 1e-12
        ]
        adversarial = max(safe, key=lambda action: (
            train_other(action) - action.train_focal, -action.train_focal_sockless,
            -action.train_focal, -action.train_spend
        ))

        closest.add(
            defensive_is_closest=float(defensive.immediate == min_immediate),
            adversarial_is_closest=float(adversarial.immediate == min_immediate),
        )

        margin_before = (other(greedy) - greedy.focal) / horizon
        margin_after = (other(adversarial) - adversarial.focal) / horizon
        focal_delta = (adversarial.focal - greedy.focal) / horizon
        other_delta = (other(adversarial) - other(greedy)) / horizon
        key = (n, budget, stock)
        summary = matrix[key]
        summary.add(
            greedy_focal=greedy.focal / horizon,
            chosen_focal=adversarial.focal / horizon,
            greedy_other=other(greedy) / horizon,
            chosen_other=other(adversarial) / horizon,
            margin_gain=margin_after - margin_before,
            focal_delta=focal_delta,
            other_delta=other_delta,
            active_per_person=active / n,
        )
        summary.mark("main_lower", margin_after > 0)
        summary.mark("discard", bool(adversarial.discard))
        summary.mark("closest", adversarial.immediate == min_immediate)
        summary.mark("opponents_worse", other_delta > 0)
        summary.mark("heldout_self_safe", focal_delta <= 0)
        summary.mark("heldout_adversarial_win", focal_delta <= 0 and other_delta > 0)

        op_key = (n, remaining, stock)
        op = operational[op_key]
        op.add(margin_gain=margin_after - margin_before, focal_delta=focal_delta,
               other_delta=other_delta)
        op.mark("main_lower", margin_after > 0)
        op.mark("discard", bool(adversarial.discard))
        op.mark("heldout_self_safe", focal_delta <= 0)
        op.mark("heldout_adversarial_win", focal_delta <= 0 and other_delta > 0)

        conserve = next(
            (action for action in actions
             if action.wear == greedy.wear and not action.discard),
            None,
        )
        if conserve is not None:
            conserve_focal_delta = (full_focal(conserve) - full_focal(greedy)) / horizon
            conserve_other_delta = (full_other(conserve) - full_other(greedy)) / horizon
            conserve_sockless_delta = full_focal_sockless(conserve) - full_focal_sockless(greedy)
            fixed = conserve_rule[key]
            fixed.add(
                focal_delta=conserve_focal_delta,
                other_delta=conserve_other_delta,
                focal_sockless_delta=conserve_sockless_delta,
                spend_delta=full_spend(conserve) - full_spend(greedy),
            )
            fixed.mark("self_better", conserve_focal_delta < 0 and conserve_sockless_delta <= 0)
            fixed.mark("self_worse", conserve_focal_delta > 0 or conserve_sockless_delta > 0)
            fixed.mark("adversarial_win", conserve_focal_delta <= 0 and conserve_other_delta > 0)

        no_discard = {action.wear: action for action in actions if not action.discard}
        for action in actions:
            if not action.discard:
                continue
            base = no_discard.get(action.wear)
            if base is None:
                continue
            self_delta = (full_focal(action) - full_focal(base)) / horizon
            others_delta = (full_other(action) - full_other(base)) / horizon
            self_sockless_delta = full_focal_sockless(action) - full_focal_sockless(base)
            action_other_sockless = (
                full_household_sockless(action) - full_focal_sockless(action)
            ) / (n - 1)
            base_other_sockless = (
                full_household_sockless(base) - full_focal_sockless(base)
            ) / (n - 1)
            others_sockless_delta = action_other_sockless - base_other_sockless
            trigger = direct_pack_trigger(
                action, int(first["pending_white"]), int(first["pending_black"]),
                first["budget_remaining"],
            )
            quality, balance = discard_quality(action.discard_shades, state)
            timing_key = (game_phase, remaining, stock, "triggers pack" if trigger else "no direct pack")
            item = timing[timing_key]
            item.add(self_delta=self_delta, others_delta=others_delta,
                     self_sockless_delta=self_sockless_delta,
                     others_sockless_delta=others_sockless_delta,
                     spend_delta=full_spend(action) - full_spend(base))
            item.mark("self_better", self_delta < 0 and self_sockless_delta <= 0)
            item.mark("self_worse", self_delta > 0 or self_sockless_delta > 0)
            item.mark("adversarial_win", self_delta <= 0 and others_delta > 0)

            stock_regime = "6+ socks/person" if stock in {
                "6-<10 socks/person", "10+ socks/person"
            } else "<6 socks/person"
            replacement_regime = (
                "replacement available"
                if remaining != "0 replacement socks/person"
                else "no replacement capacity"
            )
            dist_key = (quality, balance, stock_regime, replacement_regime)
            dist = distribution[dist_key]
            dist.add(self_delta=self_delta, others_delta=others_delta,
                     self_sockless_delta=self_sockless_delta,
                     partnerability=state_distribution["partnerable"],
                     imbalance=state_distribution["imbalance"])
            dist.mark("self_better", self_delta < 0 and self_sockless_delta <= 0)
            dist.mark("adversarial_win", self_delta <= 0 and others_delta > 0)

    for row in connection.execute(sql, (SOCKLESS_PENALTY, SOCKLESS_PENALTY)):
        decision_id = row["decision_id"]
        if current_id is not None and decision_id != current_id:
            process(current_rows)
            current_rows = []
        current_id = decision_id
        current_rows.append(row)
    process(current_rows)
    connection.close()

    return {
        "completed_runs": completed,
        "policy_rows": [dict(row) for row in policy_rows],
        "matrix": matrix,
        "operational": operational,
        "conserve_rule": conserve_rule,
        "timing": timing,
        "distribution": distribution,
        "closest": closest,
        "limitations": limitations,
    }


def write_csv(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow([
            "roommates", "starting_budget_per_person", "active_stock_bucket", "decisions",
            "mean_active_socks_per_person", "greedy_focal_embarrassment_per_day",
            "selected_focal_embarrassment_per_day", "greedy_other_average_per_day",
            "selected_other_average_per_day", "margin_gain_per_day", "focal_delta_per_day",
            "other_delta_per_day", "main_below_other_pct", "selected_discards_pct",
            "selected_closest_pair_pct", "heldout_self_safe_pct",
            "heldout_self_safe_and_others_worse_pct",
        ])
        for key, summary in sorted(report["matrix"].items()):
            n, budget, stock = key
            writer.writerow([
                n, budget, stock, summary.count, summary.mean("active_per_person"),
                summary.mean("greedy_focal"), summary.mean("chosen_focal"),
                summary.mean("greedy_other"), summary.mean("chosen_other"),
                summary.mean("margin_gain"), summary.mean("focal_delta"),
                summary.mean("other_delta"), summary.pct("main_lower"),
                summary.pct("discard"), summary.pct("closest"),
                summary.pct("heldout_self_safe"), summary.pct("heldout_adversarial_win"),
            ])


def write_html(report: dict, path: Path, csv_path: Path) -> None:
    policy_order = {"$0": 0, "$25": 1, "$100": 2, "$300": 3, "unlimited": 4}
    policy_rows = sorted(report["policy_rows"], key=lambda row: (
        policy_order.get(row["budget_pp"], 99), row["policy"]
    ))
    policy_table = table(
        ["Budget/person", "Policy", "Runs", "Embarrassment/person", "Sockless/person", "Spend/person"],
        [[row["budget_pp"], row["policy"], row["runs"], fmt(row["embarrassment_pp"], 0),
          fmt(row["sockless_pp"]), f'${fmt(row["spend_pp"])}'] for row in policy_rows],
    )

    matrix_rows = []
    matrix_classes = []
    for (n, budget, stock), summary in sorted(report["matrix"].items(), key=lambda item: (
        item[0][0], policy_order.get(item[0][1], 99), item[0][2]
    )):
        matrix_rows.append([
            n, budget, stock, summary.count, fmt(summary.mean("active_per_person")),
            fmt(summary.mean("focal_delta")), fmt(summary.mean("other_delta")),
            fmt(summary.mean("margin_gain")), f'{summary.pct("main_lower"):.1f}%',
            f'{summary.pct("discard"):.1f}%', f'{summary.pct("closest"):.1f}%',
            f'{summary.pct("heldout_self_safe"):.1f}%',
            f'{summary.pct("heldout_adversarial_win"):.1f}%',
        ])
        matrix_classes.append([
            "", "", "", "", "", cell_class(summary.mean("focal_delta"), False),
            cell_class(summary.mean("other_delta"), True),
            cell_class(summary.mean("margin_gain"), True), "", "", "", "", "",
        ])
    matrix_table = table(
        ["n", "Start budget/person", "Active stock", "States", "Mean socks/person",
         "Our Δ embarrassment/day", "Others' Δ/day", "Relative-margin gain/day",
         "Our score lower", "Chosen action discards", "Chosen pair is closest",
         "Held-out self-safe", "Held-out self-safe + others worse"],
        matrix_rows, matrix_classes,
    )

    operational_rows = []
    operational_classes = []
    for (n, remaining, stock), summary in sorted(report["operational"].items()):
        operational_rows.append([
            n, remaining, stock, summary.count, fmt(summary.mean("focal_delta")),
            fmt(summary.mean("other_delta")), fmt(summary.mean("margin_gain")),
            f'{summary.pct("main_lower"):.1f}%', f'{summary.pct("discard"):.1f}%',
            f'{summary.pct("heldout_self_safe"):.1f}%',
            f'{summary.pct("heldout_adversarial_win"):.1f}%',
        ])
        operational_classes.append([
            "", "", "", "", cell_class(summary.mean("focal_delta"), False),
            cell_class(summary.mean("other_delta"), True),
            cell_class(summary.mean("margin_gain"), True), "", "", "", "",
        ])
    operational_table = table(
        ["n", "Remaining purchasing power", "Active stock", "States",
         "Our Δ/day", "Others' Δ/day", "Margin gain/day", "Our score lower", "Discard selected",
         "Held-out self-safe", "Held-out self-safe + others worse"],
        operational_rows, operational_classes,
    )

    conserve_rows = []
    conserve_classes = []
    for (n, budget, stock), summary in sorted(report["conserve_rule"].items(), key=lambda item: (
        item[0][0], policy_order.get(item[0][1], 99), item[0][2]
    )):
        conserve_rows.append([
            n, budget, stock, summary.count, fmt(summary.mean("focal_delta")),
            fmt(summary.mean("other_delta")), fmt(summary.mean("focal_sockless_delta"), 3),
            f'${fmt(summary.mean("spend_delta"))}', f'{summary.pct("self_better"):.1f}%',
            f'{summary.pct("self_worse"):.1f}%', f'{summary.pct("adversarial_win"):.1f}%',
        ])
        conserve_classes.append([
            "", "", "", "", cell_class(summary.mean("focal_delta"), False),
            cell_class(summary.mean("other_delta"), True),
            cell_class(summary.mean("focal_sockless_delta"), False), "", "", "", "",
        ])
    conserve_table = table(
        ["n", "Start budget/person", "Active stock", "States", "Our Δ/day",
         "Others' Δ/day", "Our Δ sockless/horizon", "Δ spend/horizon",
         "Self improves", "Self worsens", "Self safe + others worse"],
        conserve_rows, conserve_classes,
    )

    timing_rows = []
    timing_classes = []
    for key, summary in sorted(report["timing"].items()):
        game_phase, remaining, stock, trigger = key
        timing_rows.append([
            game_phase, remaining, stock, trigger, summary.count,
            fmt(summary.mean("self_delta")), fmt(summary.mean("others_delta")),
            fmt(summary.mean("spend_delta")), f'{summary.pct("self_better"):.1f}%',
            f'{summary.pct("self_worse"):.1f}%', f'{summary.pct("adversarial_win"):.1f}%',
        ])
        timing_classes.append([
            "", "", "", "", "", cell_class(summary.mean("self_delta"), False),
            cell_class(summary.mean("others_delta"), True), "", "", "", "",
        ])
    timing_table = table(
        ["Phase", "Remaining purchasing power", "Active stock", "Intentional discard",
         "Comparisons", "Our Δ/day", "Others' Δ/day", "Δ spend/horizon",
         "Self improves", "Self worsens", "Self safe + others worse"],
        timing_rows, timing_classes,
    )

    distribution_rows = []
    distribution_classes = []
    for (quality, balance, stock_regime, replacement_regime), summary in sorted(
        report["distribution"].items()
    ):
        distribution_rows.append([
            quality, balance, stock_regime, replacement_regime, summary.count,
            fmt(100 * summary.mean("partnerability"), 1) + "%",
            fmt(100 * summary.mean("imbalance"), 1) + "%", fmt(summary.mean("self_delta")),
            fmt(summary.mean("others_delta")), f'{summary.pct("self_better"):.1f}%',
            f'{summary.pct("adversarial_win"):.1f}%',
        ])
        distribution_classes.append([
            "", "", "", "", "", "", "", cell_class(summary.mean("self_delta"), False),
            cell_class(summary.mean("others_delta"), True), "", "",
        ])
    distribution_table = table(
        ["Discarded sock", "Color balance effect", "Stock regime", "Replacement regime",
         "Comparisons", "State pairability", "State color imbalance", "Our Δ/day",
         "Others' Δ/day", "Self improves", "Self safe + others worse"],
        distribution_rows, distribution_classes,
    )

    closest = report["closest"]
    matrix_summaries = list(report["matrix"].values())
    decision_count = sum(summary.count for summary in matrix_summaries)
    heldout_self_safe = (
        100 * sum(summary.counts["heldout_self_safe"] for summary in matrix_summaries)
        / decision_count
    )
    heldout_adversarial = (
        100 * sum(summary.counts["heldout_adversarial_win"] for summary in matrix_summaries)
        / decision_count
    )
    css = """
    :root{font-family:Inter,system-ui,sans-serif;color:#17212b;background:#f3f6f7}
    *{box-sizing:border-box}body{margin:0}main{max-width:1500px;margin:auto;padding:30px 22px 60px}
    h1{margin:0 0 8px;font-size:30px}h2{margin:0 0 10px;font-size:20px}h3{margin:24px 0 8px}
    p{line-height:1.5}.intro{color:#50606c;max-width:950px}.card{background:#fff;border:1px solid #d9e2e5;border-radius:14px;padding:20px;margin:18px 0}
    .rules{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.rule{background:#edf5f4;border-left:4px solid #187a72;padding:14px;border-radius:7px;line-height:1.45}
    .warning{background:#fff3db;border-left:4px solid #b37b20;padding:14px 16px;border-radius:7px}.scroll{overflow:auto;max-height:620px;border:1px solid #e2e8ea}
    table{border-collapse:collapse;width:100%;font-size:12px;background:#fff}th{position:sticky;top:0;background:#eaf0f2;z-index:1}th,td{padding:8px 9px;border-bottom:1px solid #e4eaec;text-align:right;white-space:nowrap}th:first-child,td:first-child{text-align:left}tr:hover td{background:#f5f8f9}
    td.good{background:#e5f4ed;color:#11634d;font-weight:650}td.bad{background:#fae9e7;color:#9a3e3e;font-weight:650}td.neutral{color:#64737d}
    code{background:#e9eef0;padding:2px 5px;border-radius:4px}.legend{font-size:13px;color:#50606c}.observable{color:#11634d;font-weight:700}.oracle{color:#8a5e15;font-weight:700}
    @media(max-width:900px){.rules{grid-template-columns:1fr}main{padding:20px 12px}table{font-size:11px}}
    """
    page = f"""<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
    <title>Conditional sock strategy rules</title><style>{css}</style><main>
    <h1>Conditional strategy evidence from the completed dataset</h1>
    <p class="intro">This report uses the existing 800-scenario SQLite dataset only. The adversarial tables restrict the background to greedy roommates and evaluate a single focal action over its recorded seven-day future. Green cells favor the main player; red cells are adverse.</p>
    <div class="warning"><strong>Evidence boundary.</strong> Repeats 0-1 select an action that appears no worse than greedy for the focal player and maximizes the gap between the average other roommate and the focal player. Every displayed consequence is then measured only on held-out repeats 2-3. Other roommates are available only as an average because the dataset stores focal and household totals, not each opponent separately. Actions used different random streams and the holdout has only two repeats, so small differences are hypotheses rather than guarantees.</div>
    <section class="card"><h2>Rules supported most strongly</h2><div class="rules">
    <div class="rule"><strong>Protect two socks per person.</strong><br>Below this active-stock ratio, it is mathematically impossible for everyone to dress that day. Between two and four, everyone may dress but full four-sock hands cannot be guaranteed.</div>
    <div class="rule"><strong>Keep $10 replacement capacity.</strong><br>Below one pack, intentional discards cannot be replenished. Six pending socks of one color matter; pending counts do not combine across colors.</div>
    <div class="rule"><strong>Take the closest pair.</strong><br>{closest.mean('defensive_is_closest') * 100:.1f}% of focal-score-minimizing actions and {closest.mean('adversarial_is_closest') * 100:.1f}% of self-safe adversarial actions use a minimum-embarrassment pair.</div>
    <div class="rule"><strong>No reliable sabotage action was found.</strong><br>The training-selected action remained self-safe in {heldout_self_safe:.1f}% of held-out states, but was self-safe while making others worse in only {heldout_adversarial:.1f}%.</div>
    <div class="rule"><strong>Distribution adjustments are weak at safe stock.</strong><br>With at least six active socks per person and replacement capacity, discard effects average close to zero. The large harms appear after stock falls below four per person.</div>
    <div class="rule"><strong>Persistent behavior matters more than one turn.</strong><br>A single keep-versus-discard deviation changes little over seven days; complete 360-day conserve and greedy runs diverge sharply under finite budgets.</div>
    </div></section>
    <section class="card"><h2>Long-run policy baseline by starting budget</h2><p class="legend">These are complete 360-day runs, averaged per person. They are stronger evidence than the local action comparisons.</p>{policy_table}</section>
    <section class="card"><h2>Budget × socks/person × roommates: training-selected response to greedy</h2><p class="legend">The action is selected on two training repeats and evaluated on two held-out repeats. A negative “Our Δ” means the chosen action improves our held-out score versus greedy in the same recorded state. A positive “Others' Δ” means the other roommates' held-out average embarrassment rises. Download the exact values from <code>{html.escape(csv_path.name)}</code>.</p>{matrix_table}</section>
    <section class="card"><h2>Fixed defense: closest pair and keep every leftover</h2><p class="legend">This pre-specified rule changes only greedy's intentional discard. It uses all four repeats because the rule was not selected from their outcomes. It is the cleanest existing-data test of defending against greedy without learning an oracle action per state.</p>{conserve_table}</section>
    <section class="card"><h2>Operational matrix using remaining budget</h2><p class="legend"><span class="observable">Observable:</span> roommate count and budget remaining. <span class="oracle">Not observable:</span> active socks/person. Use the stock column to understand risk, not as a direct tournament rule.</p>{operational_table}</section>
    <section class="card"><h2>When discarding helped or hurt</h2><p class="legend">Each discard action is matched to keeping the same leftovers while wearing the same pair in the same state. “Triggers pack” means the intentional discard alone crosses a six-sock pending boundary and at least $10 remains.</p>{timing_table}</section>
    <section class="card"><h2>Distribution effects of discarded socks</h2><p class="legend">“Pairable” means the discarded sock had another same-color sock within six shade points in the active inventory. These are oracle explanations; a player sees only its hand.</p>{distribution_table}</section>
    <section class="card"><h2>What a real player can and cannot condition on</h2>
    <p><span class="observable">Can observe:</span> offered shades, day, starting capacity, roommate count, selection unit, total household spending, remaining budget, and its own embarrassment history.</p>
    <p><span class="oracle">Cannot observe:</span> current drawer size or distribution, pending counts by color, draw order position, other players' scores, their hands, or whether a discard will complete a pack. Rules using those fields require a conservative estimate or a learned probability based on observable history.</p>
    <p>The engine randomizes roommate order daily. The dataset therefore provides no mechanism that can guarantee every opponent scores worse than the main player. It can identify actions that increased the opponents' <em>average</em> score without increasing the focal mean in sampled states.</p></section>
    </main></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(page, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", nargs="?", type=Path,
                        default=Path("datasets/socks_counterfactual.sqlite"))
    parser.add_argument("--output", type=Path, default=Path("results/strategy_rules.html"))
    parser.add_argument("--csv", type=Path, default=Path("results/strategy_rules_matrix.csv"))
    args = parser.parse_args()
    report = analyze(args.database)
    write_csv(report, args.csv)
    write_html(report, args.output, args.csv)
    print(args.output.resolve())
    print(args.csv.resolve())


if __name__ == "__main__":
    main()
