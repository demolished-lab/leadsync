"""
Automated SLA breach checker with multi-channel alerting.

Runs as a background task, periodically checks the queue for
SLA-breached prospects, and sends alerts via:
  - Telegram (via Bot API)
  - Email (via SMTP)

Architecture:
    SLABreachChecker (background loop)
        → get_queue().get_breached()
        → AlertManager.send_alert()
            → TelegramSender / EmailSender
        → Audit log entry

No external dependencies — uses httpx for Telegram and stdlib smtplib for email.
"""

import asyncio
import logging
import os
import smtplib
import threading
import time
from datetime import datetime
from email.mime.text import MIMEText
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger("Alerts")


# ── Alert Content Builder ──────────────────────────────────────
def build_breach_alert(prospect) -> Dict[str, str]:
    """
    Build alert content for a breached prospect.

    Returns dict with subject, body, and summary fields.
    """
    conv = prospect.conversation
    name = "Unknown"
    email_addr = ""
    if conv and conv.participants:
        name = conv.participants[0].get("name", "Unknown")
        email_addr = conv.participants[0].get("email", "")

    priority = prospect.priority_score or 0
    urgency = getattr(conv, "urgency", "unknown") if conv else "unknown"
    deal = getattr(conv, "deal_size", None) if conv else None
    requeues = prospect.times_requeued or 0
    deadline = prospect.sla_deadline.strftime("%Y-%m-%d %H:%M") if prospect.sla_deadline else "unknown"

    subject = f"🔴 SLA BREACH: {name} (priority {priority:.2f})"

    body_lines = [
        f"SLA BREACH ALERT",
        f"",
        f"Prospect: {name}",
        f"Email: {email_addr}",
        f"Priority Score: {priority:.2f}",
        f"Urgency: {urgency.upper()}",
        f"SLA Deadline: {deadline}",
        f"Times Requeued: {requeues}",
        f"Conversation ID: {prospect.conversation_id}",
    ]
    if deal:
        body_lines.append(f"Deal Value: ${deal:,.0f}")
    if conv and conv.commitments:
        body_lines.append(f"Commitments: {', '.join(conv.commitments[:3])}")
    if requeues >= 3:
        body_lines.append(f"")
        body_lines.append(f"⚠️ ESCALATION: This prospect has been requeued {requeues} times!")
        body_lines.append(f"Consider escalating to a manager.")

    body = "\n".join(body_lines)

    summary = f"{name} | Score: {priority:.2f} | {urgency} | Requeued: {requeues}x"

    return {
        "subject": subject,
        "body": body,
        "summary": summary,
        "name": name,
        "email": email_addr,
        "priority": priority,
        "urgency": urgency,
        "requeues": requeues,
    }


