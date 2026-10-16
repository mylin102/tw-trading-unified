    def _check_options_contract_staleness(self):
        """[Phase 1 Fix] Check if options ticks are stale and attempt recovery."""
        if self.dry_run or not self.api:
            return
        
        secs_since_tick = time.time() - self.last_tick_at
        if secs_since_tick < 120:  # Less than 2 min, all good
            return
        
        console.print(f"[yellow]⚠️ Options data stale for {secs_since_tick/60:.1f} min, checking contracts...[/yellow]")
        
        # Check if current contracts have expired
        today_str = datetime.date.today().strftime("%Y/%m/%d")
        needs_refresh = False

        for side, contract in [("C", self.active_contracts.get("C")), ("P", self.active_contracts.get("P"))]:
            if not contract:
                needs_refresh = True
                break
            # GSD: Compare standardized YYYY/MM/DD strings
            dd = getattr(contract, 'delivery_date', None)
            if dd and isinstance(dd, str):
                # Clean Shioaji delivery_date format if needed
                dd_clean = dd.replace("-", "/")
                if dd_clean <= today_str:
                    console.print(f"[yellow]⚠️ {side} contract {contract.code} expired (delivery: {dd})[/yellow]")
                    needs_refresh = True
        
        if needs_refresh:
            console.print("[bold yellow]🔄 Refreshing options contracts...[/bold yellow]")
            try:
                # Clear existing to trigger resolve in next loop or immediate
                for side in ["C", "P"]:
                    self.active_contracts[side] = None
                self._resolve_active_contracts()
                
                # Re-subscribe will happen in the next iteration via run() logic
                # or we can force it here
                self.last_tick_at = time.time() # Reset timer to prevent loop
            except Exception as e:
                console.print(f"[red]Refresh contracts error:[/red] {e}")
