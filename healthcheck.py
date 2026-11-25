#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""容器/脚本健康检查：GET /healthz，status==ok 退出码 0。"""

import json
import os
import sys
import urllib.request

port = os.environ.get("MCP_PORT", "18088")
try:
    with urllib.request.urlopen("http://127.0.0.1:%s/healthz" % port, timeout=3) as r:
        data = json.loads(r.read().decode())
    sys.exit(0 if data.get("status") == "ok" else 1)
except Exception:
    sys.exit(1)
