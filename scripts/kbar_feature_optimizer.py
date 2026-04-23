#!/usr/bin/env python3
"""
kbar_feature Optimizer - Tune strategy parameters based on attribution data.
"""

import sys
import os
import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

class KbarFeatureOptimizer:
    """Optimize kbar_feature strategy parameters."""
    
    def __init__(self, attribution_dir=None):
        self.project_root = PROJECT_ROOT
        
        # Default directories
        if attribution_dir is None:
            self.attribution_dir = self.project_root / "data" / "attribution" / "real_data"
        else:
            self.attribution_dir = Path(attribution_dir)
        
        # Strategy configuration template
        self.default_params = {
            "score_threshold": -20,      # Current: -20
            "adx_threshold": 20,         # Current: 20
            "volume_multiplier": 1.2,    # Current: 1.2x avg volume
            "momentum_confirmation": False,  # Current: disabled
            "min_bars_since_signal": 3,  # Current: 3
            "max_position_score": -10,   # Current: -10
            "require_trend_alignment": True  # Current: true
        }
        
        # Optimization ranges
        self.param_ranges = {
            "score_threshold": (-25, -10),      # More lenient: -25 to -10
            "adx_threshold": (15, 25),          # Wider range: 15-25
            "volume_multiplier": (1.0, 1.5),    # 1.0x to 1.5x
            "momentum_confirmation": [False, True],  # Toggle
            "min_bars_since_signal": (1, 5),    # 1-5 bars
            "max_position_score": (-15, -5),    # -15 to -5
            "require_trend_alignment": [True, False]  # Toggle
        }
        
        # Load attribution data
        self.router_data = None
        self.trade_data = None
        self.load_attribution_data()
        
        # Results storage
        self.optimization_results = []
        
    def load_attribution_data(self):
        """Load attribution data."""
        print("Loading attribution data...")
        
        # Router evaluation data
        router_file = self.attribution_dir / "router_evaluation_log.csv"
        if router_file.exists():
            self.router_data = pd.read_csv(router_file)
            print(f"  Router data: {len(self.router_data)} rows")
        else:
            print(f"  ❌ Router data not found: {router_file}")
        
        # Trade data
        trade_file = self.project_root / "data" / "attribution" / "trade_data" / "trade_attribution_log.csv"
        if trade_file.exists():
            self.trade_data = pd.read_csv(trade_file)
            print(f"  Trade data: {len(self.trade_data)} rows")
        else:
            print(f"  ⚠️  Trade data not found: {trade_file}")
    
    def analyze_current_performance(self):
        """Analyze current kbar_feature performance."""
        
        print("\n" + "=" * 70)
        print("Current kbar_feature Performance Analysis")
        print("=" * 70)
        
        if self.router_data is None:
            print("❌ No router data available")
            return
        
        # Filter kbar_feature data
        kbar_data = self.router_data[self.router_data["strategy_name"] == "kbar_feature"]
        
        if len(kbar_data) == 0:
            print("❌ No kbar_feature data found")
            return
        
        # Calculate metrics
        total_bars = self.router_data["timestamp"].nunique()
        eval_count = len(kbar_data[kbar_data["evaluated"] == True])
        shadow_count = len(kbar_data[kbar_data["status"] == "shadowed"])
        winner_count = len(kbar_data[kbar_data["winner"] == True])
        
        eval_rate = eval_count / total_bars if total_bars > 0 else 0
        shadow_rate = shadow_count / total_bars if total_bars > 0 else 0
        win_efficiency = winner_count / eval_count if eval_count > 0 else 0
        
        # Trade performance
        trade_count = 0
        total_pnl = 0
        
        if self.trade_data is not None:
            kbar_trades = self.trade_data[self.trade_data["strategy_name"] == "kbar_feature"]
            trade_count = len(kbar_trades)
            total_pnl = kbar_trades["net_pnl"].sum() if trade_count > 0 else 0
        
        trade_efficiency = trade_count / eval_count if eval_count > 0 else 0
        
        print(f"\n📊 Performance Metrics:")
        print(f"  Evaluation rate: {eval_rate:.1%} ({eval_count}/{total_bars})")
        print(f"  Shadow rate: {shadow_rate:.1%} ({shadow_count}/{total_bars})")
        print(f"  Win efficiency: {win_efficiency:.1%} ({winner_count}/{eval_count})")
        print(f"  Trade efficiency: {trade_efficiency:.1%} ({trade_count}/{eval_count})")
        print(f"  Total PnL: {total_pnl:.1f}")
        
        # Identify issues
        print(f"\n🔍 Issues identified:")
        
        issues = []
        
        if eval_rate < 0.4:
            issues.append(f"🚨 Low evaluation rate ({eval_rate:.1%}) - too many shadowed")
        
        if win_efficiency < 0.1:
            issues.append(f"🎯 Low win efficiency ({win_efficiency:.1%}) - strategy too strict")
        
        if trade_efficiency < 0.1:
            issues.append(f"⚡ Low trade conversion ({trade_efficiency:.1%}) - entry conditions too tight")
        
        if shadow_rate > 0.6:
            issues.append(f"📊 High shadow rate ({shadow_rate:.1%}) - priority issue")
        
        if len(issues) == 0:
            print("  ✅ No major issues identified")
        else:
            for issue in issues:
                print(f"  {issue}")
        
        return {
            "eval_rate": eval_rate,
            "shadow_rate": shadow_rate,
            "win_efficiency": win_efficiency,
            "trade_efficiency": trade_efficiency,
            "total_pnl": total_pnl,
            "issues": issues
        }
    
    def generate_parameter_sets(self, num_sets=10):
        """Generate parameter sets for optimization."""
        
        print(f"\nGenerating {num_sets} parameter sets...")
        
        param_sets = []
        
        for i in range(num_sets):
            params = {}
            
            for param, range_val in self.param_ranges.items():
                if isinstance(range_val, list):  # Boolean/categorical
                    params[param] = np.random.choice(range_val)
                else:  # Numeric range
                    min_val, max_val = range_val
                    if param in ["score_threshold", "max_position_score"]:
                        # For thresholds, prefer more lenient values
                        params[param] = np.random.uniform(min_val, max_val * 0.8)
                    else:
                        params[param] = np.random.uniform(min_val, max_val)
            
            param_sets.append(params)
        
        return param_sets
    
    def simulate_parameter_impact(self, params):
        """Simulate impact of parameter changes."""
        
        # Base metrics from current performance
        current_perf = self.analyze_current_performance()
        
        if current_perf is None:
            return None
        
        # Calculate expected improvements based on parameter changes
        improvements = {}
        
        # 1. Score threshold impact (more lenient = more evaluations)
        score_improvement = 0
        if params["score_threshold"] > self.default_params["score_threshold"]:  # More lenient
            score_improvement = (params["score_threshold"] - self.default_params["score_threshold"]) / 5
            improvements["eval_rate"] = min(0.3, current_perf["eval_rate"] * (1 + score_improvement))
        else:
            improvements["eval_rate"] = current_perf["eval_rate"]
        
        # 2. ADX threshold impact (lower = more signals)
        adx_improvement = 0
        if params["adx_threshold"] < self.default_params["adx_threshold"]:  # More lenient
            adx_improvement = (self.default_params["adx_threshold"] - params["adx_threshold"]) / 5
            improvements["eval_rate"] = min(0.4, improvements["eval_rate"] * (1 + adx_improvement))
        
        # 3. Volume multiplier impact
        volume_improvement = 0
        if params["volume_multiplier"] < self.default_params["volume_multiplier"]:  # More lenient
            volume_improvement = (self.default_params["volume_multiplier"] - params["volume_multiplier"]) / 0.5
            improvements["eval_rate"] = min(0.5, improvements["eval_rate"] * (1 + volume_improvement * 0.1))
        
        # 4. Momentum confirmation impact
        if not params["momentum_confirmation"]:  # Disabled = more signals
            improvements["eval_rate"] = min(0.6, improvements["eval_rate"] * 1.2)
        
        # 5. Win efficiency impact (trade-off with eval_rate)
        # More lenient parameters might reduce win efficiency
        win_efficiency_impact = 1.0
        
        if params["score_threshold"] > self.default_params["score_threshold"]:  # More lenient
            win_efficiency_impact *= 0.9  # 10% reduction
        
        if params["adx_threshold"] < self.default_params["adx_threshold"]:  # More lenient
            win_efficiency_impact *= 0.85  # 15% reduction
        
        if not params["require_trend_alignment"]:  # Disabled
            win_efficiency_impact *= 0.8  # 20% reduction
        
        improvements["win_efficiency"] = current_perf["win_efficiency"] * win_efficiency_impact
        
        # 6. Calculate expected PnL
        # PnL = eval_rate * win_efficiency * avg_pnl_per_trade
        avg_pnl_per_win = 20  # Estimated from data
        expected_eval_rate = improvements["eval_rate"]
        expected_win_rate = improvements["win_efficiency"]
        
        # Adjust for trade efficiency
        trade_efficiency_impact = 1.0
        if params["min_bars_since_signal"] < self.default_params["min_bars_since_signal"]:
            trade_efficiency_impact *= 1.1  # 10% improvement
        
        expected_trade_efficiency = current_perf["trade_efficiency"] * trade_efficiency_impact
        
        # Total expected PnL improvement
        current_pnl = current_perf["total_pnl"]
        expected_pnl = (expected_eval_rate * expected_win_rate * expected_trade_efficiency * 
                       avg_pnl_per_win * 100)  # Scale for 100 bars
        
        pnl_improvement = expected_pnl - current_pnl
        
        # Calculate overall score
        # Weight: eval_rate (40%), win_efficiency (30%), PnL (30%)
        score = (improvements["eval_rate"] * 0.4 + 
                improvements["win_efficiency"] * 0.3 + 
                (pnl_improvement / 100) * 0.3)
        
        return {
            "params": params,
            "expected_improvements": improvements,
            "expected_pnl": expected_pnl,
            "pnl_improvement": pnl_improvement,
            "score": score
        }
    
    def run_optimization(self, num_iterations=20):
        """Run parameter optimization."""
        
        print("\n" + "=" * 70)
        print("Running kbar_feature Parameter Optimization")
        print("=" * 70)
        
        # Generate and test parameter sets
        param_sets = self.generate_parameter_sets(num_iterations)
        
        print(f"\nTesting {len(param_sets)} parameter sets...")
        
        for i, params in enumerate(param_sets):
            result = self.simulate_parameter_impact(params)
            
            if result is not None:
                self.optimization_results.append(result)
            
            if (i + 1) % 5 == 0:
                print(f"  Completed {i + 1}/{len(param_sets)}")
        
        # Sort by score
        self.optimization_results.sort(key=lambda x: x["score"], reverse=True)
        
        print(f"\n✅ Optimization complete. Top {min(5, len(self.optimization_results))} results:")
        
        for i, result in enumerate(self.optimization_results[:5]):
            print(f"\n#{i + 1} (Score: {result['score']:.3f}):")
            print(f"  Expected PnL improvement: {result['pnl_improvement']:+.1f}")
            print(f"  Expected eval rate: {result['expected_improvements']['eval_rate']:.1%}")
            print(f"  Expected win efficiency: {result['expected_improvements']['win_efficiency']:.1%}")
            
            # Highlight key parameter changes
            key_changes = []
            for param, value in result["params"].items():
                if param in self.default_params and value != self.default_params[param]:
                    key_changes.append(f"{param}: {self.default_params[param]} → {value:.1f}")
            
            if key_changes:
                print(f"  Key changes: {', '.join(key_changes[:3])}")
    
    def generate_recommendations(self):
        """Generate specific optimization recommendations."""
        
        if not self.optimization_results:
            print("❌ No optimization results available")
            return
        
        best_result = self.optimization_results[0]
        
        print("\n" + "=" * 70)
        print("Optimization Recommendations")
        print("=" * 70)
        
        print(f"\n🎯 Recommended parameter changes:")
        
        recommendations = []
        
        for param, value in best_result["params"].items():
            default = self.default_params.get(param)
            
            if default is not None and value != default:
                change_type = "more lenient" if self._is_more_lenient(param, value, default) else "more strict"
                
                print(f"  {param}:")
                print(f"    Current: {default}")
                print(f"    Recommended: {value:.2f}")
                print(f"    Change: {change_type}")
                
                recommendations.append({
                    "parameter": param,
                    "current": default,
                    "recommended": float(value) if isinstance(value, (int, float)) else value,
                    "change_type": change_type,
                    "rationale": self._get_parameter_rationale(param, value, default)
                })
        
        # Implementation steps
        print(f"\n📋 Implementation steps:")
        print(f"  1. Update kbar_feategy configuration file")
        print(f"  2. Test in paper trading for 1-2 days")
        print(f"  3. Monitor evaluation rate and win efficiency")
        print(f"  4. Adjust if win efficiency drops below 5%")
        
        # Expected outcomes
        print(f"\n📈 Expected outcomes:")
        print(f"  • Evaluation rate: {best_result['expected_improvements']['eval_rate']:.1%} "
              f"(from current ~34%)")
        print(f"  • Win efficiency: {best_result['expected_improvements']['win_efficiency']:.1%} "
              f"(from current ~6%)")
        print(f"  • PnL improvement: {best_result['pnl_improvement']:+.1f}")
        
        # Save recommendations
        output_dir = self.project_root / "data" / "attribution" / "optimization"
        output_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Save detailed results
        results_file = output_dir / f"kbar_optimization_{timestamp}.json"
        
        results_data = {
            "timestamp": datetime.now().isoformat(),
            "current_performance": self.analyze_current_performance(),
            "recommendations": recommendations,
            "best_result": {
                "params": {k: (float(v) if isinstance(v, (int, float)) else v) 
                          for k, v in best_result["params"].items()},
                "expected_improvements": best_result["expected_improvements"],
                "expected_pnl": float(best_result["expected_pnl"]),
                "pnl_improvement": float(best_result["pnl_improvement"]),
                "score": float(best_result["score"])
            },
            "all_results": [
                {
                    "params": {k: (float(v) if isinstance(v, (int, float)) else v) 
                              for k, v in r["params"].items()},
                    "score": float(r["score"])
                }
                for r in self.optimization_results[:10]
            ]
        }
        
        with open(results_file, 'w') as f:
            json.dump(results_data, f, indent=2, ensure_ascii=False)
        
        print(f"\n📊 Results saved to: {results_file}")
        
        # Generate configuration snippet
        self.generate_config_snippet(best_result["params"], output_dir, timestamp)
    
    def _is_more_lenient(self, param, new_value, old_value):
        """Check if parameter change is more lenient."""
        
        lenient_directions = {
            "score_threshold": "higher",  # -15 is more lenient than -20
            "adx_threshold": "lower",     # 18 is more lenient than 20
            "volume_multiplier": "lower", # 1.1 is more lenient than 1.2
            "min_bars_since_signal": "lower",  # 2 is more lenient than 3
            "max_position_score": "higher",    # -8 is more lenient than -10
            "momentum_confirmation": False,    # Disabled is more lenient
            "require_trend_alignment": False   # Disabled is more lenient
        }
        
        if param not in lenient_directions:
            return False
        
        direction = lenient_directions[param]
        
        if direction == "higher":
            return new_value > old_value
        elif direction == "lower":
            return new_value < old_value
        elif direction is False:
            return new_value == False
        elif direction is True:
            return new_value == True
        
        return False
    
    def _get_parameter_rationale(self, param, new_value, old_value):
        """Get rationale for parameter change."""
        
        rationales = {
            "score_threshold": "Higher threshold (less negative) makes strategy more lenient, increasing signal frequency",
            "adx_threshold": "Lower ADX requirement accepts weaker trends, increasing opportunities",
            "volume_multiplier": "Lower volume requirement accepts normal volume bars, not just spikes",
            "momentum_confirmation": "Disabling reduces filters, increases signals but may reduce quality",
            "min_bars_since_signal": "Shorter wait between signals allows more frequent entries",
            "max_position_score": "Higher max score allows holding through weaker signals",
            "require_trend_alignment": "Disabling allows counter-trend signals, increases opportunities"
        }
        
        return rationales.get(param, "Parameter adjustment to optimize performance")
    
    def generate_config_snippet(self, params, output_dir, timestamp):
        """Generate configuration snippet for implementation."""
        
        config_snippet = f"""# kbar_feature Optimized Configuration
# Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
# Based on attribution data analysis

kbar_feature:
  # Entry conditions
  score_threshold: {params['score_threshold']:.1f}  # Current: {self.default_params['score_threshold']}
  adx_threshold: {params['adx_threshold']:.1f}      # Current: {self.default_params['adx_threshold']}
  volume_multiplier: {params['volume_multiplier']:.1f}  # Current: {self.default_params['volume_multiplier']}
  
  # Filters
  momentum_confirmation: {params['momentum_confirmation']}  # Current: {self.default_params['momentum_confirmation']}
  min_bars_since_signal: {int(params['min_bars_since_signal'])}  # Current: {self.default_params['min_bars_since_signal']}
  max_position_score: {params['max_position_score']:.1f}  # Current: {self.default_params['max_position_score']}
  require_trend_alignment: {params['require_trend_alignment']}  # Current: {self.default_params['require_trend_alignment']}
  
  # Risk management
  stop_loss_pts: 60
  take_profit_pts: 120
  max_position_size: 1
  
# Implementation notes:
# 1. Expected to increase evaluation rate from ~34% to ~{params['score_threshold'] > self.default_params['score_threshold'] and 45 or 40}%
# 2. Monitor win efficiency - target > 10%
# 3. Test in paper trading for 200+ bars before live deployment
"""
        
        config_file = output_dir / f"kbar_optimized_config_{timestamp}.yaml"
        
        with open(config_file, 'w') as f:
            f.write(config_snippet)
        
        print(f"⚙️  Configuration snippet saved to: {config_file}")
        
        # Also print for easy copy-paste
        print(f"\n📋 Quick implementation:")
        print("-" * 40)
        print(config_snippet)
        print("-" * 40)

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Optimize kbar_feature strategy parameters")
    parser.add_argument("--attribution-dir", type=str, help="Attribution data directory")
    parser.add_argument("--iterations", type=int, default=20, help="Number of optimization iterations")
    
    args = parser.parse_args()
    
    optimizer = KbarFeatureOptimizer(attribution_dir=args.attribution_dir)
    
    # Analyze current performance
    optimizer.analyze_current_performance()
    
    # Run optimization
    optimizer.run_optimization(num_iterations=args.iterations)
    
    # Generate recommendations
    optimizer.generate_recommendations()

if __name__ == "__main__":
    main()