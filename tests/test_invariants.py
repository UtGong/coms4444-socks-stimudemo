"""Property-style checks over whole simulations.

The tests elsewhere pin single steps and exact handout numbers. These exist for
the other failure mode: a simulator that runs to completion, produces plausible
JSON, and is quietly wrong - socks appearing from nowhere, a shade drifting the
wrong way, money not adding up, one roommate always drawing first. None of that
shows in aggregate output, and none of it is visible in a one-day test.

Everything here walks a full run day by day and asserts the invariant on every
single day, not just at the end. A conservation bug that self-corrects before
the last day is still a bug.
"""

from collections import Counter

import pytest

from core.engine import PACK_COST, PACK_SIZE, Engine
from models.sock import (
	BLACK_CEILING,
	BLACK_FADE,
	BLACK_START,
	WHITE_FADE,
	WHITE_FLOOR,
	WHITE_START,
	Color,
)
from players.greedy_player import GreedyPlayer
from players.random_player import RandomPlayer

SEEDS = (1, 7, 4444)

# Enough days for the drawer to saturate at 127/64 and start churning holes,
# which is where most of these invariants would break if they were going to.
DAYS = 720

# Chosen against a measured natural spend of about $1150/year on the mixed
# roster: this exhausts partway through and takes the run over the cliff, so
# the same invariants get exercised while the drawer is shrinking.
TIGHT_BUDGET = 300.0


def engine_for(seed: int, budget: float | None = None, days: int = DAYS, **kwargs) -> Engine:
	settings = dict(
		players=[GreedyPlayer] * 2 + [RandomPlayer] * 2,
		capacity=40,
		selection_unit=4,
		days=days,
		seed=seed,
		timeout=0,
		budget=budget,
	)
	settings.update(kwargs)
	return Engine(**settings)


BUDGETS = [None, TIGHT_BUDGET]


def scenarios():
	return [(seed, budget) for seed in SEEDS for budget in BUDGETS]


# ---------------------------------------------------------------- conservation


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_socks_are_conserved_every_single_day(seed, budget):
	"""C, plus six per pack bought, minus every discard, is the drawer.

	Socks do not appear from nowhere and do not evaporate. Checked after each
	day rather than at the end, because a conservation error that cancels
	itself out later is still an error.
	"""
	engine = engine_for(seed, budget)
	packs = 0
	discards = 0

	while (record := engine.step()) is not None:
		packs += sum(record.packs_bought.values())
		discards += sum(record.discarded.values())
		expected = engine.capacity + PACK_SIZE * packs - discards
		assert len(engine.drawer) == expected, (
			f'day {engine.day}: drawer {len(engine.drawer)} != '
			f'{engine.capacity} + 6*{packs} - {discards}'
		)


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_conservation_holds_per_colour(seed, budget):
	"""Whites and blacks are replaced in kind, so each colour balances on its
	own. A cross-colour slip would net out in the total above."""
	engine = engine_for(seed, budget)
	packs: Counter = Counter()
	discards: Counter = Counter()

	while (record := engine.step()) is not None:
		packs.update(record.packs_bought)
		discards.update(record.discarded)
		held = Counter(s.color for s in engine.drawer)
		for color in (Color.WHITE, Color.BLACK):
			expected = engine.capacity // 2 + PACK_SIZE * packs[color] - discards[color]
			assert held[color] == expected, f'day {engine.day}, {color}'


def test_conservation_survives_budget_exhaustion():
	"""Past the cliff nothing is ever replaced, so the drawer only shrinks.
	The books still have to balance while it does."""
	engine = engine_for(4444, TIGHT_BUDGET, days=1200)
	discards = 0
	packs = 0
	while (record := engine.step()) is not None:
		packs += sum(record.packs_bought.values())
		discards += sum(record.discarded.values())
		assert len(engine.drawer) == engine.capacity + PACK_SIZE * packs - discards

	assert engine.exhausted_on is not None, 'budget never ran out; test proves nothing'
	assert sum(engine.sockless) > 0, 'never went sockless; the cliff was not reached'
	assert len(engine.drawer) < engine.capacity


