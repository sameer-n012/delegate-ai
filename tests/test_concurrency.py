"""Many agents claiming at once, each with its own connection, must never share a task."""

from __future__ import annotations

import threading
from collections import Counter
from pathlib import Path

from delegate.core import NewTask, TaskService, connect


def test_parallel_claims_are_exclusive(db_path: Path, tmp_path: Path) -> None:
    n_tasks, n_agents = 40, 8
    setup = TaskService(connect(db_path), "human")
    root = tmp_path / "proj"
    root.mkdir()
    project = setup.set_project_state(setup.ensure_project(root).id, "in_progress")
    setup.create_tasks(project.id, [NewTask(title=f"T{i}") for i in range(n_tasks)])

    claimed: list[str] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(n_agents)

    def worker(i: int) -> None:
        svc = TaskService(connect(db_path), f"agent#{i:04d}")
        barrier.wait()
        try:
            while (task := svc.claim_task(project.id)) is not None:
                with lock:
                    claimed.append(task.ref)
        except BaseException as exc:  # surfaced via the errors list below
            errors.append(exc)
        finally:
            svc.conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_agents)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(claimed) == n_tasks
    assert max(Counter(claimed).values()) == 1
