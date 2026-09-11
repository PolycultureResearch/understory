{#
  One row per clarification_returned, joined to the first
  clarification_applied for the same trap in the same server session.
  abandoned is true when nothing was applied.
#}

with events as (

    select * from {{ ref('int_understory__sessionized_events') }}

),

returned as (

    select * from events where event = 'clarification_returned'

),

applied as (

    select * from events where event = 'clarification_applied'

),

matched as (

    select
        r.event_id as returned_event_id,
        r.tenant,
        r.user_hash,
        r.session_id,
        r.derived_session_id,
        r.trap_id,
        r.ts as returned_at,
        r.options,
        a.event_id as applied_event_id,
        a.ts as applied_at,
        a.choice,
        row_number() over (partition by r.event_id order by a.ts, a.event_id) as match_rank
    from returned as r
    left join applied as a
        on a.tenant = r.tenant
        and a.session_id = r.session_id
        and a.trap_id = r.trap_id
        and a.ts >= r.ts

)

select
    returned_event_id,
    tenant,
    user_hash,
    session_id,
    derived_session_id,
    trap_id,
    returned_at,
    cast(returned_at as date) as returned_date,
    options,
    applied_event_id,
    applied_at,
    choice,
    applied_event_id is null as abandoned,
    {{ understory_seconds_between('returned_at', 'applied_at') }} as seconds_to_apply
from matched
where match_rank = 1
