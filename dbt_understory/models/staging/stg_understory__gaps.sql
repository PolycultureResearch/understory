{#
  The gaps family: one row per question the semantic layer could not answer
  as governed (ADR 0002). No user identity, so the data team can read it.
  List fields live in payload and are parsed here.
#}

with raw as (

    select * from {{ understory_read_parquet('gaps') }}

)

select
    gap_id,
    tenant,
    {{ understory_utc('ts') }} as ts,
    cast(dt as date) as log_date,
    kind,
    key,
    question,
    reason,
    sql,
    phrase,
    spec_json,
    {{ understory_json_string_list('payload', 'missing') }} as missing,
    {{ understory_json_string_list('payload', 'relations') }} as relations,
    {{ understory_json_string_list('payload', 'nearest') }} as nearest,
    payload
from raw
