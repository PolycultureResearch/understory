{#
  Relation expression for one log family: 'events' or 'text'.

  DuckDB reads the Parquet files straight off the log tree with a glob over
  tenant and dt partitions. BigQuery reads through the `understory_log` source,
  which the deployment binds to external tables over the GCS prefixes.
#}
{% macro understory_read_parquet(family) -%}
    {%- if target.type == 'bigquery' -%}
        {{ source('understory_log', family) }}
    {%- else -%}
        read_parquet(
            '{{ var("understory_log_dir") }}/{{ var("understory_tenant") }}/{{ family }}/*/*.parquet',
            hive_partitioning = true,
            union_by_name = true
        )
    {%- endif -%}
{%- endmacro %}
