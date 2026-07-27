"""
Regression pins for the whale-tenant attribute-filter timeout (2026-07-24).

A SPAN_ATTRIBUTE filter (`customer_id = 26065846`) on the trace list made
every query 400 with CH Code 159: the `trace_id IN (SELECT … FROM spans …)`
membership subquery scanned the project's ENTIRE span history (157M+ rows,
19+ GiB of attrs maps) inside the 10s execution budget.

Two distinct emitters were at fault:

1. The v2 filter compiler bounded the subquery on ``created_at`` — correct
   for the v1 table (partitioned by ``toYYYYMM(created_at)``) but a no-op on
   the CH25 table, which is partitioned by ``toDate(start_time)`` with
   ``toStartOfHour(start_time)`` in the primary key and no index at all on
   ``created_at``.

2. ``TimeSeriesQueryBuilder`` built its filter compiler with no project and
   no date scope, so the graph query's subquery was a full cross-tenant
   table scan (``WHERE 1 = 1 AND … mapContains(attrs_number, …)``).
"""

from __future__ import annotations

from tracer.services.clickhouse.query_builders.time_series import (
    TimeSeriesQueryBuilder,
)
from tracer.services.clickhouse.v2.query_builders.trace_list import (
    TraceListQueryBuilderV2,
)

PROJECT_ID = "11111111-1111-1111-1111-111111111111"

DATETIME_FILTER = {
    "column_id": "created_at",
    "filter_config": {
        "filter_type": "datetime",
        "filter_op": "between",
        "filter_value": ["2026-06-24T17:23:59.000Z", "2026-07-24T18:30:00.000Z"],
    },
}

SPAN_ATTR_FILTER = {
    "column_id": "customer_id",
    "filter_config": {
        "col_type": "SPAN_ATTRIBUTE",
        "filter_type": "number",
        "filter_op": "equals",
        "filter_value": 26065846,
    },
}


def _membership_subquery(sql: str) -> str:
    assert "trace_id IN (" in sql, f"no membership subquery emitted:\n{sql}"
    return sql.split("trace_id IN (")[1]


class TestV2MembershipSubqueryBounds:
    def test_span_attr_membership_bounds_on_start_time(self):
        builder = TraceListQueryBuilderV2(
            project_id=PROJECT_ID,
            page_number=0,
            page_size=10,
            filters=[DATETIME_FILTER, SPAN_ATTR_FILTER],
            sort_params=[],
            eval_config_ids=[],
            annotation_label_ids=[],
        )
        sql, params = builder.build()
        sub = _membership_subquery(sql)
        assert "start_time >= %(start_date)s - INTERVAL 1 DAY" in sub
        assert "created_at >=" not in sub
        assert "start_date" in params

    def test_system_metric_membership_bounds_on_start_time(self):
        builder = TraceListQueryBuilderV2(
            project_id=PROJECT_ID,
            page_number=0,
            page_size=10,
            filters=[
                DATETIME_FILTER,
                {
                    "column_id": "model",
                    "filter_config": {
                        "col_type": "SYSTEM_METRIC",
                        "filter_type": "text",
                        "filter_op": "equals",
                        "filter_value": "gpt-4o",
                    },
                },
            ],
            sort_params=[],
            eval_config_ids=[],
            annotation_label_ids=[],
        )
        sql, _ = builder.build()
        sub = _membership_subquery(sql)
        assert "start_time >= %(start_date)s - INTERVAL 1 DAY" in sub
        assert "created_at >=" not in sub


class TestTimeSeriesAttrFilterScope:
    def test_attr_subquery_is_project_scoped_and_time_bounded(self):
        builder = TimeSeriesQueryBuilder(
            project_id=PROJECT_ID,
            filters=[DATETIME_FILTER, SPAN_ATTR_FILTER],
            interval="day",
        )
        sql, params = builder.build()
        sub = _membership_subquery(sql)
        assert "project_id = %(project_id)s" in sub
        assert "start_time >= %(start_date)s - INTERVAL 1 DAY" in sub
        assert "1 = 1" not in sub
        assert params["project_id"] == PROJECT_ID
        assert "start_date" in params