# ---------------------------------------------------------------- shades


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_no_sock_is_ever_out_of_bounds(seed, budget):
	engine = engine_for(seed, budget)
	while engine.step() is not None:
		for sock in engine.drawer:
			if sock.color is Color.WHITE:
				assert WHITE_FLOOR <= sock.shade <= WHITE_START, f'white at {sock.shade}'
			else:
				assert BLACK_START <= sock.shade <= BLACK_CEILING, f'black at {sock.shade}'


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_shades_only_ever_move_toward_grey(seed, budget):
	"""Whites fade down, blacks rise. A sock's shade may hold - it was not worn,
	or it is already at the limit - but it must never reverse.

	A sock can move by at most one fade step per day. Worn socks wait until the
	end of the day, while kept leftovers return immediately and may be offered
	to a later roommate without ageing.
	"""
	engine = engine_for(seed, budget)
	before = {s.id: s.shade for s in engine.drawer}
	ceiling = 1

	while engine.step() is not None:
		after = {s.id: s.shade for s in engine.drawer}
		colors = {s.id: s.color for s in engine.drawer}

		for sock_id, shade in after.items():
			if sock_id not in before:
				continue  # arrived in a six-pack this morning
			was = before[sock_id]
			if colors[sock_id] is Color.WHITE:
				assert shade <= was, f'white sock went from {was} to {shade}'
				moved = was - shade
				assert moved % WHITE_FADE == 0, f'white moved {was} -> {shade}'
			else:
				assert shade >= was, f'black sock went from {was} to {shade}'
				moved = shade - was
				assert moved % BLACK_FADE == 0, f'black moved {was} -> {shade}'
			fade = WHITE_FADE if colors[sock_id] is Color.WHITE else BLACK_FADE
			assert moved <= ceiling * fade, (
				f'day {engine.day}: sock aged {moved // fade} times with {ceiling} roommates'
			)

		before = after


