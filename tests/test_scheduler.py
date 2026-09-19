"""When the scheduler decides a recompute is worth doing.

Each source has its own interval, and every tick used to end in a full
recompute -- twenty thousand simulated seasons -- whether or not the tick had
brought anything to recompute from. These pin the two halves of the rule: no
new inputs means no recompute, and new inputs mean one.
"""

def test_a_job_hands_the_decision_to_the_recompute(pipeline, monkeypatch):
    """The recompute is the expensive half and it is deterministic.

    Six jobs on their own timers each triggering a full recompute meant the
    app replayed twenty thousand seasons several times an hour to arrive at
    the answer already on screen. A stage that fetched nothing new cannot
    change the output of a deterministic pass over the same inputs.

    That test used to live in this loop, comparing a fingerprint either side
    of the stage. It has moved into `recompute` itself, because this loop is
    off by default and the browser's own polling never comes through here --
    so the guard covered the quiet path and left the busy one unguarded. What
    this function owes now is simply to ask, once, without forcing.
    """
    import asyncio

    from nflpicker.scheduler import Job, Scheduler

    sched = Scheduler(pipeline=pipeline)
    sched._lock = asyncio.Lock()

    asked: list[list[str]] = []
    forced: list[bool] = []

    def fake_refresh(stages=None, **kw):
        asked.append(list(stages or []))
        from nflpicker.pipeline import RefreshResult

        result = RefreshResult()
        result.record((stages or ["x"])[0], True, "nothing new")
        return result

    monkeypatch.setattr(sched.pipeline, "refresh", fake_refresh)
    monkeypatch.setattr(sched.pipeline, "recompute",
                        lambda *a, force=False, **kw: forced.append(force))
    asyncio.run(sched.run_once(Job("news", 900, ["news"])))

    assert asked == [["news"]], "the stage went through refresh"
    assert forced == [False], "the recompute was asked, and not forced"


def test_the_loop_never_forces_the_expensive_pass(pipeline, monkeypatch):
    """Naming a stage forces it, and that is exactly what this must not do.

    `refresh(["recompute"])` would have been the obvious way to write this and
    is the wrong one: an explicit stage list bypasses every interval and every
    gate, so the loop would have gone back to recomputing on each of its six
    timers regardless of whether anything had arrived.
    """
    import asyncio

    from nflpicker.scheduler import Job, Scheduler

    sched = Scheduler(pipeline=pipeline)
    sched._lock = asyncio.Lock()

    asked: list[list[str]] = []
    monkeypatch.setattr(sched.pipeline, "refresh",
                        lambda stages=None, **kw: asked.append(list(stages or []))
                        or __import__("nflpicker.pipeline", fromlist=["RefreshResult"])
                        .RefreshResult())
    monkeypatch.setattr(sched.pipeline, "recompute", lambda *a, **kw: None)
    asyncio.run(sched.run_once(Job("schedule", 300, ["schedule"])))

    assert ["recompute"] not in asked, "the recompute must not go through refresh"
