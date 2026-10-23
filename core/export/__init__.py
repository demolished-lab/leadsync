"""
Export module for compliance reporting.

Generates CSV and PDF reports from conversation history,
follow-up drafts, and audit trail.

Usage:
    from core.export import ExportManager

    exporter = ExportManager()

    # CSV export
    csv_path = exporter.export_conversations_csv("report.csv", source="email")

    # PDF export
    pdf_path = exporter.export_conversations_pdf("report.pdf", urgency="high")

    # Audit trail
    csv_path = exporter.export_audit_csv("audit.csv", action="email:sent")
"""

import csv
import io
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger("Export")

# ── CSV formula-injection hardening (CWE-1236) ─────────────────
# Characters that make spreadsheet applications (Excel, LibreOffice,
# Google Sheets) treat a cell as a formula, plus TAB/CR/LF and
# common space look-alikes used to disguise leading formulas.
CSV_FORMULA_PREFIXES = (
    "=", "+", "-", "@",
    "\t", "\r", "\n",
    "\x20",         # regular space (leading spaces are also risky in Excel)
    "\xa0",         # no-break space
    "\u2000", "\u2001", "\u2002", "\u2003", "\u2004", "\u2005",
    "\u2006", "\u2007", "\u2008", "\u2009", "\u200a",  # en/em/thin spaces
    "\u3000",       # ideographic space
)


def _sanitize_csv_value(value):
    """Neutralize spreadsheet formula injection. Returns raw string unchanged otherwise."""
    if not isinstance(value, str) or not value:
        return value
    if value.startswith(CSV_FORMULA_PREFIXES):
        # A leading apostrophe makes spreadsheets treat the cell as text.
        # Embedded quoting/escaping is left to the csv writer.
        return "'" + value
    return value


def _pdf_safe(text):
    """Make text safe for latin-1 core fonts used by fpdf (fpdf2).

    Replaces characters outside latin-1 (emoji, CJK, curly quotes, ...)
    with '?' so the PDF export never crashes on non-latin-1 input.
    """
    if isinstance(text, str):
        return text.encode("latin-1", errors="replace").decode("latin-1")
    return text


