"""POI(구글/네이버/카카오 링크·상호·주소) -> 자전거 경로 GPX 생성 서버.

흐름: 구글맵 공유 텍스트/링크 -> 좌표 추출 -> BRouter(도로 자전거)로 경로 계산 -> GPX 반환
"""
import os
import re
import urllib.parse
import xml.etree.ElementTree as ET

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

app = Flask(__name__, static_folder="static", static_url_path="/static")

# 도로 자전거용 프로필. 바꾸고 싶으면 환경변수로 지정 (fastbike, trekking, gravel 등)
BROUTER_URL = os.environ.get("BROUTER_URL", "https://brouter.de/brouter")
BROUTER_PROFILE = os.environ.get("BROUTER_PROFILE", "fastbike")
UA = {"User-Agent": "poi2gpx/1.0 (personal use)"}

URL_RE = re.compile(r"https?://[^\s]+")


# ---------- 1. 공유 텍스트에서 URL / 장소명 / 좌표 뽑기 ----------

def extract_url(text: str):
    m = URL_RE.search(text or "")
    return m.group(0).rstrip(".,)") if m else None


def parse_coords(url: str):
    """구글맵 URL 안에서 목적지 좌표를 찾는다. 못 찾으면 None."""
    u = urllib.parse.unquote(url)
    # 1순위: 장소 핀 좌표 (!3d위도!4d경도)
    m = re.search(r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)", u)
    if m:
        return float(m.group(1)), float(m.group(2))
    # 2순위: ?q=위도,경도 / ll= / destination=
    m = re.search(r"[?&](?:q|ll|query|destination|daddr)=(-?\d+\.\d+),\s*(-?\d+\.\d+)", u)
    if m:
        return float(m.group(1)), float(m.group(2))
    # 3순위: 지도 중심 @위도,경도 (핀이 아니라 화면 중심이라 정확도 낮음)
    m = re.search(r"@(-?\d+\.\d+),(-?\d+\.\d+)", u)
    if m:
        return float(m.group(1)), float(m.group(2))
    return None


def parse_name(url: str, shared_text: str = ""):
    u = urllib.parse.unquote(url)
    m = re.search(r"/place/([^/@?]+)", u)
    if m:
        return m.group(1).replace("+", " ")
    # 공유 텍스트의 첫 줄(장소명)을 사용
    first = (shared_text or "").strip().splitlines()[0] if shared_text else ""
    if first and not first.startswith("http"):
        return first.strip()
    return "목적지"


def geocode(query: str, limit: int = 5):
    """상호/주소 -> 후보 장소 목록 (OpenStreetMap Nominatim)"""
    if not query.strip():
        return []
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": query, "format": "jsonv2", "limit": limit, "accept-language": "ko"},
        headers=UA, timeout=20,
    )
    if r.status_code != 200:
        return []
    out = []
    for x in r.json():
        parts = [p.strip() for p in x.get("display_name", "").split(",")]
        out.append({"name": x.get("name") or parts[0], "addr": ", ".join(parts[1:4]),
                    "lat": float(x["lat"]), "lon": float(x["lon"])})
    return out


def page_title(html: str):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
    if not m:
        m = re.search(r"<title>([^<]+)</title>", html)
    t = (m.group(1) if m else "").strip()
    t = re.sub(r"\s*[-:|·]\s*(네이버 ?지도|Naver Map|카카오맵|Google (지도|Maps)).*$", "", t)
    return "" if t in ("", "네이버지도", "카카오맵", "Google Maps", "Google 지도") else t


