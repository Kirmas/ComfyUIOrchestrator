"""Live timing model of each backend: how long one job of a given node actually
took there, and whether a new job is better off waiting for a busy-but-fast
backend than starting on a free-but-slow one.

Kept in RAM on purpose, never in the DB. A duration is only comparable inside
one batch of variants -- same prompt, same resolution, same params, same models
already loaded on that machine. Across batches (let alone across days) it
describes nothing that is still true, so persisting it would buy a stale number
and a migration. The first jobs of a batch therefore go out unmeasured (an
unmeasured backend makes plan_dispatch abstain, and dispatcher's own
least-loaded rule sends one job to each free backend, which is what produces the
measurements), and every later job of that same batch is placed against what
those first ones actually clocked.

The one mistake this exists to fix: the least-loaded rule cannot see past
`max_queue_length`, so a backend that is merely *busy* is invisible to it and a
job gets handed to whichever backend happens to be free -- even when that one is
slow enough that waiting a minute for the fast one to drain would still finish
sooner. Holding out is only right while there is nothing else to fill the free
backend with, though: with several jobs still queued the slow backend earns its
keep, because the last of them would otherwise sit behind all the others on the
fast one. plan_dispatch decides that by simulating the placement of the whole
waiting batch, not just this one job (see its docstring for the worked example).
"""
import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# A measurement only claims to describe the batch it was taken in; older than
# this it counts as no measurement at all, and the backend gets re-probed.
_SAMPLE_TTL_SECONDS = 30 * 60

# Never hold a job back for longer than the wait it was promised plus this much
# slack: if the backend we were holding out for overran its own estimate, stop
# being clever and take whatever is free.
_DEFER_GRACE_SECONDS = 60

# Upper bound on the placement simulation, so a big backlog can't turn one
# dispatch decision into a long loop.
_MAX_SIMULATED_JOBS = 32


@dataclass
class _Sample:
    seconds: float
    at: float


@dataclass
class _InFlight:
    node_id: str
    started_at: float
    # False once a second job of ours was dispatched to the same backend while
    # this one was still running: the wall clock we'd measure then includes
    # queueing behind a sibling, which says nothing about how fast the machine
    # is. ComfyUI backends report max_queue_length=1 so this is normally always
    # True; API backends (max_queue_length=None) really can overlap.
    exclusive: bool


_samples: dict[tuple[str, str], _Sample] = {}
_in_flight: dict[str, dict[str, _InFlight]] = {}
# job id -> (when it was first held back, the wait it was quoted then)
_deferred_since: dict[str, tuple[float, float]] = {}


@dataclass
class BackendSlot:
    """One candidate as select_backend already sees it: is there room to submit
    to this backend right now, under its own max_queue_length."""

    backend_id: str
    free_now: bool


@dataclass
class Plan:
    # None means "hold this job back" -- it stays waiting_for_backend and
    # re-polls, rather than being dispatched to a free but slower backend.
    backend_id: str | None
    detail: str


def note_dispatch(backend_id: str, node_id: str, job_id: str) -> None:
    """Called under select_backend's own reservation lock, so a batch of
    concurrent selections sees each other's picks in the same order the
    reservation counter does."""
    running = _in_flight.setdefault(backend_id, {})
    was_idle = not running
    for other in running.values():
        other.exclusive = False
    running[job_id] = _InFlight(node_id=node_id, started_at=time.monotonic(), exclusive=was_idle)
    _deferred_since.pop(job_id, None)


def note_finish(backend_id: str, job_id: str, *, succeeded: bool) -> None:
    """Idempotent -- run_variant_job calls it from a finally, so a job that
    already recorded (or was never dispatched here) is a no-op."""
    running = _in_flight.get(backend_id)
    if running is None:
        return
    entry = running.pop(job_id, None)
    if not running:
        _in_flight.pop(backend_id, None)
    if entry is None or not succeeded or not entry.exclusive:
        # A failed/timed-out run measures the failure mode, not the machine.
        return
    now = time.monotonic()
    _samples[(backend_id, entry.node_id)] = _Sample(seconds=now - entry.started_at, at=now)
    _prune(now)
    logger.info(
        "dispatch stats: backend %s ran node %s in %.0fs", backend_id, entry.node_id, now - entry.started_at
    )


def _prune(now: float) -> None:
    for key, sample in list(_samples.items()):
        if now - sample.at > _SAMPLE_TTL_SECONDS:
            del _samples[key]
    for job_id, (deferred_at, _promised) in list(_deferred_since.items()):
        if now - deferred_at > _SAMPLE_TTL_SECONDS:
            del _deferred_since[job_id]  # cancelled, or otherwise never came back


