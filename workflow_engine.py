"""Generic, local-first LangGraph workflow with a human approval gate."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List, Optional, TypedDict

from database import SYSTEM_KNOWLEDGE, WorkflowDatabase

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
    tenant_id: str
    workflow_key: str
    workflow_definition_id: int
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
    employee_id: str
    leave_type: str
    requested_dates: List[str]
    available_balance: int
    employee_data: Dict[str, Any]
    team_calendar: List[Dict[str, Any]]
    policy_status: str
    policy_reason: str
    project_impact_summary: str
    manager_decision: str
    notification_draft: str


KNOWLEDGE_BASE = SYSTEM_KNOWLEDGE
Handler = Callable[[WorkflowState, Dict[str, Any], WorkflowDatabase], Awaitable[Dict[str, Any]]]


def _trace(state: WorkflowState, message: str) -> List[str]:
    return state.get("execution_trace", []) + [message]


async def retrieve_context(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    knowledge = database.get_knowledge(state["tenant_id"])
    terms = set(state["question"].lower().split())
    ranked = sorted(knowledge, key=lambda item: len(terms & set(item.lower().split())), reverse=True)
    max_results = int(config.get("max_results", 3))
    context = [item for item in ranked[:max_results] if len(terms & set(item.lower().split())) > 0] or knowledge[:2]
    return {"retrieved_context": context, "execution_trace": _trace(state, "Retrieved grounded context from the configured knowledge base.")}


async def evaluate_request(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    context = state["retrieved_context"]
    answer = " ".join(context)
    evaluation = {"grounded": bool(context), "context_count": len(context), "needs_review": True}
    return {"draft_answer": answer, "evaluation": evaluation, "execution_trace": _trace(state, "Drafted an answer and marked it for human review.")}


async def human_review(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    allowed_decisions = config.get("allowed_decisions", ["Approved", "Rejected", "Need Changes"])
    payload = {
        "question": state["question"],
        "draft_answer": state["draft_answer"],
        "retrieved_context": state["retrieved_context"],
        "evaluation": state["evaluation"],
        "allowed_decisions": allowed_decisions,
    }
    response = interrupt(payload)
    decision = response.get("decision", allowed_decisions[-1])
    if decision not in allowed_decisions:
        decision = allowed_decisions[-1]
    return {
        "reviewer_decision": decision,
        "reviewer_notes": response.get("notes", ""),
        "execution_trace": _trace(state, "Received an explicit reviewer decision."),
    }


async def finalize(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    decision = state.get("reviewer_decision", "Need Changes")
    if decision == "Approved":
        answer = state["draft_answer"]
    elif decision == "Rejected":
        answer = "The draft was rejected and was not published."
    else:
        answer = "The draft needs changes before it can be published."
    return {"final_answer": answer, "execution_trace": _trace(state, "Finalized the reviewed workflow result.")}


async def fetch_leave_data(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    employee = database.get_employee(state["tenant_id"], state["employee_id"])
    balance = database.get_leave_balance(state["employee_id"], state["leave_type"])
    calendar = database.get_team_calendar(state["tenant_id"], employee["department"], state["requested_dates"])
    return {
        "employee_data": employee,
        "available_balance": balance,
        "team_calendar": calendar,
        "execution_trace": _trace(state, "Fetched employee balance and team calendar data."),
    }


async def check_leave_policy(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    requested_days = len(state["requested_dates"])
    balance = state["available_balance"]
    if requested_days == 0:
        status, reason = "REJECTED", "At least one leave date is required."
    elif requested_days > balance:
        status, reason = "REJECTED", f"The request covers {requested_days} days, but only {balance} days are available."
    else:
        status, reason = "APPROVED_FOR_REVIEW", "The request is within the available leave balance."
    return {
        "policy_status": status,
        "policy_reason": reason,
        "execution_trace": _trace(state, f"Checked leave policy: {status.lower()}.") ,
    }


async def analyze_leave_impact(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    calendar = state.get("team_calendar", [])
    if calendar:
        impact = "; ".join(
            f"{item['employee_count']} teammate(s) are already out on {item['leave_date']} for {item['project_name']} ({item['risk_level']} risk)"
            for item in calendar
        )
    else:
        impact = "No conflicting team leave was found for the requested dates."
    return {
        "project_impact_summary": impact,
        "execution_trace": _trace(state, "Analyzed team availability and project impact."),
    }


async def manager_review(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    allowed_decisions = config.get("allowed_decisions", ["Approved", "Rejected", "Need Changes"])
    employee = state["employee_data"]
    payload = {
        "employee_id": state["employee_id"],
        "employee_name": employee["employee_name"],
        "manager_name": employee["manager_name"],
        "leave_type": state["leave_type"],
        "requested_dates": state["requested_dates"],
        "available_balance": state["available_balance"],
        "project_impact_summary": state.get("project_impact_summary", ""),
        "policy_reason": state.get("policy_reason", ""),
        "allowed_decisions": allowed_decisions,
    }
    response = interrupt(payload)
    decision = response.get("decision", "Need Changes")
    if decision not in allowed_decisions:
        decision = "Need Changes"
    result: Dict[str, Any] = {
        "manager_decision": decision,
        "reviewer_decision": decision,
        "reviewer_notes": response.get("notes", ""),
        "execution_trace": _trace(state, f"Manager decision received: {decision}."),
    }
    if response.get("requested_dates"):
        result["requested_dates"] = response["requested_dates"]
    return result


async def negotiate_leave(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    return {
        "notification_draft": "Your manager requested changes to the leave dates. Please submit revised dates.",
        "execution_trace": _trace(state, "Returned the request to the employee for date negotiation."),
    }


async def finalize_leave(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    return {
        "final_answer": f"Leave request {state['manager_decision'].lower()} for {state['employee_data']['employee_name']}.",
        "status": "APPROVED" if state["manager_decision"] == "Approved" else "REJECTED",
        "execution_trace": _trace(state, "Updated the leave request with the manager decision."),
    }


async def notify_employee(state: WorkflowState, config: Dict[str, Any], database: WorkflowDatabase) -> Dict[str, Any]:
    decision = state.get("manager_decision") or state.get("policy_status")
    if decision == "REJECTED":
        message = f"Leave request cannot be approved: {state.get('policy_reason', 'The request did not meet policy.') }"
    elif decision == "Rejected":
        message = "Your leave request was rejected by your manager."
    elif decision == "Need Changes":
        message = state.get("notification_draft", "Please submit revised leave dates.")
    else:
        message = f"Your {state['leave_type']} leave request for {', '.join(state['requested_dates'])} was approved."
    status = "REJECTED" if decision in {"REJECTED", "Rejected"} else "APPROVED"
    return {
        "notification_draft": message,
        "final_answer": state.get("final_answer", message),
        "status": status,
        "execution_trace": _trace(state, "Prepared the employee notification and completed the workflow."),
    }


HANDLERS: Dict[str, Handler] = {
    "retrieve_context": retrieve_context,
    "evaluate_request": evaluate_request,
    "human_review": human_review,
    "finalize": finalize,
    "fetch_leave_data": fetch_leave_data,
    "check_leave_policy": check_leave_policy,
    "analyze_leave_impact": analyze_leave_impact,
    "manager_review": manager_review,
    "negotiate_leave": negotiate_leave,
    "finalize_leave": finalize_leave,
    "notify_employee": notify_employee,
}


def _node_handler(handler: Handler, config: Dict[str, Any], database: WorkflowDatabase):
    async def run(state: WorkflowState) -> Dict[str, Any]:
        return await handler(state, config, database)

    return run


def _runtime_node(node_name: str):
    return START if node_name == "__start__" else END if node_name == "__end__" else node_name


def build_workflow(definition: Dict[str, Any], database: WorkflowDatabase, checkpointer: Optional[MemorySaver] = None):
    graph = StateGraph(WorkflowState)
    for node in definition["nodes"]:
        handler = HANDLERS.get(node["handler_key"])
        if handler is None:
            raise ValueError(f"Unsupported workflow handler: {node['handler_key']}")
        graph.add_node(node["node_key"], _node_handler(handler, node["config"], database))
    edges_by_source: Dict[str, List[Dict[str, Any]]] = {}
    for edge in definition["edges"]:
        edges_by_source.setdefault(edge["source_node"], []).append(edge)
    for source_name, edges in edges_by_source.items():
        conditional_edges = [edge for edge in edges if edge["condition_key"]]
        if conditional_edges:
            mapping = {edge["condition_key"]: _runtime_node(edge["target_node"]) for edge in conditional_edges}
            def route(state, source_name=source_name):
                if source_name == "check_leave_policy":
                    return "policy_pass" if state.get("policy_status") == "APPROVED_FOR_REVIEW" else "policy_reject"
                return state.get("manager_decision", state.get("reviewer_decision", "Need Changes"))
            graph.add_conditional_edges(
                _runtime_node(source_name),
                route,
                mapping,
            )
        else:
            graph.add_edge(_runtime_node(source_name), _runtime_node(edges[0]["target_node"]))
    return graph.compile(checkpointer=checkpointer)


class WorkflowAgent:
    def __init__(self, database: Optional[WorkflowDatabase] = None, tenant_id: str = "demo-tenant", workflow_key: str = "qa_review"):
        self.database = database or WorkflowDatabase()
        self.tenant_id = tenant_id
        self.workflow_key = workflow_key
        self.definition = self.database.get_definition(workflow_key, tenant_id)
        self.graph = build_workflow(self.definition, self.database, MemorySaver())

    async def run(self, request_id: str, question: str, **workflow_values: Any):
        result = await self.graph.ainvoke(
            {
                "request_id": request_id,
                "tenant_id": self.tenant_id,
                "workflow_key": self.workflow_key,
                "workflow_definition_id": self.definition["id"],
                "question": question,
                "context": self.database.get_knowledge(self.tenant_id),
                "execution_trace": [],
                **workflow_values,
            },
            {"configurable": {"thread_id": request_id}},
        )
        if "__interrupt__" in result:
            interrupt_value = result["__interrupt__"][0]
            payload = getattr(interrupt_value, "value", interrupt_value)
            result["review_payload"] = payload
            result["status"] = "WAITING_FOR_REVIEW"
        self.database.save_run(result, self.definition["id"], completed=result.get("status") == "COMPLETED")
        return result

    async def resume(self, request_id: str, decision: str, notes: str, requested_dates: Optional[List[str]] = None):
        try:
            from langgraph.types import Command
            resume_data = {"decision": decision, "notes": notes}
            if requested_dates:
                resume_data["requested_dates"] = requested_dates
            result = await self.graph.ainvoke(Command(resume=resume_data), {"configurable": {"thread_id": request_id}})
        except (ImportError, TypeError):
            result = await self.graph.ainvoke({}, {"configurable": {"thread_id": request_id}}, resume={"decision": decision, "notes": notes, "requested_dates": requested_dates})
        if "__interrupt__" in result:
            interrupt_value = result["__interrupt__"][0]
            result["review_payload"] = getattr(interrupt_value, "value", interrupt_value)
            result["status"] = "WAITING_FOR_REVIEW"
        elif self.workflow_key == "qa_review":
            result["status"] = "COMPLETED"
        self.database.save_review(request_id, decision, notes)
        self.database.save_run(result, self.definition["id"], completed=result.get("status") not in {"WAITING_FOR_REVIEW", "RUNNING"})
        return result

    def record_evaluation(self, request_id: str, evaluation: Dict[str, Any]) -> None:
        self.database.save_evaluation(request_id, evaluation)
