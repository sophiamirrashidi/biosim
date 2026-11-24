"""Discrete-time STL robustness (safety.stl), checked against hand-computed values."""
from __future__ import annotations

import math

from safety.stl import (And, Between, F, Flag, G, Ge, Le, Not, OnlineMonitor, Or, Pred, Until)


def sig(xs, key="x"):
    return [{key: x} for x in xs]


def test_predicate_robustness_is_distance_to_threshold():
    s = sig([1.0, 2.5, 4.0])
    assert Le("x", 3.0).trace(s) == [2.0, 0.5, -1.0]
    assert Ge("x", 3.0).trace(s) == [-2.0, -0.5, 1.0]


def test_always_is_min_and_eventually_is_max():
    s = sig([1.0, 0.5, -0.5, -1.0, 0.5, 0.0, 0.5])
    phi = Ge("x", 0.0)
    assert G(phi).rho(s) == -1.0
    assert F(phi).rho(s) == 1.0
    assert G(phi, 0, 1).rho(s) == 0.5            # min(1, 0.5)
    assert F(phi, 2, 3).rho(s) == -0.5           # max(-0.5, -1)
    assert G(phi, 0, 1).trace(s)[:3] == [0.5, -0.5, -1.0]


def test_boolean_and_quantitative_semantics_agree_on_sign():
    s = sig([3.0, 2.0, 1.0, 2.0])
    phi = G(Between("x", 0.5, 3.5))
    assert phi.rho(s) == 0.5 and phi.holds(s)
    assert not G(Between("x", 1.5, 3.5)).holds(s)


def test_negation_and_or_and():
    s = sig([2.0])
    a, b = Ge("x", 1.0), Le("x", 1.5)
    assert Not(a).rho(s) == -1.0
    assert And(a, b).rho(s) == -0.5
    assert Or(a, b).rho(s) == 1.0
    assert a.implies(b).rho(s) == -0.5          # !a | b


def test_until():
    # x stays >= 0 until y becomes >= 1 at t=2
    s = [{"x": 1.0, "y": 0.0}, {"x": 0.5, "y": 0.0}, {"x": -5.0, "y": 2.0}]
    phi = Until(Ge("x", 0.0), Ge("y", 1.0))
    assert phi.rho(s) == 0.5                    # min(y_2 - 1 = 1, x_0 = 1, x_1 = 0.5)
    assert Until(Ge("x", 0.0), Ge("y", 1.0), 0, 1).rho(s) < 0


def test_response_with_deadline():
    # whenever fault, flag within 2 ticks
    trace = [{"fault": f, "flag": g} for f, g in [(0, 0), (1, 0), (1, 0), (1, 1), (0, 0)]]
    assert G(Flag("fault").implies(F(Flag("flag"), 0, 2))).holds(trace)
    assert not G(Flag("fault").implies(F(Flag("flag"), 0, 1))).holds(trace)


def test_finite_trace_conventions():
    s = sig([1.0])
    assert G(Ge("x", 0.0), 5, 9).rho(s) == math.inf      # empty window: nothing violated it
    assert F(Ge("x", 0.0), 5, 9).rho(s) == -math.inf     # empty window: nothing satisfied it


def test_missing_value_counts_as_violation():
    s = [{"x": 1.0}, {"x": None}, {}]
    assert Ge("x", 0.0).trace(s) == [1.0, -math.inf, -math.inf]
    assert Pred("unknown", lambda smp: None).rho(s) == -math.inf


def test_online_monitor_matches_offline_and_waits_for_lookahead():
    xs = [3.0, 2.0, 1.0, 0.5, 2.0, -1.0, 2.0]
    phi = F(Ge("x", 1.5), 0, 1)
    mon = OnlineMonitor(phi, horizon=1)
    verdicts = [mon.update({"x": x}) for x in xs]
    assert verdicts[0] is None                          # tick 0 needs tick 1
    offline = phi.trace(sig(xs))
    assert verdicts[1:] == offline[:-1]
    assert mon.first_violation == 2 and mon.violated
    assert mon.worst == min(offline[:-1])
