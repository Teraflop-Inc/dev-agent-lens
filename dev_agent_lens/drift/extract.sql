-- v3: same output as v2 (every level, its own type), v1's cost profile.
--
-- v2 was ~250x slower than v1 because its `lvl` CTE projected the jsonb VALUES at every
-- level (v1..v4). For a raw_gen_ai_request row v1 is the whole `llm` subtree with the
-- ~427 KB payload inside it, so the CTE carried gigabytes, and MATERIALIZED (tried as a fix
-- for CTE re-evaluation) forced all of it to disk and timed out harder. v1 was fast because
-- it only ever projected jsonb_typeof(). v3 does the same: one pass, one LATERAL chain,
-- paths and types unnested from arrays, no value ever leaves the join.
WITH src AS (
    SELECT id, attributes FROM (
        SELECT id, attributes,
               row_number() OVER (
                   PARTITION BY regexp_replace(
                       regexp_replace(name, '^(Claude_Code_Tool_|Claude_Code_Internal_)[A-Za-z0-9_.-]+$', '\1*'),
                       '^turn-[0-9]+$', 'turn-*')
                   ORDER BY id) AS rn
        FROM {schema}.spans
        WHERE start_time >= %(lo)s AND start_time < %(hi)s
    ) z WHERE rn <= %(cap)s
)
SELECT path, jtype, count(*) AS n, count(DISTINCT rid) AS rows_with
FROM (
    SELECT s.id AS rid, u.path, u.jtype
    FROM src s
    CROSS JOIN LATERAL jsonb_each(s.attributes)                                        AS e1(k, v)
    LEFT  JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e1.v)='object' THEN e1.v END) AS e2(k, v) ON true
    LEFT  JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e2.v)='object' THEN e2.v END) AS e3(k, v) ON true
    LEFT  JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(e3.v)='object' THEN e3.v END) AS e4(k, v) ON true
    CROSS JOIN LATERAL unnest(
        ARRAY[e1.k,
              e1.k||'.'||e2.k,
              e1.k||'.'||e2.k||'.'||e3.k,
              e1.k||'.'||e2.k||'.'||e3.k||'.'||e4.k],
        ARRAY[jsonb_typeof(e1.v), jsonb_typeof(e2.v), jsonb_typeof(e3.v), jsonb_typeof(e4.v)]
    ) AS u(path, jtype)
    WHERE u.path IS NOT NULL
) w
GROUP BY 1, 2;
