"""Local import, deduplication, manual updates, and actionable follow-up views."""

from __future__ import annotations

import copy
import csv
import hashlib
import json
from contextlib import nullcontext
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

from .ingestion import parse_csv
from .models import ConnectionStatus, timestamp, utcnow
from .store import FIELDS, truth


def stable_key(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


class NeedsReview(ValueError):
    pass


def tokens(value):
    return set(filter(None, (value or "").split("|")))


def folder_segment(reference):
    """Labels come from inbox folders, without requiring producer CSV changes."""
    for parent in Path(reference).parents:
        if parent.name.casefold() in {"founders", "vps"}:
            return "founders" if parent.name.casefold() == "founders" else "VPs"
    return ""


def union(old, new):
    return "|".join(sorted(tokens(old) | {str(new)}))


class GTMService:
    def __init__(self, store, state, rules, *, research=None):
        self.store, self.state, self.rules = store, state, rules
        self.research = research

    def write_rows(self, rows):
        if self.research and self.rules.get("scoring"):
            from .scoring import SCORE_COLUMNS, score_rows

            results = score_rows(rows, self.research, self.rules["scoring"])
            for row, result in zip(rows, results, strict=True):
                row.update({key: result[key] for key in SCORE_COLUMNS})
        self.store.write(rows)

    def content_key(self, content, source, reference):
        segment = folder_segment(reference) if str(source) == "grok" else ""
        parts = [
            "local-csv-v1",
            str(source),
            hashlib.sha256(content).hexdigest(),
            self.rules["mappings"][source],
        ]
        if segment:
            parts.append(segment)
        return stable_key(*parts)

    def file_key(self, path, source, reference=None):
        return self.content_key(Path(path).read_bytes(), source, reference or path)

    def merge(self, rows, p, observation):
        # Observation identity is persisted in the same atomic CSV write as the person.
        # This closes the crash window before the SQLite row checkpoint.
        for row in rows:
            if any(
                item["key"] == observation["key"] for item in json.loads(row["source_data"] or "[]")
            ):
                return row, "skip"
        matches = [
            r
            for r in rows
            if (p.email and r.get("email") == p.email)
            or (p.linkedin_url and r.get("linkedin_url") == p.linkedin_url)
        ]
        if len(matches) > 1:
            raise NeedsReview(
                "Email/LinkedIn matches multiple master rows; resolve these rows in the master CSV"
            )
        row = matches[0] if matches else None
        if row:
            for field in ("email", "linkedin_url"):
                if getattr(p, field) and row.get(field) and getattr(p, field) != row[field]:
                    raise NeedsReview(
                        f"Conflicting {field}; inspect the master CSV before matching"
                    )
        name_matches = [
            r
            for r in rows
            if r["full_name"].casefold() == p.full_name.casefold()
            and (
                (p.domain and r.get("domain") == p.domain)
                or (
                    p.company_name
                    and r.get("company_name", "").casefold() == p.company_name.casefold()
                )
            )
        ]
        if row is None and name_matches:
            raise NeedsReview(
                "Possible name/company duplicate; add a confirmed matching email or LinkedIn URL in the master CSV"
            )
        action = "update" if row else "create"
        now = utcnow().isoformat()
        if row is None:
            row = dict.fromkeys(FIELDS, "")
            row.update(
                id=str(uuid4()),
                original_source=str(p.source),
                discovered_at=p.discovered_at.isoformat(),
                activity_history="[]",
                completed_actions="[]",
                source_data="[]",
                connection_status="discovered",
                status_changed_at=(p.status_observed_at or p.discovered_at).isoformat(),
                do_not_contact="false",
                needs_review="false",
            )
            if not p.email and not p.linkedin_url:
                row.update(
                    needs_review="true",
                    review_reason="Missing email and LinkedIn URL; captured for manual review",
                )
            rows.append(row)
        for field in (
            "full_name",
            "email",
            "linkedin_url",
            "company_name",
            "domain",
            "website",
            "job_title",
        ):
            incoming = getattr(p, field)
            if incoming and not row.get(field):
                row[field] = incoming
        if observation.get("segment"):
            row["segments"] = union(row.get("segments"), observation["segment"])
        row["sources"] = union(row.get("sources"), p.source)
        row["objectives"] = union(row.get("objectives"), p.objective)
        row["discovered_at"] = min(timestamp(row["discovered_at"]), p.discovered_at).isoformat()
        row["last_seen"] = now
        # Imported lifecycle data fills blanks; operator edits always win on subsequent imports.
        if action == "create" or not row.get("connection_status"):
            row["connection_status"] = str(p.connection_status)
        for field in ("connection_request_at", "last_contact_at", "next_followup_at"):
            if getattr(p, field) and not row.get(field):
                row[field] = getattr(p, field).isoformat()
        if p.notes and p.notes not in (row.get("notes") or ""):
            row["notes"] = "\n".join(filter(None, [row.get("notes"), p.notes]))
        if p.do_not_contact:
            row["do_not_contact"] = "true"
        recognized = {
            alias.casefold()
            for key, names in self.rules["mappings"][p.source].items()
            for alias in [key, *names]
        } | set(type(p).model_fields)
        for key, incoming in observation.get("data", {}).items():
            key = str(key).strip().casefold()
            if key not in recognized and key not in FIELDS and incoming and not row.get(key):
                row[key] = incoming
        if p.create_opportunity:
            stage = p.opportunity_stage or "identified"
            if stage not in self.rules["pipelines"][p.objective]:
                raise NeedsReview("Invalid opportunity stage")
            field = "job_stage" if p.objective == "remote_job" else "consulting_stage"
            if not row.get(field):
                row[field] = stage
        observations = json.loads(row["source_data"])
        observations.append(observation)
        row["source_data"] = json.dumps(observations, ensure_ascii=False)
        return row, action

    def process(self, path, source, *, dry_run=False, source_path=None, seen=None):
        path = Path(path)
        if path.resolve() == self.store.path.resolve():
            raise ValueError(
                "The master CSV is not an input file; import a Grok or LinkedIn export"
            )
        key = self.file_key(path, source, source_path)
        rows = (
            seen.setdefault("local_rows", self.store.load())
            if seen is not None
            else self.store.load()
        )
        summary = {
            "file": str(source_path or path),
            "dry_run": dry_run,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "review": 0,
            "invalid": 0,
            "failed": 0,
            "rows": [],
        }
        with path.open(encoding="utf-8-sig", newline="") as file:
            raw_rows = list(csv.DictReader(file, strict=True))
        parsed = list(
            parse_csv(
                path,
                source,
                self.rules["mappings"][source],
                discovered_at=utcnow(),
                reference=str(source_path or path),
            )
        )
        for parsed_row in parsed:
            number = parsed_row.number
            old = self.state.row(key, number)
            if (
                old
                and old["status"] == "success"
                and any(r["id"] == old["record_id"] for r in rows)
            ):
                summary["skipped"] += 1
                continue
            if parsed_row.error:
                summary["invalid"] += 1
                summary["rows"].append(
                    {"row": number, "action": "invalid", "error": parsed_row.error}
                )
                if not dry_run:
                    self.state.result(key, number, "invalid", error=parsed_row.error)
                continue
            p = parsed_row.prospect
            raw = raw_rows[number - 2]
            observation = {
                "key": stable_key(key, number),
                "source": str(source),
                "file": str(source_path or path),
                "row": number,
                "discovered_at": p.discovered_at.isoformat(),
                "data": raw,
                "segment": folder_segment(source_path or path) if str(source) == "grok" else "",
            }
            try:
                # Failed/review rows must not mutate the preview or the on-disk master.
                candidate = copy.deepcopy(rows)
                row, action = self.merge(candidate, p, observation)
                if not dry_run:
                    if action != "skip":
                        self.write_rows(candidate)
                    self.state.result(key, number, "success", record_id=row["id"])
                rows[:] = candidate
                summary[{"create": "created", "update": "updated", "skip": "skipped"}[action]] += 1
                summary["rows"].append(
                    {
                        "row": number,
                        "action": action,
                        "id": row["id"],
                        "needs_review": truth(row["needs_review"]),
                    }
                )
            except NeedsReview as exc:
                summary["review"] += 1
                summary["rows"].append({"row": number, "action": "review", "error": str(exc)})
                if not dry_run:
                    self.state.result(key, number, "review", error=str(exc))
            except OSError:
                if not dry_run:
                    self.state.result(
                        key,
                        number,
                        "failed",
                        error="Local CSV write failed; check disk space and permissions",
                    )
                raise
        return summary

    def preview_rows(self, path, source, *, reference=None, seen=None):
        return self.process(path, source, dry_run=True, source_path=reference, seen=seen)

    def import_file(self, path, source, *, locked=False, source_path=None, snapshot_path=None):
        with nullcontext() if locked else self.state.lock():
            key = self.file_key(path, source, source_path)
            self.state.file(key, source, source_path or path)
            if snapshot_path:
                self.state.snapshot(key, snapshot_path)
            run = self.state.run_started(key, source_path or path)
            try:
                summary = self.process(path, source, source_path=source_path)
                status = (
                    "partial"
                    if any(summary[k] for k in ("review", "invalid", "failed"))
                    else "complete"
                )
                self.state.finish(key, status)
                self.state.run_finished(run, status, summary)
                return summary
            except Exception:
                self.state.finish(key, "failed")
                self.state.run_finished(
                    run, "failed", error="Import interrupted by a local error; retry original file"
                )
                raise

    def update(
        self,
        person_id,
        *,
        status=None,
        followup=None,
        notes=None,
        do_not_contact=None,
        objective=None,
        stage=None,
        activity=None,
    ):
        rows = self.store.load()
        row = next((r for r in rows if r["id"] == person_id), None)
        if row is None:
            raise ValueError("Unknown prospect ID; run gtm people")
        now = utcnow().isoformat()
        if status:
            if row["connection_status"] != str(status):
                row["status_changed_at"] = now
            row["connection_status"] = str(ConnectionStatus(status))
            if status in {"connection_sent", "connection_pending"}:
                row["connection_request_at"] = row["connection_request_at"] or now
            if status in {"conversation_started", "replied", "meeting_booked"}:
                row["last_contact_at"] = now
        if followup:
            row["next_followup_at"] = timestamp(followup).isoformat()
        if notes:
            row["notes"] = "\n".join(filter(None, [row.get("notes"), notes]))
        if do_not_contact is not None:
            row["do_not_contact"] = str(do_not_contact).lower()
        if stage:
            if not objective or stage not in self.rules["pipelines"][objective]:
                raise ValueError("Supply a valid --objective and --stage; see config/gtm.yaml")
            field = "job_stage" if objective == "remote_job" else "consulting_stage"
            row[field] = stage
            row["objectives"] = union(row["objectives"], objective)
        history = json.loads(row["activity_history"] or "[]")
        history.append(
            {
                "id": str(uuid4()),
                "type": activity or "manual_update",
                "timestamp": now,
                "status": status,
                "notes": notes,
                "stage": stage,
                "objective": objective,
            }
        )
        row["activity_history"] = json.dumps(history)
        self.write_rows(rows)
        self.state.audit("local_update", "prospects", person_id)
        return row

    def actions(self, *, now=None):
        now = now or utcnow()
        actions = []
        for row in self.store.load():
            if truth(row.get("do_not_contact")) or row["connection_status"] == "closed":
                continue
            status = row["connection_status"]
            rule, due, priority = None, None, 2
            if status in {"connection_sent", "connection_pending"} and row["connection_request_at"]:
                rule = "Review pending connection"
                due = timestamp(row["connection_request_at"]) + timedelta(
                    days=self.rules["followups"]["pending_days"]
                )
            elif status == "connected":
                rule, due = (
                    "Start a personalized conversation",
                    timestamp(row["status_changed_at"] or row["discovered_at"]),
                )
            elif status == "replied":
                rule, due, priority = (
                    "Respond to reply",
                    timestamp(row["last_contact_at"] or row["last_seen"]),
                    1,
                )
            if row.get("next_followup_at") and status != "replied":
                rule, due = "Review follow-up", timestamp(row["next_followup_at"])
            if rule and due <= now:
                action_id = stable_key(row["id"], rule, due.isoformat())[:20]
                if action_id not in json.loads(row.get("completed_actions") or "[]"):
                    actions.append(
                        {
                            "id": action_id,
                            "person_id": row["id"],
                            "name": row["full_name"],
                            "action": rule,
                            "due": due.isoformat(),
                            "priority": priority,
                        }
                    )
        return sorted(actions, key=lambda a: (a["priority"], a["due"], a["name"]))

    def complete(self, action_id):
        action = next((a for a in self.actions() if a["id"] == action_id), None)
        if not action:
            raise ValueError("Unknown/currently inactive action; run gtm followups")
        rows = self.store.load()
        row = next(r for r in rows if r["id"] == action["person_id"])
        completed = json.loads(row["completed_actions"] or "[]")
        completed.append(action_id)
        row["completed_actions"] = json.dumps(completed)
        self.write_rows(rows)

    def record_event(self, person_id, event, *, notes=None, followup=None, event_id=None):
        """Human-observed CRM events; repeated explicit IDs do not duplicate activity."""
        statuses = {
            "connection_sent": "connection_sent",
            "connection_accepted": "connected",
            "outreach_sent": "conversation_started",
            "reply": "replied",
            "positive_reply": "replied",
            "meeting_booked": "meeting_booked",
            "meeting_completed": None,
            "note": None,
            "do_not_contact": None,
        }
        if event not in statuses:
            raise ValueError(
                "Unsupported event; use connection_sent, connection_accepted, outreach_sent, reply, positive_reply, meeting_booked, meeting_completed, note, or do_not_contact"
            )
        rows = self.store.load()
        row = next((r for r in rows if r["id"] == person_id), None)
        if row is None:
            raise ValueError("Unknown prospect ID; run gtm people")
        if truth(row.get("do_not_contact")) and event not in {"note", "do_not_contact"}:
            raise ValueError("Do-not-contact prospect; only notes are allowed")
        history = json.loads(row["activity_history"] or "[]")
        if event_id and any(e["id"] == event_id for e in history):
            previous = next(e for e in history if e["id"] == event_id)
            if previous["type"] != event:
                raise ValueError("Event ID already belongs to a different event")
            return row
        now = utcnow()
        status = statuses[event]
        if status:
            row["connection_status"] = status
            row["status_changed_at"] = now.isoformat()
        if event == "connection_sent":
            row["connection_request_at"] = now.isoformat()
        if event in {
            "outreach_sent",
            "reply",
            "positive_reply",
            "meeting_booked",
            "meeting_completed",
        }:
            row["last_contact_at"] = now.isoformat()
        if followup:
            row["next_followup_at"] = timestamp(followup).isoformat()
        elif event == "outreach_sent":
            row["next_followup_at"] = (
                now + timedelta(days=self.rules["followups"]["outreach_days"])
            ).isoformat()
        elif event in {"reply", "positive_reply", "connection_accepted"}:
            row["next_followup_at"] = ""
        elif event == "meeting_completed":
            row["next_followup_at"] = now.isoformat()
        if event == "do_not_contact":
            row["do_not_contact"] = "true"
        if notes:
            row["notes"] = "\n".join(filter(None, [row.get("notes"), notes]))
        history.append(
            {
                "id": event_id or str(uuid4()),
                "type": event,
                "timestamp": now.isoformat(),
                "status": status,
                "notes": notes,
                "next_followup_at": row.get("next_followup_at"),
            }
        )
        row["activity_history"] = json.dumps(history)
        self.write_rows(rows)
        self.state.audit("manual_event", "prospects", person_id, event=event)
        return row
