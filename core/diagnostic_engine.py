"""
Diagnostic Rule Engine — Automatic health checks and decision triggers.

This engine analyzes recent performance and market drift to trigger 
protective actions via the DecisionLogger.
"""
from __future__ import annotations
import pandas as pd
from dataclasses import dataclass
from core.decision_logger import DecisionLogger

@dataclass
class DiagnosticResult:
    triggered: bool
    action: str
    reason: str
    confidence: float

class DiagnosticEngine:
    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        
    def check_health(self) -> list[DiagnosticResult]:
        """Perform all health checks and return triggered results."""
        results = []
        try:
            df = pd.read_csv(self.ledger_path)
            if df.empty: return []
            
            # 1. Edge Decay Check
            decay = self.detect_edge_decay(df)
            if decay.triggered:
                results.append(decay)
                DecisionLogger.log(
                    type="audit", session="all", action=decay.action,
                    detail=decay.reason, author="diagnostic_engine", risk_level="medium"
                )
                
            # 2. Execution Anomaly Check
            exec_issue = self.detect_execution_issues(df)
            if exec_issue.triggered:
                results.append(exec_issue)
                DecisionLogger.log(
                    type="circuit_breaker", session="all", action=exec_issue.action,
                    detail=exec_issue.reason, author="diagnostic_engine", risk_level="high"
                )
                
        except Exception as e:
            print(f"Diagnostic failure: {e}")
            
        return results

    def detect_edge_decay(self, df: pd.DataFrame, window: int = 10) -> DiagnosticResult:
        """Detect if the strategy alpha is fading based on rolling win rate."""
        exits = df[df["Action"].str.contains("EXIT", na=False)].tail(window)
        if len(exits) < window:
            return DiagnosticResult(False, "", "", 0.0)
            
        win_rate = (exits["PnL"] > 0).mean()
        if win_rate < 0.3: # Threshold 30% win rate for detection
            return DiagnosticResult(
                triggered=True,
                action="COOLDOWN",
                reason=f"Edge Decay: Win rate dropped to {win_rate:.0%} over last {window} trades.",
                confidence=0.85
            )
        return DiagnosticResult(False, "", "", 0.0)

    def detect_execution_issues(self, df: pd.DataFrame) -> DiagnosticResult:
        """Detect high slippage or excessive fees relative to PnL."""
        # This is a simplified proxy for execution issues
        recent = df.tail(10)
        total_pnl = recent["PnL"].sum()
        # If we have trades but PnL is deeply negative despite 'points' being okay, 
        # it suggests fee churning (the -9500 issue)
        if len(recent) >= 5 and total_pnl < -5000:
            return DiagnosticResult(
                triggered=True,
                action="HALT",
                reason="Execution Crisis: Deep losses detected. Possible fee churning or extreme slippage.",
                confidence=0.95
            )
        return DiagnosticResult(False, "", "", 0.0)

# Implementation Note:
# In the main monitor loop, we will periodically call:
# diag_engine.check_health()
