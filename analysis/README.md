# Counterfactual sock experiments

This folder is an **offline research tool**. It does not change the tournament
player or the project engine. Parameters are inputs; the guide's example budget,
drawer size, and day count are not assumed to be optimal or universal.

## Mathematical model

Let the state at the start of day `t` be

`X_t = (D_t, qW_t, qB_t, M_t, E_t, H_t)`, where `D_t` is the multiset of
individual socks `(color, shade)`, `qW/qB` are unreplaced discard counts,
`M_t` is household spending, `E_t` is each roommate's accumulated
embarrassment, and `H_t` is each roommate's sockless count. The engine also has
a random state. Initially `|D_0| = C`, with `C/2` pristine white socks at 255
and `C/2` pristine black socks at 0. Legal `C` is a multiple of four and
`C > unit * n + 10`; `unit` is four or five.

Each day randomly permutes the `n` roommates. For each roommate, a uniform
sample without replacement of at most `unit` socks is removed from the drawer.
Socks returned by one roommate are unavailable to later roommates **that day**.
If the hand has fewer than two socks, the roommate's score rises by `256^2`;
any drawn sock goes back tomorrow unchanged. Otherwise an action is a pair
`W = {i,j}` of distinct wear indices and a subset `A` of the remaining indices
to discard. The number of legal actions is

`choose(unit, 2) * 2^(unit-2)`, which is 24 for four and 80 for five.

Immediate embarrassment is

`e(i,j) = |s_i - s_j| * 1[|s_i - s_j| > 6]`.

For a worn white sock, washing changes `s` to `max(127, s-2)`; for a worn
black sock it changes `s` to `min(64, s+1)`. A sock **already** at 127 or 64
when selected for wear is discarded with probability 0.25 instead. Unworn,
undiscarded socks keep their shades. At the end of the day, returns enter the
drawer, followed by six-packs of each color. For color `c`, the number of
packs bought is

`p_c = min(floor(q_c/6), floor((B-M)/10))`

when a finite budget `B` exists; for an unlimited budget the second bound is
omitted. White is processed before black, so the second color sees spending on
the first. Then `q_c <- q_c - 6*p_c`, `M <- M + 10*p_c`, and `6*p_c` pristine
socks enter the drawer. Pending discards carry over.

An exact finite-horizon full-information solution would use

`V_t(X) = min_a E[e_t + V_(t+1)(X_(t+1)) | X_t=X, a]`,

with all other roommates' policies and random draws included in the
transition. This becomes huge over many days. A real player has only an
observation `O_t` consisting of its offered shades, day, game parameters,
spending/budget remaining, and its embarrassment history. Its decisions must
depend on `O_t` and its own memory, not on `D_t` or `q_c`. The oracle state in
this tool is used to **explain** consequences, never supplied to a player.

### What "spending" means

There is **no daily fee and no per-roommate charge**. Spending is an outcome of
the simulation. Discarding a white sock adds one to the pending white count;
discarding a black sock adds one to the pending black count. A hole does the
same. When a same-color pending count reaches six, the engine may buy one
six-pack of that color for $10 at the end of the day. If the remaining
household budget is below $10, no pack is bought and the discarded socks remain
missing from the drawer.

For example, if five black socks are already pending and today's choice throws
away another black sock, that choice reaches six and can trigger a $10 purchase
today. If only one black sock was pending, today's discard costs $0 immediately
but moves the household one sock closer to a future $10 purchase. Players
cannot see pending discard counts; this causal connection is visible only in
the offline oracle analysis.

In the action dashboard, **additional household spending over the horizon** is
the total cost of packs bought after the sampled choice through the end of the
look-ahead window. `spent_so_far` is the household total before the choice. The
displayed value is averaged across randomized futures, so it can be $5 or
$8.75 even though each individual run spends only in $10 increments.

In the comparison dashboards, spending is still paid by the household. The
display divides total household spending by roommate count and scales it to
360 days only to compare differently sized households on the same basis. The
simulator never bills each roommate separately.

## Bounded experiment

1. Run the repository's greedy baseline once and pause at evenly spaced days
   that allow a full horizon before the run ends, just after roommate 0 receives
   a hand. The other roommates' actions before that point have already happened.
2. Enumerate **all** legal actions for that hand. For each action, restart from
   the identical paused state. Complete that day and up to `horizon-1` more
   days with greedy roommates. Use `repeats` stochastic future streams per
   action, sharing repeat seeds across actions.
