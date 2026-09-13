import asyncio

from evaluation import evaluate_response
from workflow_engine import WorkflowAgent


def test_workflow_pauses_resumes_and_evaluates():
    async def scenario():
        agent = WorkflowAgent()
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

    asyncio.run(scenario())