def test_a_sock_cannot_be_worn_more_than_once_a_day():
	"""Regression for a live-class report: a professor watched four roommates
	all dress out of a two-sock drawer, because a worn sock was going straight
	back into the pool for the next roommate the same day. It must not - a
	sock a roommate wore this morning is only available again starting
	tomorrow, no matter how many roommates share the drawer today. So no
	single sock should ever age by more than one wash inside one day.

	(An earlier version of this test asserted the opposite - that reuse within
	a day was fine because returns landed immediately - and that assumption
	was the bug. If this ever starts failing again, ``step`` is back to
	returning socks mid-day instead of queuing them for the next one.)
	"""
	from models.sock import Color as C

	def worst_daily_ageing(roommates: int) -> int:
		engine = engine_for(7, None, days=300, players=[GreedyPlayer] * roommates)
		before = {s.id: (s.color, s.shade) for s in engine.drawer}
		worst = 0
		while engine.step() is not None:
			after = {s.id: (s.color, s.shade) for s in engine.drawer}
			for sock_id, (color, shade) in after.items():
				if sock_id not in before:
					continue
				fade = WHITE_FADE if color is C.WHITE else BLACK_FADE
				worst = max(worst, abs(shade - before[sock_id][1]) // fade)
			before = after
		return worst

	assert worst_daily_ageing(1) == 1, 'one roommate cannot wear the same sock twice'
	assert worst_daily_ageing(4) == 1, 'a sock must not be reused by another roommate the same day'


def test_kept_leftovers_are_available_to_the_next_roommate():
	"""Only worn socks wait until day end; unworn kept socks return at once."""

	class WearsFirstTwo(GreedyPlayer):
		def select_socks(self, offered, turn):
			from models.player import Selection

			return Selection(wear=(0, 1), discard=())

	class PredictableRng:
		def shuffle(self, values):
			return None

		def sample(self, population, count):
			return list(population)[:count]

		def random(self):
			return 0.5

	engine = Engine(
		players=[WearsFirstTwo, WearsFirstTwo],
		capacity=40,
		selection_unit=4,
		days=1,
		seed=1,
		timeout=0,
	)
	engine.drawer = [_white(255), _white(251), _white(247), _white(243)]
	engine.rng = PredictableRng()

	record = engine.step()

	assert len(record.offered[0]) == 4
	assert len(record.offered[1]) == 2
	assert record.sockless == []


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_at_most_two_socks_per_roommate_age_in_a_day(seed, budget):
	"""Only worn socks age, and each roommate wears exactly two. Anything more
	changing shade means socks are ageing in the drawer.
	"""
	engine = engine_for(seed, budget)
	before = {s.id: s.shade for s in engine.drawer}

	while (record := engine.step()) is not None:
		after = {s.id: s.shade for s in engine.drawer}
		moved = sum(
			1 for sock_id, shade in after.items() if sock_id in before and shade != before[sock_id]
		)
		dressed = engine.roommates - len(record.sockless)
		assert moved <= 2 * dressed, f'day {engine.day}: {moved} socks aged, {dressed} dressed'
		before = after


def test_a_sock_left_unworn_keeps_its_shade_exactly():
	"""The pointed version of the invariant above, on a drawer small enough to
	name every sock. One roommate, four socks, all distinguishable: whichever
	two are not worn must come back untouched.
	"""

	class WearsFirstTwo(GreedyPlayer):
		def select_socks(self, offered, turn):
			from models.player import Selection

			return Selection(wear=(0, 1), discard=())

	engine = Engine(
		players=[WearsFirstTwo], capacity=40, selection_unit=4, days=1, seed=5, timeout=0
	)
	# Distinct shades, none at a limit, so any ageing is visible.
	engine.drawer = [
		s
		for s in (
			_white(255),
			_white(251),
			_white(247),
			_white(243),
		)
	]
	before = {s.id: s.shade for s in engine.drawer}

	engine.step()

	unchanged = [s for s in engine.drawer if before.get(s.id) == s.shade]
	aged = [s for s in engine.drawer if s.id in before and before[s.id] != s.shade]

	assert len(aged) == 2, 'exactly the two worn socks should have aged'
	assert len(unchanged) == 2, 'the two unworn socks should be untouched'
	for sock in aged:
		assert before[sock.id] - sock.shade == WHITE_FADE


def _white(shade: int):
	from models.sock import Sock, pristine

	return Sock(id=pristine(Color.WHITE).id, color=Color.WHITE, shade=shade)


# ---------------------------------------------------------------- money


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_money_only_ever_leaves_in_ten_dollar_packs(seed, budget):
	engine = engine_for(seed, budget)
	packs = 0

	while (record := engine.step()) is not None:
		packs += sum(record.packs_bought.values())
		assert engine.total_spent % PACK_COST == 0, f'${engine.total_spent} is not a whole pack'
		assert engine.total_spent == PACK_COST * packs
		assert record.spent_today == PACK_COST * sum(record.packs_bought.values())


@pytest.mark.parametrize('seed', SEEDS)
def test_spend_never_exceeds_the_budget(seed):
	engine = engine_for(seed, TIGHT_BUDGET, days=1200)
	while engine.step() is not None:
		assert engine.total_spent <= TIGHT_BUDGET
		assert engine.budget_remaining >= 0
	assert engine.results()['total_spent'] <= TIGHT_BUDGET


def test_an_unlimited_budget_leaves_remaining_infinite():
	engine = engine_for(1, None, days=60)
	while engine.step() is not None:
		assert engine.budget_remaining == float('inf')


# ---------------------------------------------------------------- discards


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_pending_discards_are_never_negative(seed, budget):
	engine = engine_for(seed, budget)
	while engine.step() is not None:
		for color in (Color.WHITE, Color.BLACK):
			assert engine.pending_discards[color] >= 0
			assert len(engine.pending_shades[color]) == engine.pending_discards[color]


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_every_discard_is_either_replaced_or_still_owed(seed, budget):
	"""Nothing is silently written off. Across a run, the discards of a colour
	equal the socks bought back plus what is still pending - including after
	the budget runs out, when the pending pile is what the household would buy
	if it could.
	"""
	engine = engine_for(seed, budget)
	packs: Counter = Counter()
	discards: Counter = Counter()

	while (record := engine.step()) is not None:
		packs.update(record.packs_bought)
		discards.update(record.discarded)
		for color in (Color.WHITE, Color.BLACK):
			replaced = PACK_SIZE * packs[color]
			assert discards[color] == replaced + engine.pending_discards[color], (
				f'day {engine.day}, {color}: {discards[color]} discarded, '
				f'{replaced} replaced, {engine.pending_discards[color]} pending'
			)


# ---------------------------------------------------------------- fairness


def test_no_roommate_is_favoured_in_the_draw_order():
	"""Order is reshuffled daily. Over a long run every roommate should sit in
	every position about equally often; a bias would hand somebody a
	systematically better or worse pick of the drawer.
	"""
	engine = engine_for(4444, None, days=4000)
	engine.run()

	n = engine.roommates
	positions: Counter = Counter()
	for record in engine.records:
		for position, index in enumerate(record.order):
			positions[(index, position)] += 1

	expected = len(engine.records) / n
	for index in range(n):
		for position in range(n):
			seen = positions[(index, position)]
			assert abs(seen - expected) < expected * 0.15, (
				f'roommate {index} took position {position} {seen} times, expected ~{expected:.0f}'
			)


def test_every_roommate_is_dressed_exactly_once_a_day():
	engine = engine_for(4444, None, days=400)
	while (record := engine.step()) is not None:
		assert sorted(record.order) == list(range(engine.roommates))
		handled = set(record.embarrassment)
		assert handled == set(range(engine.roommates)), f'day {engine.day}: {handled}'


@pytest.mark.parametrize(('seed', 'budget'), scenarios())
def test_every_roommate_gets_a_score_every_day(seed, budget):
	"""Sockless days still score. A roommate that silently produced no entry
	would drag its own mean down by shortening the divisor.
	"""
	engine = engine_for(seed, budget)
	engine.run()
	for index in range(engine.roommates):
		assert len(engine.embarrassment[index]) == engine.days
