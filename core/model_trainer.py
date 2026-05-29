"""
Edge Model Trainer — Upgrades Heuristic scoring to Statistical Probabilities.
Trains a Logistic Regression model to predict P(Win | Features).
"""
import pandas as pd
import numpy as np
import json
import joblib
from pathlib import Path
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"
MODEL_DIR.mkdir(exist_ok=True)

class EdgeTrainer:
    def __init__(self, attribution_path: str):
        self.path = Path(attribution_path)
        self.scaler = StandardScaler()
        self.model = LogisticRegression(C=1.0, class_weight='balanced')
        
    def prepare_data(self):
        """Convert trade_attribution.csv to ML-ready features and weighted labels."""
        if not self.path.exists():
            return None, None
            
        df = pd.read_csv(self.path)
        if len(df) < 50:
            return None, None
            
        features_list = []
        labels = []
        sample_weights = [] # [GSD Phase 4.8] PnL Weighting
        
        for _, row in df.iterrows():
            try:
                f = json.loads(row["features"])
                o = json.loads(row["outcome"])
                
                # [GSD 4.9] Data Integrity Check: Discard samples from legacy schema
                essential_alpha = ["breakout_strength", "volume_spike", "trend_strength_raw"]
                if not all(k in f for k in essential_alpha):
                    continue # Skip legacy data to prevent model pollution
                
                pnl = float(o.get("pnl", 0))
                
                vec = [
                    f.get("trend_strength", 0.5),
                    f.get("volatility", 0.5),
                    f.get("signal_strength", 0.5),
                    f.get("vwap_distance", 0.0),
                    f.get("momentum_norm", 0.0),
                    f.get("breakout_strength", 0.0),
                    f.get("volume_spike", 1.0),
                    f.get("trend_strength_raw", 0.0)
                ]
                features_list.append(vec)
            except Exception:
                continue # Skip malformed or incompatible rows
            
            # Label: High-quality win (1) vs Significant loss (0)
            # Filter out noise around zero
            labels.append(1 if pnl > 50 else 0) # Only count wins above cost
            
            # Weight: Extreme PnL values are more important to learn
            weight = min(10.0, abs(pnl) / 100.0) + 1.0
            sample_weights.append(weight)
            
        return np.array(features_list), np.array(labels), np.array(sample_weights)

    def train(self, strategy_filter: str | None = None):
        """Train the model with sample weights."""
        X, y, weights = self.prepare_data()
        if X is None:
            print("❌ Not enough trade data.")
            return False
            
        X_df = pd.DataFrame(X)
        X_df = X_df.fillna(X_df.median())
        X = X_df.values

        X_scaled = self.scaler.fit_transform(X)
        # Apply Sample Weights to force model to learn the 'big' trades
        self.model.fit(X_scaled, y, sample_weight=weights)
        
        probs = self.model.predict_proba(X_scaled)[:, 1]
        auc = roc_auc_score(y, probs, sample_weight=weights)
        print(f"✅ PnL-Weighted Model Trained. Weighted AUC: {auc:.2f}")
        
        # Validate
        probs = self.model.predict_proba(X_scaled)[:, 1]
        auc = roc_auc_score(y, probs)
        print(f"✅ Model Trained. AUC: {auc:.2f}")
        
        # Save artifacts
        suffix = f"_{strategy_filter}" if strategy_filter else ""
        joblib.dump(self.model, MODEL_DIR / f"edge_lr{suffix}.pkl")
        joblib.dump(self.scaler, MODEL_DIR / f"scaler{suffix}.pkl")
        return True

    def get_probability(self, feature_vec: list) -> float:
        """Prediction interface for real-time use."""
        X = np.array([feature_vec])
        X_scaled = self.scaler.transform(X)
        return float(self.model.predict_proba(X_scaled)[0, 1])

# Global helper to trigger training
def update_global_edge_model():
    trainer = EdgeTrainer("logs/trade_attribution.csv")
    return trainer.train()
