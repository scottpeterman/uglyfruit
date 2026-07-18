"""
uglyfruit / core — the law. The vendor-agnostic determination primitives.

State / Status / Frame / Reading / classify / frame_status, plus the
Discriminator type. This is the ~200-line core Design Note 05 §1 identifies as
"shared forever": written once, imported by every vendor module and by the
registry. It is a LEAF — it imports nothing from uglyfruit, so no vendor module
can ever create an import cycle through it (a vendor imports the law; the law
imports no one). That is what lets `reading` import both the law and the vendor
modules without ordering fragility.

Stdlib only. Python 3.10+.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


# §3  State
# ──────────────────────────────────────────────────────────────────────────
class State(str, Enum):
    PRESENT = "PRESENT"   # read succeeded, capability is configured/running
    ABSENT  = "ABSENT"    # read succeeded, box positively reports it's not here
    UNREAD  = "UNREAD"    # read failed, OR read succeeded but absence is unproven

    def __str__(self) -> str:  # so f-strings print PRESENT, not State.PRESENT
        return self.value


class Status(str, Enum):
    OK   = "OK"
    WARN = "WARN"
    CRIT = "CRIT"

    def __str__(self) -> str:
        return self.value


# ──────────────────────────────────────────────────────────────────────────
# §3  Frame — the uniform health envelope. The *envelope* normalizes;
#     the payload stays vendor-shaped (→ C5).
# ──────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Frame:
    label: str          # e.g. "down adjacencies"
    value: float        # the measured number
    ceiling: float      # the number it should not exceed (often 0)
    status: Status      # OK / WARN / CRIT


def frame_status(value: float, ceiling: float) -> Status:
    """Default frame-status policy.

    §7 OPEN QUESTION: whether thresholding lives in the parser or in a frame
    policy beside it. For now it lives here, in one named place, so the slice
    can run — and so the question stays visible instead of scattered through
    per-vendor parsers. CRIT vs WARN for "over ceiling" is a deliberate
    placeholder; the down-adjacency / down-peer frame treats any breach as
    CRIT, matching the mockup's WARNING header on one down peer.
    """
    if value <= ceiling:
        return Status.OK
    return Status.CRIT


# ──────────────────────────────────────────────────────────────────────────
# §3  Reading — one per manifest entry, per poll.
# ──────────────────────────────────────────────────────────────────────────
@dataclass
class Reading:
    key: str                       # capability key, e.g. "ospf"
    state: State
    payload: Any = None            # vendor-shaped; what the box actually said
    frames: list[Frame] = field(default_factory=list)
    as_of: float = field(default_factory=time.time)  # epoch of the read
    reason: str = ""               # human/audit string: *why* this state

    def __post_init__(self) -> None:
        # The load-bearing invariant (§2.2): a frame exists ONLY on PRESENT.
        # This is the "default-to-green is impossible by construction" clause.
        # We refuse to build an object that could lie, rather than trusting a
        # caller to remember not to.
        if self.state is not State.PRESENT and self.frames:
            raise ValueError(
                f"{self.key}: frames are only valid on PRESENT readings "
                f"(got {len(self.frames)} frame(s) on {self.state})"
            )

    @property
    def age(self) -> float:
        """Seconds since the read. Drift toward Tier 1 is visible (§3)."""
        return time.time() - self.as_of

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "state": str(self.state),
            "reason": self.reason,
            "as_of": self.as_of,
            "age_s": round(self.age, 1),
            "frames": [
                {"label": f.label, "value": f.value,
                 "ceiling": f.ceiling, "status": str(f.status)}
                for f in self.frames
            ],
            # payload deliberately omitted from the digest; it's vendor-shaped
            # and can be large. Consumers fetch it from .payload when needed.
        }


# ──────────────────────────────────────────────────────────────────────────
# §4  The discriminator law — the single rule, as a primitive every
#     per-vendor discriminator routes through.
# ──────────────────────────────────────────────────────────────────────────
def classify(read_ok: bool, present: bool | None) -> tuple[State, str]:
    """The one law.

        read_ok == False                  -> UNREAD   (failed to answer)
        read_ok, present is True          -> PRESENT
        read_ok, present is False         -> ABSENT   (answered: positively gone)
        read_ok, present is None          -> UNREAD   (answered, but absence unproven)

    The tri-state `present` is the whole game. A vendor discriminator returns
    None whenever it got well-formed output it cannot read as a *positive*
    absence marker. None never becomes ABSENT — that is exactly the
    "empty defaults to UNREAD" clause, and it is why a green panel can be
    trusted by a loop that cannot route around it the way a human can (C6).
    """
    if not read_ok:
        return State.UNREAD, "read failed"
    if present is True:
        return State.PRESENT, "read and in frame"
    if present is False:
        return State.ABSENT, "read: capability positively absent"
    return State.UNREAD, "read succeeded but absence is unproven"



# The discriminator type — a (value, as_of=None) -> Reading callable. Lives with
# the law because it is typed on Reading; vendor dicts annotate against it.
Discriminator = Callable[..., Reading]   # (value, as_of=None) -> Reading