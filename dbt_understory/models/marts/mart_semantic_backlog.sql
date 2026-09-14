{#
  The data team's backlog: gaps grouped on what was missing, ranked by how
  often and by how many people. Built from the gaps family, so it carries
  question text and SQL but no user identity; users is a distinct count
  joined from events on gap_id. The sample SQL is a draft of the metric to
  build.
#}

with gaps as (

    select * from {{ ref('stg_understory__gaps') }}

),

askers as (

    select event_id, user_hash
    from {{ ref('stg_understory__events') }}
    where event in ('refused', 'query_executed')

)

select
    g.tenant,
    g.kind,
    g.key,
    count(*) as occurrences,
    count(distinct a.user_hash) as users,
    min(g.ts) as first_seen,
    max(g.ts) as last_seen,
    any_value(g.question) as sample_question,
    any_value(g.reason) as sample_reason,
    any_value(g.sql) as sample_sql,
    any_value(g.nearest) as sample_nearest
from gaps as g
left join askers as a on a.event_id = g.gap_id
group by 1, 2, 3
