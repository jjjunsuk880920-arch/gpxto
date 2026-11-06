"""POI(구글/네이버/카카오 링크·상호·주소) -> 자전거 경로 GPX 생성 서버.

흐름: 구글맵 공유 텍스트/링크 -> 좌표 추출 -> BRouter(도로 자전거)로 경로 계산 -> GPX 반환
"""
import json
import os
import re
import urllib.parse

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


def haversine_km(a, b):
    from math import radians, sin, cos, asin, sqrt
    la1, lo1, la2, lo2 = map(radians, (a[0], a[1], b[0], b[1]))
    h = sin((la2 - la1) / 2) ** 2 + cos(la1) * cos(la2) * sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * asin(sqrt(h))


def geocode(query: str, near=None, limit: int = 5):
    """상호/주소 -> 후보 장소 목록. 현재 위치(near)가 있으면 가까운 곳을 우선한다.
    Photon(위치 가중치 지원) + Nominatim 결과를 합친다."""
    if not query.strip():
        return []
    out = []
    try:
        params = {"q": query, "limit": limit}
        if near:
            params.update(lat=near[0], lon=near[1])
            if not (32 < near[0] < 39.5 and 123 < near[1] < 132.5):  # 한국 밖이면 영어 표기
                params["lang"] = "en"
        r = requests.get("https://photon.komoot.io/api/", params=params, headers=UA, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                pr, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
                addr = ", ".join(x for x in (pr.get("street"), pr.get("district") or pr.get("city") or pr.get("county"),
                                             pr.get("state"), pr.get("country")) if x)
                out.append({"name": pr.get("name") or addr or query, "addr": addr, "lat": lat, "lon": lon})
    except (requests.RequestException, ValueError, KeyError, AttributeError, TypeError):
        pass
    try:
        params = {"q": query, "format": "jsonv2", "limit": limit, "accept-language": "ko"}
        if near:  # 현재 위치 주변(약 ±1도)을 우선 (경계 밖 결과도 허용)
            params["viewbox"] = f"{near[1]-1},{near[0]+1},{near[1]+1},{near[0]-1}"
        r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=20)
        if r.status_code == 200:
            for x in r.json():
                parts = [p.strip() for p in x.get("display_name", "").split(",")]
                out.append({"name": x.get("name") or parts[0], "addr": ", ".join(parts[1:4]),
                            "lat": float(x["lat"]), "lon": float(x["lon"])})
    except (requests.RequestException, ValueError, KeyError, AttributeError, TypeError):
        pass
    if near:
        for c in out:
            c["km"] = round(haversine_km(near, (c["lat"], c["lon"])), 1)
    return out


def page_title(html: str):
    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html)
    if not m:
        m = re.search(r"<title>([^<]+)</title>", html)
    t = (m.group(1) if m else "").strip()
    t = re.sub(r"\s*[-:|·]\s*(네이버 ?지도|Naver Map|카카오맵|Google (지도|Maps)).*$", "", t)
    return "" if t in ("", "네이버지도", "카카오맵", "Google Maps", "Google 지도") else t


def find_candidates(text: str, near=None):
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
        k = (round(c["lat"], 3), round(c["lon"], 3))
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
            for c in geocode(q, near):
                add(c)
            if cands:
                break
    if not cands:
        raise ValueError("장소를 찾지 못했습니다. 상호와 주소를 같이 붙여넣거나 구글맵 링크를 사용해 보세요.")
    return cands[:5]


# ---------- 2. 경로 계산 + 도로 검사 + GPX ----------

MAIN = {"trunk", "trunk_link", "primary", "primary_link"}            # 국도·간선
LOCAL = {"secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
         "residential", "service", "living_street", "road"}          # 지방도·소로
OFFROAD = {"cycleway", "path", "footway", "pedestrian", "steps", "bridleway"}
UNPAVED = {"unpaved", "gravel", "dirt", "ground", "sand", "mud", "compacted",
           "fine_gravel", "grass", "earth", "pebblestone"}
MAX_RETRY = 3


def _tags(s: str) -> dict:
    return dict(t.split("=", 1) for t in (s or "").split() if "=" in t)


def is_forbidden(t: dict) -> bool:
    """자동차전용도로/고속도로 등 자전거가 들어가면 안 되는 도로"""
    hw = t.get("highway", "")
    if hw in ("motorway", "motorway_link") or t.get("motorroad") == "yes":
        return True
    return t.get("bicycle") in ("no", "dismount") and hw in MAIN


def _brouter_geojson(start, end, nogos):
    params = {"lonlats": f"{start[1]},{start[0]}|{end[1]},{end[0]}", "profile": BROUTER_PROFILE,
              "alternativeidx": 0, "format": "geojson"}
    if nogos:
        params["nogos"] = "|".join(f"{lo},{la},{rad}" for lo, la, rad in nogos)
    r = requests.get(BROUTER_URL, params=params, headers=UA, timeout=90)
    if r.status_code != 200:
        raise RuntimeError(f"경로 계산 실패: {r.text[:200]}")
    return r.json()


