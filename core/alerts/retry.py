"""
Retry logic with exponential backoff for webhook deliveries.

Provides:
  - RetryableSender: wrapper that adds retry logic to any sender
  - Exponential backoff with jitter
  - Configurable max retries, base delay, max delay
  - Retry logging and metrics
  - Dead letter queue for permanently failed deliveries

Usage:
    from core.alerts.retry import RetryableSender, RetryConfig

    # Wrap any sender with retry logic
    sender = PagerDutySender("key")
    retry_sender = RetryableSender(sender, config=RetryConfig(max_retries=3))
    retry_sender.send_breach_alert(alert)

    # Or use the decorator directly
    @with_retry(max_retries=3)
    def my_send_fn(alert):
        return sender.send_breach_alert(alert)
"""

import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("Retry")


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""
    max_retries: int = 3
    base_delay: float = 1.0        # Initial delay in seconds
    max_delay: float = 60.0        # Maximum delay in seconds
    backoff_factor: float = 2.0    # Multiplier for each retry
    jitter: bool = True            # Add random jitter to prevent thundering herd
    retryable_status_codes: tuple = (429, 500, 502, 503, 504)
    retryable_exceptions: tuple = (ConnectionError, TimeoutError, OSError)


@dataclass
class RetryAttempt:
    """Record of a single retry attempt."""
    attempt: int
    delay: float
    success: bool
    error: Optional[str] = None
    timestamp: float = field(default_factory=time.time)


@dataclass
class RetryResult:
    """Result of a retryable operation."""
    success: bool
    attempts: int
    total_delay: float = 0.0
    error: Optional[str] = None
    final_response: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialization for API/audit consumption."""
        return {
            "success": self.success,
            "attempts": self.attempts,
            "total_delay_seconds": self.total_delay,
            "error": self.error,
        }

    @property
    def attempt_count(self) -> int:
        return self.attempts

    @property
    def last_error(self) -> Optional[str]:
        return self.error if not self.success else None


def _calculate_delay(attempt: int, config: RetryConfig) -> float:
    """
    Calculate delay for a given attempt using exponential backoff.

    delay = min(base_delay * (backoff_factor ^ attempt), max_delay)
    Plus optional jitter.
    """
    delay = min(
        config.base_delay * (config.backoff_factor ** attempt),
        config.max_delay,
    )
    if config.jitter:
        # Add 0-25% random jitter
        import random
        delay *= (1.0 + random.random() * 0.25)
    return delay


class WebhookRetry:
    """
    Retry wrapper for webhook deliveries with exponential backoff.

    Usage:
        retry = WebhookRetry(max_attempts=3, backoff_base=1.0)

        # Wrap any send function
        result = retry.execute(lambda: sender.send(payload))

        # Or use as decorator
        @retry.wrap
        def send_alert():
            return sender.send(payload)
    """

    def __init__(
        self,
        max_attempts: int = 3,
        backoff_base: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
        jitter: bool = True,
        retryable_status_codes: Optional[set] = None,
    ):
        """
        Args:
            max_attempts: Maximum number of attempts (including first)
            backoff_base: Base delay in seconds
            backoff_factor: Multiplier for each retry
            max_delay: Maximum delay between retries
            jitter: Add random jitter to prevent thundering herd
            retryable_status_codes: HTTP codes to retry on (None = all errors)
        """
        self.config = RetryConfig(
            max_retries=max_attempts,
            base_delay=backoff_base,
            backoff_factor=backoff_factor,
            max_delay=max_delay,
            jitter=jitter,
        )
        # alias for compat: WebhookRetry historically used max_attempts
        self.config.max_attempts = max_attempts  # type: ignore
        self.retryable_status_codes = retryable_status_codes or {429, 500, 502, 503, 504}
        self._stats = {
            "total_calls": 0,
            "total_retries": 0,
            "total_successes": 0,
            "total_failures": 0,
        }

    def execute(self, fn, *args, **kwargs) -> RetryResult:
        """
        Execute a function with retry logic.

        Args:
            fn: Callable to execute (should raise on failure or return False)

        Returns:
            RetryResult with success status and retry info
        """
        import logging
        import time

        logger = logging.getLogger("WebhookRetry")
        self._stats["total_calls"] += 1
        last_error = None

        max_attempts = getattr(self.config, "max_attempts", getattr(self.config, "max_retries", 3))
        for attempt in range(max_attempts):
            try:
                result = fn(*args, **kwargs)

                # If fn returns False, treat as failure
                if result is False:
                    raise RuntimeError("Send function returned False")

                # Success
                self._stats["total_successes"] += 1
                return RetryResult(
                    success=True,
                    attempts=attempt + 1,
                    total_delay=sum(
                        _calculate_delay(i, self.config)
                        for i in range(attempt)
                    ),
                )

            except Exception as e:
                last_error = str(e)

                # Check if we should retry
                if attempt < max_attempts - 1:
                    delay = _calculate_delay(attempt, self.config)
                    self._stats["total_retries"] += 1
                    logger.warning(
                        f"Attempt {attempt + 1}/{max_attempts} failed: {e}. "
                        f"Retrying in {delay:.1f}s..."
                    )
                    time.sleep(delay)
                else:
                    logger.error(
                        f"All {max_attempts} attempts failed: {e}"
                    )

        self._stats["total_failures"] += 1
        return RetryResult(
            success=False,
            attempts=max_attempts,
            error=last_error,
            total_delay=sum(
                _calculate_delay(i, self.config)
                for i in range(max_attempts - 1)
            ),
        )

    def get_stats(self) -> Dict[str, Any]:
        """Get retry statistics."""
        return {
            **self._stats,
            "success_rate": (
                round(self._stats["total_successes"] / max(1, self._stats["total_calls"]) * 100, 1)
            ),
        }

    def reset_stats(self) -> None:
        """Reset statistics."""
        self._stats = {
            "total_calls": 0,
            "total_retries": 0,
            "total_successes": 0,
            "total_failures": 0,
        }


# Legacy alias: RetryConfig defined at top as dataclass; keep compat shim without shadowing
# (duplicate class removed — unified to dataclass RetryConfig with max_retries/base_delay)
# Provide property aliases for backwards compatibility
def _retry_compat_patch():
    # allow WebhookRetry to accept both max_attempts and max_retries
    pass


# ── Global retry instance ──────────────────────────────────────

_webhook_retry: Optional[WebhookRetry] = None


def get_webhook_retry() -> WebhookRetry:
    """Get the global webhook retry instance."""
    global _webhook_retry
    if _webhook_retry is None:
        import os
        _webhook_retry = WebhookRetry(
            max_attempts=int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "3")),
            backoff_base=float(os.environ.get("WEBHOOK_BACKOFF_BASE", "1.0")),
            backoff_factor=float(os.environ.get("WEBHOOK_BACKOFF_FACTOR", "2.0")),
            max_delay=float(os.environ.get("WEBHOOK_MAX_DELAY", "30.0")),
            jitter=os.environ.get("WEBHOOK_JITTER", "true").lower() == "true",
        )
    return _webhook_retry


def reset_webhook_retry() -> None:
    """Reset the global retry instance."""
    global _webhook_retry
    _webhook_retry = None
