{#
  Phrases the semantic layer could not serve, ranked by frequency: refusals
  and questions that fell to run_sql (query_executed with governed = false).
  Joins the text family by event_id, so it lives in the restricted schema.
  phrase is the registry phrase for refusals when one fired, otherwise the
  normalised question text.
#}

with events as (

    select * from {{ ref('stg_understory__events') }}

),

text as (

    select * from {{ ref('stg_understory__text') }}

),

candidates as (

    select
        event_id,
        tenant,
        user_hash,
        ts,
        'refused' as kind,
        reason,
        phrase
    from events
    where event = 'refused'

    union all

    select
        event_id,
        tenant,
        user_hash,
        ts,
        'ungoverned_sql' as kind,
        cast(null as varchar) as reason,
        cast(null as varchar) as phrase
    from events
    where event = 'query_executed' and not governed

),

joined as (

    select
        c.*,
        t.question,
        t.sql,
        coalesce(c.phrase, lower(trim(t.question))) as backlog_phrase
    from candidates as c
    left join text as t on t.event_id = c.event_id

)

select
    tenant,
    kind,
    backlog_phrase as phrase,
    reason,
    count(*) as occurrences,
    count(distinct user_hash) as users,
    min(ts) as first_seen,
    max(ts) as last_seen,
    any_value(question) as sample_question,
    any_value(sql) as sample_sql
from joined
where backlog_phrase is not null
group by 1, 2, 3, 4
