{#
  Use a custom schema name verbatim, so the restricted marts land in a schema
  literally called `restricted` rather than `<target>_restricted`. dbt only
  honours this override in the root project; when this package is installed
  into a client project, that project decides the naming.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
