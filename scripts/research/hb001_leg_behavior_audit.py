# HB-001: Leg Behavior Audit & Asymmetric Exit Policy Evaluation Script
# Purpose: Quantify Near vs Far return variance, lead-lag cross-correlation,
# price discovery share, and evaluate 4-policy counterfactual performance.

import json
import math
import statistics
from collections import defaultdict

def calculate_variance(vals):
    if len(vals) < 2:
        return 0.0
    m = statistics.mean(vals)
    return sum((x - m) ** 2 for x in vals) / (len(vals) - 1)

def calculate_correlation(x, y):
    if len(x) != len(y) or len(x) < 2:
        return 0.0
    mx, my = statistics.mean(x), statistics.mean(y)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = math.sqrt(sum((xi - mx) ** 2 for xi in x) * sum((yi - my) ** 2 for yi in y))
    return num / den if den > 1e-9 else 0.0

def run_leg_behavior_audit():
    # 1. Load paired 1-min data from tmf_near and tmf_far
    near_file = 'data/tmf_near_20260728.csv'
    far_file = 'data/tmf_far_20260728.csv'
    
    near_prices = {}
    far_prices = {}
    
    with open(near_file) as f:
        header = f.readline()
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 5:
                ts, close = parts[0], float(parts[4])
                near_prices[ts] = close
                
    with open(far_file) as f:
        header = f.readline()
        for line in f:
            parts = line.strip().split(',')
            if len(parts) >= 5:
                ts, close = parts[0], float(parts[4])
                far_prices[ts] = close
                
    common_ts = sorted(list(set(near_prices.keys()).intersection(set(far_prices.keys()))))
    print(f'Matched 1-min data points: {len(common_ts)}')
    
    near_returns = []
    far_returns = []
    for i in range(1, len(common_ts)):
        t0, t1 = common_ts[i-1], common_ts[i]
        dn = near_prices[t1] - near_prices[t0]
        df = far_prices[t1] - far_prices[t0]
        near_returns.append(dn)
        far_returns.append(df)
        
    var_near = calculate_variance(near_returns)
    var_far = calculate_variance(far_returns)
    var_ratio = var_near / var_far if var_far > 1e-9 else 0.0
    
    print(f'\n--- 1. Return Variance Comparison (1-min returns) ---')
    print(f'  Near Return Variance: {var_near:.4f}')
    print(f'  Far Return Variance:  {var_far:.4f}')
    print(f'  Variance Ratio (Near / Far): {var_ratio:.2f}x')
    
    # 2. Lead-Lag Cross-Correlation
    print(f'\n--- 2. Cross-Correlation & Lead-Lag Analysis rho(dNear_t, dFar_{{t+tau}}) ---')
    best_tau, max_corr = 0, -1.0
    corrs = {}
    for tau in range(-5, 6):
        if tau < 0:
            x = near_returns[-tau:]
            y = far_returns[:len(far_returns)+tau]
        elif tau > 0:
            x = near_returns[:len(near_returns)-tau]
            y = far_returns[tau:]
        else:
            x = near_returns
            y = far_returns
            
        c = calculate_correlation(x, y)
        corrs[tau] = c
        if c > max_corr:
            max_corr = c
            best_tau = tau
        sign = '+' if tau >= 0 else ''
        print(f'  tau = {sign}{tau} min: corr = {c:.4f}')
        
    print(f'  Peak Correlation at tau = {best_tau} min (corr = {max_corr:.4f})')
    if best_tau > 0:
        print(f'  Interpretation: Near LEADS Far by {best_tau} minute(s).')
    elif best_tau < 0:
        print(f'  Interpretation: Far LEADS Near by {-best_tau} minute(s).')
    else:
        print(f'  Interpretation: Synchronous movements at 1-min resolution.')

    # 3. 4-Policy Counterfactual Performance Comparison on 53 Clean Trades
    fills_path = 'logs/mts_trade_fills.jsonl'
    trades = defaultdict(lambda: {'entries': [], 'releases': [], 'exits': []})
    
    with open(fills_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            tid = r['trade_id']
            fill_type = r['fill_type'].lower()
            if fill_type == 'entry':
                trades[tid]['entries'].append(r)
            elif fill_type == 'release':
                trades[tid]['releases'].append(r)
            elif fill_type == 'exit':
                trades[tid]['exits'].append(r)

    clean_trades = []
    for tid, data in trades.items():
        entries, releases, exits = data['entries'], data['releases'], data['exits']
        if not entries or len(releases) != 1 or len(exits) != 1:
            continue
        rel, ex = releases[0], exits[0]
        rel_pnl = rel.get('realized_pnl', 0.0) or 0.0
        trail_pnl = ex.get('realized_pnl', 0.0) or 0.0
        actual_net = rel_pnl + trail_pnl
        if abs(actual_net) > 100000:
            continue
            
        near_pnl_rel = rel.get('near_pnl')
        far_pnl_rel = rel.get('far_pnl')
        if near_pnl_rel is None or far_pnl_rel is None:
            continue
            
        combined_rel = near_pnl_rel + far_pnl_rel
        rel_contract = rel.get('contract')
        
        # Policy 3: Asymmetric Exit Policy
        # If Release FAR (leave Near) -> Combined Exit @ Release
        # If Release NEAR (leave Far) -> Keep Single-Leg (Actual)
        if rel_contract == 'FAR':
            asym_net = combined_rel
        else:
            asym_net = actual_net
            
        clean_trades.append({
            'trade_id': tid,
            'rel_contract': rel_contract,
            'actual_net': actual_net,
            'combined_net': combined_rel,
            'asym_net': asym_net
        })

    print(f'\n--- 3. 4-Policy Counterfactual Performance Comparison (N = {len(clean_trades)}) ---')
    act_pnls = [t['actual_net'] for t in clean_trades]
    comb_pnls = [t['combined_net'] for t in clean_trades]
    asym_pnls = [t['asym_net'] for t in clean_trades]
    
    print(f'  Policy 1 (Actual Single-Leg Release):')
    print(f'    Mean PnL:   {statistics.mean(act_pnls):.1f} NTD | Med PnL: {statistics.median(act_pnls):.1f} NTD')
    print(f'    Win Rate:   {sum(1 for x in act_pnls if x > 0)/len(act_pnls)*100:.1f}%')
    
    print(f'  Policy 2 (All Combined Exit @ Release):')
    print(f'    Mean PnL:   {statistics.mean(comb_pnls):.1f} NTD | Med PnL: {statistics.median(comb_pnls):.1f} NTD')
    print(f'    Win Rate:   {sum(1 for x in comb_pnls if x > 0)/len(comb_pnls)*100:.1f}%')
    print(f'    Mean Delta vs Policy 1: {statistics.mean(comb_pnls) - statistics.mean(act_pnls):+.1f} NTD')
    
    print(f'  Policy 3 (Asymmetric Policy: Release FAR->Combined, Release NEAR->Single-Leg):')
    print(f'    Mean PnL:   {statistics.mean(asym_pnls):.1f} NTD | Med PnL: {statistics.median(asym_pnls):.1f} NTD')
    print(f'    Win Rate:   {sum(1 for x in asym_pnls if x > 0)/len(asym_pnls)*100:.1f}%')
    print(f'    Mean Delta vs Policy 1: {statistics.mean(asym_pnls) - statistics.mean(act_pnls):+.1f} NTD')

if __name__ == '__main__':
    run_leg_behavior_audit()
