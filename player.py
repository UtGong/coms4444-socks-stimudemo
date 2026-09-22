"""Starting point for a group's player.

Copy this whole directory to ``players/player_<k>/`` using your group number,
then rename the class to ``Player<k>``. Group 4 would end up with
``players/player_4/player.py`` containing ``class Player4``. The registry looks
for exactly that; nothing else needs editing.

Keep the ``__init__.py``. Discovery uses ``pkgutil.iter_modules``, which only
reports directories that have one, so a group directory without it is silently
invisible to the simulator - no error, just a player that never turns up.

This directory is not itself discovered - the registry only matches
``player_<digits>`` - so the template can never appear in a run as a competitor.
"""

from dataclasses import dataclass
from itertools import combinations
from math import inf, isinf, pi, sin, sqrt

from models.player import GameContext, PlayerSnapshot, Selection, TurnContext
from models.player import Player as BasePlayer


@dataclass(frozen=True)
class SockObservation:
	"""The day and socks offered, in their original order."""

	day: int
	offered: tuple[int, ...]

	@property
	def black_shades(self) -> tuple[int, ...]:
		shades = []
		for shade in self.offered:
			if shade < 65:
				shades.append(shade)
		return tuple(shades)

	@property
	def white_shades(self) -> tuple[int, ...]:
		shades = []
		for shade in self.offered:
			if shade >= 127:  # White socks stop fading at 127.
				shades.append(shade)
		return tuple(shades)


class SockHistory:
	"""A separate history for each player."""

	def __init__(self) -> None:
		self._records: list[SockObservation] = []

	def record(self, *, day: int, offered: tuple[int, ...]) -> None:
		observation = SockObservation(day=day, offered=tuple(offered))
		self._records.append(observation)

	@property
	def records(self) -> tuple[SockObservation, ...]:
		# Return a tuple so callers cannot change the stored list.
		return tuple(self._records)


