"""Executes remediation actions returned by the Qwen client."""
import logging

from collector import MetricsCollector

logger = logging.getLogger(__name__)

VALID_ACTIONS = {"alert", "restart", "halt"}


class ActionExecutor:
    def __init__(self, collector: MetricsCollector):
        self._collector = collector
        self._halted = False

    @property
    def halted(self) -> bool:
        return self._halted

    def execute(self, action: str, diagnosis: str) -> str:
        """Returns a human-readable outcome string."""
        action = action.lower().strip()
        if action not in VALID_ACTIONS:
            logger.warning("Unknown action %r, defaulting to alert", action)
            action = "alert"

        if action == "alert":
            msg = f"ALERT: {diagnosis}"
            logger.warning(msg)
            return "alerted"

        if action == "restart":
            logger.warning("ACTION restart — calling POST /reset on demo service")
            success = self._collector.reset_service()
            if success:
                logger.info("Demo service restarted (fault cleared)")
                return "restarted"
            else:
                logger.error("Restart failed — escalating to alert")
                return "restart_failed"

        if action == "halt":
            logger.critical("ACTION halt — agent entering halted state")
            self._halted = True
            return "halted"

        return "unknown"
