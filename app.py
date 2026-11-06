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


class NeedPin(Exception):
    """이름/주소로는 정확한 위치를 못 찾음 -> 화면에서 지도에 직접 찍게 한다."""
    def __init__(self, message, hint=None, name=None):
        super().__init__(message)
        self.hint, self.name = hint, name


# 이름이 맞는지 검사할 때 무시할 흔한 단어
GENERIC = {"hotel", "boutique", "resort", "restaurant", "cafe", "coffee", "shop", "store", "road", "street",
           "soi", "the", "and", "house", "hostel", "inn", "villa", "spa", "mall", "center", "centre", "lane",
           "district", "province", "city", "chiang", "mai", "bangkok", "seoul", "thailand", "korea"}


def relevant(query: str, c: dict) -> bool:
    """영문 검색어의 핵심 단어가 결과에 하나도 없으면 엉뚱한 곳(예: 같은 'Boutique Hotel'의 호주 호텔)으로 보고 제외"""
    sig = [t for t in re.findall(r"[a-z]{3,}", query.lower()) if t not in GENERIC]
    if not sig:
        return True
    blob = (c.get("name", "") + " " + c.get("addr", "")).lower()
    return any(t in blob for t in sig)


def geocode(query: str, near=None, limit: int = 5, within=None):
    """상호/주소 -> 후보 장소 목록 (영어 표기).
    near=(lat,lon): 가까운 곳 우선, within=(lat,lon,km): 그 반경 밖 결과는 제외.
    Photon + Nominatim 결과를 합치고, 이름이 안 맞는 결과는 걸러낸다."""
    if not query.strip():
        return []
    center = near or (within[:2] if within else None)
    out = []
    try:
        params = {"q": query, "limit": limit, "lang": "en"}
        if center:
            params.update(lat=center[0], lon=center[1])
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
        params = {"q": query, "format": "jsonv2", "limit": limit, "accept-language": "en", "namedetails": 1}
        if center:
            params["viewbox"] = f"{center[1]-1},{center[0]+1},{center[1]+1},{center[0]-1}"
        r = requests.get("https://nominatim.openstreetmap.org/search", params=params, headers=UA, timeout=20)
        if r.status_code == 200:
            for x in r.json():
                parts = [p.strip() for p in x.get("display_name", "").split(",")]
                nd = x.get("namedetails") or {}
                out.append({"name": nd.get("name:en") or parts[0], "addr": ", ".join(parts[1:4]),
                            "lat": float(x["lat"]), "lon": float(x["lon"])})
    except (requests.RequestException, ValueError, KeyError, AttributeError, TypeError):
        pass
    out = [c for c in out if relevant(query, c)]
    if within:
        out = [c for c in out if haversine_km(within[:2], (c["lat"], c["lon"])) <= within[2]]
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


COUNTRY_WORDS = {"태국", "thailand", "대한민국", "south korea", "korea", "일본", "japan", "베트남", "vietnam",
                 "대만", "taiwan", "라오스", "laos", "말레이시아", "malaysia", "싱가포르", "singapore"}


def fid_anchor(url: str):
    """구글 장소 고유번호(!1s0x...:0x...)의 앞부분은 S2 셀 번호라, 대략 몇 km 오차의 위치를 복원할 수 있다.
    정확한 좌표는 아니고 '이 동네' 정도의 기준점으로만 쓴다."""
    m = re.search(r"0x([0-9a-f]{8,16}):0x[0-9a-f]+", urllib.parse.unquote(url))
    if not m:
        return None
    try:
        import s2sphere
        cell = s2sphere.CellId(int(m.group(1), 16))
        if not cell.is_valid():
            return None
        ll = cell.to_lat_lng()
        return round(ll.lat().degrees, 5), round(ll.lng().degrees, 5)
    except Exception:
        return None


