{#
  One row per derived session: a run of activity by one user_hash with no gap
  of understory_session_gap_minutes or more. session_id is the server's id for
  the first connection seen in the session; several server sessions can fold
  into one derived session.
#}

with events as (

    select * from {{ ref('int_understory__sessionized_events') }}

)

select
    derived_session_id,
    tenant,
    user_hash,
    min(first_server_session_id) as session_id,
    count(distinct session_id) as server_session_count,
    min(ts) as started_at,
    max(ts) as ended_at,
    {{ understory_seconds_between('min(ts)', 'max(ts)') }} as duration_s,
    count(*) as event_count,
    sum(case when event in ('query_executed', 'refused') then 1 else 0 end) as question_count,
    sum(case when event = 'query_executed' and governed then 1 else 0 end) as governed_question_count,
    sum(case when event = 'query_executed' and not governed then 1 else 0 end) as ungoverned_question_count,
    sum(case when event = 'clarification_returned' then 1 else 0 end) as clarifications_returned,
    sum(case when event = 'clarification_applied' then 1 else 0 end) as clarifications_applied,
    sum(case when event = 'refused' then 1 else 0 end) as refusal_count,
    sum(case when event = 'answer_logged' then 1 else 0 end) as answer_logged_count,
    sum(case when event = 'answer_logged' then 1 else 0 end) > 0 as log_answer_called
from events
group by 1, 2, 3
