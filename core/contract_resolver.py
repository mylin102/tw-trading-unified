#!/usr/bin/env python3
"""
Contract Resolver for Shioaji Futures

Handles:
1. Near/far month contract selection
2. Rolling contract exclusion
3. Expiry date handling
4. Contract switching logic
"""

import os
import sys
from datetime import datetime, timedelta
from typing import List, Tuple, Optional, Dict, Any
import pandas as pd

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import shioaji as sj
    SHIOAJI_AVAILABLE = True
except ImportError:
    SHIOAJI_AVAILABLE = False


class ContractResolver:
    """Resolves near and far month contracts for futures trading."""
    
    def __init__(self, api: Optional[Any] = None):
        """
        Initialize contract resolver.
        
        Args:
            api: Shioaji API instance (optional)
        """
        self.api = api
        self._contract_cache: Dict[str, List[Any]] = {}
        self._last_update: Dict[str, datetime] = {}
        
    def set_api(self, api: Any) -> None:
        """Set Shioaji API instance."""
        self.api = api
        
    def get_valid_contracts(self, product: str = "MXF", force_refresh: bool = False) -> List[Any]:
        """
        Get valid contracts for a product, excluding rolling contracts.
        
        Args:
            product: Product code (TMF, TXF, etc.)
            force_refresh: Force refresh from API
            
        Returns:
            List of valid contract objects sorted by delivery date
        """
        if not self.api:
            raise ValueError("Shioaji API not set")
            
        # Check cache
        cache_key = f"{product}_valid"
        if not force_refresh and cache_key in self._contract_cache:
            last_update = self._last_update.get(cache_key)
            if last_update and (datetime.now() - last_update).seconds < 3600:  # 1 hour cache
                return self._contract_cache[cache_key]
        
        try:
            # Get all contracts for the product
            contracts = list(self.api.Contracts.Futures[product])
            
            # Filter out rolling contracts (R1, R2, R3, etc.)
            valid_contracts = []
            today = datetime.now()
            
            for contract in contracts:
                # Skip rolling contracts
                if hasattr(contract, 'code') and contract.code.endswith(('R1', 'R2', 'R3')):
                    continue
                    
                # Check if contract has delivery date
                if not hasattr(contract, 'delivery_date') or not contract.delivery_date:
                    continue
                    
                # Parse delivery date
                try:
                    delivery_date = datetime.strptime(contract.delivery_date, "%Y/%m/%d")
                    
                    # Only include contracts with delivery date >= today
                    if delivery_date >= today:
                        valid_contracts.append({
                            'contract': contract,
                            'code': contract.code,
                            'delivery_date': delivery_date,
                            'days_to_expiry': (delivery_date - today).days
                        })
                except ValueError:
                    continue
            
            # Sort by delivery date
            valid_contracts.sort(key=lambda x: x['delivery_date'])
            
            # Extract contract objects
            contract_objects = [item['contract'] for item in valid_contracts]
            
            # Update cache
            self._contract_cache[cache_key] = contract_objects
            self._last_update[cache_key] = datetime.now()
            
            return contract_objects
            
        except Exception as e:
            print(f"Error getting valid contracts for {product}: {e}")
            return []
    
    def get_near_far_contracts(self, product: str = "MXF", days_to_switch: int = 3) -> Tuple[Optional[Any], Optional[Any]]:
        """
        Get near and far month contracts with rollover handling.
        
        Args:
            product: Product code (TMF, TXF, etc.)
            days_to_switch: Days before expiry to switch to next contract
            
        Returns:
            Tuple of (near_contract, far_contract) or (None, None) if not enough contracts
        """
        valid_contracts = self.get_valid_contracts(product)
        
        if len(valid_contracts) < 2:
            print(f"Not enough valid contracts for {product}: {len(valid_contracts)}")
            return None, None
        
        # Get near contract (first in sorted list)
        near_contract = valid_contracts[0]
        
        # Check if near contract is close to expiry
        today = datetime.now()
        delivery_date = datetime.strptime(near_contract.delivery_date, "%Y/%m/%d")
        days_to_expiry = (delivery_date - today).days
        
        if days_to_expiry <= days_to_switch:
            # Switch to next contract as near
            if len(valid_contracts) < 3:
                print(f"Near contract expires in {days_to_expiry} days, but not enough contracts to switch")
                return near_contract, valid_contracts[1] if len(valid_contracts) > 1 else None
            
            print(f"Switching near contract: {near_contract.code} expires in {days_to_expiry} days")
            near_contract = valid_contracts[1]
            far_contract = valid_contracts[2]
        else:
            far_contract = valid_contracts[1]
        
        # Log contract info
        print(f"Near contract: {near_contract.code} (delivery: {near_contract.delivery_date})")
        print(f"Far contract: {far_contract.code} (delivery: {far_contract.delivery_date})")
        
        return near_contract, far_contract
    
    def get_contract_info(self, contract: Any) -> Dict[str, Any]:
        """
        Get detailed information about a contract.
        
        Args:
            contract: Contract object
            
        Returns:
            Dictionary with contract information
        """
        info = {
            'code': contract.code,
            'name': contract.name if hasattr(contract, 'name') else '',
            'delivery_date': contract.delivery_date if hasattr(contract, 'delivery_date') else '',
            'symbol': contract.symbol if hasattr(contract, 'symbol') else '',
            'category': contract.category if hasattr(contract, 'category') else '',
        }
        
        # Calculate days to expiry
        if info['delivery_date']:
            try:
                delivery_date = datetime.strptime(info['delivery_date'], "%Y/%m/%d")
                days_to_expiry = (delivery_date - datetime.now()).days
                info['days_to_expiry'] = days_to_expiry
                info['is_near_expiry'] = days_to_expiry <= 3
            except ValueError:
                info['days_to_expiry'] = None
                info['is_near_expiry'] = False
        
        return info
    
    def fetch_kbars(self, contract: Any, start_date: str, end_date: str, interval: str = "5m") -> pd.DataFrame:
        """
        Fetch K-line data for a contract and convert to DataFrame.
        
        Args:
            contract: Contract object
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)
            interval: K-line interval (5m, 15m, 1h)
            
        Returns:
            DataFrame with OHLCV data
        """
        if not self.api:
            raise ValueError("Shioaji API not set")
        
        try:
            # Fetch K-line data
            kbars = self.api.kbars(
                contract=contract,
                start=start_date,
                end=end_date,
            )
            
            # Convert to list
            kbars_list = list(kbars)
            
            if not kbars_list:
                print(f"No data returned for {contract.code}")
                return pd.DataFrame()
            
            # Check data format
            first_item = kbars_list[0]
            
            if isinstance(first_item, tuple):
                # Handle tuple format (Shioaji returns tuples)
                return self._parse_tuple_kbars(kbars_list, contract.code)
            elif hasattr(first_item, 'ts'):
                # Handle object format
                return self._parse_object_kbars(kbars_list, contract.code)
            else:
                print(f"Unknown data format for {contract.code}: {type(first_item)}")
                return pd.DataFrame()
                
        except Exception as e:
            print(f"Error fetching data for {contract.code}: {e}")
            import traceback
            traceback.print_exc()
            return pd.DataFrame()
    
    def _parse_tuple_kbars(self, kbars_list: List[tuple], contract_code: str) -> pd.DataFrame:
        """Parse tuple format Kbars."""
        # Shioaji returns tuples like ('ts', [timestamp1, timestamp2, ...]), ('Open', [open1, open2, ...]), etc.
        data_dict = {}
        
        for item in kbars_list:
            if isinstance(item, tuple) and len(item) >= 2:
                field_name = item[0]
                field_values = item[1]
                
                if isinstance(field_values, list):
                    data_dict[field_name] = field_values
        
        # Check if we have all required fields
        required_fields = ['ts', 'Open', 'High', 'Low', 'Close', 'Volume']
        for field in required_fields:
            if field not in data_dict:
                print(f"Missing field {field} in data for {contract_code}")
                return pd.DataFrame()
        
        # Create DataFrame
        df = pd.DataFrame(data_dict)
        
        # Convert timestamp from nanoseconds to datetime
        if 'ts' in df.columns and len(df) > 0:
            # Shioaji timestamps are in nanoseconds
            df['ts'] = pd.to_datetime(df['ts'], unit='ns')
        
        print(f"Parsed {len(df)} bars for {contract_code}")
        return df
    
    def _parse_object_kbars(self, kbars_list: List[Any], contract_code: str) -> pd.DataFrame:
        """Parse object format Kbars."""
        data = []
        
        for kb in kbars_list:
            # [Compat] Support both lowercase (1.3.3) and Uppercase (1.5.9)
            ts = getattr(kb, 'Timestamp', getattr(kb, 'ts', None))
            o = getattr(kb, 'Open', getattr(kb, 'open', 0))
            h = getattr(kb, 'High', getattr(kb, 'high', 0))
            l = getattr(kb, 'Low', getattr(kb, 'low', 0))
            c = getattr(kb, 'Close', getattr(kb, 'close', 0))
            v = getattr(kb, 'Volume', getattr(kb, 'volume', 0))
            
            if ts is not None:
                data.append({
                    'ts': ts,
                    'Open': o,
                    'High': h,
                    'Low': l,
                    'Close': c,
                    'Volume': v,
                })
        
        df = pd.DataFrame(data)
        
        # Convert timestamp if needed
        if 'ts' in df.columns and len(df) > 0 and isinstance(df['ts'].iloc[0], (int, float)):
            # Assume nanoseconds if large numbers
            if df['ts'].iloc[0] > 1e15:  # Nanoseconds
                df['ts'] = pd.to_datetime(df['ts'], unit='ns')
            elif df['ts'].iloc[0] > 1e9:  # Milliseconds
                df['ts'] = pd.to_datetime(df['ts'], unit='ms')
        
        print(f"Parsed {len(df)} bars for {contract_code}")
        return df
    
    def calculate_spread_metrics(self, df_near: pd.DataFrame, df_far: pd.DataFrame, 
                                window: int = 20) -> pd.DataFrame:
        """
        Calculate spread metrics for calendar spread strategy.
        
        Args:
            df_near: DataFrame with near-month data
            df_far: DataFrame with far-month data
            window: Rolling window size
            
        Returns:
            DataFrame with spread metrics
        """
        if df_near.empty or df_far.empty:
            print("Empty dataframes provided")
            return pd.DataFrame()
        
        # Merge data on timestamp
        df_merged = pd.merge(
            df_near[['ts', 'Close']],
            df_far[['ts', 'Close']],
            on='ts',
            suffixes=('_near', '_far')
        )
        
        if df_merged.empty:
            print("No overlapping timestamps between near and far contracts")
            return pd.DataFrame()
        
        # Calculate spread
        df_merged['spread'] = df_merged['Close_near'] - df_merged['Close_far']
        
        # Calculate rolling statistics
        df_merged['spread_ma'] = df_merged['spread'].rolling(window=window, min_periods=window).mean()
        df_merged['spread_std'] = df_merged['spread'].rolling(window=window, min_periods=window).std()
        
        # Calculate z-score
        safe_spread_std = df_merged['spread_std'].replace(0, pd.NA)
        df_merged['spread_z'] = (df_merged['spread'] - df_merged['spread_ma']) / safe_spread_std
        
        # Add VWAP for near month (simplified as rolling mean)
        df_merged['vwap'] = df_near['Close'].rolling(window=window, min_periods=window).mean()
        df_merged['vwap_std'] = df_near['Close'].rolling(window=window, min_periods=window).std()
        
        # Calculate VWAP z-score
        safe_vwap_std = df_merged['vwap_std'].replace(0, pd.NA)
        df_merged['vwap_z'] = (df_merged['Close_near'] - df_merged['vwap']) / safe_vwap_std
        
        # Add price vs VWAP
        df_merged['price_vs_vwap'] = df_merged['Close_near'] - df_merged['vwap']
        
        return df_merged


