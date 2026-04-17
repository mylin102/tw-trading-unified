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
        """Convert trade_attribution.csv to ML-ready features and labels."""
        if not self.path.exists():
            return None, None
            
        df = pd.read_csv(self.path)
        if len(df) < 50: # Need minimum sample size
            return None, None
            
        features_list = []
        labels = []
        
        for _, row in df.iterrows():
            f = json.loads(row["features"])
            o = json.loads(row["outcome"])
            
            # Feature Vector: Standardize across all strategies
            vec = [
                f.get("trend_strength", 0.5),
                f.get("volatility", 0.5),
                f.get("signal_strength", 0.5),
                f.get("vwap_distance", 0.0),
                f.get("momentum_norm", 0.0),
                # [Phase 4.3] New Alpha Features
                f.get("breakout_strength", 0.0),
                f.get("volume_spike", 1.0),
                f.get("trend_strength_raw", 0.0)
            ]
            features_list.append(vec)
            
            # Label: 1 if profitable, 0 if loss
            # Advanced: could use pnl > threshold to ignore noise
            labels.append(1 if o.get("pnl", 0) > 0 else 0)
            
        return np.array(features_list), np.array(labels)

    def train(self, strategy_filter: str | None = None):
        """Train the model and save it."""
        X, y = self.prepare_data()
        if X is None:
            print("❌ Not enough trade data to train ML model.")
            return False
            
        # [GSD Fix] Handle NaN values in X
        X_df = pd.DataFrame(X)
        if X_df.isnull().any().any():
            print(f"  ⚠️ Found {X_df.isnull().sum().sum()} NaNs in features. Filling with median.")
            X_df = X_df.fillna(X_df.median())
            X = X_df.values

        # Fit scaler and model
        X_scaled = self.scaler.fit_transform(X)
        self.model.fit(X_scaled, y)
        
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
