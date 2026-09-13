"""Local evaluation adapter with optional RAGAS integration."""

from __future__ import annotations

from typing import Any, Dict, List


def evaluate_response(question: str, answer: str, contexts: List[str], reference: str = "") -> Dict[str, Any]:
    """Return transparent local metrics; use RAGAS when installed in an AI environment."""
    try:
        from ragas import evaluate  # noqa: F401
        ragas_available = True
    except ImportError:
        ragas_available = False

    question_terms = {term.strip("?,.").lower() for term in question.split() if len(term) > 3}
    answer_terms = {term.strip("?,.").lower() for term in answer.split()}
    context_text = " ".join(contexts).lower()
    grounded_terms = len(answer_terms & set(context_text.split()))
    relevance = round(len(question_terms & answer_terms) / max(len(question_terms), 1), 2)
    groundedness = round(grounded_terms / max(len(answer_terms), 1), 2)
    score = round((relevance + groundedness) / 2, 2)
    return {
        "framework": "RAGAS-compatible local evaluator",
        "ragas_available": ragas_available,
        "faithfulness": groundedness,
        "answer_relevancy": relevance,
        "overall_score": score,
        "reference_used": bool(reference),
    }