# Example usage
if __name__ == "__main__":
    # This is an example - you need to have Shioaji API configured
    if SHIOAJI_AVAILABLE:
        import os
        from dotenv import load_dotenv
        
        load_dotenv()
        
        api = sj.Shioaji()
        api_key = os.getenv('SHIOAJI_API_KEY')
        secret_key = os.getenv('SHIOAJI_SECRET_KEY')
        
        if api_key and secret_key:
            api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)
            
            resolver = ContractResolver(api)
            
            # Get near and far contracts
            near, far = resolver.get_near_far_contracts("TMF")
            
            if near and far:
                print(f"Selected contracts:")
                print(f"  Near: {near.code} (delivery: {near.delivery_date})")
                print(f"  Far: {far.code} (delivery: {far.delivery_date})")
                
                # Fetch data
                end_date = datetime.now().strftime("%Y-%m-%d")
                start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                
                df_near = resolver.fetch_kbars(near, start_date, end_date)
                df_far = resolver.fetch_kbars(far, start_date, end_date)
                
                if not df_near.empty and not df_far.empty:
                    df_spread = resolver.calculate_spread_metrics(df_near, df_far)
                    print(f"Spread metrics calculated: {len(df_spread)} rows")
                    
                    if not df_spread.empty:
                        print(f"Latest spread: {df_spread['spread'].iloc[-1]:.2f}")
                        print(f"Latest spread z-score: {df_spread['spread_z'].iloc[-1]:.2f}")
                        print(f"Latest VWAP z-score: {df_spread['vwap_z'].iloc[-1]:.2f}")
    else:
        print("Shioaji not available - this is just a demonstration")