class Player8(BasePlayer):
	"""Closest-pair player with a conservative, data-informed discard policy."""

	# These are starting values to tune with matched simulations.  Keeping them in
	# one place makes a parameter sweep straightforward.
	LEARNING_END = 0.20
	SPENDING_END = 0.85
	WINDOW_SHAPE = 2.0
	OBSERVATION_ALPHA = 0.10
	RESERVE_Z = 1.645
	SURPLUS_FOR_FULL_PERMISSION = 20.0
	MIN_SPENDING_PERMISSION = 0.30
	MATCH_RADIUS = 6
	MATCH_PRIOR = 1.0
	CONFIDENCE_PRIOR = 20.0
	SAFE_SOCKS_PER_PERSON = 8.0
	PACK_COST = 10.0
	PACK_SIZE = 6
	HOLE_PROBABILITY = 0.25

	def __init__(self, snapshot: PlayerSnapshot, ctx: GameContext) -> None:
		super().__init__(snapshot, ctx)

		# super() has already set these from ctx and snapshot:
		#
		#   self.index           which roommate you are (0-based)
		#   self.id              your UUID, stable for the whole simulation
		#   self.capacity        C, the drawer size at the start
		#   self.roommates       n, how many of you share the drawer
		#   self.selection_unit  how many socks you are handed each day
		#   self.days            how long the simulation runs
		#
		# The engine constructs you once, before day 1, and it constructs you
		# itself - you cannot preload state into an already-built object. Anything
		# you want to carry between days lives on self, so initialise it here.
		self.days_seen = 0
		self.history = SockHistory()
		self.black_samples: list[int] = []
		self.white_samples: list[int] = []
		self.initial_budget: float | None = None
		# A weak 5% prior prevents the reserve from starting at exactly zero before
		# we have observed any old socks.
		self.terminal_rate = 0.05

	def select_socks(self, offered: tuple[int, ...], turn: TurnContext) -> Selection:
		"""Choose two socks to wear, and decide the fate of the rest.

		Called once per day, in an order that is reshuffled daily. Everything you
		are allowed to know is in the two arguments.

		``offered`` is a tuple of ``selection_unit`` shade values, 0-255.

		WHAT YOU CAN SEE

			offered[i]                  the shade of the i-th sock on offer
			turn.day                    today's day number, 1-based
			turn.total_spent            dollars spent by the household so far
			turn.embarrassment_history  your own daily scores, one per day
			turn.total_embarrassment    the sum of that history
			self.capacity / self.roommates / self.selection_unit / self.days

		WHAT YOU CANNOT SEE

			- Which sock is which. Indices are positions in THIS tuple only. The
				same index tomorrow is a different sock, so you cannot track an
				individual sock across turns or build up a map of the drawer.
			- Anyone else's socks, choices or embarrassment.
			- The shade distribution left in the drawer.
			- How many socks have been discarded, or how close the household is to
				the next six-pack. You see total_spent only, after the fact.

		With n == 1 you are alone with the drawer, so tracking its full state IS
		possible. That is intentional, not a leak - it is what makes the pooled
		versus separate comparison in goal 3 meaningful.

		WHAT THE SHADES MEAN

		White socks start at 255 and fade by 2 per wear, stopping at 127. Black
		socks start at 0 and rise by 1 per wear, stopping at 64. The two ranges
		never overlap, so a shade above 64 is a white sock and a shade at or below
		64 is a black one. Inferring colour from shade is fair game.

		Wearing a pair whose shades differ by MORE than 6 costs you that
		difference. A difference of exactly 6 is free.

		A sock already at 127 or 64 when you are handed it has a 25% chance of
		developing a hole when worn, and is thrown out immediately. Six discards
		of one colour buy a fresh six-pack for $10, and the surplus carries over.

		RETURNING A DECISION

			wear     exactly two distinct indices into ``offered``
			discard  any subset of the REMAINING indices, possibly empty

		Anything you neither wear nor discard goes back in the drawer unworn and
		keeps its shade. Only worn socks age.

		IF YOU GET IT WRONG

		An invalid selection, an exception, or taking longer than the --timeout
		budget forfeits your turn: the engine wears the first two socks and
		discards nothing. It is recorded as a fault and shown in the results, so a
		forfeit is visible rather than silent. Your failure never affects the
		other groups.
		"""
		self.days_seen += 1
		self.history.record(day=turn.day, offered=offered)
		self.black_samples.extend(shade for shade in offered if self._is_black(shade))
		self.white_samples.extend(shade for shade in offered if not self._is_black(shade))
		self._update_terminal_rate(offered)
		if self.initial_budget is None:
			# Unlike the old model, use the real game budget instead of assuming 4*n*D.
			self.initial_budget = turn.total_spent + turn.budget_remaining

		# Edge cases
		# Handle when a pair of socks cannot be made
		n = len(offered)
		if n == 0:
			return Selection(wear=(), discard=())
		if n == 1:
			return Selection(wear=(0,), discard=())

		# Embarrassment is zero for every difference <= 6 and is the full
		# difference otherwise.  The future terms are tie-breakers only; they can
		# never justify accepting a larger immediate score.
		best_pair = min(
			combinations(range(n), 2),
			key=lambda pair: self._pair_key(pair, offered, turn.budget_remaining),
		)

		# Create an array of the remaining socks for discard method
		worn = set(best_pair)
		unworn = [i for i in range(n) if i not in worn]

		permission = self._spending_permission(turn)
		if permission < self.MIN_SPENDING_PERMISSION:
			return Selection(wear=best_pair, discard=())

		# A voluntary removal is made only when its predicted long-run benefit is
		# positive.  Cap at one sock: two removals double inventory loss while a
		# replacement is still delayed until six same-colour removals accumulate.
		values = [(self._discard_value(offered[i], turn), i) for i in unworn]
		best_value, best_index = max(values, default=(-inf, -1))
		if best_value <= 0:
			return Selection(wear=best_pair, discard=())
		return Selection(wear=best_pair, discard=(best_index,))

	@staticmethod
	def _is_black(shade: int) -> bool:
		return shade <= 64

	@staticmethod
	def _is_terminal(shade: int) -> bool:
		return shade in (64, 127)

	@staticmethod
	def _embarrassment(first: int, second: int) -> int:
		difference = abs(first - second)
		return 0 if difference <= Player8.MATCH_RADIUS else difference

	def _normalised_day(self) -> float:
		if self.days <= 1:
			return 1.0
		return (self.days_seen - 1) / (self.days - 1)

	def _time_value(self) -> float:
		"""Smooth bump: zero early/late and highest in the useful middle."""
		tau = self._normalised_day()
		if tau <= self.LEARNING_END or tau >= self.SPENDING_END:
			return 0.0
		u = (tau - self.LEARNING_END) / (self.SPENDING_END - self.LEARNING_END)
		return sin(pi * u) ** self.WINDOW_SHAPE

	def _update_terminal_rate(self, offered: tuple[int, ...]) -> None:
		if not offered:
			return
		today = sum(self._is_terminal(shade) for shade in offered) / len(offered)
		a = self.OBSERVATION_ALPHA
		self.terminal_rate = a * today + (1 - a) * self.terminal_rate

	def _budget_reserve(self) -> float:
		"""Money protected for expected holes plus uncertainty and one pack."""
		if self.initial_budget is None or isinf(self.initial_budget):
			return 0.0
		remaining_days = max(0, self.days - self.days_seen)
		trials = 2 * self.roommates * remaining_days
		hole_probability = self.terminal_rate * self.HOLE_PROBABILITY
		expected_holes = trials * hole_probability
		mean_cost = self.PACK_COST / self.PACK_SIZE * expected_holes
		cost_sd = self.PACK_COST / self.PACK_SIZE * sqrt(
			trials * hole_probability * (1 - hole_probability)
		)
		pack_buffer = self.PACK_COST if remaining_days else 0.0
		return min(self.initial_budget, mean_cost + self.RESERVE_Z * cost_sd + pack_buffer)

	def _spending_permission(self, turn: TurnContext) -> float:
		"""Combine the middle-game time value with actual budget surplus."""
		if turn.budget_remaining < self.PACK_COST:
			return 0.0
		if isinf(turn.budget_remaining):
			budget_factor = 1.0
		else:
			surplus = turn.budget_remaining - self._budget_reserve()
			budget_factor = max(
				0.0, min(1.0, surplus / self.SURPLUS_FOR_FULL_PERMISSION)
			)
		return self._time_value() * budget_factor

	def _observed_shades(self, black: bool) -> list[int]:
		# Maintain these lists incrementally so a 1,000-day game does not repeatedly
		# rescan its entire history inside every candidate-pair calculation.
		return self.black_samples if black else self.white_samples

	def _match_probability(self, shade: int) -> float:
		"""Laplace-smoothed chance that a same-colour observation is close."""
		samples = self._observed_shades(self._is_black(shade))
		close = sum(abs(other - shade) <= self.MATCH_RADIUS for other in samples)
		beta = self.MATCH_PRIOR
		return (close + beta) / (len(samples) + 2 * beta)

	def _isolation_probability(self, shade: int) -> float:
		all_seen = len(self.black_samples) + len(self.white_samples)
		colour_seen = len(self._observed_shades(self._is_black(shade)))
		colour_share = colour_seen / all_seen if all_seen else 0.5
		# The other m-1 positions contain this many same-colour candidates on
		# average. A fractional exponent is the smooth expectation approximation.
		candidates = max(0.25, (self.selection_unit - 1) * colour_share)
		return (1 - self._match_probability(shade)) ** candidates

	def _expected_mismatch(self, shade: int) -> float:
		samples = self._observed_shades(self._is_black(shade))
		bad = [abs(other - shade) for other in samples if abs(other - shade) > 6]
		return sum(bad) / len(bad) if bad else 0.0

	def _pair_key(
		self, pair: tuple[int, int], offered: tuple[int, ...], budget_remaining: float
	) -> tuple[float, float]:
		first, second = pair
		immediate = self._embarrassment(offered[first], offered[second])
		leftovers = [shade for i, shade in enumerate(offered) if i not in pair]
		leftover_risk = sum(self._isolation_probability(shade) for shade in leftovers)

		# When the reserve is tight, prefer not to expose terminal socks to their
		# 25% hole check.  This affects only pairs tied on immediate embarrassment.
		terminal_count = sum(self._is_terminal(offered[i]) for i in pair)
		if isinf(budget_remaining):
			hole_weight = 0.0
		else:
			hole_weight = self._budget_reserve() / max(1.0, budget_remaining)
		future_risk = leftover_risk + hole_weight * self.HOLE_PROBABILITY * terminal_count
		return immediate, future_risk

	def _normalised_age(self, shade: int) -> float:
		if self._is_black(shade):
			return shade / 64
		return (255 - shade) / 128

	def _discard_value(self, shade: int, turn: TurnContext) -> float:
		"""Estimated long-run benefit of discarding one offered leftover."""
		remaining_fraction = max(0.0, (self.days - self.days_seen) / max(1, self.days))
		samples = self._observed_shades(self._is_black(shade))
		confidence = len(samples) / (len(samples) + self.CONFIDENCE_PRIOR)
		isolation = self._isolation_probability(shade)
		age = self._normalised_age(shade)

		# Keeping an isolated sock risks a future mismatch.  The age term adds
		# modest removal pressure near 64/127 without treating every old sock as bad.
		keep_harm = isolation * self._expected_mismatch(shade) + 6.0 * age**3
		benefit = remaining_fraction * confidence * keep_harm

		if isinf(turn.budget_remaining):
			budget_pressure = 0.0
		else:
			budget_pressure = self._budget_reserve() / max(1.0, turn.budget_remaining)
		budget_cost = self.PACK_COST / self.PACK_SIZE * (1 + budget_pressure)

		# Current drawer size is hidden. Initial socks/person is a conservative,
		# legal proxy; the cost rises sharply in small-capacity games.
		socks_per_person = self.capacity / self.roommates
		stock_cost = 4.0 * (self.SAFE_SOCKS_PER_PERSON / socks_per_person) ** 3
		endgame_cost = 4.0 * (1 - remaining_fraction)
		return benefit - budget_cost - stock_cost - endgame_cost
