{#
  One row per event with the payload JSON parsed into typed columns.
  Every event-specific column is null for events that do not carry it.
#}

with raw as (

    select * from {{ understory_read_parquet('events') }}

)

select
    event_id,
    tenant,
    user_hash,
    session_id,
    {{ understory_utc('ts') }} as ts,
    cast(dt as date) as log_date,
    event,

    -- promoted columns, present in the file
    tool,
    status,
    governed,
    latency_ms,
    trap_id,
    reason,

    -- clarification_returned / clarification_applied
    {{ understory_json_string_list('payload', 'options') }} as options,
    {{ understory_json_string('payload', 'choice') }} as choice,

    -- query_executed
    {{ understory_json_string_list('payload', 'metrics') }} as metrics,
    {{ understory_json_string_list('payload', 'dimensions') }} as dimensions,
    {{ understory_json_string('payload', 'sql_hash') }} as sql_hash,
    {{ understory_json_string('payload', 'spec_hash') }} as spec_hash,
    {{ understory_json_int('payload', 'row_count') }} as row_count,
    {{ understory_json_bool('payload', 'cache_hit') }} as cache_hit,
    {{ understory_json_bool('payload', 'truncated') }} as truncated,

    -- refused
    {{ understory_json_string('payload', 'phrase') }} as phrase,

    -- answer_logged
    {{ understory_json_int('payload', 'numbers_checked') }} as numbers_checked,
    {{ understory_json_int('payload', 'numbers_unsourced') }} as numbers_unsourced,
    {{ understory_json_int('payload', 'disclosures_present') }} as disclosures_present,
    {{ understory_json_int('payload', 'disclosures_missing') }} as disclosures_missing,

    payload

from raw
