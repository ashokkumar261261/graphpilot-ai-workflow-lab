"""SQLite persistence and tenant-aware workflow configuration."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_DB_PATH = Path(__file__).with_name("graphpilot.db")


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_definitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_key TEXT NOT NULL,
    name TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('SYSTEM', 'TENANT')),
    tenant_id TEXT REFERENCES tenants(id),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    CHECK ((scope_type = 'SYSTEM' AND tenant_id IS NULL) OR
           (scope_type = 'TENANT' AND tenant_id IS NOT NULL)),
    UNIQUE (workflow_key, scope_type, tenant_id, version)
);

CREATE TABLE IF NOT EXISTS workflow_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    definition_id INTEGER NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    handler_key TEXT NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE (definition_id, node_key)
);

CREATE TABLE IF NOT EXISTS workflow_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    definition_id INTEGER NOT NULL REFERENCES workflow_definitions(id) ON DELETE CASCADE,
    source_node TEXT NOT NULL,
    target_node TEXT NOT NULL,
    condition_key TEXT,
    priority INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS knowledge_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT REFERENCES tenants(id),
    statement TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employees (
    employee_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    employee_name TEXT NOT NULL,
    manager_name TEXT NOT NULL,
    department TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS leave_balances (
    employee_id TEXT NOT NULL REFERENCES employees(employee_id) ON DELETE CASCADE,
    leave_type TEXT NOT NULL,
    available_days INTEGER NOT NULL CHECK (available_days >= 0),
    PRIMARY KEY (employee_id, leave_type)
);

CREATE TABLE IF NOT EXISTS team_calendar (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    department TEXT NOT NULL,
    leave_date TEXT NOT NULL,
    employee_count INTEGER NOT NULL DEFAULT 0 CHECK (employee_count >= 0),
    project_name TEXT NOT NULL,
    risk_level TEXT NOT NULL DEFAULT 'LOW'
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    request_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    workflow_key TEXT NOT NULL,
    definition_id INTEGER NOT NULL REFERENCES workflow_definitions(id),
    question TEXT NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES workflow_runs(request_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES workflow_runs(request_id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL REFERENCES workflow_runs(request_id) ON DELETE CASCADE,
    faithfulness REAL,
    answer_relevancy REAL,
    overall_score REAL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workflow_definitions_lookup
    ON workflow_definitions (workflow_key, tenant_id, is_active, version);
CREATE INDEX IF NOT EXISTS idx_knowledge_items_scope
    ON knowledge_items (tenant_id, active);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_tenant_status
    ON workflow_runs (tenant_id, status, started_at);
CREATE INDEX IF NOT EXISTS idx_workflow_events_request
    ON workflow_events (request_id, created_at);
"""


SYSTEM_KNOWLEDGE = [
    "Our service target is 99.9 percent monthly availability.",
    "Customer data is encrypted in transit and at rest.",
    "Support requests are acknowledged within one business day.",
    "Major changes require human review before publication.",
    "The evaluation set measures groundedness, relevance, and answer quality.",
]


