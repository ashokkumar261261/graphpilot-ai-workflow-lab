"""Streamlit console for configurable HR and AI workflows."""

import asyncio
import uuid

import streamlit as st

from database import WorkflowDatabase
from evaluation import evaluate_response
from workflow_engine import WorkflowAgent


st.set_page_config(page_title="Workflow orchestration lab", page_icon=":material/account_tree:", layout="wide")


@st.cache_resource
def get_database() -> WorkflowDatabase:
    return WorkflowDatabase()


@st.cache_resource
def get_agent(tenant_id: str, workflow_key: str) -> WorkflowAgent:
    return WorkflowAgent(get_database(), tenant_id=tenant_id, workflow_key=workflow_key)


def run(coroutine):
    return asyncio.run(coroutine)


database = get_database()
tenants = database.list_tenants()
tenant_options = {tenant["name"]: tenant["id"] for tenant in tenants}

st.title("Workflow orchestration lab")
st.caption("Run a tenant-configured LangGraph workflow from submission through policy checks, human review, and notification.")

with st.sidebar:
    st.header("Start a workflow")
    selected_tenant = st.selectbox("Tenant", list(tenant_options), index=0)
    tenant_id = tenant_options[selected_tenant]
    workflow_label = st.selectbox("Workflow", ["Employee leave request", "Knowledge question"])
    workflow_key = "leave_request" if workflow_label == "Employee leave request" else "qa_review"

    if workflow_key == "leave_request":
        employee_id = st.text_input("Employee ID", value="EMP-1001")
        leave_type = st.selectbox("Leave type", ["Vacation", "Sick"])
        requested_dates = st.date_input("Requested dates", value=[])
        requested_dates = [date.isoformat() for date in requested_dates] if requested_dates else []
        leave_reason = st.text_area("Reason", value="Personal leave request.", height=90)
        form_values = {
            "employee_id": employee_id.strip(),
            "leave_type": leave_type,
            "requested_dates": requested_dates,
            "question": leave_reason,
        }
    else:
        question = st.text_area(
            "User request",
            value="What is our availability target and how is customer data protected?",
            height=120,
        )
        form_values = {"question": question}

    run_workflow = st.button("Start workflow", type="primary", width="stretch")
    reset = st.button("Clear run", width="stretch")
    if reset:
        for key in ("run_id", "workflow_result", "final_result", "evaluation", "tenant_id", "workflow_key"):
            st.session_state.pop(key, None)
        st.rerun()

if run_workflow:
    request_id = f"run-{uuid.uuid4().hex[:8]}"
    try:
        if workflow_key == "leave_request" and (not form_values["employee_id"] or not form_values["requested_dates"]):
            st.error("Enter an employee ID and at least one requested date.")
        else:
            st.session_state.run_id = request_id
            st.session_state.tenant_id = tenant_id
            st.session_state.workflow_key = workflow_key
            st.session_state.workflow_result = run(
                get_agent(tenant_id, workflow_key).run(request_id, **form_values)
            )
            st.session_state.final_result = None
            st.session_state.evaluation = None
    except Exception as error:
        st.error(str(error))

workflow_result = st.session_state.get("workflow_result")
if workflow_result:
    active_workflow = st.session_state.get("workflow_key", "qa_review")
    active_tenant = st.session_state.get("tenant_id", tenant_id)
    payload = workflow_result.get("review_payload", {})
    st.subheader("Workflow review")
    metrics = st.columns(4)
    metrics[0].metric("Status", workflow_result.get("status", "WAITING_FOR_REVIEW").replace("_", " ").title())
    metrics[1].metric("Workflow", "Leave request" if active_workflow == "leave_request" else "Knowledge question")
    metrics[2].metric("Tenant", active_tenant)
    metrics[3].metric("Human gate", "Required" if payload else "Not required")

    if active_workflow == "leave_request":
        st.write("**Employee**", payload.get("employee_name", workflow_result.get("employee_id", "")))
        left, right = st.columns(2)
        with left:
            st.write("**Leave request**")
            st.info(f"{payload.get('leave_type', workflow_result.get('leave_type', ''))}: {', '.join(payload.get('requested_dates', workflow_result.get('requested_dates', [])))}")
            st.write("**Policy check**")
            st.write(payload.get("policy_reason", workflow_result.get("policy_reason", "Pending")))
        with right:
            st.write("**Available balance**")
            st.metric("Days available", payload.get("available_balance", workflow_result.get("available_balance", "-")))
            st.write("**Project impact**")
            st.warning(payload.get("project_impact_summary", workflow_result.get("project_impact_summary", "No impact analysis available.")))
    else:
        left, right = st.columns(2)
        with left:
            st.write("**Question**")
            st.info(payload.get("question", ""))
            st.write("**Draft answer**")
            st.write(payload.get("draft_answer", ""))
        with right:
            st.write("**Retrieved context**")
            for source in payload.get("retrieved_context", []):
                st.success(source, icon=":material/menu_book:")

    if payload:
        st.divider()
        st.subheader("Human-in-the-loop review")
        allowed_decisions = payload.get("allowed_decisions", ["Approved", "Rejected", "Need Changes"])
        decision = st.segmented_control("Decision", allowed_decisions, default=allowed_decisions[0])
        notes = st.text_area("Reviewer notes", value="Reviewed the request and supporting data.")
        revised_dates = []
        if active_workflow == "leave_request" and decision == "Need Changes":
            revised_dates = st.date_input("Proposed revised dates", value=[])
            revised_dates = [date.isoformat() for date in revised_dates] if revised_dates else []
        if st.button("Submit review", type="primary"):
            try:
                final_result = run(
                    get_agent(active_tenant, active_workflow).resume(
                        st.session_state.run_id,
                        decision,
                        notes,
                        requested_dates=revised_dates or None,
                    )
                )
                st.session_state.final_result = final_result
                if active_workflow == "qa_review":
                    st.session_state.evaluation = evaluate_response(
                        payload.get("question", ""),
                        final_result.get("final_answer", ""),
                        payload.get("retrieved_context", []),
                    )
                    get_agent(active_tenant, active_workflow).record_evaluation(st.session_state.run_id, st.session_state.evaluation)
            except Exception as error:
                st.error(str(error))

final_result = st.session_state.get("final_result")
if final_result:
    st.success("Workflow completed.")
    st.subheader("Final result")
    st.write(final_result.get("final_answer", ""))
    if st.session_state.get("workflow_key") == "leave_request":
        st.info(final_result.get("notification_draft", "Employee notification prepared."))
    else:
        evaluation = st.session_state.get("evaluation", {})
        st.subheader("Evaluation")
        scores = st.columns(4)
        scores[0].metric("Faithfulness", evaluation.get("faithfulness", "-"))
        scores[1].metric("Answer relevancy", evaluation.get("answer_relevancy", "-"))
        scores[2].metric("Overall score", evaluation.get("overall_score", "-"))
        scores[3].metric("RAGAS detected", "Yes" if evaluation.get("ragas_available") else "Local fallback")
    with st.expander("Execution trace", icon=":material/route:"):
        for step in final_result.get("execution_trace", []):
            st.write(step)
