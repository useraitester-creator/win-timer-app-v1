"""Thin wrapper around fast-bitrix24 for talking to a Bitrix24 portal.

Keeps the rest of the app decoupled from the library: the UI/controller depend
on this small interface, and tests inject a fake client via ``client_factory``.
"""
from __future__ import annotations

import asyncio
import re
import warnings

# https://portal.bitrix24.ru/rest/<user_id>/<token>/
_WEBHOOK_RE = re.compile(r"^https://[^/\s]+/rest/\d+/[^/\s]+/?$")


def looks_like_webhook(url: str) -> bool:
    """Lightweight format check for an inbound-webhook URL (not a live check)."""
    return bool(_WEBHOOK_RE.match((url or "").strip()))


class Bitrix24Error(Exception):
    """Raised for configuration problems with the Bitrix24 client."""


# Smart-process (СПА) "Реестр проектов".
PROJECTS_ENTITY_TYPE_ID = 150
# Smart-process (СПА) журнала работ (запись о затраченном времени по проекту).
WORKLOG_ENTITY_TYPE_ID = 1092


def entity_url(webhook_url: str, link: dict | None) -> str | None:
    """Build the portal URL for an imported entity from the webhook + link.

    ``link`` is the task's stored ``{"source", "id"}``. Returns ``None`` if the
    URL can't be built. The host and user id come from the webhook itself.
    """
    if not isinstance(link, dict):
        return None
    match = re.match(r"(https://[^/\s]+)/rest/(\d+)/", (webhook_url or "").strip())
    if not match:
        return None
    base, user_id = match.group(1), match.group(2)
    item_id = link.get("id")
    if not item_id:
        return None
    source = link.get("source")
    if source == "project":
        return f"{base}/crm/type/{PROJECTS_ENTITY_TYPE_ID}/details/{item_id}/"
    if source == "task":
        return f"{base}/company/personal/user/{user_id}/tasks/task/view/{item_id}/"
    return None


