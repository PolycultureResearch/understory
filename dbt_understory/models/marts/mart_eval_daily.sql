{#
  Per tenant and UTC day, the production side of the eval metrics in design
  section 12. Rates are null when the denominator is zero. Sessions are
  counted on the day they started.
#}

with questions as (

    select
        tenant,
        question_date as day,
        count(*) as questions,
        sum(case when governed then 1 else 0 end) as governed_questions
    from {{ ref('fct_questions') }}
    group by 1, 2

),

clarifications as (

    select
        tenant,
        returned_date as day,
        count(*) as clarifications_returned,
        sum(case when abandoned then 1 else 0 end) as clarifications_abandoned
    from {{ ref('fct_clarifications') }}
    group by 1, 2

),

refusals as (

    select
        tenant,
        refused_date as day,
        count(*) as refusals,
        sum(case when reason = 'unanswerable' then 1 else 0 end) as refused_unanswerable,
        sum(case when reason = 'invalid' then 1 else 0 end) as refused_invalid,
        sum(case when reason = 'uncovered' then 1 else 0 end) as refused_uncovered,
        sum(case when reason = 'sql_rejected' then 1 else 0 end) as refused_sql_rejected,
        sum(case when reason = 'too_broad' then 1 else 0 end) as refused_too_broad
    from {{ ref('fct_refusals') }}
    group by 1, 2

),

sessions as (

    select
        tenant,
        cast(started_at as date) as day,
        count(*) as sessions,
        sum(answer_logged_count) as answer_logged_events,
        sum(case when log_answer_called then 1 else 0 end) as sessions_with_log_answer
    from {{ ref('fct_sessions') }}
    group by 1, 2

),

days as (

    select tenant, day from questions
    union
    select tenant, day from clarifications
    union
    select tenant, day from refusals
    union
    select tenant, day from sessions

)

select
    d.tenant || ':' || cast(d.day as varchar) as eval_day_id,
    d.tenant,
    d.day,

    coalesce(q.questions, 0) as questions,
    coalesce(q.governed_questions, 0) as governed_questions,
    cast(q.governed_questions as double) / nullif(q.questions, 0) as governed_share,

    coalesce(c.clarifications_returned, 0) as clarifications_returned,
    cast(c.clarifications_returned as double) / nullif(q.questions, 0) as clarification_rate,
    coalesce(c.clarifications_abandoned, 0) as clarifications_abandoned,
    cast(c.clarifications_abandoned as double) / nullif(c.clarifications_returned, 0)
        as abandonment_rate,

    coalesce(r.refusals, 0) as refusals,
    cast(r.refusals as double) / nullif(q.questions, 0) as refusal_rate,
    coalesce(r.refused_unanswerable, 0) as refused_unanswerable,
    coalesce(r.refused_invalid, 0) as refused_invalid,
    coalesce(r.refused_uncovered, 0) as refused_uncovered,
    coalesce(r.refused_sql_rejected, 0) as refused_sql_rejected,
    coalesce(r.refused_too_broad, 0) as refused_too_broad,
    cast(r.refused_unanswerable as double) / nullif(q.questions, 0) as refusal_rate_unanswerable,
    cast(r.refused_invalid as double) / nullif(q.questions, 0) as refusal_rate_invalid,
    cast(r.refused_uncovered as double) / nullif(q.questions, 0) as refusal_rate_uncovered,
    cast(r.refused_sql_rejected as double) / nullif(q.questions, 0) as refusal_rate_sql_rejected,
    cast(r.refused_too_broad as double) / nullif(q.questions, 0) as refusal_rate_too_broad,

    coalesce(s.sessions, 0) as sessions,
    coalesce(s.answer_logged_events, 0) as answer_logged_events,
    cast(s.answer_logged_events as double) / nullif(s.sessions, 0) as log_answer_rate,
    coalesce(s.sessions_with_log_answer, 0) as sessions_with_log_answer,
    cast(s.sessions_with_log_answer as double) / nullif(s.sessions, 0) as capture_rate

from days as d
left join questions as q on q.tenant = d.tenant and q.day = d.day
left join clarifications as c on c.tenant = d.tenant and c.day = d.day
left join refusals as r on r.tenant = d.tenant and r.day = d.day
left join sessions as s on s.tenant = d.tenant and s.day = d.day
