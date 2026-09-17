import random
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from core.sandbox import PlayerFault, call_player
from models.player import GameContext, Player, PlayerSnapshot, Selection, TurnContext
from models.sock import Color, Sock, pristine

EMBARRASSMENT_THRESHOLD = 6
HOLE_PROBABILITY = 0.25
PACK_SIZE = 6
PACK_COST = 10.0

# A roommate handed fewer than two socks cannot dress. The handout prices that
# at 256**2 - two orders of magnitude above the worst possible mismatched pair
# (255), so going sockless can never be the cheap way out of a bad day.
SOCKLESS_PENALTY = float(256**2)


def _wrong_index_type(values: tuple[object, ...]) -> str | None:
	"""Name the first index that is not a plain ``int``, or None if all are.

	Worth its own message: ``np.int64`` does not subclass ``int``, so a
	vectorised strategy returning perfectly in-range indices used to be told
	they were out of range. Reporting the type is the difference between a
	group fixing it in a minute and a group rewriting a working strategy.
	"""
	for value in values:
		if not isinstance(value, int):
			cls = type(value)
			if cls.__module__ == 'builtins':
				return cls.__qualname__
			return f'{cls.__module__}.{cls.__qualname__}'
	return None


class _Unbuilt(Player):
	"""Stand-in for a group whose constructor failed.

	It forfeits every turn with the construction error, so the fault report
	names the culprit every day rather than once at startup, and the engine's
	ordinary fallback (wear the first two socks, discard nothing) consumes the
	RNG exactly as ``Forfeiter`` does.
	"""

	def __init__(self, snapshot: PlayerSnapshot, ctx: GameContext, name: str, reason: str) -> None:
		super().__init__(snapshot, ctx)
		self.name = name
		self.reason = reason

	def select_socks(self, offered: tuple[int, ...], turn: TurnContext) -> Selection:
		raise PlayerFault(f'unusable: {self.reason}')


@dataclass
class DayRecord:
	"""Everything the GUI or a replay needs about one simulated day."""

	day: int
	order: list[int]
	worn: dict[int, tuple[int, int]] = field(default_factory=dict)
	# The handful each roommate was given, as shade ints, plus the indices they
	# named. The visualiser shows offered vs picked from these; it does not
	# get to reconstruct a hand from the worn pair.
	offered: dict[int, tuple[int, ...]] = field(default_factory=dict)
	wear_idx: dict[int, tuple[int, int]] = field(default_factory=dict)
	discard_idx: dict[int, tuple[int, ...]] = field(default_factory=dict)
	embarrassment: dict[int, float] = field(default_factory=dict)
	discarded: Counter = field(default_factory=Counter)
	holes: Counter = field(default_factory=Counter)
	packs_bought: Counter = field(default_factory=Counter)
	spent_today: float = 0.0
	sockless: list[int] = field(default_factory=list)
	budget_remaining: float = float('inf')
	faults: list[str] = field(default_factory=list)


