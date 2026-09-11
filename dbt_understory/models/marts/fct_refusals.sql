{# One row per refused event. phrase is the registry phrase that fired, never user text. #}

select
    event_id,
    tenant,
    user_hash,
    session_id,
    derived_session_id,
    ts,
    cast(ts as date) as refused_date,
    reason,
    phrase
from {{ ref('int_understory__sessionized_events') }}
where event = 'refused'