def parse_google_place(url: str):
    """/maps/place/이름,+주소,+동네,+도시+우편번호+국가/ -> (이름, 동네 문자열)"""
    m = re.search(r"/place/([^/@?]+)", urllib.parse.unquote(url))
    if not m:
        return None, ""
    parts = [x.strip() for x in m.group(1).replace("+", " ").split(",") if x.strip()]
    if parts:
        parts[-1] = re.sub(r"\s*\d{4,6}\b.*$", "", parts[-1]).strip()   # 우편번호 이하(국가) 제거
        parts = [x for x in parts if x and x.lower() not in COUNTRY_WORDS]
    name = None
    for x in (parts[:-2] if len(parts) > 3 else parts):
        if not re.match(r"^\d", x) and re.search(r"[A-Za-z\uAC00-\uD7A3\u0E00-\u0E7F]", x):
            name = x
            break
    return name, ", ".join(parts[-2:])


def split_address_shop(s: str):
    """'다산로 137-1 교촌치킨 약수점' 형태를 (주소, 상호)로 나눔"""
    m = re.match(r"^(.*?\d+(?:-\d+)?)\s+(\S.*)$", s)
    return (m.group(1), m.group(2)) if m else (None, None)


def find_candidates(text: str, near=None):
    """붙여넣은 내용(구글/네이버/카카오 링크, 상호, 주소, 좌표) -> 후보 장소 목록.
    정확한 위치를 못 찾으면 NeedPin 예외(지도에서 직접 찍도록 안내)."""
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
        add({"name": "Pinned location", "addr": text, "lat": float(m.group(1)), "lon": float(m.group(2))})
        return cands

    title, anchor, within, pname = "", None, None, None
    if url:
        coords = parse_coords(url)
        final_url, html = url, ""
        if coords is None or "google" not in url:
            try:
                r = requests.get(url, headers={**UA, "Accept-Language": "en"},
                                 allow_redirects=True, timeout=15)
                final_url, html = r.url, r.text[:300000]
            except requests.RequestException:
                pass
            coords = parse_coords(final_url)
        if coords:
            add({"name": parse_name(final_url, text), "addr": "Google Maps link", "lat": coords[0], "lon": coords[1]})
        title = page_title(html) if html else ""
        # 구글 단축 링크는 좌표 없이 '이름, 주소, 동네'만 주는 경우가 많다
        if not coords and "google" in final_url:
            pname, locality = parse_google_place(final_url)
            anchor = fid_anchor(final_url)
            if not anchor and locality:                      # 고유번호를 못 읽으면 동네 이름으로 기준점
                g = geocode(locality, limit=1)
                anchor = (g[0]["lat"], g[0]["lon"]) if g else None
            if pname:
                lines = [pname] + lines
            if anchor:
                within = (anchor[0], anchor[1], 10)           # 기준점 반경 10km 안에서만 검색

    if not cands:
        queries = []
        for ln in lines[:2]:
            queries.append(ln)
            addr, shop = split_address_shop(ln)               # 주소/상호 따로도 시도
            if addr:
                queries += [addr, shop]
        if len(lines) > 1:
            queries.insert(0, " ".join(lines[:2]))
        if title:
            queries.append(title)
        for q in dict.fromkeys(queries):
            for c in geocode(q, near=anchor or near, within=within):
                add(c)
            if cands:
                break
    if not cands:
        name = pname or (lines[0] if lines else None)
        hint = {"lat": anchor[0], "lon": anchor[1]} if anchor else None
        msg = ("이 장소는 지도 데이터에서 찾지 못했어요. 지도에서 직접 위치를 찍어 주세요." if name
               else "장소를 찾지 못했습니다. 지도에서 직접 찍거나 다른 이름으로 시도해 보세요.")
        raise NeedPin(msg, hint, name)
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