3. Report mean household and focal embarrassment, spending, and sockless days
   over the branch horizon, their Monte Carlo standard errors, the greedy
   reference action, and the Pareto
   frontier over embarrassment/spend/sockless. A single scalar ranking is not
   a budget strategy. Repeat across
   seeds and budget regimes before claiming a rule.

For each outcome `Y`, the local effect relative to greedy is
`Delta_Y(a) = mean_r[Y(a,r) - Y(greedy,r)]`. The printed means permit this
subtraction; standard errors show how noisy each estimate is. A candidate
rule should be checked on new seeds and longer full runs before use.

This is local counterfactual evidence. The future is conditioned on greedy
behavior after the candidate choice, and the short horizon can miss long-run
effects. Equal seeds do not imply identical future draws after actions change
the drawer or the sequence of hole checks. Comparing more seeds reduces that
noise. `samples`, `repeats`, and `horizon` bound the work. Defaults evaluate
at most six hands, eight futures per action, and 14 days per future.

Run from the repository root with Python 3.12 or newer:

```sh
python3.12 -m analysis.sock_counterfactual --capacity 40 --roommates 4 \
  --unit 4 --days 90 --budget 600 --samples 6 --repeats 8 --horizon 14
```

The command writes `results/socks_choices.html`, a standalone interactive
scatter plot. Choose a sampled day, hover over every action, and click a dot to
compare it with greedy. The Pareto table shows actions that are not worse on
all three means: embarrassment, spend, and sockless days. `--top` limits the
number of frontier entries shown; **every** legal choice is plotted. Use
`--output PATH` for a different HTML path or `--json PATH` for optional raw
data. Result files are ignored by git and can be regenerated.

To expose the offered hand and every legal action on **every simulated day**,
add `--every-day`. Days near the end use the remaining game length when fewer
than `horizon` days remain:

```sh
python3.12 -m analysis.sock_counterfactual --capacity 40 --roommates 4 \
  --unit 4 --days 90 --budget 600 --repeats 4 --horizon 7 --every-day
```

The day selector then contains days 1 through 90. Each day shows the actual
offered shades, all 24 legal actions for a full four-sock hand, the greedy
choice, immediate embarrassment, and estimated future outcomes. A depleted
drawer can produce a partial hand: two socks have
1 legal action and three socks have 6.
If a depleted drawer offers fewer than two socks, that day instead shows zero
legal actions and the fixed 65,536 sockless penalty. The player is not called
on such a turn.

## Pooling comparison

Pooled versus separate matches socks and budget per person. For each seed, the
separate setting averages `n` solo drawers, each with `C/n` socks and `B/n`
budget. A solo capacity must itself satisfy the project capacity rule:

```sh
python3.12 -m analysis.compare pooling --capacity 64 --roommates 4 \
  --unit 4 --days 360 --budget 2500 --seeds 1 2 3 4 5 6 7 8 9 10
```

This writes `results/socks_compare_pooling.html`, showing seed distributions and means
for daily embarrassment, annual spend per person, and annual sockless days per
person. Override `--output` to choose a destination; `--json` is optional.
The comparison policy is greedy in every drawer. It tests the effect of pooling
under that policy, not the advantage a solo player might gain
from reconstructing its drawer. The seed set is shared, but different
configurations do not experience identical random events after they diverge.

## Generate the complete dashboard set

The easiest entry point generates the four-sock every-day choice dashboard,
the pooling comparison, and a landing page that keeps them separate:

```sh
python3.12 -m analysis.build_dashboards --days 90 --budget 600 \
  --roommates 4 --capacity 40 --pool-capacity 64 \
  --repeats 4 --horizon 7 --seeds 1 2 3 4 5 6 7 8 9 10
```

Open `results/index.html`. Increase `repeats` or `horizon` when stronger
estimates justify the additional runtime.

## Large recorded dataset

`generate_dataset.py` stores raw, resumable research data in SQLite instead of
putting it into an HTML file. The four-sock dataset varies:

- roommates and valid drawer capacities;
- household budgets, including zero, constrained, generous, and unlimited;
- early, middle, late, depleted, and sockless game states;
- random seeds;
- four background policies: greedy, conserve, aggressive discard, and random;
- every legal action at each sampled decision point;
- repeated stochastic futures for each action.

Each decision record includes the complete oracle drawer state, offered hand,
returned socks waiting for tomorrow, pending discards by color, spending,
scores, and the background action. Each action records worn/discarded indices
and shades plus immediate embarrassment. Each rollout records its random seed,
future embarrassment, pack spending, sockless days, discards, holes, pending
counts, and ending drawer size.

