from odoo_activity.panes.stacks import filter_workers
from odoo_activity.probes import Thread, Worker


def _thread(name: str, funcs: list[str], url: str | None = None) -> Thread:
    return {
        "name": name,
        "db": None,
        "uid": None,
        "url": url,
        "query_count": None,
        "query_time": None,
        "python_time": None,
        "frames": [{"file": f"/opt/odoo/{f}.py", "line": 10, "func": f} for f in funcs],
        "idle": False,
    }


def _workers() -> list[Worker]:
    return [
        {"pid": "1", "threads": [_thread("http-1", ["dispatch", "_compute_amount"])]},
        {"pid": "2", "threads": [_thread("http-2", ["dispatch", "unlink"]), _thread("cron", ["run_job"])]},
    ]


def test_filter_workers_keeps_whole_matching_threads():
    """A frame match keeps that thread's *every* frame — a stack cut down to
    the matching lines is no longer a stack — and drops the workers left with
    no thread at all."""
    kept = filter_workers(_workers(), "COMPUTE_AMOUNT")  # case-insensitive

    assert [w["pid"] for w in kept] == ["1"]
    assert [f["func"] for f in kept[0]["threads"][0]["frames"]] == ["dispatch", "_compute_amount"]

    # thread-level fields match too, and a worker keeps only its own matches
    assert [t["name"] for w in filter_workers(_workers(), "cron") for t in w["threads"]] == ["cron"]
    assert filter_workers(_workers(), "nothing") == []
