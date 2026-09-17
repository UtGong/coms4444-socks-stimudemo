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

With five socks, the count is `choose(5,2) * 2^3 = 80` actions per hand. The
simulator automatically raises the trajectory cap to at least
`1 + n*(actions_per_hand-1)`, which is 231 for ten roommates in four-sock mode
and 791 in five-sock mode. Workload estimates use the selected `--unit`.

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

## Parameter space and bounded design

The primary inputs are:

- roommate count `n` in `{1,2,...,10}`;
- game length `d` in `{1,2,...,1000}`;
- initial socks `C` in `[4*n+10, 20*n]`;
- household budget `B` in `[0, 4*n*d]`;
- chance samples per retained state;
- sequential action trajectories per state and chance sample;
- retained states per parameter condition and day.

The engine requires capacity to be a multiple of four and strictly greater than
`unit*n+10`. For the standard four-sock game, the first legal value in the
requested interval is therefore `4*n+12`. Let `s` and `b` be normalized sock
and budget levels in `[0,1]`. The simulator maps them to

`C_min = 4 * (floor(max(4*n+10, unit*n+10)/4) + 1)`,

`C(s) = nearest legal multiple of 4 to C_min + s*(20*n-C_min)`, and

`B(b) = round(b*4*n*d)`.

The database stores `s`, `b`, `C`, `C/n`, `B`, and `B/(n*d)`. Conditions that
round to the same `(n,d,C,B,seed)` are simulated once.

The research profile samples `d={1,10,30,100,360,1000}` and five levels for
both `s` and `b`: `{0,0.25,0.5,0.75,1}`. A 13-point space-filling design covers
the two diagonals and central boundary cases instead of crossing all 25 pairs.
This retains low/high resources, aligned ratios, opposing ratios, and midpoint
effects. `--full-grid` enables the complete cross product.

The built-in profiles are:

| Profile | Parameter conditions | State/action limits | Estimated upper bound |
| --- | --- | --- | --- |
| `small` | `n={1,5,10}`, `d={1,10,100,1000}`, levels `{0,0.5,1}`, sparse design, one seed | 96 unique conditions; adaptive 1-20 states/day, 1 chance sample, 256 trajectories | about 10.3M transitions |
| `research` | every `n=1..10`, six sampled `d` values, five resource levels, 13-point design, one seed | 762 unique conditions; adaptive 1-30 states/day, 1 chance sample, 256 trajectories | about 109.2M transitions |
| `large` | every `n=1..10`, six sampled `d` values, full 25-point grid, two seeds | 2,820 unique conditions; adaptive 2-50 states/day, 2 chance samples, 384 trajectories | about 2.28B transitions |

The estimates are ceilings after the first day and assume every layer reaches
its state and trajectory caps. State merging and depleted hands can reduce work.
Actual speed depends heavily on the computer and SSD.

Expected planning ranges:

- `small`: roughly 0.6-2.9 hours and 2.9-8.6 GiB;
- `research`: roughly 6-30 hours and 31-92 GiB;
- `large`: roughly 127-635 hours and 638 GiB-1.9 TiB.

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
  --output datasets/sock_tree_space.sqlite
```

The command is resumable. A completed day-layer is skipped. If a layer was
interrupted, its partial transition rows are deleted and that layer is rebuilt.

Check progress from another terminal:

```sh
python3.12 -m analysis.tree_status datasets/sock_tree_space.sqlite
```

Generate the visualization after the run, or while it is partially complete:

```sh
python3.12 -m analysis.tree_report datasets/sock_tree_space.sqlite \
  --output results/tree_report.html
```

Open `results/tree_report.html` in a browser.

Run a custom grid:

```sh
python3.12 -m analysis.tree_simulation --profile small \
  --roommates 1 2 3 4 5 6 7 8 9 10 \
  --days 1 10 100 360 1000 \
  --sock-levels 0 0.25 0.5 0.75 1 \
  --budget-levels 0 0.25 0.5 0.75 1 \
  --full-grid \
  --seeds 1 2 3 \
  --chance-samples 4 \
  --max-trajectories 256 \
  --max-states 300 \
  --output datasets/sock_tree_custom.sqlite
```

Roommate counts are limited to 1-10, days to 1-1000, and normalized resource
levels to `[0,1]`. All CLI overrides replace the corresponding profile defaults.
`--max-states` replaces the adaptive long-run caps, so use it cautiously with
large day values. Always use `--dry-run` after changing limits.

This parameter model uses schema version 5. Older tree databases cannot be
resumed; choose a new output filename. The simulator rejects an old database
rather than mixing incompatible data.

## Stored data

The normalized tables are:

- `scenarios`: normalized resource levels, exact resources, game lengths,
  limits, seeds, and status;
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
