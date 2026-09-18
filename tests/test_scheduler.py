"""When the scheduler decides a recompute is worth doing.

Each source has its own interval, and every tick used to end in a full
recompute -- twenty thousand simulated seasons -- whether or not the tick had
brought anything to recompute from. These pin the two halves of the rule: no
new inputs means no recompute, and new inputs mean one.
"""

def test_a_job_that_brought_nothing_new_does_not_recompute(pipeline, monkeypatch):
    """The recompute is the expensive half and it is deterministic.

    Six jobs on their own timers each triggering a full recompute meant the
    app replayed twenty thousand seasons several times an hour to arrive at
    the answer already on screen. A stage that fetched nothing new cannot
    change the output of a deterministic pass over the same inputs.
    """
    import asyncio

    from nflpicker.scheduler import Job, Scheduler

    sched = Scheduler(pipeline=pipeline)
    sched._lock = asyncio.Lock()
    monkeypatch.setattr(sched, "recompute_overdue", lambda: False)

    asked: list[list[str]] = []

    def fake_refresh(stages=None, **kw):
        asked.append(list(stages or []))
        from nflpicker.pipeline import RefreshResult

        result = RefreshResult()
        result.record((stages or ["x"])[0], True, "nothing new")
        return result

    monkeypatch.setattr(sched.pipeline, "refresh", fake_refresh)
    asyncio.run(sched.run_once(Job("news", 900, ["news"])))

    assert asked == [["news"]], "the stage ran; the recompute did not"


def test_new_data_does_recompute(pipeline, monkeypatch):
    import asyncio

    from nflpicker.scheduler import Job, Scheduler

    sched = Scheduler(pipeline=pipeline)
    sched._lock = asyncio.Lock()
    monkeypatch.setattr(sched, "recompute_overdue", lambda: False)

    prints = iter([("a",), ("b",)])
    monkeypatch.setattr(sched, "inputs_fingerprint", lambda: next(prints))

    asked: list[list[str]] = []

    def fake_refresh(stages=None, **kw):
        asked.append(list(stages or []))
        from nflpicker.pipeline import RefreshResult

        result = RefreshResult()
        result.record((stages or ["x"])[0], True, "a score landed")
        return result

    monkeypatch.setattr(sched.pipeline, "refresh", fake_refresh)
    asyncio.run(sched.run_once(Job("schedule", 300, ["schedule"])))

    assert asked == [["schedule"], ["recompute"]]