# ── Telegram Sender ────────────────────────────────────────────
class TelegramSender:
    """Send alerts via Telegram Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._api_base = f"https://api.telegram.org/bot{bot_token}"

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message to the configured Telegram chat."""
        try:
            import httpx
            resp = httpx.post(
                f"{self._api_base}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": parse_mode,
                    "disable_web_page_preview": True,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return True
            logger.warning(f"Telegram send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send Telegram alerts")
            return False
        except Exception as e:
            logger.error(f"Telegram send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Send a formatted SLA breach alert to Telegram."""
        html = (
            f"🔴 <b>SLA BREACH</b>\n\n"
            f"<b>Prospect:</b> {alert['name']}\n"
            f"<b>Priority:</b> {alert['priority']:.2f}\n"
            f"<b>Urgency:</b> {alert['urgency'].upper()}\n"
            f"<b>Requeued:</b> {alert['requeues']}x\n"
        )
        if alert.get("email"):
            html += f"<b>Email:</b> {alert['email']}\n"
        if alert["requeues"] >= 3:
            html += f"\n⚠️ <b>ESCALATION REQUIRED</b> — {alert['requeues']} requeues"

        return self.send(html)


# ── Email Sender ───────────────────────────────────────────────
class EmailSender:
    """Send alerts via SMTP email."""

    def __init__(
        self,
        to_address: str,
        smtp_host: str = "smtp.gmail.com",
        smtp_port: int = 587,
        smtp_username: Optional[str] = None,
        smtp_password: Optional[str] = None,
        from_address: Optional[str] = None,
    ):
        self.to_address = to_address
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_username = smtp_username
        self.smtp_password = smtp_password
        self.from_address = from_address or smtp_username or "alerts@sfa.local"

    def send(self, subject: str, body: str) -> bool:
        """Send an email alert."""
        if not self.smtp_username or not self.smtp_password:
            logger.warning("SMTP credentials not configured — cannot send email alerts")
            return False

        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = self.from_address
            msg["To"] = self.to_address
            msg["Subject"] = subject
            msg["X-Mailer"] = "SFA-SLA-Checker/1.0"

            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as server:
                server.ehlo()
                if self.smtp_port == 587:
                    server.starttls()
                    server.ehlo()
                server.login(self.smtp_username, self.smtp_password)
                server.sendmail(self.from_address, [self.to_address], msg.as_string())

            return True
        except Exception as e:
            logger.error(f"Email alert send failed: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Send a formatted SLA breach email."""
        return self.send(alert["subject"], alert["body"])


# ── Slack Sender ──────────────────────────────────────────────
class SlackSender:
    """Send alerts via Slack Incoming Webhook."""

    def __init__(self, webhook_url: str, channel: Optional[str] = None):
        self.webhook_url = webhook_url
        self.channel = channel

    def send(self, payload: Dict[str, Any]) -> bool:
        """Send a payload to Slack via webhook."""
        try:
            import httpx
            if self.channel:
                payload["channel"] = self.channel
            resp = httpx.post(self.webhook_url, json=payload, timeout=10)
            if resp.status_code == 200 and resp.text == "ok":
                return True
            logger.warning(f"Slack send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send Slack alerts")
            return False
        except Exception as e:
            logger.error(f"Slack send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Send a formatted SLA breach alert to Slack."""
        urgency_color = {
            "high": "#FF0000",
            "medium": "#FFA500",
            "low": "#FFD700",
        }.get(alert["urgency"].lower(), "#808080")

        fields = [
            {"title": "Priority", "value": f"{alert['priority']:.2f}", "short": True},
            {"title": "Urgency", "value": alert["urgency"].upper(), "short": True},
            {"title": "Requeued", "value": f"{alert['requeues']}x", "short": True},
        ]
        if alert.get("email"):
            fields.append({"title": "Email", "value": alert["email"], "short": True})

        attachment = {
            "color": urgency_color,
            "title": f"🔴 SLA BREACH: {alert['name']}",
            "fields": fields,
            "footer": "SFA SLA Monitor",
            "ts": int(time.time()),
        }

        if alert["requeues"] >= 3:
            attachment["fields"].append({
                "title": "⚠️ ESCALATION",
                "value": f"Requeued {alert['requeues']} times — consider manager review",
                "short": False,
            })

        payload = {
            "text": f"SLA breach for {alert['name']} (priority {alert['priority']:.2f})",
            "attachments": [attachment],
        }
        return self.send(payload)


# ── Discord Sender ─────────────────────────────────────────────
class DiscordSender:
    """Send alerts via Discord Webhook."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, embed: Dict[str, Any]) -> bool:
        """Send an embed to Discord via webhook."""
        try:
            import httpx
            resp = httpx.post(
                self.webhook_url,
                json={"embeds": [embed]},
                timeout=10,
            )
            if resp.status_code in (200, 204):
                return True
            logger.warning(f"Discord send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send Discord alerts")
            return False
        except Exception as e:
            logger.error(f"Discord send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Send a formatted SLA breach alert to Discord."""
        urgency_color = {
            "high": 0xFF0000,
            "medium": 0xFFA500,
            "low": 0xFFD700,
        }.get(alert["urgency"].lower(), 0x808080)

        fields = [
            {"name": "Priority", "value": f"{alert['priority']:.2f}", "inline": True},
            {"name": "Urgency", "value": alert["urgency"].upper(), "inline": True},
            {"name": "Requeued", "value": f"{alert['requeues']}x", "inline": True},
        ]
        if alert.get("email"):
            fields.append({"name": "Email", "value": alert["email"], "inline": True})

        if alert["requeues"] >= 3:
            fields.append({
                "name": "⚠️ ESCALATION",
                "value": f"Requeued {alert['requeues']} times — consider manager review",
                "inline": False,
            })

        embed = {
            "title": f"🔴 SLA BREACH: {alert['name']}",
            "description": alert["summary"],
            "color": urgency_color,
            "fields": fields,
            "footer": {"text": "SFA SLA Monitor"},
        }
        return self.send(embed)


# ── Microsoft Teams Sender ─────────────────────────────────────
class TeamsSender:
    """Send alerts via Microsoft Teams Incoming Webhook (Office 365 Connector)."""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url

    def send(self, card: Dict[str, Any]) -> bool:
        """Send an Adaptive Card to Teams via webhook."""
        try:
            import httpx
            resp = httpx.post(self.webhook_url, json=card, timeout=10)
            if resp.status_code == 200:
                return True
            logger.warning(f"Teams send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send Teams alerts")
            return False
        except Exception as e:
            logger.error(f"Teams send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Send a formatted SLA breach alert to Teams."""
        theme_color = {
            "high": "FF0000",
            "medium": "FFA500",
            "low": "FFD700",
        }.get(alert["urgency"].lower(), "808080")

        facts = [
            {"name": "Priority Score", "value": f"{alert['priority']:.2f}"},
            {"name": "Urgency", "value": alert["urgency"].upper()},
            {"name": "Times Requeued", "value": str(alert["requeues"])},
        ]
        if alert.get("email"):
            facts.append({"name": "Email", "value": alert["email"]})

        sections = [{
            "activityTitle": f"🔴 SLA BREACH: {alert['name']}",
            "facts": facts,
            "markdown": True,
        }]

        if alert["requeues"] >= 3:
            sections.append({
                "activityTitle": "⚠️ ESCALATION REQUIRED",
                "text": f"This prospect has been requeued **{alert['requeues']} times**. Consider escalating to a manager.",
                "markdown": True,
            })

        card = {
            "@type": "MessageCard",
            "themeColor": theme_color,
            "summary": f"SLA Breach: {alert['name']}",
            "sections": sections,
        }
        return self.send(card)


# ── PagerDuty Sender ──────────────────────────────────────────
class PagerDutySender:
    """Send alerts via PagerDuty Events API v2.

    Requires a PagerDuty Integration Key (from Events API v2).
    Creates incidents for high-priority breaches and resolves them
    when the breach is handled.
    """

    # PagerDuty severity mapping
    SEVERITY_MAP = {
        "high": "critical",
        "medium": "warning",
        "low": "info",
    }

    def __init__(self, integration_key: str, from_email: Optional[str] = None):
        """
        Args:
            integration_key: PagerDuty Events API v2 integration key
            from_email: Email of the user triggering the event (optional)
        """
        self.integration_key = integration_key
        self.from_email = from_email or "sfa-alerts@sfa.local"
        self._api_url = "https://events.pagerduty.com/v2/enqueue"

    def send(self, payload: Dict[str, Any]) -> bool:
        """Send an event to PagerDuty."""
        try:
            import httpx
            resp = httpx.post(self._api_url, json=payload, timeout=15)
            if resp.status_code in (200, 202):
                return True
            logger.warning(f"PagerDuty send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send PagerDuty alerts")
            return False
        except Exception as e:
            logger.error(f"PagerDuty send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Create a PagerDuty incident for an SLA breach."""
        severity = self.SEVERITY_MAP.get(alert["urgency"].lower(), "warning")

        payload = {
            "routing_key": self.integration_key,
            "event_action": "trigger",
            "dedup_key": f"sfa-sla-{alert.get('email', 'unknown')}-{alert['priority']:.2f}",
            "payload": {
                "summary": f"SLA Breach: {alert['name']} (priority {alert['priority']:.2f})",
                "source": "Sales Follow-Up Agent",
                "severity": severity,
                "component": "sla-monitor",
                "group": "sales-followup",
                "class": "sla_breach",
                "custom_details": {
                    "prospect_name": alert["name"],
                    "email": alert.get("email", ""),
                    "priority_score": alert["priority"],
                    "urgency": alert["urgency"],
                    "requeues": alert["requeues"],
                    "body": alert["body"],
                },
            },
            "links": [
                {
                    "href": "https://your-domain.com/dashboard",
                    "text": "Sales Follow-Up Dashboard",
                }
            ],
            "images": [],
        }

        if self.from_email:
            payload["payload"]["source"] = f"Sales Follow-Up Agent ({self.from_email})"

        return self.send(payload)

    def resolve(self, dedup_key: str) -> bool:
        """Resolve a PagerDuty incident."""
        payload = {
            "routing_key": self.integration_key,
            "event_action": "resolve",
            "dedup_key": dedup_key,
        }
        return self.send(payload)


# ── Opsgenie Sender ───────────────────────────────────────────
class OpsgenieSender:
    """Send alerts via Opsgenie Alert API.

    Requires an Opsgenie API Integration key.
    Creates alerts for SLA breaches with proper routing.
    """

    # Opsgenie priority mapping
    PRIORITY_MAP = {
        "high": "P1",
        "medium": "P2",
        "low": "P3",
    }

    def __init__(self, api_key: str, team: Optional[str] = None, priority: str = "P2"):
        """
        Args:
            api_key: Opsgenie API Integration key
            team: Team name for routing (optional)
            priority: Default priority (P1-P5)
        """
        self.api_key = api_key
        self.team = team
        self.default_priority = priority
        self._api_url = "https://api.opsgenie.com/v2/alerts"

    def send(self, payload: Dict[str, Any]) -> bool:
        """Send an alert to Opsgenie."""
        try:
            import httpx
            resp = httpx.post(
                self._api_url,
                json=payload,
                headers={
                    "Authorization": f"GenieKey {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            if resp.status_code in (200, 201, 202):
                return True
            logger.warning(f"Opsgenie send failed ({resp.status_code}): {resp.text[:200]}")
            return False
        except ImportError:
            logger.warning("httpx not installed — cannot send Opsgenie alerts")
            return False
        except Exception as e:
            logger.error(f"Opsgenie send error: {e}")
            return False

    def send_breach_alert(self, alert: Dict[str, str]) -> bool:
        """Create an Opsgenie alert for an SLA breach."""
        priority = self.PRIORITY_MAP.get(alert["urgency"].lower(), self.default_priority)

        payload = {
            "message": f"SLA Breach: {alert['name']} (priority {alert['priority']:.2f})",
            "alias": f"sfa-sla-{alert.get('email', 'unknown')}-{alert['priority']:.2f}",
            "description": alert["body"],
            "priority": priority,
            "source": "Sales Follow-Up Agent",
            "tags": [
                "sla-breach",
                f"urgency:{alert['urgency']}",
                f"priority:{alert['priority']:.2f}",
            ],
            "details": {
                "prospect_name": alert["name"],
                "email": alert.get("email", ""),
                "priority_score": str(alert["priority"]),
                "urgency": alert["urgency"],
                "requeues": str(alert["requeues"]),
            },
            "entity": {
                "type": "sla-monitor",
                "name": "Sales Follow-Up Agent",
            },
        }

        if self.team:
            payload["responders"] = [{"name": self.team, "type": "team"}]

        return self.send(payload)

    def close_alert(self, alias: str) -> bool:
        """Close an Opsgenie alert."""
        try:
            import httpx
            close_url = f"{self._api_url}/{alias}/close"
            resp = httpx.post(
                close_url,
                headers={
                    "Authorization": f"GenieKey {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"note": "SLA breach resolved"},
                timeout=15,
            )
            return resp.status_code in (200, 202)
        except Exception as e:
            logger.error(f"Opsgenie close error: {e}")
            return False


# ── Alert Manager ──────────────────────────────────────────────
class AlertManager:
    """
    Manages multiple alert channels and deduplication.

    Prevents spamming the same breach alert repeatedly.
    Tracks which prospects have been alerted and for how long.
    """

    def __init__(self, cooldown_seconds: int = 3600, enable_retry: bool = True):
        """
        Args:
            cooldown_seconds: Don't re-alert for the same prospect within this window
            enable_retry: Enable exponential backoff retry for failed deliveries
        """
        self._channels: List[Callable] = []
        self._alerted: Dict[str, float] = {}  # conversation_id → last_alert_time
        self._cooldown = cooldown_seconds
        self._enable_retry = enable_retry
        self._total_sent = 0
        self._total_failed = 0
        self._total_retries = 0

        # Retry config
        self._retry = None
        if enable_retry:
            from .retry import WebhookRetry, _calculate_delay as _calc_delay
            self._calc_delay = _calc_delay
            self._retry = WebhookRetry(
                max_attempts=int(os.environ.get("WEBHOOK_MAX_ATTEMPTS", "3")),
                backoff_base=float(os.environ.get("WEBHOOK_BACKOFF_BASE", "1.0")),
                backoff_factor=float(os.environ.get("WEBHOOK_BACKOFF_FACTOR", "2.0")),
                max_delay=float(os.environ.get("WEBHOOK_MAX_DELAY", "30.0")),
            )

    def add_channel(self, send_fn: Callable) -> None:
        """Register an alert channel (e.g. telegram.send_breach_alert)."""
        self._channels.append(send_fn)

    def _send_with_retry(self, channel: Callable, alert: Dict) -> Dict[str, Any]:
        """Send through a channel with optional retry logic."""
        if not self._retry:
            # No retry — direct call
            try:
                start = time.time()
                success = channel(alert)
                latency_ms = round((time.time() - start) * 1000)
                return {
                    "success": success,
                    "attempts": 1,
                    "latency_ms": latency_ms,
                }
            except Exception as e:
                return {
                    "success": False,
                    "attempts": 1,
                    "error": str(e),
                }

        # With retry
        start = time.time()
        last_error = None
        attempts = 0

        max_attempts = getattr(self._retry.config, "max_attempts", getattr(self._retry.config, "max_retries", 3))
        for attempt in range(max_attempts):
            attempts = attempt + 1
            try:
                result = channel(alert)
                if result is True:
                    latency_ms = round((time.time() - start) * 1000)
                    if attempt > 0:
                        self._total_retries += attempt
                        logger.info(
                            f"Channel {channel.__name__} succeeded after {attempts} attempts"
                        )
                    return {
                        "success": True,
                        "attempts": attempts,
                        "latency_ms": latency_ms,
                    }
                elif result is False:
                    last_error = "Channel returned False"
                else:
                    # Truthy non-bool return (e.g., dict response)
                    latency_ms = round((time.time() - start) * 1000)
                    return {
                        "success": True,
                        "attempts": attempts,
                        "latency_ms": latency_ms,
                        "response": result,
                    }
            except Exception as e:
                last_error = str(e)

            # Wait before retry (skip on last attempt)
            if attempt < max_attempts - 1:
                delay = self._calc_delay(attempt, self._retry.config)
                logger.warning(
                    f"Channel {channel.__name__} attempt {attempts} failed: {last_error}. "
                    f"Retrying in {delay:.1f}s..."
                )
                time.sleep(delay)

        # All attempts failed
        latency_ms = round((time.time() - start) * 1000)
        self._total_retries += max(0, attempts - 1)
        return {
            "success": False,
            "attempts": attempts,
            "error": last_error,
            "latency_ms": latency_ms,
        }

    def send_alert(self, alert: Dict[str, str], prospect_id: str) -> Dict[str, Any]:
        """
        Send alert through all channels. Respects cooldown.
        Uses exponential backoff retry for failed deliveries.

        Returns:
            Dict with sent/failed counts and per-channel results
        """
        # Check cooldown
        now = time.time()
        last_alert = self._alerted.get(prospect_id, 0)
        if (now - last_alert) < self._cooldown:
            return {
                "status": "skipped",
                "reason": f"Cooldown active ({self._cooldown - (now - last_alert):.0f}s remaining)",
            }

        results = []
        sent = 0
        failed = 0

        for channel in self._channels:
            channel_result = self._send_with_retry(channel, alert)

            if channel_result["success"]:
                sent += 1
                self._total_sent += 1
            else:
                failed += 1
                self._total_failed += 1

            results.append({
                "channel": channel.__name__,
                **channel_result,
            })

        if sent > 0:
            self._alerted[prospect_id] = now

        return {
            "status": "sent" if sent > 0 else "failed",
            "sent": sent,
            "failed": failed,
            "results": results,
        }

    def get_stats(self) -> Dict[str, Any]:
        stats = {
            "channels": len(self._channels),
            "total_sent": self._total_sent,
            "total_failed": self._total_failed,
            "total_retries": self._total_retries,
            "retry_enabled": self._enable_retry,
            "active_alerts": len(self._alerted),
        }
        if self._retry:
            stats["retry_stats"] = self._retry.get_stats()
        return stats


# ── SLA Breach Checker ────────────────────────────────────────
class SLABreachChecker:
    """
    Background task that periodically checks for SLA breaches.

    Runs in a daemon thread. Checks the queue every `check_interval`
    seconds, finds breached prospects, and sends alerts.

    Usage:
        checker = SLABreachChecker(check_interval=60)
        checker.start()
        # ... later ...
        checker.stop()
    """

    def __init__(
        self,
        check_interval: int = 60,
        alert_manager: Optional[AlertManager] = None,
    ):
        self.check_interval = check_interval
        self.alert_manager = alert_manager or AlertManager()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False
        self._last_check: Optional[datetime] = None
        self._checks_done = 0
        self._breaches_found = 0

    def start(self) -> None:
        """Start the background checker thread."""
        if self._running:
            logger.warning("SLA checker already running")
            return

        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            name="sla-checker",
            daemon=True,
        )
        self._thread.start()
        logger.info(f"SLA breach checker started (interval: {self.check_interval}s)")

    def stop(self) -> None:
        """Stop the background checker thread."""
        self._stop_event.set()
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("SLA breach checker stopped")

    def check_now(self) -> Dict[str, Any]:
        """
        Run a single check immediately (synchronous).

        Returns:
            Dict with check results
        """
        from ..queue import get_queue
        from ..realtime import emit_queue_event

        queue = get_queue()
        breached = queue.get_breached()

        results = []
        for prospect in breached:
            alert = build_breach_alert(prospect)
            send_result = self.alert_manager.send_alert(alert, prospect.conversation_id)
            results.append({
                "conversation_id": prospect.conversation_id,
                "name": alert["name"],
                "priority": alert["priority"],
                "alert_result": send_result,
            })

            # Emit real-time event
            emit_queue_event(
                "sla:breach_alert",
                conversation_id=prospect.conversation_id,
                priority_score=prospect.priority_score,
                alert_status=send_result.get("status"),
            )

        self._last_check = datetime.utcnow()
        self._checks_done += 1
        self._breaches_found += len(breached)

        return {
            "checked_at": self._last_check.isoformat(),
            "breaches_found": len(breached),
            "alerts_sent": sum(1 for r in results if r["alert_result"].get("status") == "sent"),
            "details": results,
        }

    def _run_loop(self) -> None:
        """Background loop that runs check_now periodically."""
        while not self._stop_event.is_set():
            try:
                self.check_now()
            except Exception as e:
                logger.error(f"SLA check error: {e}")
            self._stop_event.wait(timeout=self.check_interval)

    def get_status(self) -> Dict[str, Any]:
        """Get checker status."""
        return {
            "running": self._running,
            "check_interval": self.check_interval,
            "last_check": self._last_check.isoformat() if self._last_check else None,
            "checks_done": self._checks_done,
            "breaches_found_total": self._breaches_found,
            "alert_manager": self.alert_manager.get_stats(),
        }


def _is_air_gapped() -> bool:
    try:
        from ..config import get_settings
        return bool(getattr(get_settings(), "air_gapped", False))
    except Exception:
        return os.environ.get("AIR_GAPPED", "false").lower() == "true"

# ── Factory ────────────────────────────────────────────────────
def create_sla_checker() -> SLABreachChecker:
    """
    Create an SLA breach checker configured from settings.

    Reads TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ALERT_EMAIL,
    and SLA_CHECK_INTERVAL from environment.
    In AIR_GAPPED mode, only console alerts are registered.
    """
    import os

    check_interval = int(os.environ.get("SLA_CHECK_INTERVAL", "60"))
    alert_manager = AlertManager(
        cooldown_seconds=int(os.environ.get("ALERT_COOLDOWN_SECONDS", "3600"))
    )

    if _is_air_gapped():
        logger.info("AIR_GAPPED mode — external alert channels disabled (console only)")
    else:
        # Telegram channel
        telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        telegram_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        if telegram_token and telegram_chat:
            sender = TelegramSender(telegram_token, telegram_chat)
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info("Telegram alerts enabled")

    # Email channel
    alert_email = os.environ.get("ALERT_EMAIL", "")
    if alert_email:
        from ..config import get_settings
        settings = get_settings()
        sender = EmailSender(
            to_address=alert_email,
            smtp_host=settings.smtp_host,
            smtp_port=settings.smtp_port,
            smtp_username=settings.smtp_username,
            smtp_password=settings.smtp_password,
        )
        alert_manager.add_channel(sender.send_breach_alert)
        logger.info(f"Email alerts enabled → {alert_email}")

    if not _is_air_gapped():
        # Slack channel
        slack_webhook = os.environ.get("SLACK_WEBHOOK_URL", "")
        if slack_webhook:
            slack_channel = os.environ.get("SLACK_CHANNEL", None)
            sender = SlackSender(slack_webhook, channel=slack_channel)
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info(f"Slack alerts enabled → {slack_channel or 'default channel'}")

        # Discord channel
        discord_webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if discord_webhook:
            sender = DiscordSender(discord_webhook)
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info("Discord alerts enabled")

        # Microsoft Teams channel
        teams_webhook = os.environ.get("TEAMS_WEBHOOK_URL", "")
        if teams_webhook:
            sender = TeamsSender(teams_webhook)
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info("Teams alerts enabled")

        # PagerDuty channel
        pagerduty_key = os.environ.get("PAGERDUTY_INTEGRATION_KEY", "")
        if pagerduty_key:
            sender = PagerDutySender(pagerduty_key, from_email=os.environ.get("ALERT_EMAIL"))
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info("PagerDuty alerts enabled")

        # Opsgenie channel
        opsgenie_key = os.environ.get("OPSGENIE_API_KEY", "")
        if opsgenie_key:
            sender = OpsgenieSender(
                opsgenie_key,
                team=os.environ.get("OPSGENIE_TEAM"),
                priority=os.environ.get("OPSGENIE_PRIORITY", "P2"),
            )
            alert_manager.add_channel(sender.send_breach_alert)
            logger.info("Opsgenie alerts enabled")

    # Console channel (always enabled for visibility)
    def console_alert(alert: Dict[str, str]) -> bool:
        logger.warning(f"SLA BREACH: {alert['summary']}")
        return True

    alert_manager.add_channel(console_alert)

    # Wire the Webhook Payload Inspector: every outbound payload sent through
    # these channels is now captured for the inspector UI/API.
    # Behind a settings flag, default ON.
    try:
        from ..config import get_settings
        enabled = bool(getattr(get_settings(), "inspector_enabled", True))
    except Exception:
        enabled = True

    if enabled:
        try:
            from .inspector import wrap_alert_channels
            wrap_alert_channels(alert_manager)
            logger.info("Webhook payload inspector wired to alert channels")
        except Exception as e:
            logger.warning(f"Failed to wire webhook inspector to alert channels: {e}")

    return SLABreachChecker(check_interval=check_interval, alert_manager=alert_manager)


# Global singleton
_sla_checker: Optional[SLABreachChecker] = None


def get_sla_checker() -> SLABreachChecker:
    """Get or create the global SLA checker singleton."""
    global _sla_checker
    if _sla_checker is None:
        _sla_checker = create_sla_checker()
    return _sla_checker
