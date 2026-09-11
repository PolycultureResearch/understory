{# The text family, one row per event_id that had text to record. Restricted readers only. #}

with raw as (

    select * from {{ understory_read_parquet('text') }}

)

select
    event_id,
    tenant,
    {{ understory_utc('ts') }} as ts,
    cast(dt as date) as log_date,
    question,
    spec_json,
    sql,
    draft_answer
from raw
