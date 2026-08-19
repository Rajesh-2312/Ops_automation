"""Delivery Monitor. CLAUDE.md §8 — ceiling **Alert, internal only**.

    | Delivery Monitor | attendance, usage, syllabus anomalies, risk scoring |
    | Alert (internal only) |

That is autonomy level 1 (Observe) on the §8 ladder: "read, report, alert
internally". Nothing in this package sends anything, decides anything, or names
an external party. It turns pre-fetched signals into named anomalies and a risk
score, and hands them back.

The package is pure (§3): every input arrives as a `MonitorContext` the caller
assembled from the systems of record, and no function here opens a connection.
R1 holds by construction — every number in an anomaly's `detail` came in as
structured input, so an agent explaining the alert cannot invent one.
"""

from app.services.monitoring.alerts import InternalAlert, build_alert
from app.services.monitoring.detectors import DETECTORS, Anomaly, detect_anomalies
from app.services.monitoring.scoring import RiskAssessment, assess_risk
from app.services.monitoring.signals import (
    AttendanceSignal,
    MonitorContext,
    SyllabusSignal,
    UsageSignal,
)

__all__ = [
    "Anomaly",
    "AttendanceSignal",
    "DETECTORS",
    "InternalAlert",
    "MonitorContext",
    "RiskAssessment",
    "SyllabusSignal",
    "UsageSignal",
    "assess_risk",
    "build_alert",
    "detect_anomalies",
]
