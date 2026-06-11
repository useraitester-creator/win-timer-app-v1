from __future__ import annotations

import pytest

from win_timer_app.bitrix import (
    Bitrix24Client,
    Bitrix24Error,
    entity_url,
    looks_like_webhook,
)
from win_timer_app.controller import AppController


class FakeBx:
    """Stand-in for fast_bitrix24.Bitrix with the same get_all() contract."""

    def __init__(self, url, *, profile=None, error=None):
        self.url = url
        self._profile = profile if profile is not None else {"NAME": "Иван"}
        self._error = error
        self.calls = []

    def get_all(self, method, params=None):
        self.calls.append((method, params))
        if self._error is not None:
            raise self._error
        if method == "profile":
            return self._profile
        return {}


def _client(url="https://acme.bitrix24.ru/rest/1/abc/", **kwargs):
    fake = FakeBx(url, **kwargs)
    return Bitrix24Client(url, client_factory=lambda u: fake), fake


# --- looks_like_webhook ------------------------------------------------------

def test_looks_like_webhook_accepts_valid_rest_url():
    assert looks_like_webhook("https://acme.bitrix24.ru/rest/1/abc123/")


def test_looks_like_webhook_accepts_url_without_trailing_slash():
    assert looks_like_webhook("https://acme.bitrix24.ru/rest/12/abc123")


def test_looks_like_webhook_rejects_empty():
    assert not looks_like_webhook("")


def test_looks_like_webhook_rejects_non_https():
    assert not looks_like_webhook("http://acme.bitrix24.ru/rest/1/abc123/")


def test_looks_like_webhook_rejects_non_rest_url():
    assert not looks_like_webhook("https://acme.bitrix24.ru/company/")


# --- Bitrix24Client ----------------------------------------------------------

def test_client_rejects_invalid_webhook():
    with pytest.raises(Bitrix24Error):
        Bitrix24Client("not-a-url")


def test_test_connection_returns_profile():
    client, fake = _client(profile={"NAME": "Иван", "LAST_NAME": "Петров"})
    result = client.test_connection()
    assert result["NAME"] == "Иван"
    assert ("profile", None) in fake.calls


def test_test_connection_wraps_errors_as_bitrix_error():
    client, _ = _client(error=RuntimeError("boom"))
    with pytest.raises(Bitrix24Error) as excinfo:
        client.test_connection()
    assert "boom" in str(excinfo.value)


def test_test_connection_strips_webhook_token_from_errors():
    """A leaked token must never reach the caller (it's shown in the UI)."""
    url = "https://acme.bitrix24.ru/rest/1/SUPERSECRET/"
    fake = FakeBx(url, error=RuntimeError(f"403 Forbidden, url='{url}profile'"))
    client = Bitrix24Client(url, client_factory=lambda u: fake)
    with pytest.raises(Bitrix24Error) as excinfo:
        client.test_connection()
    message = str(excinfo.value)
    assert "SUPERSECRET" not in message
    assert "***" in message


def test_real_client_constructs_in_worker_thread_without_event_loop():
    """Regression: the real fast-bitrix24 client builds asyncio primitives at
    construction time, which need a current event loop (Python 3.9). Built in a
    worker thread (e.g. QThread) with no loop, this raised
    'There is no current event loop'. The client must set one up itself.
    """
    import threading

    pytest.importorskip("fast_bitrix24")
    errors = []

    def worker():
        try:
            # default factory -> real fast_bitrix24.Bitrix (offline construction)
            Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/")._client()
        except Exception as exc:  # noqa: BLE001 - we assert on the captured error
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    assert not errors, f"construction failed in worker thread: {errors!r}"


# --- controller persistence --------------------------------------------------

def test_bitrix_webhook_defaults_empty(controller):
    assert controller.bitrix_webhook() == ""


def test_set_bitrix_webhook_trims_and_roundtrips(controller):
    controller.set_bitrix_webhook("  https://acme.bitrix24.ru/rest/1/abc/  ")
    assert controller.bitrix_webhook() == "https://acme.bitrix24.ru/rest/1/abc/"


def test_bitrix_webhook_persists_across_reload(storage):
    first = AppController(storage)
    first.set_bitrix_webhook("https://acme.bitrix24.ru/rest/1/abc/")
    second = AppController(storage)
    assert second.bitrix_webhook() == "https://acme.bitrix24.ru/rest/1/abc/"


# --- import: projects & tasks listing -----------------------------------------

