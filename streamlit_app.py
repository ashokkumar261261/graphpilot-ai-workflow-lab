"""Streamlit console for the generic AI workflow evaluation lab."""

import asyncio
import uuid

import streamlit as st

from evaluation import evaluate_response
from workflow_engine import WorkflowAgent


st.set_page_config(page_title="AI Workflow Lab", page_icon=":material/account_tree:", layout="wide")


@st.cache_resource
def get_agent() -> WorkflowAgent:
    return WorkflowAgent()


def run(coroutine):
    return asyncio.run(coroutine)


st.title("AI workflow evaluation lab")
st.caption("A local, end-to-end demonstration of graph orchestration, grounded generation, human review, and evaluation.")

with st.sidebar:
    st.header("Run a workflow")
    question = st.text_area(
        "User request",
        value="What is our availability target and how is customer data protected?",
        height=120,
    )
    run_workflow = st.button("Run workflow", type="primary", width="stretch")
    reset = st.button("Clear run", width="stretch")
    if reset:
        for key in ("run_id", "workflow_result", "final_result", "evaluation"):
            st.session_state.pop(key, None)
        st.rerun()

if run_workflow:
    request_id = f"run-{uuid.uuid4().hex[:8]}"
    try:
        st.session_state.run_id = request_id
        st.session_state.workflow_result = run(get_agent().run(request_id, question))
        st.session_state.final_result = None
        st.session_state.evaluation = None
    except Exception as error:
        st.error(str(error))

workflow_result = st.session_state.get("workflow_result")
if workflow_result:
    payload = workflow_result.get("review_payload", {})
    st.subheader("Workflow review")
    metrics = st.columns(4)
    metrics[0].metric("Status", workflow_result.get("status", "WAITING_FOR_REVIEW").replace("_", " ").title())
    metrics[1].metric("Retrieved sources", len(payload.get("retrieved_context", [])))
    metrics[2].metric("Grounded", "Yes" if payload.get("evaluation", {}).get("grounded") else "No")
    metrics[3].metric("Human gate", "Required")

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

    st.divider()
    st.subheader("Human-in-the-loop review")
    decision = st.segmented_control("Decision", ["Approved", "Rejected", "Need Changes"], default="Approved")
    notes = st.text_area("Reviewer notes", value="Reviewed for grounding and relevance.")
    if st.button("Submit review", type="primary"):
        try:
            final_result = run(get_agent().resume(st.session_state.run_id, decision, notes))
            st.session_state.final_result = final_result
            st.session_state.evaluation = evaluate_response(
                payload.get("question", ""),
                final_result.get("final_answer", ""),
                payload.get("retrieved_context", []),
            )
        except Exception as error:
            st.error(str(error))

final_result = st.session_state.get("final_result")
if final_result:
    st.success("Workflow completed.")
    st.subheader("Final result")
    st.write(final_result.get("final_answer", ""))
    evaluation = st.session_state.get("evaluation", {})
    st.subheader("Evaluation")
    scores = st.columns(4)
    scores[0].metric("Faithfulness", evaluation.get("faithfulness", "-"))
    scores[1].metric("Answer relevancy", evaluation.get("answer_relevancy", "-"))
    scores[2].metric("Overall score", evaluation.get("overall_score", "-"))
    scores[3].metric("RAGAS detected", "Yes" if evaluation.get("ragas_available") else "Local fallback")
    st.caption("The local fallback is deterministic and transparent. Install ragas to connect this adapter to a full RAGAS evaluation pipeline.")
    with st.expander("Execution trace", icon=":material/route:"):
        for step in final_result.get("execution_trace", []):
            st.write(step)