def analyze_roads(feature):
    """BRouter 구간 정보 -> 도로 종류별 거리(km), 금지 도로 구간 위치"""
    msgs = feature["properties"].get("messages") or []
    if not msgs:
        return None, []
    ix = {h: i for i, h in enumerate(msgs[0])}
    km = {"main": 0.0, "local": 0.0, "offroad": 0.0, "forbidden": 0.0, "unpaved": 0.0}
    bad = []
    for m in msgs[1:]:
        t = _tags(m[ix["WayTags"]]); d = int(m[ix["Distance"]]) / 1000
        hw = t.get("highway", "")
        if is_forbidden(t):
            km["forbidden"] += d
            bad.append((int(m[ix["Longitude"]]) / 1e6, int(m[ix["Latitude"]]) / 1e6))
        elif hw in MAIN: km["main"] += d
        elif hw in OFFROAD: km["offroad"] += d
        else: km["local"] += d
        if t.get("surface") in UNPAVED or hw == "track" or t.get("tracktype") in ("grade3", "grade4", "grade5"):
            km["unpaved"] += d
    return {k: round(v, 1) for k, v in km.items()}, bad


def _fmt_time(sec: int) -> str:
    h, rem = divmod(int(sec), 3600); m, s = divmod(rem, 60)
    return (f"{h}h " if h else "") + (f"{m}m " if h or m else "") + f"{s}s"


def to_gpx(feature, name: str) -> str:
    from xml.sax.saxutils import escape
    pr = feature["properties"]
    pts = "".join(
        f'<trkpt lat="{c[1]:.6f}" lon="{c[0]:.6f}">' + (f"<ele>{c[2]:.1f}</ele>" if len(c) > 2 else "") + "</trkpt>"
        for c in feature["geometry"]["coordinates"])
    # 거리/고도/시간 요약 주석 (화면 표시에 사용)
    note = (f"<!-- track-length = {pr.get('track-length', 0)} filtered ascend = {pr.get('filtered ascend', 0)} "
            f"plain-ascend = {pr.get('plain-ascend', 0)} time={_fmt_time(pr.get('total-time', 0))} -->")
    n = escape(name)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n' + note + "\n"
            '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="GPXTo (BRouter)">'
            f"<metadata><name>{n}</name></metadata><trk><name>{n}</name><trkseg>{pts}</trkseg></trk></gpx>")


def build_route(start, end, name: str):
    """도로 종류를 검사하고, 자동차전용도로가 섞이면 그 구간을 피해 다시 계산한다.
    반환: (gpx 문자열, 도로 요약 dict)"""
    nogos, info = [], None
    for attempt in range(MAX_RETRY + 1):
        feature = _brouter_geojson(start, end, nogos)["features"][0]
        info, bad = analyze_roads(feature)
        if info is None or not bad:
            break
        if attempt == MAX_RETRY:
            break
        # 금지 도로 지점 주변(반경 60m)을 막고 다시 계산
        for lo, la in bad[::max(1, len(bad) // 8)]:
            nogos.append((round(lo, 6), round(la, 6), 60))
    warn = []
    if info is None:
        warn.append("도로 종류를 확인하지 못했습니다. 경로를 직접 확인하세요.")
    elif info["forbidden"] > 0:
        warn.append(f"자동차전용도로 등 진입 불가 도로가 약 {info['forbidden']}km 포함됐습니다. "
                    "출발지/목적지가 그 도로 위에 있는지 확인하세요.")
    if info and info["unpaved"] >= 0.5:
        warn.append(f"비포장 추정 구간이 약 {info['unpaved']}km 있습니다.")
    return to_gpx(feature, name), {"roads": info, "warnings": warn}


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
        near = None
        try:
            near = (float(request.args["lat"]), float(request.args["lon"]))
        except (KeyError, ValueError):
            pass
        return jsonify(candidates=find_candidates(request.args.get("text", ""), near))
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.get("/api/gpx")
def api_gpx():
    try:
        slat, slon = float(request.args["slat"]), float(request.args["slon"])
        elat, elon = float(request.args["elat"]), float(request.args["elon"])
        name = request.args.get("name", "목적지")
        gpx, summary = build_route((slat, slon), (elat, elon), name)
    except (KeyError, ValueError):
        return jsonify(error="좌표가 올바르지 않습니다."), 400
    except Exception as e:
        return jsonify(error=str(e)), 502
    fn = urllib.parse.quote(safe_filename(name) + ".gpx")
    return Response(gpx, mimetype="application/gpx+xml",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{fn}",
                             "X-Road-Summary": urllib.parse.quote(json.dumps(summary, ensure_ascii=False))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