class WorkflowDatabase:
    """Small SQLite repository used by the demo and its tests."""

    def __init__(self, path: Optional[str] = None):
        configured_path = path or os.getenv("GRAPHPILOT_DB_PATH")
        self.path = Path(configured_path) if configured_path else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._seed(connection)

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _seed(self, connection: sqlite3.Connection) -> None:
        now = self._now()
        connection.execute(
            "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
            ("demo-tenant", "Demo tenant", now),
        )
        connection.execute(
            "INSERT OR IGNORE INTO tenants (id, name, created_at) VALUES (?, ?, ?)",
            ("enterprise-tenant", "Enterprise tenant", now),
        )
        for statement in SYSTEM_KNOWLEDGE:
            exists = connection.execute(
                "SELECT 1 FROM knowledge_items WHERE tenant_id IS NULL AND statement = ?",
                (statement,),
            ).fetchone()
            if not exists:
                connection.execute(
                    """
                    INSERT INTO knowledge_items
                        (tenant_id, statement, created_at, updated_at)
                    VALUES (NULL, ?, ?, ?)
                    """,
                    (statement, now, now),
                )
        self._seed_employees(connection, now)
        self._seed_definition(connection, "SYSTEM", None, "System approval workflow")
        self._seed_definition(connection, "TENANT", "demo-tenant", "Demo tenant approval workflow")
        self._seed_leave_definition(connection, "SYSTEM", None, "System leave orchestration workflow")
        self._seed_leave_definition(connection, "TENANT", "demo-tenant", "Demo tenant leave orchestration workflow")
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_items
                (tenant_id, statement, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            """,
            ("demo-tenant", "Demo tenant requests are reviewed by the operations team.", now, now),
        )

    def _seed_employees(self, connection: sqlite3.Connection, now: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO employees (employee_id, tenant_id, employee_name, manager_name, department) VALUES (?, ?, ?, ?, ?)",
            ("EMP-1001", "demo-tenant", "Ava Sharma", "Jordan Lee", "Engineering"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO employees (employee_id, tenant_id, employee_name, manager_name, department) VALUES (?, ?, ?, ?, ?)",
            ("EMP-2001", "enterprise-tenant", "Noah Patel", "Morgan Chen", "Product"),
        )
        for employee_id, leave_type, available_days in (
            ("EMP-1001", "Vacation", 15),
            ("EMP-1001", "Sick", 8),
            ("EMP-2001", "Vacation", 20),
            ("EMP-2001", "Sick", 10),
        ):
            connection.execute(
                "INSERT OR IGNORE INTO leave_balances (employee_id, leave_type, available_days) VALUES (?, ?, ?)",
                (employee_id, leave_type, available_days),
            )
        connection.execute(
            "INSERT OR IGNORE INTO team_calendar (tenant_id, department, leave_date, employee_count, project_name, risk_level) VALUES (?, ?, ?, ?, ?, ?)",
            ("demo-tenant", "Engineering", "2026-12-15", 2, "Quarterly platform release", "HIGH"),
        )
        connection.execute(
            "INSERT OR IGNORE INTO team_calendar (tenant_id, department, leave_date, employee_count, project_name, risk_level) VALUES (?, ?, ?, ?, ?, ?)",
            ("enterprise-tenant", "Product", "2026-12-15", 1, "Roadmap planning", "MEDIUM"),
        )

    def _seed_leave_definition(
        self,
        connection: sqlite3.Connection,
        scope_type: str,
        tenant_id: Optional[str],
        name: str,
    ) -> None:
        existing = connection.execute(
            "SELECT id FROM workflow_definitions WHERE workflow_key = 'leave_request' AND scope_type = ? AND tenant_id IS ? AND version = 1",
            (scope_type, tenant_id),
        ).fetchone()
        if existing:
            return
        now = self._now()
        cursor = connection.execute(
            "INSERT INTO workflow_definitions (workflow_key, name, scope_type, tenant_id, version, created_at) VALUES ('leave_request', ?, ?, ?, 1, ?)",
            (name, scope_type, tenant_id, now),
        )
        definition_id = cursor.lastrowid
        nodes = [
            ("fetch_leave_data", "fetch_leave_data", {}, 1),
            ("check_leave_policy", "check_leave_policy", {}, 2),
            ("analyze_leave_impact", "analyze_leave_impact", {}, 3),
            ("manager_review", "manager_review", {"allowed_decisions": ["Approved", "Rejected", "Need Changes"]}, 4),
            ("negotiate_leave", "negotiate_leave", {}, 5),
            ("finalize_leave", "finalize_leave", {}, 6),
            ("notify_employee", "notify_employee", {}, 7),
        ]
        connection.executemany(
            "INSERT INTO workflow_nodes (definition_id, node_key, handler_key, config_json, position) VALUES (?, ?, ?, ?, ?)",
            [(definition_id, key, handler, json.dumps(config), position) for key, handler, config, position in nodes],
        )
        edges = [
            ("__start__", "fetch_leave_data", None, 0),
            ("fetch_leave_data", "check_leave_policy", None, 0),
            ("check_leave_policy", "analyze_leave_impact", "policy_pass", 1),
            ("check_leave_policy", "notify_employee", "policy_reject", 2),
            ("analyze_leave_impact", "manager_review", None, 0),
            ("manager_review", "finalize_leave", "Approved", 1),
            ("manager_review", "notify_employee", "Rejected", 2),
            ("manager_review", "negotiate_leave", "Need Changes", 3),
            ("negotiate_leave", "fetch_leave_data", None, 0),
            ("finalize_leave", "notify_employee", None, 0),
            ("notify_employee", "__end__", None, 0),
        ]
        connection.executemany(
            "INSERT INTO workflow_edges (definition_id, source_node, target_node, condition_key, priority) VALUES (?, ?, ?, ?, ?)",
            [(definition_id, source, target, condition, priority) for source, target, condition, priority in edges],
        )

    def _seed_definition(
        self,
        connection: sqlite3.Connection,
        scope_type: str,
        tenant_id: Optional[str],
        name: str,
    ) -> None:
        existing = connection.execute(
            """
            SELECT id FROM workflow_definitions
            WHERE workflow_key = 'qa_review'
              AND scope_type = ?
              AND tenant_id IS ?
              AND version = 1
            """,
            (scope_type, tenant_id),
        ).fetchone()
        if existing:
            return
        now = self._now()
        cursor = connection.execute(
            """
            INSERT INTO workflow_definitions
                (workflow_key, name, scope_type, tenant_id, version, created_at)
            VALUES ('qa_review', ?, ?, ?, 1, ?)
            """,
            (name, scope_type, tenant_id, now),
        )
        definition_id = cursor.lastrowid
        nodes = [
            ("retrieve_context", "retrieve_context", {"max_results": 3}, 1),
            ("evaluate_request", "evaluate_request", {}, 2),
            ("human_review", "human_review", {"allowed_decisions": ["Approved", "Rejected", "Need Changes"]}, 3),
            ("finalize", "finalize", {}, 4),
        ]
        connection.executemany(
            """
            INSERT INTO workflow_nodes (definition_id, node_key, handler_key, config_json, position)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(definition_id, key, handler, json.dumps(config), position) for key, handler, config, position in nodes],
        )
        edges = [
            ("__start__", "retrieve_context", None, 0),
            ("retrieve_context", "evaluate_request", None, 0),
            ("evaluate_request", "human_review", None, 0),
            ("human_review", "finalize", "Approved", 1),
            ("human_review", "finalize", "Rejected", 2),
            ("human_review", "finalize", "Need Changes", 3),
            ("finalize", "__end__", None, 0),
        ]
        connection.executemany(
            """
            INSERT INTO workflow_edges
                (definition_id, source_node, target_node, condition_key, priority)
            VALUES (?, ?, ?, ?, ?)
            """,
            [(definition_id, source, target, condition, priority) for source, target, condition, priority in edges],
        )

    def list_tenants(self) -> List[Dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT id, name FROM tenants WHERE active = 1 ORDER BY name"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_workflow_definition(
        self,
        workflow_key: str,
        name: str,
        scope_type: str,
        tenant_id: Optional[str],
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
        version: int = 1,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Create a versioned system or tenant workflow from configuration data."""
        if scope_type not in {"SYSTEM", "TENANT"}:
            raise ValueError("scope_type must be SYSTEM or TENANT")
        if (scope_type == "SYSTEM") != (tenant_id is None):
            raise ValueError("SYSTEM workflows cannot have a tenant and TENANT workflows require one")
        if not nodes or not edges:
            raise ValueError("A workflow requires at least one node and one edge")
        now = self._now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO workflow_definitions
                    (workflow_key, name, scope_type, tenant_id, version, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (workflow_key, name, scope_type, tenant_id, version, json.dumps(metadata or {}), now),
            )
            definition_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO workflow_nodes
                    (definition_id, node_key, handler_key, config_json, position)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        definition_id,
                        node["node_key"],
                        node["handler_key"],
                        json.dumps(node.get("config", {})),
                        node.get("position", index),
                    )
                    for index, node in enumerate(nodes, start=1)
                ],
            )
            connection.executemany(
                """
                INSERT INTO workflow_edges
                    (definition_id, source_node, target_node, condition_key, priority)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        definition_id,
                        edge["source_node"],
                        edge["target_node"],
                        edge.get("condition_key"),
                        edge.get("priority", index),
                    )
                    for index, edge in enumerate(edges, start=1)
                ],
            )
        return definition_id

    def get_definition(self, workflow_key: str, tenant_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            definition = connection.execute(
                """
                SELECT * FROM workflow_definitions
                WHERE workflow_key = ? AND is_active = 1
                  AND (tenant_id = ? OR tenant_id IS NULL)
                ORDER BY CASE WHEN tenant_id = ? THEN 0 ELSE 1 END, version DESC
                LIMIT 1
                """,
                (workflow_key, tenant_id, tenant_id),
            ).fetchone()
            if not definition:
                raise ValueError(f"No active workflow '{workflow_key}' for tenant '{tenant_id}'")
            definition_id = definition["id"]
            nodes = connection.execute(
                "SELECT * FROM workflow_nodes WHERE definition_id = ? ORDER BY position, id",
                (definition_id,),
            ).fetchall()
            edges = connection.execute(
                "SELECT * FROM workflow_edges WHERE definition_id = ? ORDER BY priority, id",
                (definition_id,),
            ).fetchall()
        result = dict(definition)
        result["metadata"] = json.loads(result.pop("metadata_json"))
        result["nodes"] = [self._decode_node(row) for row in nodes]
        result["edges"] = [dict(row) for row in edges]
        return result

    @staticmethod
    def _decode_node(row: sqlite3.Row) -> Dict[str, Any]:
        node = dict(row)
        node["config"] = json.loads(node.pop("config_json"))
        return node

    def get_knowledge(self, tenant_id: str) -> List[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT statement FROM knowledge_items
                WHERE active = 1 AND (tenant_id = ? OR tenant_id IS NULL)
                ORDER BY CASE WHEN tenant_id = ? THEN 0 ELSE 1 END, id
                """,
                (tenant_id, tenant_id),
            ).fetchall()
        return [row["statement"] for row in rows]

    def get_employee(self, tenant_id: str, employee_id: str) -> Dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM employees WHERE tenant_id = ? AND employee_id = ? AND active = 1",
                (tenant_id, employee_id),
            ).fetchone()
        if not row:
            raise ValueError(f"Employee '{employee_id}' was not found for tenant '{tenant_id}'")
        return dict(row)

    def get_leave_balance(self, employee_id: str, leave_type: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT available_days FROM leave_balances WHERE employee_id = ? AND leave_type = ?",
                (employee_id, leave_type),
            ).fetchone()
        return int(row["available_days"]) if row else 0

    def get_team_calendar(self, tenant_id: str, department: str, requested_dates: List[str]) -> List[Dict[str, Any]]:
        if not requested_dates:
            return []
        placeholders = ",".join("?" for _ in requested_dates)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM team_calendar WHERE tenant_id = ? AND department = ? AND leave_date IN ({placeholders})",
                [tenant_id, department, *requested_dates],
            ).fetchall()
        return [dict(row) for row in rows]

    def save_run(self, state: Dict[str, Any], definition_id: int, completed: bool = False) -> None:
        now = self._now()
        serializable_state = {key: value for key, value in state.items() if key != "__interrupt__"}
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO workflow_runs
                    (request_id, tenant_id, workflow_key, definition_id, question, status, state_json, started_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(request_id) DO UPDATE SET
                    status = excluded.status,
                    state_json = excluded.state_json,
                    completed_at = excluded.completed_at
                """,
                (
                    state["request_id"],
                    state["tenant_id"],
                    state["workflow_key"],
                    definition_id,
                    state["question"],
                    state.get("status", "RUNNING"),
                    json.dumps(serializable_state, default=str),
                    now,
                    now if completed else None,
                ),
            )
            for event in state.get("execution_trace", []):
                connection.execute(
                    "INSERT INTO workflow_events (request_id, event_type, payload_json, created_at) VALUES (?, ?, ?, ?)",
                    (state["request_id"], "trace", json.dumps({"message": event}), now),
                )

    def save_review(self, request_id: str, decision: str, notes: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO review_actions (request_id, decision, notes, created_at) VALUES (?, ?, ?, ?)",
                (request_id, decision, notes, self._now()),
            )

    def save_evaluation(self, request_id: str, evaluation: Dict[str, Any]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO evaluations
                    (request_id, faithfulness, answer_relevancy, overall_score, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    evaluation.get("faithfulness"),
                    evaluation.get("answer_relevancy"),
                    evaluation.get("overall_score"),
                    json.dumps(evaluation, default=str),
                    self._now(),
                ),
            )

    def count_rows(self, table_name: str) -> int:
        allowed_tables = {"tenants", "workflow_definitions", "workflow_nodes", "workflow_edges", "knowledge_items", "workflow_runs", "workflow_events", "review_actions", "evaluations"}
        if table_name not in allowed_tables:
            raise ValueError("Unsupported table")
        with self.connect() as connection:
            return int(connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()[0])
