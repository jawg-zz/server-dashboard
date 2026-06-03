#!/bin/bash
# Start the server dashboard
cd /opt/data/workspace/server-dashboard
nohup python3 server.py > /tmp/server-dashboard.log 2>&1 &
sleep 1
curl -s http://localhost:8765/api/stats | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'Dashboard running — {d[\"network\"][\"hostname\"]} | {d[\"cpu\"][\"cores\"]} cores | {d[\"memory\"][\"used\"]} RAM | {d[\"uptime\"]} uptime')"