class Engine:
	def __init__(
		self,
		players: list[type[Player]],
		capacity: int,
		selection_unit: int,
		days: int,
		seed: int,
		timeout: float = 1.0,
		keep_records: bool = True,
		budget: float | None = None,
	) -> None:
		if capacity % 4 != 0:
			raise ValueError('C must be a multiple of 4 to balance white and black pairs.')

		roommates = len(players)
		floor = selection_unit * roommates + 10
		if capacity <= floor:
			raise ValueError(
				f'C must exceed {floor} (selection_unit * n + 10) so the drawer never runs dry.'
			)

		self.rng = random.Random(seed)
		self.capacity = capacity
		self.roommates = roommates
		self.selection_unit = selection_unit
		self.days = days
		self.timeout = timeout
		self.keep_records = keep_records

		# None means unlimited, which is the default and the behaviour every
		# pre-budget run had. Once a real budget is set the endgame is a cliff
		# rather than a slope: when it can no longer cover a $10 pack the
		# drawer only shrinks, so there is no route back.
		self.budget = budget
		self.exhausted_on: int | None = None

		self.day = 0
		self.total_spent = 0.0
		self.pending_discards: Counter = Counter({Color.WHITE: 0, Color.BLACK: 0})
		# The actual shades behind those counts, in discard order. The engine
		# knows them; without this the visualiser has to guess what a thrown-out
		# sock looked like.
		self.pending_shades: dict[Color, list[int]] = {Color.WHITE: [], Color.BLACK: []}
		# The player-facing view of its own scores. A TurnContext hands out a
		# snapshot of this, and a determined player can reach the list itself
		# through the accessor's closure - see models/player.py. Nothing the
		# engine reports is derived from it, so tampering costs a player only
		# the accuracy of its own view.
		self.embarrassment: dict[int, list[float]] = {i: [] for i in range(roommates)}

		# The authoritative score, engine-owned and out of a player's reach.
		# Accumulated in the same left-to-right order as sum() over the list, so
		# every figure stays bit-identical to summing the history.
		self.__totals: list[float] = [0.0] * roommates
		self.__turns: list[int] = [0] * roommates
		self.__embarrassed_days: list[int] = [0] * roommates
		self.sockless: list[int] = [0] * roommates

		# With keep_records off, DayRecords are dropped once the day is scored so a
		# 3600-day sweep cell does not retain 3600 of them. Faults are accumulated
		# separately because results() reports them either way.
		self.records: list[DayRecord] = []
		self.__faults: list[str] = []

		self.drawer: list[Sock] = self.__initial_drawer()

		ctx = GameContext(
			capacity=capacity,
			roommates=roommates,
			selection_unit=selection_unit,
			days=days,
		)
		# Construction runs inside the sandbox for the same reason moves do. A
		# group whose __init__ raises used to end the run with a traceback and
		# no results at all, and one whose __init__ looped hung the projector
		# with no alarm set - both of them taking down every other group's
		# demo. A player that cannot be built is replaced by a stand-in that
		# forfeits every turn, so the roster keeps its length and the drawer
		# arithmetic is unchanged.
		self.players = []
		self.player_names = []
		for i, cls in enumerate(players):
			snapshot = PlayerSnapshot(id=uuid.uuid4(), index=i)
			name = getattr(cls, '__name__', 'player')
			try:
				player = call_player(
					lambda cls=cls, snapshot=snapshot: cls(snapshot=snapshot, ctx=ctx),
					timeout=timeout,
				)
			except PlayerFault as exc:
				self.__faults.append(f'{name}: could not be constructed - {exc}')
				player = _Unbuilt(snapshot, ctx, name, str(exc))
			self.players.append(player)
			self.player_names.append(getattr(player, 'name', name))

	@property
	def budget_remaining(self) -> float:
		"""What is left to spend. ``inf`` when no budget was set."""
		if self.budget is None:
			return float('inf')
		return self.budget - self.total_spent

	def __initial_drawer(self) -> list[Sock]:
		half = self.capacity // 2
		socks = [pristine(Color.WHITE) for _ in range(half)]
		socks += [pristine(Color.BLACK) for _ in range(half)]
		return socks

	# ------------------------------------------------------------------
	# Player interaction
	# ------------------------------------------------------------------

	def __draw(self) -> list[Sock]:
		"""Remove up to `selection_unit` socks uniformly at random.

		Fewer than a full unit only happens once a budget has run out and the
		drawer has started shrinking. While there is money the parameter
		constraint C > unit * n + 10 guarantees a full draw, so the sample size
		is unchanged and so is the RNG stream.
		"""
		drawer = self.drawer
		picked = self.rng.sample(range(len(drawer)), min(self.selection_unit, len(drawer)))
		picked.sort(reverse=True)
		pop = drawer.pop
		return [pop(i) for i in picked]

	def __turn_context(self, index: int) -> TurnContext:
		history = self.embarrassment[index]
		return TurnContext(
			day=self.day,
			total_spent=self.total_spent,
			total_embarrassment=self.__totals[index],
			budget_remaining=self.budget_remaining,
			# Deferred: most players never read the history, and copying it
			# eagerly made a long run quadratic. The lambda hands back a fresh
			# tuple, but it does close over the list, and a player willing to
			# walk __closure__ can reach it. That is why nothing the engine
			# scores on is read back out of self.embarrassment.
			_history=lambda: tuple(history),
		)

	def __validate(self, selection: object, offered: int) -> Selection:
		"""Reject malformed selections rather than trusting player output.

		Mirrors the 2025 engine's habit of silently dropping invalid moves,
		but falls back to a legal default so one bad group cannot desync the
		drawer for everybody else.

		``selection`` is whatever the player handed back, which need not be a
		Selection at all. Anything this raises is converted to a PlayerFault by
		the sandbox this runs inside.
		"""
		n = offered
		wear = tuple(selection.wear)
		if len(wear) != 2 or len(set(wear)) != 2:
			raise PlayerFault('wear must name two distinct indices')
		bad = _wrong_index_type(wear)
		if bad is not None:
			raise PlayerFault(f'wear indices must be plain ints, not {bad}')
		if not all(0 <= i < n for i in wear):
			raise PlayerFault(f'wear indices must lie in [0, {n})')

		# The overwhelmingly common case. Short-circuiting it skips three
		# temporaries per turn; the checks below are all no-ops on an empty
		# discard, so the result is the same object either way.
		if not selection.discard:
			return Selection(wear=wear, discard=())

		discard = tuple(dict.fromkeys(selection.discard))
		bad = _wrong_index_type(discard)
		if bad is not None:
			raise PlayerFault(f'discard indices must be plain ints, not {bad}')
		if not all(0 <= i < n for i in discard):
			raise PlayerFault(f'discard indices must lie in [0, {n})')
		if set(discard) & set(wear):
			raise PlayerFault('cannot discard a sock that is being worn')

		return Selection(wear=wear, discard=discard)

	# ------------------------------------------------------------------
	# Daily simulation
	# ------------------------------------------------------------------

	def __decide(self, index: int, shades: tuple[int, ...], turn: TurnContext) -> Selection:
		"""Ask a player for its move and check the answer.

		Both halves run inside the sandbox on purpose. Validation touches
		player-supplied objects - iterating ``wear``, comparing indices - and
		those can raise or hang just as readily as ``select_socks`` itself. Run
		out here it would take the whole class's demo down with it; run in
		there it is one forfeited turn.
		"""
		selection = self.players[index].select_socks(shades, turn)
		return self.__validate(selection, len(shades))

	def __dress(self, index: int, record: DayRecord, worn_returning: list[Sock]) -> None:
		offered = self.__draw()
		shades = tuple(s.shade for s in offered)
		record.offered[index] = shades

		if len(offered) < 2:
			self.__go_sockless(index, offered, record)
			return

		try:
			# The context is built out here on purpose: it touches only engine
			# state, so it cannot raise or hang, and building it should not eat
			# into the player's time budget. Materialising the history does
			# happen inside the guard, on the player's dime, if it asks.
			turn = self.__turn_context(index)
			selection = call_player(self.__decide, index, shades, turn, timeout=self.timeout)
		except PlayerFault as exc:
			record.faults.append(f'{self.player_names[index]}: {exc}')
			selection = Selection(wear=(0, 1), discard=())

		wear = selection.wear
		first, second = offered[wear[0]], offered[wear[1]]
		shade_a, shade_b = first.shade, second.shade
		diff = shade_a - shade_b
		if diff < 0:
			diff = -diff
		score = float(diff) if diff > EMBARRASSMENT_THRESHOLD else 0.0
		self.embarrassment[index].append(score)
		self.__totals[index] += score
		self.__turns[index] += 1
		if score > 0:
			self.__embarrassed_days[index] += 1
		record.embarrassment[index] = score
		record.worn[index] = (shade_a, shade_b)
		record.wear_idx[index] = wear
		record.discard_idx[index] = selection.discard

		random = self.rng.random

		# Worn socks are washed, then queued to return once the whole day has
		# been dressed. They are unavailable to later roommates that day. The
		# hole check applies to socks that were already worn out when drawn.
		for sock in (first, second):
			if sock.worn_out and random() < HOLE_PROBABILITY:
				self.__discard(sock, record, hole=True)
			else:
				worn_returning.append(sock.washed())

		# Unworn socks are returned unaged, or thrown out at the roommate's
		# discretion. A kept leftover goes back immediately, so a later
		# roommate may draw it during the same day.
		discarded = selection.discard
		for i, sock in enumerate(offered):
			if i in wear:
				continue
			if discarded and i in discarded:
				self.__discard(sock, record, hole=False)
			else:
				self.drawer.append(sock)

	def __go_sockless(self, index: int, offered: list[Sock], record: DayRecord) -> None:
		"""Fewer than two socks on offer: the roommate cannot dress.

		The player is not consulted - there is no legal Selection over one
		sock - so this is not a fault and nothing is attributed to the group.
		Any offered sock goes back immediately, unworn and unaged, so it can be
		offered to a later roommate that day.
		"""
		self.embarrassment[index].append(SOCKLESS_PENALTY)
		self.__totals[index] += SOCKLESS_PENALTY
		self.__turns[index] += 1
		self.__embarrassed_days[index] += 1
		self.sockless[index] += 1

		record.embarrassment[index] = SOCKLESS_PENALTY
		record.sockless.append(index)

		self.drawer.extend(offered)

	def __discard(self, sock: Sock, record: DayRecord, hole: bool) -> None:
		self.pending_discards[sock.color] += 1
		self.pending_shades[sock.color].append(sock.shade)
		record.discarded[sock.color] += 1
		if hole:
			record.holes[sock.color] += 1

	def __replenish(self, record: DayRecord) -> None:
		"""Buy as many six-packs as the discard count supports, carrying the
		remainder forward to the next batch."""
		for color in (Color.WHITE, Color.BLACK):
			packs, remainder = divmod(self.pending_discards[color], PACK_SIZE)
			if not packs:
				continue

			# Buy only what the budget covers. Discards that cannot be
			# replaced stay pending rather than being written off, so the
			# moment money reappears they are still owed - and with no budget
			# this clamp is a no-op.
			affordable = packs
			if self.budget is not None:
				affordable = min(packs, int(self.budget_remaining // PACK_COST))
			if affordable < 1:
				if self.exhausted_on is None:
					self.exhausted_on = self.day
				continue

			bought = affordable * PACK_SIZE
			self.pending_discards[color] -= bought
			del self.pending_shades[color][:bought]
			for _ in range(bought):
				self.drawer.append(pristine(color))
			cost = affordable * PACK_COST
			self.total_spent += cost
			record.spent_today += cost
			record.packs_bought[color] += affordable

	def step(self) -> DayRecord | None:
		if self.day >= self.days:
			return None

		self.day += 1
		order = list(range(self.roommates))
		self.rng.shuffle(order)
		record = DayRecord(day=self.day, order=order)

		# Only worn socks wait until everyone has dressed. Kept, unworn socks
		# return inside __dress and can be offered again later the same day.
		worn_returning: list[Sock] = []
		for index in order:
			self.__dress(index, record, worn_returning)
		self.drawer.extend(worn_returning)

		self.__replenish(record)
		record.budget_remaining = self.budget_remaining
		self.__faults.extend(record.faults)
		if self.keep_records:
			self.records.append(record)
		return record

	def run(self, on_day: Callable[[DayRecord], None] | None = None) -> dict:
		"""Run every remaining day. ``on_day`` is how the debug log observes
		each DayRecord without the engine opening a file."""
		while True:
			record = self.step()
			if record is None:
				break
			if on_day is not None:
				on_day(record)
		return self.results()

	# ------------------------------------------------------------------
	# Reporting
	# ------------------------------------------------------------------

	def total_embarrassment(self, index: int) -> float:
		"""Running score for one roommate.

		Public because the visualiser needs it every frame and should not be
		re-deriving it from ``embarrassment`` - that list is the player-facing
		view, and it is not what the engine scores on.
		"""
		return self.__totals[index]

	def results(self) -> dict:
		years = self.days / 360
		per_player = []
		for i, name in enumerate(self.player_names):
			total = self.__totals[i]
			turns = self.__turns[i]
			per_player.append(
				{
					'index': i,
					'name': name,
					'total_embarrassment': total,
					'mean_daily_embarrassment': total / turns if turns else 0.0,
					'embarrassed_days': self.__embarrassed_days[i],
					'sockless_days': self.sockless[i],
				}
			)

		shade_counts = Counter(s.color.value for s in self.drawer)

		return {
			'parameters': {
				'capacity': self.capacity,
				'roommates': self.roommates,
				'selection_unit': self.selection_unit,
				'days': self.days,
			},
			'total_spent': self.total_spent,
			'spend_per_year': self.total_spent / years if years else 0.0,
			'budget': self.budget,
			'budget_remaining': None if self.budget is None else self.budget_remaining,
			'budget_exhausted_on_day': self.exhausted_on,
			'total_sockless_days': sum(self.sockless),
			'total_embarrassment': sum(self.__totals),
			'players': per_player,
			'drawer_size': len(self.drawer),
			'drawer_composition': dict(shade_counts),
			'pending_discards': {c.value: n for c, n in self.pending_discards.items()},
			'faults': list(self.__faults),
		}