def _ensure_event_loop() -> None:
    """Make sure the current thread has a usable asyncio event loop.

    fast-bitrix24's client builds asyncio primitives (``asyncio.Event``) at
    construction time, which require a current event loop in the calling thread
    (Python 3.9). Worker threads (e.g. a ``QThread``) have none, so create one.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("event loop is closed")
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _default_factory(webhook_url: str):
    # Imported lazily so the module (and tests) don't require fast-bitrix24.
    from fast_bitrix24 import Bitrix

    return Bitrix(webhook_url)


def _default_post(webhook_url: str, method: str, payload: dict):
    """Direct JSON POST to a REST method (preserves int/float types).

    fast-bitrix24's call() batches requests, and batch serialization turns
    values into query-string strings — which methods like task.elapseditem.add
    (strict about integer types) reject. A JSON body keeps types intact.
    """
    import json
    import urllib.error
    import urllib.request

    url = webhook_url.rstrip("/") + "/" + method
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:
            raise RuntimeError(f"HTTP {exc.code} {exc.reason}") from None
    if isinstance(body, dict) and body.get("error"):
        raise RuntimeError(body.get("error_description") or body.get("error"))
    return body.get("result") if isinstance(body, dict) else body


class Bitrix24Client:
    def __init__(self, webhook_url: str, *, client_factory=None, post_func=None) -> None:
        url = (webhook_url or "").strip()
        if not looks_like_webhook(url):
            raise Bitrix24Error("Некорректный URL вебхука")
        self._webhook_url = url
        self._factory = client_factory or _default_factory
        self._post_func = post_func or _default_post
        self._bx = None

    def _post(self, method: str, payload: dict):
        """Single write request via direct JSON POST, token stripped from errors."""
        try:
            return self._post_func(self._webhook_url, method, payload)
        except Bitrix24Error:
            raise
        except Exception as exc:
            raise Bitrix24Error(self._sanitize(str(exc))) from None

    def _client(self):
        if self._bx is None:
            _ensure_event_loop()
            self._bx = self._factory(self._webhook_url)
        return self._bx

    def _sanitize(self, text: str) -> str:
        """Strip the webhook URL/token from a message so it never leaks.

        fast-bitrix24 / aiohttp errors embed the full request URL, which
        contains the secret token — and that message is shown in the UI.
        """
        text = str(text)
        text = text.replace(self._webhook_url, "***")
        match = re.search(r"/rest/\d+/([^/]+)", self._webhook_url)
        if match:
            text = text.replace(match.group(1), "***")
        return text

    def _safe_get_all(self, method: str, params: dict | None = None):
        """Call ``get_all`` with the token stripped from any error.

        ``profile`` and other single-object methods warn that ``get_all`` is
        meant for list methods — we silence that one expected warning.
        """
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=UserWarning, message="get_all")
                return self._client().get_all(method, params)
        except Bitrix24Error:
            raise
        except Exception as exc:
            raise Bitrix24Error(self._sanitize(str(exc))) from None

    def _safe_call(self, method: str, params: dict):
        """Call a single method with the token stripped from any error."""
        try:
            return self._client().call(method, params)
        except Bitrix24Error:
            raise
        except Exception as exc:
            raise Bitrix24Error(self._sanitize(str(exc))) from None

    def _profile(self) -> dict:
        return self._safe_get_all("profile")

    def test_connection(self) -> dict:
        """Call ``profile`` and return the current user's profile dict."""
        return self._profile()

    def current_user_id(self) -> int:
        """Return the id of the webhook's user (from ``profile``)."""
        return int(self._profile().get("ID"))

    def _final_project_stage_ids(self) -> set:
        """Stage ids of СПА 150 that are final (won/lost), by stage semantics.

        Mirrors the portal's "В работе" view: active = any stage whose
        ``SEMANTICS`` is not ``'S'`` (success) or ``'F'`` (fail).
        """
        prefix = f"DYNAMIC_{PROJECTS_ENTITY_TYPE_ID}_STAGE_"
        statuses = self._safe_get_all(
            "crm.status.list", {"select": ["STATUS_ID", "ENTITY_ID", "SEMANTICS"]}
        ) or []
        return {
            status.get("STATUS_ID")
            for status in statuses
            if str(status.get("ENTITY_ID", "")).startswith(prefix)
            and status.get("SEMANTICS") in ("S", "F")
        }

    def list_projects(self, user_id) -> list[dict]:
        """Active projects (СПА 150) where the user is main executor or supporter.

        Bitrix filters can't OR across fields, so we query each field and merge.
        "Active" means the project's stage is not final (won/lost) — matching the
        portal's "В работе" filter — rather than the unrelated "Проект сдан" flag.
        """
        final_stages = self._final_project_stage_ids()
        found: dict[str, str] = {}
        for field in ("ufCrm16MainIspolnitel", "ufCrm16Supporters"):
            items = self._safe_get_all(
                "crm.item.list",
                {
                    "entityTypeId": PROJECTS_ENTITY_TYPE_ID,
                    "filter": {field: user_id},
                    "select": ["id", "title", "stageId"],
                },
            ) or []
            for item in items:
                if item.get("stageId") in final_stages:
                    continue
                found[str(item.get("id"))] = item.get("title", "")
        return [{"id": key, "title": title} for key, title in found.items()]

    def list_tasks(self, user_id) -> list[dict]:
        """Active tasks where the user is responsible or an accomplice.

        Results come back lower-cased (``id``/``title``/``status``); status
        ``'5'`` (done) / ``'7'`` (declined) are excluded.
        """
        found: dict[str, str] = {}
        for field in ("RESPONSIBLE_ID", "ACCOMPLICE"):
            items = self._safe_get_all(
                "tasks.task.list",
                {
                    "filter": {field: user_id, "!STATUS": "5"},
                    "select": ["ID", "TITLE", "STATUS"],
                },
            ) or []
            for item in items:
                if str(item.get("status", "")) in ("5", "7"):
                    continue
                found[str(item.get("id"))] = item.get("title", "")
        return [{"id": key, "title": title} for key, title in found.items()]

    def search_companies(self, query: str, limit: int = 30) -> list[dict]:
        """Search CRM companies by title substring. Empty for queries < 3 chars."""
        query = (query or "").strip()
        if len(query) < 3:
            return []
        # get_all() rejects 'order'/'start', so we sort client-side.
        items = self._safe_get_all(
            "crm.company.list",
            {"filter": {"%TITLE": query}, "select": ["ID", "TITLE"]},
        ) or []
        companies = [{"id": str(c.get("ID")), "title": c.get("TITLE", "")} for c in items]
        companies.sort(key=lambda c: c["title"].lower())
        return companies[:limit]

    def create_portal_task(
        self, title: str, description: str, responsible_id, company_id=None
    ) -> str:
        """Create a task in Bitrix24 (tasks.task.add). Returns the new task id."""
        fields = {
            "TITLE": title,
            "RESPONSIBLE_ID": responsible_id,
            "DESCRIPTION": description or "",
        }
        if company_id:
            fields["UF_CRM_TASK"] = [f"CO_{company_id}"]
        result = self._safe_call("tasks.task.add", {"fields": fields})
        task = result.get("task", result) if isinstance(result, dict) else {}
        return str(task.get("id"))

    def add_project_time(
        self, project_id, date_iso: str, hours: float, comment: str, responsible_id
    ) -> str:
        """Create a worklog item (СПА 1092) for a project. Returns the new item id."""
        fields = {
            "parentId150": int(project_id),
            "assignedById": int(responsible_id),
            "ufCrm88HoursWork": float(hours),
            "ufCrm88CommentWork": comment,
            "ufCrm88DateWork": date_iso,
        }
        result = self._post(
            "crm.item.add", {"entityTypeId": WORKLOG_ENTITY_TYPE_ID, "fields": fields}
        )
        item = result.get("item", result) if isinstance(result, dict) else {}
        return str(item.get("id"))

    def add_task_time(self, task_id, seconds: int, comment: str) -> str:
        """Log elapsed time on a Bitrix24 task (task.elapseditem.add). Returns record id."""
        result = self._post(
            "task.elapseditem.add",
            {"taskId": int(task_id), "arFields": {"SECONDS": int(seconds), "COMMENT_TEXT": comment}},
        )
        if isinstance(result, dict):
            return str(result.get("id"))
        return str(result)

    def complete_portal_task(self, task_id) -> None:
        """Mark a Bitrix24 task as completed (tasks.task.complete)."""
        self._safe_call("tasks.task.complete", {"taskId": task_id})

    def renew_portal_task(self, task_id) -> None:
        """Re-open a completed Bitrix24 task (tasks.task.renew)."""
        self._safe_call("tasks.task.renew", {"taskId": task_id})
