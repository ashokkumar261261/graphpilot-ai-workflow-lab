"""Generic, local-first LangGraph workflow with a human approval gate."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, TypedDict

try:
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import interrupt
except ImportError:  # A tiny fallback keeps the architecture inspectable offline.
    END, START = "__end__", "__start__"

    class MemorySaver:
        def __init__(self):
            self.states = {}

        def get(self, config):
            return self.states.get(config["configurable"]["thread_id"])

        def put(self, config, state):
            self.states[config["configurable"]["thread_id"]] = state.copy()

    class WorkflowPaused(Exception):
        pass

    def interrupt(payload):
        raise WorkflowPaused(payload)

    class StateGraph:
        def __init__(self, schema):
            self.nodes, self.edges, self.routes = {}, {}, {}

        def add_node(self, name, function):
            self.nodes[name] = function

        def add_edge(self, source, target):
            self.edges[source] = target

        def add_conditional_edges(self, source, route, mapping):
            self.routes[source] = (route, mapping)

        def compile(self, checkpointer=None):
            return LocalGraph(self, checkpointer or MemorySaver())

    class LocalGraph:
        def __init__(self, graph, checkpointer):
            self.graph, self.checkpointer = graph, checkpointer

        async def ainvoke(self, inputs, config, resume=None):
            state = self.checkpointer.get(config) or inputs.copy()
            if resume:
                state.update(resume)
                current = self.graph.routes["human_review"][1][self.graph.routes["human_review"][0](state)]
            else:
                current = self.graph.edges[START]
            try:
                while current != END:
                    state.update(await self.graph.nodes[current](state))
                    if current in self.graph.routes:
                        route, mapping = self.graph.routes[current]
                        current = mapping[route(state)]
                    else:
                        current = self.graph.edges[current]
            except WorkflowPaused as paused:
                state["status"] = "WAITING_FOR_REVIEW"
                state["review_payload"] = paused.args[0]
                self.checkpointer.put(config, state)
                return state
            state["status"] = "COMPLETED"
            self.checkpointer.put(config, state)
            return state


class WorkflowState(TypedDict, total=False):
    request_id: str
    question: str
    context: List[str]
    retrieved_context: List[str]
    draft_answer: str
    evaluation: Dict[str, Any]
    reviewer_decision: str
    reviewer_notes: str
    final_answer: str
    status: str
    execution_trace: List[str]
    review_payload: Dict[str, Any]


KNOWLEDGE_BASE = [
    "Our service target is 99.9 percent monthly availability.",
    "Customer data is encrypted in transit and at rest.",
    "Support requests are acknowledged within one business day.",
    "Major changes require human review before publication.",
    "The evaluation set measures groundedness, relevance, and answer quality.",
]


def _trace(state: WorkflowState, message: str) -> List[str]:
    return state.get("execution_trace", []) + [message]


async def retrieve_context(state: WorkflowState) -> Dict[str, Any]:
    terms = set(state["question"].lower().split())
    ranked = sorted(KNOWLEDGE_BASE, key=lambda item: len(terms & set(item.lower().split())), reverse=True)
    context = [item for item in ranked[:3] if len(terms & set(item.lower().split())) > 0] or KNOWLEDGE_BASE[:2]
    return {"retrieved_context": context, "execution_trace": _trace(state, "Retrieved grounded context from the local knowledge base.")}


async def evaluate_request(state: WorkflowState) -> Dict[str, Any]:
    context = state["retrieved_context"]
    answer = " ".join(context)
    evaluation = {"grounded": bool(context), "context_count": len(context), "needs_review": True}
    return {"draft_answer": answer, "evaluation": evaluation, "execution_trace": _trace(state, "Drafted an answer and marked it for human review.")}


async def human_review(state: WorkflowState) -> Dict[str, Any]:
    payload = {
        "question": state["question"],
        "draft_answer": state["draft_answer"],
        "retrieved_context": state["retrieved_context"],
        "evaluation": state["evaluation"],
        "allowed_decisions": ["Approved", "Rejected", "Need Changes"],
    }
    response = interrupt(payload)
    return {
        "reviewer_decision": response.get("decision", "Need Changes"),
        "reviewer_notes": response.get("notes", ""),
        "execution_trace": _trace(state, "Received an explicit reviewer decision."),
    }


async def finalize(state: WorkflowState) -> Dict[str, Any]:
    decision = state.get("reviewer_decision", "Need Changes")
    if decision == "Approved":
        answer = state["draft_answer"]
    elif decision == "Rejected":
        answer = "The draft was rejected and was not published."
    else:
        answer = "The draft needs changes before it can be published."
    return {"final_answer": answer, "execution_trace": _trace(state, "Finalized the reviewed workflow result.")}


def route_after_review(state: WorkflowState) -> str:
    return "finalize"


def build_workflow(checkpointer: Optional[MemorySaver] = None):
    graph = StateGraph(WorkflowState)
    graph.add_node("retrieve_context", retrieve_context)
    graph.add_node("evaluate_request", evaluate_request)
    graph.add_node("human_review", human_review)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "retrieve_context")
    graph.add_edge("retrieve_context", "evaluate_request")
    graph.add_edge("evaluate_request", "human_review")
    graph.add_conditional_edges("human_review", route_after_review, {"finalize": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


class WorkflowAgent:
    def __init__(self):
        self.graph = build_workflow(MemorySaver())

    async def run(self, request_id: str, question: str):
        result = await self.graph.ainvoke(
            {"request_id": request_id, "question": question, "context": KNOWLEDGE_BASE, "execution_trace": []},
            {"configurable": {"thread_id": request_id}},
        )
        if "__interrupt__" in result:
            interrupt_value = result["__interrupt__"][0]
            payload = getattr(interrupt_value, "value", interrupt_value)
            result["review_payload"] = payload
            result["status"] = "WAITING_FOR_REVIEW"
        return result

    async def resume(self, request_id: str, decision: str, notes: str):
        try:
            from langgraph.types import Command
            result = await self.graph.ainvoke(Command(resume={"decision": decision, "notes": notes}), {"configurable": {"thread_id": request_id}})
            result["status"] = "COMPLETED"
            return result
        except (ImportError, TypeError):
            result = await self.graph.ainvoke({}, {"configurable": {"thread_id": request_id}}, resume={"decision": decision, "notes": notes})
            result["status"] = "COMPLETED"
            return result
