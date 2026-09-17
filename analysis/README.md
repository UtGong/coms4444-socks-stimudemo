# Bounded multi-roommate decision tree

This analysis replaces the earlier one-player counterfactual sweep. It follows
all roommates through a short game, branches on their choices, and merges
equivalent next-day states. It uses the project rules rather than treating the
sample parameters in the guide as fixed.

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

1. Enumerate every legal action available to every roommate in every retained
   state.
2. Include a baseline joint profile and every one-player deviation, so each
   individual action is covered.
3. Fill the remaining daily profile budget with deterministically sampled
   two-roommate action interactions. If the complete joint product fits under
   the configured limit, enumerate it exactly.
4. Sample random roommate orders and hands with common, reproducible seeds.
5. Apply the complete wear, wash, hole, discard, replacement-pack, budget,
   embarrassment, and sockless rules.
6. Canonicalize socks by color and shade, then merge equivalent next-day
   states.
7. If a layer remains too large, retain a stratified frontier covering stock
   pressure, color imbalance, pairability, budget, score spread, and sockless
   exposure.

This is a bounded state graph, not a claim of exhaustive coverage. The database
records theoretical joint-profile counts, explored profiles, unique next
states, and retained states for every simulated day.

## Parameter ratios

The primary inputs are:

- roommate count `n`;
- requested initial socks per roommate `C/n`;
- initial household budget per roommate `B/n`;
- number of days;
- chance samples per retained state;
- joint action profiles per sampled hand configuration;
- retained states per parameter condition and day.

Capacity is rounded upward to a multiple of four and must satisfy
`C > 4*n + 10`. Both the requested and actual socks-per-roommate ratios are
stored in SQLite.

The built-in profiles are:

| Profile | Parameter conditions | State/action limits | Estimated upper bound |
| --- | --- | --- | --- |
| `small` | `n={2,4}`, ratios `{8,12}`, budgets/person `{0,100,unlimited}`, one seed | 50 states/day, 2 chance samples, 128 profiles | about 1.4M transitions |
| `research` | `n={2,4,6}`, ratios `{8,12,16}`, budgets/person `{0,25,100,300,unlimited}`, one seed | 200 states/day, 3 chance samples, 192 profiles | about 46.7M transitions |
| `large` | same conditions, two seeds | 400 states/day, 4 chance samples, 384 profiles | about 498M transitions |

The estimates are ceilings after the first day and assume every layer reaches
its state and profile caps. State merging and depleted hands can reduce work.
Actual speed depends heavily on the computer and SSD.

Expected planning ranges:

- `small`: roughly 5-25 minutes and 0.4-1.2 GiB;
- `research`: roughly 3-13 hours and 13-39 GiB;
- `large`: roughly 28-138 hours and 139-417 GiB.

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
  --output datasets/sock_tree.sqlite
```

The command is resumable. A completed day-layer is skipped. If a layer was
interrupted, its partial transition rows are deleted and that layer is rebuilt.

Check progress from another terminal:

```sh
python3.12 -m analysis.tree_status datasets/sock_tree.sqlite
```

Generate the visualization after the run, or while it is partially complete:

```sh
python3.12 -m analysis.tree_report datasets/sock_tree.sqlite \
  --output results/tree_report.html
```

Open `results/tree_report.html` in a browser.

Run a custom grid:

```sh
python3.12 -m analysis.tree_simulation --profile small \
  --roommates 2 4 6 \
  --sock-ratios 8 10 12 16 \
  --budgets-per-person 0 25 100 300 unlimited \
  --seeds 1 2 3 \
  --days 10 \
  --chance-samples 4 \
  --max-joint-profiles 256 \
  --max-states 300 \
  --output datasets/sock_tree_custom.sqlite
```

All CLI overrides replace the corresponding profile defaults. Use `--dry-run`
after changing limits.

## Stored data

The normalized tables are:

- `scenarios`: parameter ratios, exact capacities, limits, seeds, and status;
- `layers`: theoretical and explored coverage for each day;
- `states`: retained canonical next-day states and distribution summaries;
- `chance_contexts`: sampled roommate order and hands, stored once for all
  joint actions evaluated from that chance realization;
- `transitions`: links to the chance realization and records the joint action
  profile, immediate scores, discards, holes, packs, spending, and next-state
  outcome.

The transition table intentionally stores actions and outcomes even when the
child state is pruned. This allows later action-effect analysis without
mistaking frontier retention for the full data distribution.

The explored joint profiles are an experimental design. They are not equally
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
