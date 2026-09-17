-- Coverage and completion.
SELECT status, roommates, capacity, policy, budget, COUNT(*) AS runs
FROM runs
GROUP BY status, roommates, capacity, policy, budget
ORDER BY roommates, capacity, policy, budget;

-- Overall outcome distribution by state-generating policy.
SELECT r.policy,
       COUNT(*) AS rollouts,
       AVG(o.household_embarrassment) AS mean_embarrassment,
       AVG(o.household_spend) AS mean_spend,
       AVG(o.household_sockless) AS mean_sockless
FROM rollouts o
JOIN action_choices a ON a.id = o.action_id
JOIN decision_points d ON d.id = a.decision_id
JOIN runs r ON r.id = d.run_id
GROUP BY r.policy;

-- Actions that are cheapest in embarrassment for each observed hand/state.
WITH means AS (
    SELECT d.id AS decision_id, a.id AS action_id, d.day, d.offered_json,
           d.pending_white, d.pending_black, d.budget_remaining,
           a.wear_json, a.discard_json, a.immediate_embarrassment,
           AVG(o.household_embarrassment) AS future_embarrassment,
           AVG(o.household_spend) AS future_spend,
           AVG(o.household_sockless) AS future_sockless
    FROM decision_points d
    JOIN action_choices a ON a.decision_id = d.id
    JOIN rollouts o ON o.action_id = a.id
    GROUP BY a.id
), ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY decision_id
        ORDER BY future_sockless, future_embarrassment, future_spend
    ) AS choice_rank
    FROM means
)
SELECT * FROM ranked WHERE choice_rank = 1;

-- Direct discard effect: compare actions wearing the same pair in the same state.
WITH means AS (
    SELECT a.id, a.decision_id, a.wear_json, a.discard_json,
           json_array_length(a.discard_json) AS discard_count,
           AVG(o.household_embarrassment) AS embarrassment,
           AVG(o.household_spend) AS spend,
           AVG(o.household_sockless) AS sockless
    FROM action_choices a
    JOIN rollouts o ON o.action_id = a.id
    GROUP BY a.id
), no_discard AS (
    SELECT * FROM means WHERE discard_count = 0
)
SELECT d.day, d.offered_json, d.pending_white, d.pending_black,
       d.budget_remaining, m.wear_json, m.discard_json,
       m.embarrassment - n.embarrassment AS discard_delta_embarrassment,
       m.spend - n.spend AS discard_delta_spend,
       m.sockless - n.sockless AS discard_delta_sockless
FROM means m
JOIN no_discard n
  ON n.decision_id = m.decision_id AND n.wear_json = m.wear_json
JOIN decision_points d ON d.id = m.decision_id
WHERE m.discard_count > 0;

-- How budget pressure changes the value of discarding.
WITH effects AS (
    WITH means AS (
        SELECT a.id, a.decision_id, a.wear_json,
               json_array_length(a.discard_json) AS discard_count,
               AVG(o.household_embarrassment) AS embarrassment,
               AVG(o.household_spend) AS spend
        FROM action_choices a
        JOIN rollouts o ON o.action_id = a.id
        GROUP BY a.id
    )
    SELECT d.budget_remaining, m.discard_count,
           m.embarrassment - n.embarrassment AS embarrassment_effect,
           m.spend - n.spend AS spend_effect
    FROM means m
    JOIN means n ON n.decision_id = m.decision_id
                AND n.wear_json = m.wear_json AND n.discard_count = 0
    JOIN decision_points d ON d.id = m.decision_id
    WHERE m.discard_count > 0
)
SELECT CASE
           WHEN budget_remaining IS NULL THEN 'unlimited'
           WHEN budget_remaining < 10 THEN 'below one pack'
           WHEN budget_remaining < 50 THEN '1-4 packs'
           ELSE '5+ packs'
       END AS budget_regime,
       COUNT(*) AS comparisons,
       AVG(embarrassment_effect) AS mean_embarrassment_effect,
       AVG(spend_effect) AS mean_spend_effect
FROM effects
GROUP BY budget_regime;
