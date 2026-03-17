"""Exception hierarchy for the agent fleet receiver."""


class AgentFleetError(Exception):
    """Base exception for all receiver errors."""


class QueueCorruptionError(AgentFleetError):
    """Queue file could not be parsed."""


class WorkerSpawnError(AgentFleetError):
    """Failed to start a worker subprocess."""


class WorkerTimeoutError(AgentFleetError):
    """Worker exceeded its time budget."""


class WebhookAuthError(AgentFleetError):
    """Webhook signature verification failed."""


class BudgetExhaustedError(AgentFleetError):
    """Daily budget limit has been reached."""