class RouteBx:
    """Fake whose get_all() routes by (method, filter) to canned data."""

    def __init__(self, router):
        self.router = router
        self.calls = []

    def get_all(self, method, params=None):
        self.calls.append((method, params or {}))
        return self.router(method, params or {})


def _routed(router):
    return Bitrix24Client(
        "https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: RouteBx(router)
    )


def test_current_user_id_from_profile():
    client = _routed(lambda m, p: {"ID": "7", "NAME": "X"} if m == "profile" else [])
    assert client.current_user_id() == 7


def test_list_projects_merges_dedupes_and_excludes_final_stages():
    def router(method, params):
        if method == "crm.status.list":
            return [
                {"STATUS_ID": "DT150_16:SUCCESS", "ENTITY_ID": "DYNAMIC_150_STAGE_16", "SEMANTICS": "S"},
                {"STATUS_ID": "DT150_16:FAIL", "ENTITY_ID": "DYNAMIC_150_STAGE_16", "SEMANTICS": "F"},
                {"STATUS_ID": "DT150_16:UC_X", "ENTITY_ID": "DYNAMIC_150_STAGE_16", "SEMANTICS": None},
                {"STATUS_ID": "C5:WON", "ENTITY_ID": "DEAL_STAGE", "SEMANTICS": "S"},  # other entity
            ]
        f = params.get("filter", {})
        if "ufCrm16MainIspolnitel" in f:
            return [
                {"id": 1, "title": "A", "stageId": "DT150_16:UC_X"},     # active
                {"id": 2, "title": "B", "stageId": "DT150_16:SUCCESS"},  # final -> excluded
            ]
        if "ufCrm16Supporters" in f:
            return [
                {"id": 1, "title": "A", "stageId": "DT150_16:UC_X"},     # dup
                {"id": 3, "title": "C", "stageId": "DT150_16:NEW"},      # active
            ]
        return []

    projects = _routed(router).list_projects(7)
    assert sorted(p["id"] for p in projects) == ["1", "3"]
    assert {p["title"] for p in projects} == {"A", "C"}


class CallBx:
    """Fake exposing call() (for create_portal_task)."""

    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {}
        self.error = error
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params or {}))
        if self.error is not None:
            raise self.error
        return self.result


def test_search_companies_returns_id_title_after_3_chars():
    def router(method, params):
        if method == "crm.company.list":
            return [{"ID": "10", "TITLE": "ООО Ромашка"}, {"ID": "11", "TITLE": "ООО Берёза"}]
        return []

    client = _routed(router)
    assert client.search_companies("ро") == []  # fewer than 3 chars
    # results come back sorted by title
    assert client.search_companies("ООО") == [
        {"id": "11", "title": "ООО Берёза"},
        {"id": "10", "title": "ООО Ромашка"},
    ]


def test_search_companies_does_not_pass_unsupported_get_all_params():
    """get_all() rejects 'order'/'start' — search must not send them."""
    captured = {}

    class Bx:
        def get_all(self, method, params=None):
            captured["params"] = params or {}
            return []

    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: Bx())
    client.search_companies("ООО")
    keys = {k.lower() for k in captured["params"]}
    assert "order" not in keys and "start" not in keys


def test_create_portal_task_returns_id_and_binds_company():
    fake = CallBx(result={"task": {"id": 42}})
    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: fake)
    assert client.create_portal_task("T", "D", 1, company_id="10") == "42"
    method, params = fake.calls[-1]
    assert method == "tasks.task.add"
    assert params["fields"]["TITLE"] == "T"
    assert params["fields"]["RESPONSIBLE_ID"] == 1
    assert params["fields"]["UF_CRM_TASK"] == ["CO_10"]


def test_add_project_time_builds_worklog_item():
    captured = {}

    def poster(url, method, payload):
        captured.update(method=method, payload=payload)
        return {"item": {"id": 555}}

    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", post_func=poster)
    assert client.add_project_time("5566", "2026-06-11", 2.5, "Работа", 1) == "555"
    assert captured["method"] == "crm.item.add"
    payload = captured["payload"]
    assert payload["entityTypeId"] == 1092
    fields = payload["fields"]
    assert fields["parentId150"] == 5566  # ids cast to int (Bitrix is strict)
    assert fields["assignedById"] == 1
    assert fields["ufCrm88HoursWork"] == 2.5
    assert fields["ufCrm88CommentWork"] == "Работа"
    assert fields["ufCrm88DateWork"] == "2026-06-11"


