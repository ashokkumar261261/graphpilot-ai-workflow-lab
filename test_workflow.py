import asyncio

from database import WorkflowDatabase
from evaluation import evaluate_response
from workflow_engine import WorkflowAgent


def test_workflow_pauses_resumes_and_evaluates(tmp_path):
    async def scenario():
        database = WorkflowDatabase(str(tmp_path / "workflow.db"))
        agent = WorkflowAgent(database)
        first = await agent.run("test-run", "What is our availability target?")
        assert first["status"] == "WAITING_FOR_REVIEW"
        assert first["review_payload"]["retrieved_context"]

        final = await agent.resume("test-run", "Approved", "Grounded and relevant.")
        assert final["status"] == "COMPLETED"
        assert final["final_answer"]

        metrics = evaluate_response(
            first["question"],
            final["final_answer"],
            first["retrieved_context"],
        )
        assert 0.0 <= metrics["overall_score"] <= 1.0
        agent.record_evaluation("test-run", metrics)
        assert database.count_rows("workflow_runs") == 1
        assert database.count_rows("review_actions") == 1
        assert database.count_rows("evaluations") == 1

    asyncio.run(scenario())


def test_tenant_workflow_overrides_system_workflow(tmp_path):
    database = WorkflowDatabase(str(tmp_path / "workflow.db"))
    system_definition = database.get_definition("qa_review", "enterprise-tenant")
    tenant_definition = database.get_definition("qa_review", "demo-tenant")

    assert system_definition["scope_type"] == "SYSTEM"
    assert tenant_definition["scope_type"] == "TENANT"
    assert tenant_definition["tenant_id"] == "demo-tenant"
    assert tenant_definition["id"] != system_definition["id"]


def test_custom_workflow_definition_can_be_created_at_system_scope(tmp_path):
    database = WorkflowDatabase(str(tmp_path / "workflow.db"))
    definition_id = database.create_workflow_definition(
        workflow_key="simple_review",
        name="Simple review workflow",
        scope_type="SYSTEM",
        tenant_id=None,
        nodes=[
            {"node_key": "retrieve", "handler_key": "retrieve_context", "config": {"max_results": 1}},
            {"node_key": "draft", "handler_key": "evaluate_request"},
        ],
        edges=[
            {"source_node": "__start__", "target_node": "retrieve"},
            {"source_node": "retrieve", "target_node": "draft"},
        ],
    )

    definition = database.get_definition("simple_review", "enterprise-tenant")
    assert definition["id"] == definition_id
    assert definition["nodes"][0]["config"]["max_results"] == 1


def test_leave_workflow_approves_and_notifies_employee(tmp_path):
    async def scenario():
        database = WorkflowDatabase(str(tmp_path / "leave.db"))
        agent = WorkflowAgent(database, tenant_id="demo-tenant", workflow_key="leave_request")
        first = await agent.run(
            "leave-approved",
            "Personal leave request",
            employee_id="EMP-1001",
            leave_type="Vacation",
            requested_dates=["2026-11-10", "2026-11-11"],
        )
        assert first["status"] == "WAITING_FOR_REVIEW"
        assert first["available_balance"] == 15
        assert "project_impact_summary" in first

        final = await agent.resume("leave-approved", "Approved", "Coverage confirmed.")
        assert final["status"] == "APPROVED"
        assert "approved" in final["notification_draft"]

    asyncio.run(scenario())


def test_leave_policy_rejection_bypasses_manager(tmp_path):
    async def scenario():
        database = WorkflowDatabase(str(tmp_path / "leave.db"))
        agent = WorkflowAgent(database, tenant_id="demo-tenant", workflow_key="leave_request")
        result = await agent.run(
            "leave-rejected",
            "Long leave request",
            employee_id="EMP-1001",
            leave_type="Vacation",
            requested_dates=[f"2026-11-{day:02d}" for day in range(1, 20)],
        )
        assert result["status"] == "REJECTED"
        assert result["policy_status"] == "REJECTED"
        assert "only 15 days" in result["notification_draft"]

    asyncio.run(scenario())


def test_leave_need_changes_loops_back_with_revised_dates(tmp_path):
    async def scenario():
        database = WorkflowDatabase(str(tmp_path / "leave.db"))
        agent = WorkflowAgent(database, tenant_id="demo-tenant", workflow_key="leave_request")
        first = await agent.run(
            "leave-negotiate",
            "Vacation request",
            employee_id="EMP-1001",
            leave_type="Vacation",
            requested_dates=["2026-12-15"],
        )
        assert first["status"] == "WAITING_FOR_REVIEW"
        changed = await agent.resume(
            "leave-negotiate",
            "Need Changes",
            "Please move this after the release.",
            requested_dates=["2026-12-22"],
        )
        assert changed["status"] == "WAITING_FOR_REVIEW"
        assert changed["requested_dates"] == ["2026-12-22"]

    asyncio.run(scenario())