def _duration(backend_id: str, node_id: str) -> float | None:
    sample = _samples.get((backend_id, node_id))
    if sample is None or time.monotonic() - sample.at > _SAMPLE_TTL_SECONDS:
        return None
    return sample.seconds


def _busy_for(backend_id: str) -> float:
    """Seconds until everything currently running on this backend is done, from
    each job's own measured duration minus how long it has already been going."""
    now = time.monotonic()
    total = 0.0
    for entry in _in_flight.get(backend_id, {}).values():
        expected = _duration(backend_id, entry.node_id)
        if expected is None:
            continue
        total += max(0.0, expected - (now - entry.started_at))
    return total


@dataclass
class _Sim:
    """One backend inside the placement simulation. ready_at walks forward as
    the simulation hands it jobs; free_now stays as it really is."""

    backend_id: str
    free_now: bool
    ready_at: float
    per_job: float


def plan_dispatch(node_id: str, slots: list[BackendSlot], waiting_jobs: int, job_id: str) -> Plan | None:
    """Greedy list-scheduling over the jobs still waiting, returning what should
    happen to *this* one. None means "no opinion" -- the caller falls back to its
    least-loaded rule (and, by dispatching, produces the missing measurement).

    Worked example, two backends measured at 4 and 7 minutes per job, at the
    moment the slow one frees up while the fast one still has 1 minute left:

      one job left   -- the only placement is on the fast backend (1+4=5 beats
                        the free slow one's 7), and it is busy -> hold back.
      two jobs left  -- the first still goes to the fast backend (5), but the
                        second is then 1+4+4=9 there against 7 on the free slow
                        one -> the slow one wins it, and this job takes it now.

    So the answer flips on queue depth, which is why the whole waiting set is
    simulated rather than just this job.
    """
    if len(slots) < 2:
        return None  # nothing to choose between; not worth reasoning about

    sims: list[_Sim] = []
    for slot in slots:
        per_job = _duration(slot.backend_id, node_id)
        if per_job is None:
            return None  # unmeasured -- let the least-loaded rule probe it first
        if slot.free_now:
            ready_at = 0.0
        else:
            ready_at = _busy_for(slot.backend_id)
            if ready_at <= 0:
                # Busy with something we didn't dispatch, or overrunning its own
                # estimate: assume a whole job's worth rather than "free now".
                ready_at = per_job
        sims.append(_Sim(backend_id=slot.backend_id, free_now=slot.free_now, ready_at=ready_at, per_job=per_job))

    take: _Sim | None = None
    expected_wait = 0.0
    for _ in range(max(1, min(waiting_jobs, _MAX_SIMULATED_JOBS))):
        best = min(sims, key=lambda s: (s.ready_at + s.per_job, s.ready_at))
        if best.free_now and best.ready_at <= 0:
            take = best
            break
        if not expected_wait:
            expected_wait = best.ready_at
        best.ready_at += best.per_job

    if take is not None:
        _deferred_since.pop(job_id, None)
        return Plan(take.backend_id, f"fastest finish ({take.per_job:.0f}s/job)")

    # Every placement the simulation could make landed on a backend that is busy
    # right now -- i.e. waiting for one of them beats starting on anything free.
    deferred_at, promised = _deferred_since.setdefault(job_id, (time.monotonic(), expected_wait))
    waited = time.monotonic() - deferred_at
    if waited <= promised + _DEFER_GRACE_SECONDS:
        return Plan(None, f"faster backend frees in ~{expected_wait:.0f}s, beats starting on a free slower one")

    # The backend we were holding out for blew through its own estimate. Stop
    # waiting on it -- but still take the best free one rather than dropping
    # back to a rule that would break the tie by whatever order the DB returned.
    # `promised` is deliberately the wait quoted at the *first* deferral, not the
    # current one: the current estimate shrinks towards zero as the backend gets
    # closer to done, which would otherwise make the deadline tighten under us
    # right when the wait was about to pay off.
    _deferred_since.pop(job_id, None)
    free = [sim for sim in sims if sim.free_now]
    logger.info("job %s waited %.0fs for a faster backend that never freed up -- taking any", job_id, waited)
    if not free:
        return None
    fallback = min(free, key=lambda s: s.per_job)
    return Plan(fallback.backend_id, f"gave up waiting after {waited:.0f}s")
