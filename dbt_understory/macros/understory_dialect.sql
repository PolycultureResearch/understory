{# Small dialect shims so the models read the same on DuckDB and BigQuery. #}

{% macro understory_json_string(column, key) -%}
    {%- if target.type == 'bigquery' -%}
        json_value({{ column }}, '$.{{ key }}')
    {%- else -%}
        json_extract_string({{ column }}, '$.{{ key }}')
    {%- endif -%}
{%- endmacro %}

{% macro understory_json_int(column, key) -%}
    cast({{ understory_json_string(column, key) }} as {{ 'int64' if target.type == 'bigquery' else 'bigint' }})
{%- endmacro %}

{% macro understory_json_bool(column, key) -%}
    cast({{ understory_json_string(column, key) }} as boolean)
{%- endmacro %}

{% macro understory_json_string_list(column, key) -%}
    {%- if target.type == 'bigquery' -%}
        json_value_array({{ column }}, '$.{{ key }}')
    {%- else -%}
        from_json(json_extract({{ column }}, '$.{{ key }}'), '["VARCHAR"]')
    {%- endif -%}
{%- endmacro %}

{# Whole minutes from `earlier` to `later`. #}
{% macro understory_minutes_between(earlier, later) -%}
    {%- if target.type == 'bigquery' -%}
        timestamp_diff({{ later }}, {{ earlier }}, minute)
    {%- else -%}
        date_diff('minute', {{ earlier }}, {{ later }})
    {%- endif -%}
{%- endmacro %}

{% macro understory_seconds_between(earlier, later) -%}
    {%- if target.type == 'bigquery' -%}
        timestamp_diff({{ later }}, {{ earlier }}, second)
    {%- else -%}
        date_diff('second', {{ earlier }}, {{ later }})
    {%- endif -%}
{%- endmacro %}

{# Timestamp-with-zone column as a naive UTC timestamp, so dates are stable regardless of session time zone. #}
{% macro understory_utc(column) -%}
    {%- if target.type == 'bigquery' -%}
        {{ column }}
    {%- else -%}
        cast({{ column }} at time zone 'UTC' as timestamp)
    {%- endif -%}
{%- endmacro %}
