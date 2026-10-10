#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OutlineKeys 抓取 API：抓取公开节点，按国家/协议筛选，并写入 jiedian.txt。

环境变量：COUNTRY=US、PROTOCOL=vless、LIMIT=50、MAX_PAGES=10、
ONLINE_ONLY=true、HOST=0.0.0.0、PORT=8080、OUTPUT_FILE=jiedian.txt。
API 示例：/api?country=US&protocol=vless&limit=10
"""
import html
import json
import os
import random
import re
import sys
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urljoin, urlparse

import requests

BASE_URL = os.environ.get("SOURCE_URL", "https://outlinekeys.com").rstrip("/")
DEFAULT_COUNTRY = os.environ.get("COUNTRY", "US").strip()
DEFAULT_PROTOCOL = os.environ.get("PROTOCOL", "vless").strip().lower()
DEFAULT_LIMIT = max(1, min(int(os.environ.get("LIMIT", "50")), 500))
MAX_PAGES = max(1, min(int(os.environ.get("MAX_PAGES", "10")), 100))
ONLINE_ONLY = os.environ.get("ONLINE_ONLY", "true").strip().lower() in {"1", "true", "yes", "on"}
OUTPUT_FILE = os.environ.get("OUTPUT_FILE", "jiedian.txt")
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "15"))
USER_AGENT = "Mozilla/5.0 (compatible; OutlineKeysNodeFetcher/1.0)"
PROTOCOL_PATHS = {"vless": "/protocols/vless/", "outline": "/protocols/outline/", "trojan": "/protocols/trojan/"}
URI_PREFIXES = {"vless": ("vless://",), "outline": ("ss://", "outline://"), "trojan": ("trojan://",)}
URI_RE = re.compile(r"(?i)(?:vless|trojan|ss|outline)://[^\s\"'<>]+")
KEY_PATH_RE = re.compile(r"/key/\d+/?")
session = requests.Session()
session.headers.update({"User-Agent": USER_AGENT})


class AnchorParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.current_href = None
        self.current_text = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            attrs = dict(attrs)
            self.current_href = attrs.get("href", "")
            self.current_text = []

    def handle_data(self, data):
        if self.current_href is not None:
            self.current_text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current_href is not None:
            self.anchors.append((self.current_href, " ".join(" ".join(self.current_text).split())))
            self.current_href = None
            self.current_text = []


def get_text(url):
    response = session.get(url, timeout=TIMEOUT)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or "utf-8"
    return response.text


def country_matches(node_country, requested):
    requested = (requested or "ALL").strip().lower()
    if requested in {"", "all", "*", "any"}:
        return True
    aliases = {
        "us": {"us", "usa", "united states", "united states of america", "美国"},
        "uk": {"uk", "gb", "united kingdom", "great britain", "英国"},
        "jp": {"jp", "japan", "日本"}, "sg": {"sg", "singapore", "新加坡"},
        "hk": {"hk", "hong kong", "香港"}, "ca": {"ca", "canada", "加拿大"},
        "de": {"de", "germany", "德国"}, "fr": {"fr", "france", "法国"},
    }
    wanted = aliases.get(requested, {requested})
    value = (node_country or "").strip().lower()
    return value in wanted


def parse_listing(protocol, page):
    url = urljoin(BASE_URL, f"{PROTOCOL_PATHS[protocol]}?page={page}")
    source = get_text(url)
    parser = AnchorParser()
    parser.feed(source)
    items = []
    for href, label in parser.anchors:
        absolute = urljoin(BASE_URL, href)
        path = urlparse(absolute).path
        if not KEY_PATH_RE.fullmatch(path):
            continue
        match = re.search(r"(?i)\b(Online|Offline)\b", label)
        status = match.group(1).lower() if match else "unknown"
        country = label.split("#", 1)[0].strip()
        items.append({"url": absolute, "label": label, "country": country, "status": status, "protocol": protocol})
    return items


def extract_uri(page_html, protocol):
    decoded = html.unescape(page_html).replace(r"\/", "/").replace(r"\u0026", "&")
    found = []
    for match in URI_RE.findall(decoded):
        value = match.rstrip("),.;]}，。；")
        if value.lower().startswith(URI_PREFIXES.get(protocol, ())):
            found.append(value)
    return list(dict.fromkeys(found))


def fetch_key(item):
    try:
        uris = extract_uri(get_text(item["url"]), item["protocol"])
        if not uris:
            return None
        result = dict(item)
        result["uri"] = uris[0]
        return result
    except requests.RequestException as exc:
        print(f"[WARN] 获取节点详情失败 {item['url']}: {exc}", file=sys.stderr)
        return None


def get_nodes(country=None, protocol=None, limit=None, online_only=None):
    country = (country if country is not None else DEFAULT_COUNTRY).strip()
    protocol = (protocol if protocol is not None else DEFAULT_PROTOCOL).strip().lower()
    limit = max(1, min(int(limit if limit is not None else DEFAULT_LIMIT), 500))
    online_only = ONLINE_ONLY if online_only is None else str(online_only).lower() in {"1", "true", "yes", "on"}
    protocols = list(PROTOCOL_PATHS) if protocol in {"all", "*"} else [protocol]
    if any(p not in PROTOCOL_PATHS for p in protocols):
        raise ValueError("protocol 必须是 vless、outline、trojan 或 all")

    candidates, seen_urls = [], set()
    for proto in protocols:
        for page in range(1, MAX_PAGES + 1):
            try:
                page_items = parse_listing(proto, page)
            except requests.RequestException as exc:
                print(f"[WARN] 列表抓取失败 protocol={proto} page={page}: {exc}", file=sys.stderr)
                if page == 1:
                    continue
                break
            if not page_items:
                break
            for item in page_items:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                if not country_matches(item["country"], country):
                    continue
                if online_only and item["status"] != "online":
                    continue
                candidates.append(item)

    random.shuffle(candidates)
    result, seen_uris = [], set()
    for item in candidates:
        node = fetch_key(item)
        if not node or node["uri"] in seen_uris:
            continue
        seen_uris.add(node["uri"])
        result.append(node)
        if len(result) >= limit:
            break
    return result


def write_nodes(nodes, output_file=None):
    path = output_file or OUTPUT_FILE
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
        for node in nodes:
            handle.write(node["uri"] + "\n")
    os.replace(tmp_path, path)
    return path


class APIHandler(BaseHTTPRequestHandler):
    server_version = "OutlineKeysAPI/1.0"

    def send_body(self, status, body, content_type="application/json; charset=utf-8"):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_body(200, json.dumps({"ok": True, "service": "outlinekeys-api"}, ensure_ascii=False))
            return
        if parsed.path not in {"/", "/api", "/api/nodes"}:
            self.send_body(404, json.dumps({"error": "not found"}, ensure_ascii=False))
            return
        params = parse_qs(parsed.query)
        country = params.get("country", [DEFAULT_COUNTRY])[0]
        protocol = params.get("protocol", [DEFAULT_PROTOCOL])[0]
        limit = params.get("limit", [str(DEFAULT_LIMIT)])[0]
        online_only = params.get("online_only", [str(ONLINE_ONLY).lower()])[0]
        try:
            nodes = get_nodes(country, protocol, limit, online_only)
            output_path = write_nodes(nodes)
            body = "".join(node["uri"] + "\n" for node in nodes)
            self.send_body(200, body, "text/plain; charset=utf-8")
            print(f"[API] country={country} protocol={protocol} count={len(nodes)} file={output_path}")
        except ValueError as exc:
            self.send_body(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        except Exception as exc:
            print(f"[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
            self.send_body(502, json.dumps({"error": "抓取上游节点失败", "detail": str(exc)}, ensure_ascii=False))

    def log_message(self, fmt, *args):
        print("[HTTP] " + (fmt % args))


def main():
    print(f"[START] source={BASE_URL} country={DEFAULT_COUNTRY} protocol={DEFAULT_PROTOCOL} limit={DEFAULT_LIMIT}")
    print(f"[START] API: http://{HOST}:{PORT}/api?country=US&protocol=vless&limit=10")
    server = ThreadingHTTPServer((HOST, PORT), APIHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[STOP] shutting down")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
