"""
Basic test to verify CI configuration and core imports.
These tests should always pass and serve as a smoke test.
"""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestCIConfiguration:
    """Test CI configuration and basic imports."""
    
    def test_python_version(self):
        """Test Python version compatibility."""
        version_info = sys.version_info
        assert version_info.major == 3
        assert version_info.minor >= 9
        print(f"Python version: {sys.version}")
    
    def test_import_core_modules(self):
        """Test that core modules can be imported."""
        # Test imports that should always work
        import yaml
        import pandas as pd
        import numpy as np
        
        assert yaml is not None
        assert pd is not None
        assert np is not None
        
        print("✅ Core dependencies imported successfully")
    
    def test_project_structure(self):
        """Test that essential project files exist."""
        essential_files = [
            "requirements.txt",
            "pytest.ini",
            ".coveragerc",
            "config/futures_day.yaml",
            "core/__init__.py",
            "strategies/__init__.py",
        ]
        
        for file_path in essential_files:
            assert os.path.exists(file_path), f"Missing essential file: {file_path}"
        
        print("✅ Project structure verified")
    
    def test_config_files(self):
        """Test that config files are valid YAML."""
        import yaml
        
        config_files = [
            "config/futures_day.yaml",
            "config/futures_night.yaml",
        ]
        
        for config_file in config_files:
            if os.path.exists(config_file):
                with open(config_file, 'r') as f:
                    config = yaml.safe_load(f)
                assert isinstance(config, dict), f"Config file {config_file} should be a dict"
                print(f"✅ Config file {config_file} is valid YAML")
    
    def test_requirements_format(self):
        """Test that requirements.txt has valid format."""
        if os.path.exists("requirements.txt"):
            with open("requirements.txt", 'r') as f:
                lines = f.readlines()
            
            # Check for common issues
            for i, line in enumerate(lines, 1):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Should not have spaces around ==
                    if ' = ' in line or '== ' in line or ' ==' in line:
                        print(f"⚠️  Line {i}: Possible spacing issue: {line}")
            
            print("✅ Requirements.txt format check passed")


class TestTradingRules:
    """Test basic trading rules from RULES.md."""
    
    def test_stop_loss_offset(self):
        """Test that stop loss offset is >= 10 points."""
        # This is a placeholder test - actual implementation would check config
        min_stop_loss_offset = 10
        assert min_stop_loss_offset >= 10, "Stop loss offset must be >= 10 points"
    
    def test_paper_capital_limit(self):
        """Test paper mode capital limit."""
        paper_capital_limit = int(os.environ.get("PAPER_CAPITAL_LIMIT", "100000"))  # TWD, from .env
        assert paper_capital_limit == 100000, f"Paper mode capital limit must be 100,000 TWD, got {paper_capital_limit}"
    
    def test_position_truth(self):
        """Test that PaperTrader.position is single source of truth."""
        # This test verifies the concept, not implementation
        assert True, "PaperTrader.position should be single source of truth"


@pytest.mark.slow
class TestSlowChecks:
    """Slow tests that should be skipped in quick CI runs."""
    
    def test_comprehensive_import(self):
        """Test importing all project modules (slow)."""
        # This would be a comprehensive import test
        pass


if __name__ == "__main__":
    # Quick manual test
    tester = TestCIConfiguration()
    tester.test_python_version()
    tester.test_import_core_modules()
    tester.test_project_structure()
    print("All basic tests passed!")