def test_add_task_time_builds_elapseditem():
    captured = {}

    def poster(url, method, payload):
        captured.update(method=method, payload=payload)
        return 777

    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", post_func=poster)
    assert client.add_task_time("50032", 9000, "Работа") == "777"
    assert captured["method"] == "task.elapseditem.add"
    payload = captured["payload"]
    assert payload["taskId"] == 50032  # int, sent as JSON (not stringified by batch)
    assert payload["arFields"] == {"SECONDS": 9000, "COMMENT_TEXT": "Работа"}


def test_complete_portal_task_calls_complete():
    fake = CallBx(result={"task": {"id": 1}})
    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: fake)
    client.complete_portal_task("50032")
    assert fake.calls[-1] == ("tasks.task.complete", {"taskId": "50032"})


def test_renew_portal_task_calls_renew():
    fake = CallBx(result={"task": {"id": 1}})
    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: fake)
    client.renew_portal_task("50032")
    assert fake.calls[-1] == ("tasks.task.renew", {"taskId": "50032"})


def test_create_portal_task_without_company_omits_binding():
    fake = CallBx(result={"task": {"id": 7}})
    client = Bitrix24Client("https://acme.bitrix24.ru/rest/1/abc/", client_factory=lambda u: fake)
    assert client.create_portal_task("T", "", 1) == "7"
    assert "UF_CRM_TASK" not in fake.calls[-1][1]["fields"]


def test_link_bitrix_sets_and_persists(storage):
    controller = AppController(storage)
    task = controller.create_task("T")
    controller.link_bitrix(task.id, {"source": "task", "id": "99"})
    assert controller.find_task(task.id).bitrix == {"source": "task", "id": "99"}
    assert AppController(storage).find_task(task.id).bitrix == {"source": "task", "id": "99"}


def test_entity_url_for_project_points_to_smart_process_item():
    url = entity_url("https://webmens.bitrix24.ru/rest/1/abc/", {"source": "project", "id": "5566"})
    assert url == "https://webmens.bitrix24.ru/crm/type/150/details/5566/"


def test_entity_url_for_task_uses_webhook_user_id():
    url = entity_url("https://webmens.bitrix24.ru/rest/12/abc/", {"source": "task", "id": "9906"})
    assert url == "https://webmens.bitrix24.ru/company/personal/user/12/tasks/task/view/9906/"


def test_entity_url_none_for_missing_or_unknown():
    assert entity_url("https://x.bitrix24.ru/rest/1/abc/", None) is None
    assert entity_url("https://x.bitrix24.ru/rest/1/abc/", {"source": "x", "id": "1"}) is None
    assert entity_url("https://x.bitrix24.ru/rest/1/abc/", {"source": "task"}) is None
    assert entity_url("not-a-webhook", {"source": "task", "id": "1"}) is None


def test_list_tasks_merges_dedupes_and_excludes_finished():
    def router(method, params):
        f = params.get("filter", {})
        if f.get("RESPONSIBLE_ID"):
            return [
                {"id": "10", "title": "T1", "status": "3"},
                {"id": "11", "title": "T2", "status": "5"},  # completed
            ]
        if f.get("ACCOMPLICE"):
            return [
                {"id": "10", "title": "T1", "status": "3"},  # dup
                {"id": "12", "title": "T3", "status": "2"},
            ]
        return []

    tasks = _routed(router).list_tasks(7)
    assert sorted(t["id"] for t in tasks) == ["10", "12"]


# --- controller: importing portal items as tasks ------------------------------

def test_create_task_stores_and_persists_bitrix_link(storage):
    controller = AppController(storage)
    task = controller.create_task("P1", bitrix={"source": "project", "id": "5566"})
    assert task.bitrix == {"source": "project", "id": "5566"}
    reloaded = AppController(storage)
    found = next(t for t in reloaded.all_tasks() if t.id == task.id)
    assert found.bitrix == {"source": "project", "id": "5566"}


def test_import_bitrix_items_creates_then_dedupes_same_day(controller):
    items = [
        {"source": "project", "id": "1", "title": "A"},
        {"source": "task", "id": "9", "title": "B"},
    ]
    imported, skipped = controller.import_bitrix_items(items)
    assert (imported, skipped) == (2, 0)

    imported2, skipped2 = controller.import_bitrix_items(items)
    assert (imported2, skipped2) == (0, 2)

    titles = [t.title for t in controller.all_tasks()]
    assert titles.count("A") == 1 and titles.count("B") == 1