def _brouter_geojson(start, end, nogos, profile=None):
    params = {"lonlats": f"{start[1]},{start[0]}|{end[1]},{end[0]}", "profile": profile or BROUTER_PROFILE,
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
    km = {"main": 0.0, "local": 0.0, "bike": 0.0, "offroad": 0.0, "forbidden": 0.0, "unpaved": 0.0}
    bad = []
    for m in msgs[1:]:
        t = _tags(m[ix["WayTags"]]); d = int(m[ix["Distance"]]) / 1000
        hw = t.get("highway", "")
        if is_forbidden(t):
            km["forbidden"] += d
            bad.append((int(m[ix["Longitude"]]) / 1e6, int(m[ix["Latitude"]]) / 1e6))
        elif hw == "cycleway" or t.get("bicycle") == "designated" or (hw in OFFROAD and t.get("bicycle") == "yes"):
            km["bike"] += d                                   # 자전거도로
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


def build_route(start, end, name: str, profile=None, with_feature=False):
    """도로 종류를 검사하고, 자동차전용도로가 섞이면 그 구간을 피해 다시 계산한다.
    반환: (gpx 문자열, 도로 요약 dict)"""
    nogos, info = [], None
    for attempt in range(MAX_RETRY + 1):
        feature = _brouter_geojson(start, end, nogos, profile)["features"][0]
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
    if with_feature:
        return to_gpx(feature, name), {"roads": info, "warnings": warn}, feature
    return to_gpx(feature, name), {"roads": info, "warnings": warn}


# 경로 선택지: 같은 출발/도착을 성격이 다른 설정으로 계산해 보여준다
ROUTE_OPTIONS = [
    {"id": "bike", "profile": "trekking", "label": "자전거도로 우선",
     "desc": "자전거도로가 있으면 조금 돌아가도 그쪽으로"},
    {"id": "road", "profile": "fastbike", "label": "도로 위주 (빠름)",
     "desc": "포장도로·국도 위주로 빠르게"},
    {"id": "bikemax", "profile": "safety", "label": "자전거도로 최대",
     "desc": "차도를 최대한 피함. 더 많이 돌아갈 수 있음"},
]


def simplify(coords, max_pts=400):
    step = max(1, len(coords) // max_pts)
    pts = [[round(c[1], 5), round(c[0], 5)] for c in coords[::step]]
    last = [round(coords[-1][1], 5), round(coords[-1][0], 5)]
    if pts[-1] != last:
        pts.append(last)
    return pts


def build_options(start, end, name):
    """선택지별 경로를 동시에 계산. 거의 같은 경로는 하나로 합친다."""
    from concurrent.futures import ThreadPoolExecutor

    def one(opt):
        try:
            gpx, summ, f = build_route(start, end, name, opt["profile"], with_feature=True)
        except Exception as e:
            return {**opt, "error": str(e)}
        pr = f["properties"]
        return {**opt, "km": round(int(pr.get("track-length", 0)) / 1000, 1),
                "ascent": int(pr.get("filtered ascend", 0)), "time": _fmt_time(pr.get("total-time", 0)),
                "roads": summ["roads"], "warnings": summ["warnings"],
                "line": simplify(f["geometry"]["coordinates"]), "gpx": gpx}

    with ThreadPoolExecutor(len(ROUTE_OPTIONS)) as ex:
        results = list(ex.map(one, ROUTE_OPTIONS))
    out = []
    for r in results:
        if "error" in r:
            continue
        dup = next((o for o in out if abs(o["km"] - r["km"]) <= max(0.3, o["km"] * 0.02)
                    and (o["roads"] or {}).get("bike") == (r["roads"] or {}).get("bike")), None)
        if dup:
            dup["label"] += " · " + r["label"]               # 같은 길이면 이름만 합침
            continue
        r.pop("profile", None)
        out.append(r)
    if not out:
        raise RuntimeError(next((r["error"] for r in results if "error" in r), "경로 계산 실패"))
    return out


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
    near = None
    try:
        near = (float(request.args["lat"]), float(request.args["lon"]))
    except (KeyError, ValueError):
        pass
    try:
        return jsonify(candidates=find_candidates(request.args.get("text", ""), near))
    except NeedPin as e:   # 못 찾음 -> 화면이 지도 찍기로 넘어가도록 안내
        return jsonify(candidates=[], message=str(e), hint=e.hint, name=e.name)
    except Exception as e:
        return jsonify(error=str(e)), 400


@app.get("/api/routes")
def api_routes():
    try:
        slat, slon = float(request.args["slat"]), float(request.args["slon"])
        elat, elon = float(request.args["elat"]), float(request.args["elon"])
    except (KeyError, ValueError):
        return jsonify(error="좌표가 올바르지 않습니다."), 400
    name = request.args.get("name", "목적지")
    try:
        return jsonify(options=build_options((slat, slon), (elat, elon), name),
                       filename=safe_filename(name) + ".gpx")
    except Exception as e:
        return jsonify(error=str(e)), 502


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
