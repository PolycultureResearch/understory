{#
  One row per question the server answered or refused: every query_executed
  and refused event. status is 'resolved' for executed queries and the
  refusal reason otherwise; governed is false for refusals and run_sql.
#}

select
    event_id,
    tenant,
    user_hash,
    session_id,
    derived_session_id,
    ts,
    cast(ts as date) as question_date,
    event,
    case when event = 'refused' then reason else 'resolved' end as status,
    coalesce(governed, false) as governed,
    latency_ms,
    metrics,
    dimensions,
    sql_hash,
    spec_hash,
    row_count,
    cache_hit,
    truncated,
    reason,
    phrase
from {{ ref('int_understory__sessionized_events') }}
where event in ('query_executed', 'refused')
