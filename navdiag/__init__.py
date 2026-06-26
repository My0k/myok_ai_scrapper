"""navdiag — toolkit de diagnóstico de navegación con Playwright."""

from .capture import DiagnosticSession, CapturedRequest, StepRecord

__all__ = ["DiagnosticSession", "CapturedRequest", "StepRecord"]
__version__ = "0.1.0"
