{#
  One row per gap: a question the semantic layer could not answer as
  governed, with what was tried, what was missing, why it fell back, and the
  ad hoc SQL that answered instead. The unit of the data team's backlog.
  users is joined from events on gap_id and is a count, never a hash.
#}

with gaps as (

    select * from {{ ref('stg_understory__gaps') }}

),

askers as (

    select event_id, user_hash, session_id
    from {{ ref('stg_understory__events') }}
    where event in ('refused', 'query_executed')

)

select
    g.gap_id,
    g.tenant,
    g.ts,
    cast(g.ts as date) as gap_date,
    g.kind,
    g.key,
    g.missing,
    g.question,
    g.reason,
    g.sql,
    g.relations,
    g.nearest,
    g.phrase,
    g.spec_json,
    a.session_id
from gaps as g
left join askers as a on a.event_id = g.gap_id
