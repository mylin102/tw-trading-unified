"""Load the real production PM2 config with the new isolated release identity."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = '/Users/myllin_mini/Documents/mylin102/tw-trading-unified-release18'


@pytest.mark.parametrize('root,ok', [(ROOT, True), (ROOT.replace('release18', 'release17'), False)])
def test_actual_ecosystem_release_identity(tmp_path, root, ok):
    node = shutil.which('node')
    if not node:
        pytest.skip('node unavailable')
    config = Path(__file__).resolve().parents[2] / 'ecosystem.config.js'
    logs = tmp_path / 'runtime' / 'logs'
    logs.mkdir(parents=True)
    env = dict(os.environ, NODE_ENV='production', TRADING_PYTHON_BIN='/usr/bin/true',
               TRADING_RUNTIME_DIR=str(logs.parent), SHIOAJI_API_KEY='fixture',
               SHIOAJI_SECRET_KEY='fixture', SHIOAJI_CA_PATH='fixture',
               SHIOAJI_CA_PASSWD='fixture')
    env.pop('TRADING_LOG_DIR', None)
    code = (
        "const fs=require('fs'),Module=require('module');"
        f"const m=new Module({json.dumps(root + '/ecosystem.config.js')});"
        f"m._compile(fs.readFileSync({json.dumps(str(config))},'utf8'),m.id);"
        "console.log(JSON.stringify(m.exports.apps.map(a=>({cwd:a.cwd,root:a.env.TRADING_CANONICAL_ROOT}))));"
    )
    result = subprocess.run([node, '-e', code], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert all(a['cwd'] == ROOT and a['root'] == ROOT for a in json.loads(result.stdout))
    else:
        assert 'RUNTIME_IDENTITY_REJECTED' in result.stderr
