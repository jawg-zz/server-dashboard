#!/usr/bin/env python3
"""Tiny proxy to serve the dashboard on port 8788."""
import http.server
import urllib.request
import json

DASHBOARD_URL = "http://localhost:8765"

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            url = DASHBOARD_URL + self.path
            resp = urllib.request.urlopen(url, timeout=5)
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ('transfer-encoding', 'content-encoding', 'connection'):
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(resp.read())
        except Exception as e:
            self.send_error(502, f"Dashboard unreachable: {e}")

if __name__ == "__main__":
    port = 8788
    server = http.server.HTTPServer(("0.0.0.0", port), ProxyHandler)
    print(f"Dashboard proxy at http://0.0.0.0:{port}")
    server.serve_forever()