Preview workload without running it:

```sh
python3.12 -m analysis.generate_dataset --profile full --dry-run
```

Start or resume the broad practical run:

```sh
python3.12 -m analysis.generate_dataset --profile full \
  --output datasets/socks_counterfactual.sqlite
```

### Exact `full` profile

The completed `full` dataset uses the following controlled parameters:

| Parameter | Values |
| --- | --- |
| Game length | 360 days |
| Roommates | 1, 2, 4, 6 |
| Total drawer capacities | 16/32, 20/36, 28/44, and 36/52 respectively |
| Purchase unit | 4 socks per pack |
| Budget per person | 0, 25, 100, 300, or unlimited |
| Household budget | per-person budget multiplied by roommate count |
| Baseline policies | greedy, conserve, aggressive discard, random |
| Scenario seeds | 1, 2, 3, 4, 5 |
| Sampled decisions | day 1, every 14 days, and day 360 |
| Counterfactual horizon | 7 days after each sampled choice |
| Repeats | 4 stochastic futures per legal action |

The cross product contains 800 scenarios and 27 sampled decision points per
scenario. At each point, the simulator evaluates every legal choice from the
offered hand. A full four-sock hand has 24 choices: select one of six pairs to
wear, then keep or discard each of the other two socks. A three-sock hand has
six choices, a two-sock hand has one, and a hand with fewer than two socks has
no wearable choice and records the player as sockless.

The completed database contains 800 scenarios, 21,600 decision points, 407,503
legal action choices, and 1,630,012 rollouts. This is below the full-hand upper
bound because depleted drawers sometimes produce partial hands or sockless
days. The run covers early, middle, and late game states; varied shades and
drawer density; worn-out socks and holes; budget exhaustion; pending discards;
pack-purchase thresholds; partial hands; sockless states; and individual versus
pooled households. Recorded outcomes include immediate and future
embarrassment, household embarrassment, spending, sockless days, holes,
discards by color, pending socks, and ending drawer size.

This profile can run for hours. Progress is committed one decision point at a
time; rerunning the same command skips completed scenarios and replays only
enough baseline history to resume incomplete ones. `smoke` is for quick
validation. `large` expands to longer games, more capacities, budgets, seeds,
and rollouts and can take substantially longer than `full`.

SQLite tables are `runs`, `decision_points`, `action_choices`, and `rollouts`.
They are normalized so later pattern analysis can join outcomes to the exact
state and choice that caused them. Generated databases live under `datasets/`
and are ignored by git.

Check a running dataset without changing it:

```sh
python3.12 -m analysis.dataset_status datasets/socks_counterfactual.sqlite
```

The database also exposes `rollout_dataset`, a flat analysis view, and
`action_outcomes`, which averages repeats for each action. Starter SQL for
coverage, best-action ranking, matched discard effects, and budget-dependent
patterns is in `analysis/pattern_queries.sql`.

## Inspect raw data and visualizations

Generate the full-dataset visual overview:

```sh
python3.12 -m analysis.visualize_dataset \
  datasets/socks_counterfactual.sqlite \
  --output results/dataset_overview.html
```

Open `results/index.html`, then select **1.63M-rollout overview**. This page is
computed directly from the completed SQLite database. The daily-choice and
pooling pages answer narrower questions and use their own configured runs.

Preview raw rows in the terminal:

```sh
python3.12 -m analysis.inspect_dataset --table runs --limit 10
python3.12 -m analysis.inspect_dataset --table decision_points --limit 10
python3.12 -m analysis.inspect_dataset --table rollout_dataset --limit 10
```

Export a manageable slice as CSV or JSON:

```sh
python3.12 -m analysis.inspect_dataset --table rollout_dataset --limit 10000 \
  --format csv --output datasets/rollout_sample.csv

python3.12 -m analysis.inspect_dataset --table action_outcomes --limit 1000 \
  --format json --output datasets/action_outcomes_sample.json
```

For unrestricted SQL exploration, open the database with the system SQLite
client:

```sh
sqlite3 -header -column datasets/socks_counterfactual.sqlite
```

Useful commands inside SQLite are `.tables`, `.schema rollout_dataset`, and
`.read analysis/pattern_queries.sql`. Exit with `.quit`. Avoid exporting the
entire 1.63-million-row flat view unless needed; filtered samples are easier to
inspect and much smaller.

The parity check compares this tool's greedy trajectory with `core.engine`
day by day, including offered hands, scores, spending, pending discards, and
drawer shades:

```sh
python3.12 -m unittest analysis.test_counterfactual
```
