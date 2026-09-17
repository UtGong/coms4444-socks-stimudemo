# Bounded sequential multi-roommate decision tree

This analysis replaces the earlier one-player counterfactual sweep. It follows
all roommates through a short game, branches on their choices, and merges
equivalent next-day states. It uses the project rules rather than treating the
sample parameters in the guide as fixed.

Turn timing matters. After a roommate chooses, every sock that was neither worn
nor discarded returns to the drawer immediately and may appear in a later
roommate's hand that day. Worn socks return after the whole day, following the
wash and hole check. Therefore later hands depend on earlier actions; the
simulator generates each day in random roommate order, one turn at a time.

## Why the literal full tree is impossible

A four-sock hand has

`choose(4,2) * 2^(4-2) = 24`

legal actions. With `n` roommates, one fully populated day has up to `24^n`
joint action profiles. Over `T` days, action branching alone is `24^(n*T)`.

For four roommates and ten days this is `24^40`, approximately
`1.47 * 10^55` paths. Random roommate order, draws without replacement, and
25% hole outcomes add further chance branches. No practical computer can
enumerate that tree.

The implemented search preserves the useful part of the proposal:

1. Sample a random roommate order and run a sequential closest-pair baseline.
2. For each roommate, enumerate every legal action in that roommate's baseline
   turn context. After a deviation, later roommates draw again from the changed
   drawer.
3. Fill the remaining trajectory budget with deterministic two-turn
   interactions. The earlier deviation is applied before the later hand is
   drawn, so every stored hand is reachable under its preceding choices.
4. Sample orders, sequential draws, and holes with common reproducible seeds.
5. Apply the complete wear, wash, hole, discard, replacement-pack, budget,
   embarrassment, and sockless rules.
6. Canonicalize socks by color and shade, then merge equivalent next-day
   states.
7. If a layer remains too large, retain a stratified frontier covering stock
   pressure, color imbalance, pairability, budget, score spread, and sockless
   exposure.

This is a bounded state graph, not a claim of exhaustive coverage. The database
records theoretical trajectory upper bounds, explored trajectories, unique next
states, and retained states for every simulated day.

## Parameter ratios

The primary inputs are:

- roommate count `n`;
- requested initial socks per roommate `C/n`;
- initial household budget per roommate `B/n`;
- number of days;
- chance samples per retained state;
- sequential action trajectories per state and chance sample;
- retained states per parameter condition and day.

Capacity is rounded upward to a multiple of four and must satisfy
`C > 4*n + 10`. Both the requested and actual socks-per-roommate ratios are
stored in SQLite. If two requested ratios round to the same legal capacity for
the same roommate count, the duplicate condition is simulated only once.

For a requested sock ratio `r_s` and budget ratio `r_b`, each condition uses

`C = 4 * ceil(max(n*r_s, 4*n + 11) / 4)` and `B = n*r_b`.

Thus comparisons across roommate counts hold the requested resources per
person constant. `B` is unlimited when `r_b=unlimited`.

The built-in profiles are:

| Profile | Parameter conditions | State/action limits | Estimated upper bound |
| --- | --- | --- | --- |
| `small` | `n={1,2,4,6,8,10}`, socks/person `{8,16}`, budget/person `{0,100,unlimited}`, one seed | 33 unique conditions; 50 states/day, 2 chance samples, 256 trajectories | about 7.6M transitions |
| `research` | every `n=1..10`, socks/person `{6,8,12,16,24}`, budget/person `{0,10,25,50,100,300,unlimited}`, one seed | 315 unique conditions; 50 states/day, 2 chance samples, 256 trajectories | about 72.7M transitions |
| `large` | same parameter grid, two seeds | 630 unique conditions; 150 states/day, 3 chance samples, 384 trajectories | about 980.5M transitions |

The estimates are ceilings after the first day and assume every layer reaches
its state and trajectory caps. State merging and depleted hands can reduce work.
Actual speed depends heavily on the computer and SSD.

Expected planning ranges:

- `small`: roughly 0.4-2.1 hours and 2.1-6.4 GiB;
- `research`: roughly 4-20 hours and 20-61 GiB;
- `large`: roughly 55-272 hours and 274-822 GiB.

Use `research` first. The `large` profile is intentionally expensive and may
be unsuitable for a laptop because of storage rather than CPU time.

## Commands

Preview the exact workload without creating a database:

```sh
python3.12 -m analysis.tree_simulation --profile research --dry-run
```

Start the recommended broad run:

```sh
python3.12 -m analysis.tree_simulation --profile research \
  --output datasets/sock_tree_sequential.sqlite
```

The command is resumable. A completed day-layer is skipped. If a layer was
interrupted, its partial transition rows are deleted and that layer is rebuilt.

Check progress from another terminal:

```sh
python3.12 -m analysis.tree_status datasets/sock_tree_sequential.sqlite
```

Generate the visualization after the run, or while it is partially complete:

```sh
python3.12 -m analysis.tree_report datasets/sock_tree_sequential.sqlite \
  --output results/tree_report.html
```

Open `results/tree_report.html` in a browser.

Run a custom grid:

```sh
python3.12 -m analysis.tree_simulation --profile small \
  --roommates 1 2 3 4 5 6 7 8 9 10 \
  --sock-ratios 6 8 12 16 24 \
  --budgets-per-person 0 10 25 50 100 300 unlimited \
  --seeds 1 2 3 \
  --days 10 \
  --chance-samples 4 \
  --max-trajectories 256 \
  --max-states 300 \
  --output datasets/sock_tree_custom.sqlite
```

Roommate counts are limited to 1-10. All CLI overrides replace the corresponding
profile defaults. Use `--dry-run` after changing limits.

Databases from the earlier simultaneous-hand model use schema version 3 and
cannot be resumed. Use the new `sock_tree_sequential.sqlite` path; the simulator
rejects an old database rather than mixing incompatible data.

## Stored data

The normalized tables are:

- `scenarios`: parameter ratios, exact capacities, limits, seeds, and status;
- `layers`: theoretical and explored coverage for each day;
- `states`: retained canonical next-day states and distribution summaries;
- `chance_contexts`: sampled roommate order plus the baseline trajectory's
  hands for that chance realization;
- `transitions`: records the actual offered hand and action for every sequential
  turn, plus immediate scores, discards, holes, packs, spending, and next-state
  outcome.

The transition table intentionally stores actions and outcomes even when the
child state is pruned. This allows later action-effect analysis without
mistaking frontier retention for the full data distribution.

The explored trajectories are an experimental design. They are not equally
likely player behavior, so an unweighted average of all transition rows is not
a tournament prediction. Compare actions within the same parameter condition,
parent state, and chance realization.

## What remains unknowable to a real player

The offline tree records the drawer, pending replacement counts, other
roommates' hands, and their scores. A tournament player cannot observe these.
It sees its offered shades, day, game parameters, household spending, remaining
budget, and its own embarrassment history. Oracle patterns must therefore be
translated into rules based only on observable quantities before they are used
in a submitted player.