def find_candidates(text: str):
    """붙여넣은 내용(구글/네이버/카카오 링크, 상호, 주소, 좌표) -> 후보 장소 목록"""
    text = (text or "").strip()
    if not text:
        raise ValueError("링크나 장소 이름을 입력해 주세요.")
    url = extract_url(text)
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("http")]
    # 링크 앞뒤 글자도 제거 (예: "[네이버지도] 스타벅스 https://...")
    lines = [re.sub(r"^\[[^\]]+\]\s*", "", URL_RE.sub("", l)).strip() for l in lines]
    lines = [l for l in lines if l]

    cands, seen = [], set()

    def add(c):
        k = (round(c["lat"], 4), round(c["lon"], 4))
        if k not in seen:
            seen.add(k)
            cands.append(c)

    # 좌표 직접 입력
    m = re.fullmatch(r"(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)", text)
    if m:
        add({"name": "목적지", "addr": text, "lat": float(m.group(1)), "lon": float(m.group(2))})
        return cands

    title = ""
    if url:
        coords = parse_coords(url)
        final_url, html = url, ""
        if coords is None or "google" not in url:
            try:
                r = requests.get(url, headers={**UA, "Accept-Language": "ko"},
                                 allow_redirects=True, timeout=15)
                final_url, html = r.url, r.text[:300000]
            except requests.RequestException:
                pass
            coords = parse_coords(final_url)
        if coords:
            add({"name": parse_name(final_url, text), "addr": "구글맵 링크", "lat": coords[0], "lon": coords[1]})
        title = page_title(html) if html else ""
        # 구글 단축 링크는 좌표 없이 '주소 상호'만 주는 경우가 많다 -> 그 이름으로 검색
        if not coords and "google" in final_url:
            gname = parse_name(final_url, "")
            if gname != "목적지":
                lines = [gname] + lines

    # 링크에서 좌표를 못 얻었거나 상호/주소만 있는 경우: 이름/주소로 검색
    if not cands:
        queries = []
        for ln in lines[:2]:
            queries.append(ln)
            # "주소 번지 상호" 형태면 주소 / 상호로도 나눠서 시도 (예: 다산로 137-1 교촌치킨 약수점)
            m = re.match(r"^(.*?\d+(?:-\d+)?)\s+(\S.*)$", ln)
            if m:
                queries += [m.group(1), m.group(2)]
        if len(lines) > 1:
            queries.insert(0, " ".join(lines[:2]))
        if title:
            queries.append(title)
        for q in dict.fromkeys(queries):
            for c in geocode(q):
                add(c)
            if cands:
                break
    if not cands:
        raise ValueError("장소를 찾지 못했습니다. 상호와 주소를 같이 붙여넣거나 구글맵 링크를 사용해 보세요.")
    return cands[:5]


# ---------- 2. 경로 계산 + GPX ----------

def build_gpx(start, end, name: str) -> str:
    lonlats = f"{start[1]},{start[0]}|{end[1]},{end[0]}"  # BRouter는 경도,위도 순서
    r = requests.get(
        BROUTER_URL,
        params={"lonlats": lonlats, "profile": BROUTER_PROFILE,
                "alternativeidx": 0, "format": "gpx"},
        headers=UA, timeout=60,
    )
    if r.status_code != 200:
        raise RuntimeError(f"경로 계산 실패: {r.text[:200]}")
    return rename_track(r.text, name)


def rename_track(gpx_text: str, name: str) -> str:
    """GPX 안의 트랙 이름을 '목적지 이름'으로 바꾼다 (가민/IGPSPORT에서 보이는 이름)."""
    ET.register_namespace("", "http://www.topografix.com/GPX/1/1")
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    comment = re.search(r"<!--.*?-->", gpx_text, re.S)  # 거리/고도/시간 요약 주석 보존
    root = ET.fromstring(gpx_text)
    ns = {"g": "http://www.topografix.com/GPX/1/1"}
    trk = root.find("g:trk", ns)
    if trk is not None:
        n = trk.find("g:name", ns)
        if n is None:
            n = ET.SubElement(trk, "{http://www.topografix.com/GPX/1/1}name")
            trk.insert(0, n)
        n.text = name
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + (comment.group(0) + "\n" if comment else "") + body


def safe_filename(name: str) -> str:
    s = re.sub(r"[^\w\-가-힣 ]", "", name).strip().replace(" ", "_")
    return (s or "route")[:40]


# ---------- 3. 웹 엔드포인트 ----------

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory("static", "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.get("/sw.js")
def sw():
    return send_from_directory("static", "sw.js", mimetype="text/javascript")


@app.get("/api/find")
def api_find():
    try:
        return jsonify(candidates=find_candidates(request.args.get("text", "")))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.get("/api/gpx")
def api_gpx():
    try:
        slat, slon = float(request.args["slat"]), float(request.args["slon"])
        elat, elon = float(request.args["elat"]), float(request.args["elon"])
        name = request.args.get("name", "목적지")
        gpx = build_gpx((slat, slon), (elat, elon), name)
    except (KeyError, ValueError):
        return jsonify(error="좌표가 올바르지 않습니다."), 400
    except Exception as e:
        return jsonify(error=str(e)), 502
    fn = urllib.parse.quote(safe_filename(name) + ".gpx")
    return Response(gpx, mimetype="application/gpx+xml",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