class ExportManager:
    """
    Export conversations, drafts, and audit logs to CSV and PDF.
    """

    def __init__(self, output_dir: str = "exports"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

    # ── CSV Exports ───────────────────────────────────────────

    def export_conversations_csv(
        self,
        filename: str = "conversations.csv",
        source: Optional[str] = None,
        urgency: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 1000,
    ) -> str:
        """Export conversations to CSV. Returns the file path."""
        from core.database import get_db
        from core.database.models import ConversationRecord
        from sqlalchemy import desc

        rows = []
        with get_db() as db:
            query = db.query(ConversationRecord)
            if source:
                query = query.filter(ConversationRecord.source == source)
            if urgency:
                query = query.filter(ConversationRecord.urgency == urgency)
            if since:
                query = query.filter(ConversationRecord.ingested_at >= since)
            if until:
                query = query.filter(ConversationRecord.ingested_at <= until)

            records = (
                query.order_by(desc(ConversationRecord.ingested_at))
                .limit(limit)
                .all()
            )

            for r in records:
                rows.append({
                    "id": r.id,
                    "source": r.source,
                    "participants": json.dumps(r.participants or []),
                    "raw_text": (r.raw_text or "")[:500],
                    "commitments": json.dumps(r.commitments or []),
                    "entities": json.dumps(r.entities or {}),
                    "sentiment": r.sentiment or "",
                    "deal_size": r.deal_size or "",
                    "urgency": r.urgency or "",
                    "conversation_date": r.conversation_date.isoformat() if r.conversation_date else "",
                    "ingested_at": r.ingested_at.isoformat() if r.ingested_at else "",
                    "created_by": r.created_by or "",
                    "tags": json.dumps(r.tags or []),
                })

        path = os.path.join(self.output_dir, filename)
        self._write_csv(path, rows)
        logger.info(f"Exported {len(rows)} conversations to {path}")
        return path

    def export_drafts_csv(
        self,
        filename: str = "drafts.csv",
        sent_only: bool = False,
        limit: int = 1000,
    ) -> str:
        """Export follow-up drafts to CSV."""
        from core.database import get_db
        from core.database.models import FollowUpDraftRecord
        from sqlalchemy import desc

        rows = []
        with get_db() as db:
            query = db.query(FollowUpDraftRecord)
            if sent_only:
                query = query.filter(FollowUpDraftRecord.sent == True)

            records = (
                query.order_by(desc(FollowUpDraftRecord.created_at))
                .limit(limit)
                .all()
            )

            for r in records:
                rows.append({
                    "id": r.id,
                    "conversation_id": r.conversation_id,
                    "prospect_name": r.prospect_name or "",
                    "company": r.company or "",
                    "variant_agreeable": (r.variant_agreeable or "")[:300],
                    "variant_direct": (r.variant_direct or "")[:300],
                    "variant_soft": (r.variant_soft or "")[:300],
                    "selected_variant": r.selected_variant or "",
                    "sent": r.sent,
                    "sent_at": r.sent_at.isoformat() if r.sent_at else "",
                    "send_status": r.send_status or "",
                    "to_address": r.to_address or "",
                    "opens": r.opens,
                    "clicks": r.clicks,
                    "replies": r.replies,
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                })

        path = os.path.join(self.output_dir, filename)
        self._write_csv(path, rows)
        logger.info(f"Exported {len(rows)} drafts to {path}")
        return path

    def export_audit_csv(
        self,
        filename: str = "audit_trail.csv",
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 5000,
    ) -> str:
        """Export audit trail to CSV."""
        from core.database import get_db
        from core.database.models import AuditLog
        from sqlalchemy import desc

        rows = []
        with get_db() as db:
            query = db.query(AuditLog)
            if action:
                query = query.filter(AuditLog.action == action)
            if entity_type:
                query = query.filter(AuditLog.entity_type == entity_type)
            if since:
                query = query.filter(AuditLog.timestamp >= since)
            if until:
                query = query.filter(AuditLog.timestamp <= until)

            records = (
                query.order_by(desc(AuditLog.timestamp))
                .limit(limit)
                .all()
            )

            for r in records:
                rows.append({
                    "id": r.id,
                    "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                    "action": r.action,
                    "entity_type": r.entity_type or "",
                    "entity_id": r.entity_id or "",
                    "details": json.dumps(r.details or {}),
                    "actor": r.actor or "",
                    "success": r.success,
                    "error_message": r.error_message or "",
                })

        path = os.path.join(self.output_dir, filename)
        self._write_csv(path, rows)
        logger.info(f"Exported {len(rows)} audit entries to {path}")
        return path

    def export_queue_csv(
        self,
        filename: str = "queue.csv",
    ) -> str:
        """Export current queue state to CSV."""
        from core.queue import get_queue

        queue = get_queue()
        items = queue.list()

        rows = []
        for s in items:
            conv = s.conversation
            rows.append({
                "conversation_id": s.conversation_id,
                "priority_score": s.priority_score,
                "status": s.status,
                "sla_deadline": s.sla_deadline.isoformat() if s.sla_deadline else "",
                "sla_breached": s.sla_breached,
                "times_requeued": s.times_requeued,
                "source": conv.source if conv else "",
                "urgency": conv.urgency if conv else "",
                "sentiment": conv.sentiment if conv else "",
                "deal_size": conv.deal_size if conv else "",
                "participants": json.dumps(conv.participants if conv else []),
                "recency_days": s.recency_days,
                "engagement_probability": s.engagement_probability,
            })

        path = os.path.join(self.output_dir, filename)
        self._write_csv(path, rows)
        logger.info(f"Exported {len(rows)} queue items to {path}")
        return path

    # ── PDF Exports ───────────────────────────────────────────

    def export_conversations_pdf(
        self,
        filename: str = "conversations.pdf",
        source: Optional[str] = None,
        urgency: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 100,
        title: str = "Sales Follow-Up Agent - Conversation Report",
    ) -> str:
        """Export conversations to PDF."""
        from core.database import get_db
        from core.database.models import ConversationRecord
        from sqlalchemy import desc

        with get_db() as db:
            query = db.query(ConversationRecord)
            if source:
                query = query.filter(ConversationRecord.source == source)
            if urgency:
                query = query.filter(ConversationRecord.urgency == urgency)
            if since:
                query = query.filter(ConversationRecord.ingested_at >= since)
            if until:
                query = query.filter(ConversationRecord.ingested_at <= until)

            records = (
                query.order_by(desc(ConversationRecord.ingested_at))
                .limit(limit)
                .all()
            )

            rows = []
            for r in records:
                rows.append({
                    "id": r.id,
                    "source": r.source,
                    "participants": json.dumps(r.participants or []),
                    "raw_text": r.raw_text or "",
                    "commitments": r.commitments or [],
                    "sentiment": r.sentiment or "",
                    "deal_size": r.deal_size,
                    "urgency": r.urgency or "",
                    "conversation_date": r.conversation_date.isoformat() if r.conversation_date else "",
                    "ingested_at": r.ingested_at.isoformat() if r.ingested_at else "",
                    "tags": r.tags or [],
                })

        # Build PDF
        pdf_path = os.path.join(self.output_dir, filename)
        self._build_conversations_pdf(pdf_path, title, rows)
        logger.info(f"Exported {len(rows)} conversations to PDF: {pdf_path}")
        return pdf_path

    def export_audit_pdf(
        self,
        filename: str = "audit_trail.pdf",
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        limit: int = 500,
        title: str = "Sales Follow-Up Agent - Audit Trail",
    ) -> str:
        """Export audit trail to PDF."""
        from core.database import get_db
        from core.database.models import AuditLog
        from sqlalchemy import desc

        with get_db() as db:
            query = db.query(AuditLog)
            if action:
                query = query.filter(AuditLog.action == action)
            if entity_type:
                query = query.filter(AuditLog.entity_type == entity_type)
            if since:
                query = query.filter(AuditLog.timestamp >= since)
            if until:
                query = query.filter(AuditLog.timestamp <= until)

            records = (
                query.order_by(desc(AuditLog.timestamp))
                .limit(limit)
                .all()
            )

            rows = []
            for r in records:
                rows.append({
                    "timestamp": r.timestamp.isoformat() if r.timestamp else "",
                    "action": r.action,
                    "entity_type": r.entity_type or "",
                    "entity_id": r.entity_id or "",
                    "details": json.dumps(r.details or {}),
                    "actor": r.actor or "",
                    "success": r.success,
                    "error_message": r.error_message or "",
                })

        pdf_path = os.path.join(self.output_dir, filename)
        self._build_audit_pdf(pdf_path, title, rows)
        logger.info(f"Exported {len(rows)} audit entries to PDF: {pdf_path}")
        return pdf_path

    def export_summary_pdf(
        self,
        filename: str = "summary.pdf",
        title: str = "Sales Follow-Up Agent - Compliance Summary",
    ) -> str:
        """Export a summary report with statistics."""
        from core.database import get_db
        from core.database.models import ConversationRecord, AuditLog
        from core.queue import get_queue

        stats = {}
        with get_db() as db:
            stats["conversations_total"] = db.query(ConversationRecord).count()
            stats["conversations_email"] = db.query(ConversationRecord).filter(
                ConversationRecord.source == "email"
            ).count()
            stats["conversations_call"] = db.query(ConversationRecord).filter(
                ConversationRecord.source == "call"
            ).count()
            stats["conversations_meeting"] = db.query(ConversationRecord).filter(
                ConversationRecord.source == "meeting"
            ).count()

            stats["audit_total"] = db.query(AuditLog).count()
            stats["audit_failed"] = db.query(AuditLog).filter(
                AuditLog.success == False
            ).count()

        queue = get_queue()
        q_stats = queue.get_queue_stats()
        stats["queue_size"] = q_stats.get("total_items", 0)
        stats["queue_breached"] = q_stats.get("breached_count", 0)

        pdf_path = os.path.join(self.output_dir, filename)
        self._build_summary_pdf(pdf_path, title, stats)
        logger.info(f"Exported summary report to {pdf_path}")
        return pdf_path

    # ── PDF Builders ──────────────────────────────────────────

    def _build_conversations_pdf(self, path: str, title: str, rows: List[Dict]) -> None:
        """Build PDF for conversations."""
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _pdf_safe(title), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, f"Generated: {datetime.utcnow().isoformat()}Z", ln=True)
        pdf.cell(0, 8, f"Total records: {len(rows)}", ln=True)
        pdf.ln(5)

        # Each conversation
        for i, row in enumerate(rows, 1):
            # Check if we need a new page
            if pdf.get_y() > 240:
                pdf.add_page()

            # Header
            pdf.set_font("Helvetica", "B", 11)
            pdf.cell(0, 7, _pdf_safe(f"Conversation #{i} - {row['source'].upper()}"), ln=True)

            # Metadata
            pdf.set_font("Helvetica", "", 9)
            pdf.cell(0, 5, _pdf_safe(f"ID: {row['id'][:16]}...  |  Urgency: {row['urgency']}  |  Sentiment: {row['sentiment']}"), ln=True)
            pdf.cell(0, 5, _pdf_safe(f"Date: {row['conversation_date']}  |  Ingested: {row['ingested_at']}"), ln=True)

            if row.get("deal_size"):
                pdf.cell(0, 5, f"Deal Size: ${row['deal_size']:,.0f}", ln=True)

            # Participants
            try:
                participants = json.loads(row["participants"]) if isinstance(row["participants"], str) else row["participants"]
                if participants:
                    names = [p.get("name", p.get("email", "?")) for p in participants]
                    pdf.cell(0, 5, _pdf_safe(f"Participants: {', '.join(names)}"), ln=True)
            except Exception:
                pass

            # Raw text (truncated)
            raw = row.get("raw_text", "")
            if raw:
                pdf.set_font("Helvetica", "", 8)
                pdf.multi_cell(0, 4, _pdf_safe(f"Content: {raw[:400]}{'...' if len(raw) > 400 else ''}"))

            # Commitments
            commitments = row.get("commitments", [])
            if commitments:
                pdf.set_font("Helvetica", "I", 8)
                for c in commitments[:3]:
                    pdf.cell(0, 4, _pdf_safe(f"  - {c}"), ln=True)

            pdf.ln(3)

        # Footer
        pdf.set_y(-15)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 10, f"Page {pdf.page_no()}/{{nb}}", align="C")

        pdf.output(path)

    def _build_audit_pdf(self, path: str, title: str, rows: List[Dict]) -> None:
        """Build PDF for audit trail."""
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 16)
        pdf.cell(0, 10, _pdf_safe(title), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, f"Generated: {datetime.utcnow().isoformat()}Z", ln=True)
        pdf.cell(0, 8, f"Total entries: {len(rows)}", ln=True)
        pdf.ln(5)

        # Table header
        pdf.set_font("Helvetica", "B", 8)
        col_widths = [35, 40, 25, 25, 30, 25, 10]
        headers = ["Timestamp", "Action", "Entity Type", "Entity ID", "Actor", "Details", "OK"]
        for w, h in zip(col_widths, headers):
            pdf.cell(w, 6, h, border=1)
        pdf.ln()

        # Table rows
        pdf.set_font("Helvetica", "", 7)
        for row in rows:
            if pdf.get_y() > 270:
                pdf.add_page()
                pdf.set_font("Helvetica", "B", 8)
                for w, h in zip(col_widths, headers):
                    pdf.cell(w, 6, h, border=1)
                pdf.ln()
                pdf.set_font("Helvetica", "", 7)

            ts = row["timestamp"][:19] if row["timestamp"] else ""
            action = row["action"][:20]
            etype = row["entity_type"][:12]
            eid = row["entity_id"][:12]
            actor = row["actor"][:12]
            details = row["details"][:15]
            ok = "Y" if row["success"] else "N"

            for w, val in zip(col_widths, [ts, action, etype, eid, actor, details, ok]):
                pdf.cell(w, 5, _pdf_safe(val), border=1)
            pdf.ln()

        # Footer
        pdf.set_y(-15)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 10, f"Page {pdf.page_no()}/{{nb}}", align="C")

        pdf.output(path)

    def _build_summary_pdf(self, path: str, title: str, stats: Dict) -> None:
        """Build summary PDF report."""
        from fpdf import FPDF

        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        # Title
        pdf.set_font("Helvetica", "B", 18)
        pdf.cell(0, 12, _pdf_safe(title), ln=True)
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 8, f"Generated: {datetime.utcnow().isoformat()}Z", ln=True)
        pdf.ln(8)

        # Summary section
        pdf.set_font("Helvetica", "B", 14)
        pdf.cell(0, 10, "Summary Statistics", ln=True)
        pdf.set_font("Helvetica", "", 11)

        sections = [
            ("Conversations", [
                ("Total", stats.get("conversations_total", 0)),
                ("Email", stats.get("conversations_email", 0)),
                ("Call", stats.get("conversations_call", 0)),
                ("Meeting", stats.get("conversations_meeting", 0)),
            ]),
            ("Queue", [
                ("Current Size", stats.get("queue_size", 0)),
                ("SLA Breaches", stats.get("queue_breached", 0)),
            ]),
            ("Audit Trail", [
                ("Total Entries", stats.get("audit_total", 0)),
                ("Failed Actions", stats.get("audit_failed", 0)),
            ]),
        ]

        for section_name, items in sections:
            pdf.set_font("Helvetica", "B", 12)
            pdf.cell(0, 8, section_name, ln=True)
            pdf.set_font("Helvetica", "", 11)
            for label, value in items:
                pdf.cell(10)
                pdf.cell(60, 6, f"{label}:")
                pdf.cell(0, 6, str(value), ln=True)
            pdf.ln(3)

        # Compliance note
        pdf.ln(10)
        pdf.set_font("Helvetica", "I", 9)
        pdf.multi_cell(0, 5,
            _pdf_safe(
                "This report was generated by LeadSync. "
                "For questions about data retention or GDPR/CCPA compliance, "
                "contact the system administrator."
            )
        )

        # Footer
        pdf.set_y(-15)
        pdf.set_font("Helvetica", "I", 8)
        pdf.cell(0, 10, f"Page {pdf.page_no()}/{{nb}}", align="C")

        pdf.output(path)

    # ── Helpers ───────────────────────────────────────────────

    def _write_csv(self, path: str, rows: List[Dict]) -> None:
        """Write rows to a CSV file, neutralizing spreadsheet formula injection."""
        if not rows:
            # Create empty file with header
            with open(path, "w", newline="", encoding="utf-8") as f:
                f.write("No data available\n")
            return

        fieldnames = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow({
                    key: _sanitize_csv_value(str(v)) if v is not None else ""
                    for key, v in row.items()
                })


# ── Global instance ────────────────────────────────────────────

_export_manager: Optional[ExportManager] = None


def get_export_manager() -> ExportManager:
    global _export_manager
    if _export_manager is None:
        _export_manager = ExportManager()
    return _export_manager
