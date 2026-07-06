"""
Train Random Forest Classifier — Wave 14 Pattern Discovery.
Identifies which market features predict a successful ORB breakout.
"""
import pandas as pd
import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, accuracy_score
import pickle
from pathlib import Path

def train_model():
    data_path = Path("data/optimization/orb_ml_dataset.csv")
    if not data_path.exists():
        print("❌ Dataset not found.")
        return

    df = pd.read_csv(data_path)
    
    # 1. Prepare Features (X) and Labels (y)
    # Features: dir, k_vel, lr_curve, atr_n, hour
    X = df[['dir', 'k_vel', 'lr_curve', 'atr_n', 'hour']]
    y = df['label']
    
    # 2. Split
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    
    # 3. Train
    print(f"🚀 Training Random Forest on {len(X_train)} samples...")
    rf = RandomForestClassifier(n_estimators=100, max_depth=5, random_state=42)
    rf.fit(X_train, y_train)
    
    # 4. Evaluate
    y_pred = rf.predict(X_test)
    acc = accuracy_score(y_test, y_pred)
    print(f"\n📊 Model Accuracy: {acc:.1%}")
    print("\n📝 Classification Report:")
    print(classification_report(y_test, y_pred))
    
    # 5. Feature Importance (The CEO's Gold)
    importances = pd.Series(rf.feature_importances_, index=X.columns).sort_values(ascending=False)
    print("\n🏆 FEATURE IMPORTANCE (The Winners):")
    print(importances)
    
    # 6. Save Model
    model_path = Path("models/orb_rf_v1.pkl") # Legacy or v3 clean?
    # GSD: Standardizing on V3 for this consolidation
    model_path = Path("models/orb_rf_v3_clean.pkl")
    model_path.parent.mkdir(parents=True, exist_ok=True)
    with open(model_path, "wb") as f:
        pickle.dump(rf, f)
    
    # 7. GSD: Model History & Drift Tracking (Wave 1.1 Step 6)
    history_path = Path("data/optimization/model_history.csv")
    history_path.parent.mkdir(parents=True, exist_ok=True)
    
    new_record = {
        "timestamp": pd.Timestamp.now().isoformat(),
        "accuracy": acc,
        "features": ";".join(importances.index[:3].tolist()),
        "top_feature_val": importances.iloc[0]
    }
    
    header = not history_path.exists()
    pd.DataFrame([new_record]).to_csv(history_path, mode='a', index=False, header=header)
    
    # Drift Check
    if not header:
        hist_df = pd.read_csv(history_path)
        if len(hist_df) > 1:
            prev_acc = hist_df.iloc[-2]['accuracy']
            diff = acc - prev_acc
            if diff < -0.05:
                print(f"\n🚨 MODEL DRIFT DETECTED: Accuracy dropped by {abs(diff):.1%}!")
            else:
                print(f"\n📈 Progress vs Last: {diff:+.1%}")

    print(f"\n✅ Model and metrics saved. History at {history_path}")

if __name__ == "__main__":
    train_model()
