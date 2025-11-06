"""
Metrics Recording Utility

This module provides functionality for recording workflow metrics to the backend API.
Metrics include execution time, success rates, LLM usage, and cost estimates.

Functions:
    - record_workflow_metrics: Record workflow metrics to the API endpoint
"""

import logging
import requests
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def record_workflow_metrics(
    workflow_id: str,
    url: str,
    results: Dict[str, Any],
    session_id: Optional[str] = None,
    backend_port: int = 8000
) -> None:
    """
    Record workflow metrics to the API endpoint for persistence.

    This function extracts metrics from workflow results and sends them to the backend
    API for storage and analysis. It includes error handling to ensure that metrics
    recording failures do not break the workflow.

    Args:
        workflow_id: Unique workflow/task identifier
        url: Target URL that was automated
        results: Workflow results containing summary and metrics
        session_id: Optional browser session ID
        backend_port: Port number for the backend API (default: 8000)

    Example:
        >>> results = {
        ...     'summary': {
        ...         'total_elements': 5,
        ...         'successful': 4,
        ...         'failed': 1,
        ...         'success_rate': 0.8
        ...     },
        ...     'execution_time': 45.2
        ... }
        >>> record_workflow_metrics('task-123', 'https://example.com', results)
    """
    try:
        summary = results.get('summary', {})
        execution_time = results.get('execution_time', 0)

        # Extract metrics from summary
        total_elements = summary.get('total_elements', 0)
        successful_elements = summary.get('successful', 0)
        failed_elements = summary.get('failed', 0)
        success_rate = summary.get('success_rate', 0.0)
        total_llm_calls = summary.get('total_llm_calls', 0)
        avg_llm_calls_per_element = summary.get('avg_llm_calls_per_element', 0.0)
        total_cost = summary.get('estimated_total_cost', 0.0)
        avg_cost_per_element = summary.get('estimated_cost_per_element', 0.0)
        custom_actions_enabled = summary.get('custom_actions_enabled', False)

        # Count custom action usage from results
        custom_action_usage_count = 0
        if 'results' in results:
            for elem_result in results['results']:
                if elem_result.get('metrics', {}).get('custom_action_used', False):
                    custom_action_usage_count += 1

        # Prepare metrics payload
        metrics_payload = {
            "workflow_id": workflow_id,
            "total_elements": total_elements,
            "successful_elements": successful_elements,
            "failed_elements": failed_elements,
            "success_rate": success_rate,
            "total_llm_calls": total_llm_calls,
            "avg_llm_calls_per_element": avg_llm_calls_per_element,
            "total_cost": total_cost,
            "avg_cost_per_element": avg_cost_per_element,
            "custom_actions_enabled": custom_actions_enabled,
            "custom_action_usage_count": custom_action_usage_count,
            "execution_time": execution_time,
            "url": url,
            "session_id": session_id
        }

        # Get the backend API URL
        backend_url = f"http://localhost:{backend_port}"
        metrics_endpoint = f"{backend_url}/api/workflow-metrics/record"

        logger.info(f"📊 Recording workflow metrics to {metrics_endpoint}")
        logger.debug(f"   Metrics payload: {metrics_payload}")

        # Send POST request to record metrics
        response = requests.post(
            metrics_endpoint,
            json=metrics_payload,
            timeout=5,
            headers={'Content-Type': 'application/json'}
        )

        if response.status_code == 200:
            logger.info(f"✅ Workflow metrics recorded successfully for workflow {workflow_id}")
        else:
            logger.warning(f"⚠️ Failed to record metrics: HTTP {response.status_code} - {response.text}")

    except Exception as e:
        logger.error(f"❌ Error recording workflow metrics: {e}", exc_info=True)
        # Don't raise - metrics recording should not break the workflow
