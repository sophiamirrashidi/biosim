"""Discrete-time Signal Temporal Logic with quantitative (robustness) semantics.

Follows CSCI 513 lecture 7 (Signal Temporal Logic). A signal is a list of samples, one per
tick, each a dict of named values. A formula maps (signal, t) to a robustness value rho:

    rho(f(x) >= 0, t)        = f(x_t)
    rho(not phi, t)          = -rho(phi, t)
    rho(phi and psi, t)      = min(rho(phi, t), rho(psi, t))
    rho(phi or psi, t)       = max(rho(phi, t), rho(psi, t))
    rho(G_[a,b] phi, t)      = min over t' in [t+a, t+b] of rho(phi, t')
    rho(F_[a,b] phi, t)      = max over t' in [t+a, t+b] of rho(phi, t')
    rho(phi U_[a,b] psi, t)  = max over t' in [t+a, t+b] of
                                   min(rho(psi, t'), min over t'' in [t, t') of rho(phi, t''))

rho > 0 means the trace satisfies phi, rho < 0 means it violates phi, and |rho| is the
"distance to violation" (how much the signal could change before the verdict flips).

Finite traces (lecture 7, "Testing: bounded horizon"): a window that runs past the end of the
trace is cut at the last sample. A window with no samples left gives +inf for G (nothing
violated it) and -inf for F (nothing satisfied it). A missing or None signal value gives -inf:
for safety requirements an unknown value is treated as unsafe (fail-safe default).

Boolean signals use the +1 / -1 convention: `Flag("x")` has robustness +1 when x is truthy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

Sample = Mapping[str, Any]
Signal = Sequence[Sample]
INF = math.inf


class Formula:
    def rho(self, sig: Signal, t: int = 0) -> float:
        raise NotImplementedError

    def holds(self, sig: Signal, t: int = 0) -> bool:
        return self.rho(sig, t) > 0

    def trace(self, sig: Signal) -> List[float]:
        """Robustness at every time point (the robustness signal rho(phi, t))."""
        return [self.rho(sig, t) for t in range(len(sig))]

    # operator sugar so requirements read like the math
    def __and__(self, other: "Formula") -> "Formula":
        return And(self, other)

    def __or__(self, other: "Formula") -> "Formula":
        return Or(self, other)

    def __invert__(self) -> "Formula":
        return Not(self)

    def implies(self, other: "Formula") -> "Formula":
        return Or(Not(self), other)


def _value(sample: Sample, key: str) -> Optional[float]:
    v = sample.get(key)
    if v is None or isinstance(v, bool):
        return None if v is None else float(v)
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


@dataclass(frozen=True)
class Ge(Formula):
    """key >= c, robustness x - c."""
    key: str
    c: float

    def rho(self, sig, t=0):
        v = _value(sig[t], self.key)
        return -INF if v is None else v - self.c

    def __repr__(self):
        return f"({self.key} >= {self.c:g})"


@dataclass(frozen=True)
class Le(Formula):
    """key <= c, robustness c - x."""
    key: str
    c: float

    def rho(self, sig, t=0):
        v = _value(sig[t], self.key)
        return -INF if v is None else self.c - v

    def __repr__(self):
        return f"({self.key} <= {self.c:g})"


@dataclass(frozen=True)
class Flag(Formula):
    """Boolean signal: +1 when truthy, -1 when falsy or missing."""
    key: str

    def rho(self, sig, t=0):
        return 1.0 if sig[t].get(self.key) else -1.0

    def __repr__(self):
        return self.key


@dataclass(frozen=True)
class Pred(Formula):
    """General predicate f(sample) >= 0, robustness f(sample). f may return None (unknown)."""
    name: str
    f: Callable[[Sample], Optional[float]]

    def rho(self, sig, t=0):
        v = self.f(sig[t])
        return -INF if v is None else float(v)

    def __repr__(self):
        return self.name


@dataclass(frozen=True)
class Not(Formula):
    phi: Formula

    def rho(self, sig, t=0):
        return -self.phi.rho(sig, t)

    def __repr__(self):
        return f"!{self.phi!r}"


class And(Formula):
    def __init__(self, *phis: Formula):
        self.phis = phis

    def rho(self, sig, t=0):
        return min(p.rho(sig, t) for p in self.phis)

    def __repr__(self):
        return "(" + " & ".join(map(repr, self.phis)) + ")"


class Or(Formula):
    def __init__(self, *phis: Formula):
        self.phis = phis

    def rho(self, sig, t=0):
        return max(p.rho(sig, t) for p in self.phis)

    def __repr__(self):
        return "(" + " | ".join(map(repr, self.phis)) + ")"


def _window(sig: Signal, t: int, a: int, b: Optional[int]) -> range:
    end = len(sig) - 1 if b is None else min(len(sig) - 1, t + b)
    return range(t + a, end + 1)


@dataclass(frozen=True)
class Always(Formula):
    """G_[a,b] phi. b=None means to the end of the trace."""
    phi: Formula
    a: int = 0
    b: Optional[int] = None

    def rho(self, sig, t=0):
        return min((self.phi.rho(sig, u) for u in _window(sig, t, self.a, self.b)), default=INF)

    def __repr__(self):
        return f"G[{self.a},{'T' if self.b is None else self.b}]{self.phi!r}"


@dataclass(frozen=True)
class Eventually(Formula):
    """F_[a,b] phi. b=None means to the end of the trace."""
    phi: Formula
    a: int = 0
    b: Optional[int] = None

    def rho(self, sig, t=0):
        return max((self.phi.rho(sig, u) for u in _window(sig, t, self.a, self.b)), default=-INF)

    def __repr__(self):
        return f"F[{self.a},{'T' if self.b is None else self.b}]{self.phi!r}"


@dataclass(frozen=True)
class Until(Formula):
    """phi U_[a,b] psi."""
    phi: Formula
    psi: Formula
    a: int = 0
    b: Optional[int] = None

    def rho(self, sig, t=0):
        best = -INF
        for u in _window(sig, t, self.a, self.b):
            hold = min((self.phi.rho(sig, v) for v in range(t, u)), default=INF)
            best = max(best, min(self.psi.rho(sig, u), hold))
        return best

    def __repr__(self):
        return f"({self.phi!r} U[{self.a},{'T' if self.b is None else self.b}] {self.psi!r})"


def G(phi: Formula, a: int = 0, b: Optional[int] = None) -> Always:
    return Always(phi, a, b)


def F(phi: Formula, a: int = 0, b: Optional[int] = None) -> Eventually:
    return Eventually(phi, a, b)


def Between(key: str, lo: float, hi: float) -> Formula:
    return And(Ge(key, lo), Le(key, hi))


class OnlineMonitor:
    """Runtime monitor (lecture 8, "Runtime monitoring: return a verdict based on only a finite
    portion of the trace"). Feed one sample per tick; for a safety formula G phi it reports the
    running robustness min_{t' <= t} rho(phi, t') and the first tick it went negative.

    phi itself may look ahead by up to `horizon` ticks (e.g. a response deadline F_[0,d]), so the
    verdict for tick t is only final at tick t + horizon. `update` returns the robustness of the
    newest tick whose verdict is final, or None while the first window is still open.
    """

    def __init__(self, phi: Formula, horizon: int = 0):
        self.phi = phi
        self.horizon = int(horizon)
        self.samples: List[Dict[str, Any]] = []
        self.rho_history: List[float] = []
        self.worst = INF
        self.first_violation: Optional[int] = None

    def update(self, sample: Mapping[str, Any]) -> Optional[float]:
        self.samples.append(dict(sample))
        t = len(self.samples) - 1 - self.horizon
        if t < 0:
            return None
        r = self.phi.rho(self.samples, t)
        self.rho_history.append(r)
        if r < self.worst:
            self.worst = r
        if r <= 0 and self.first_violation is None:
            self.first_violation = t
        return r

    @property
    def violated(self) -> bool:
        return self.first_violation is not None
