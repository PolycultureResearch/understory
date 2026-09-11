{#
  Every event with a derived session id. A new session starts for a
  (tenant, user_hash) after understory_session_gap_minutes of inactivity
  (design section 8.4). The server's own session_id is kept alongside.
#}

with events as (

    select * from {{ ref('stg_understory__events') }}

),

with_previous as (

    select
        *,
        lag(ts) over (partition by tenant, user_hash order by ts, event_id) as previous_ts
    from events

),

flagged as (

    select
        *,
        case
            when previous_ts is null then 1
            when {{ understory_minutes_between('previous_ts', 'ts') }}
                >= {{ var('understory_session_gap_minutes') }} then 1
            else 0
        end as is_session_start
    from with_previous

),

numbered as (

    select
        *,
        sum(is_session_start) over (
            partition by tenant, user_hash
            order by ts, event_id
            rows between unbounded preceding and current row
        ) as session_seq
    from flagged

),

keyed as (

    select
        *,
        md5(tenant || ':' || user_hash || ':' || cast(session_seq as varchar)) as derived_session_id
    from numbered

)

select
    *,
    first_value(session_id) over (
        partition by derived_session_id order by ts, event_id
    ) as first_server_session_id
from keyed
