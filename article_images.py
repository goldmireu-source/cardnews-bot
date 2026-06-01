"""기사 URL에서 이미지 후보 추출.

흐름:
  1. 클러스터 ID 받아서 데일리싱크 DB 에서 해당 클러스터의 모든 기사 URL 조회
  2. 각 URL 페이지를 fetch → og:image, twitter:image, link rel=image_src, 본문 inline <img>
  3. 중복 제거 (URL 정규화) + 너무 작은 아이콘/스프라이트 필터링
  4. (옵션) UNSPLASH_ACCESS_KEY 가 .env 에 있으면 키워드 검색 결과 추가
  5. 메모리 캐시 5분 + 디스크 캐시

결과 형식: [{"url": ..., "source": "og:image"|"inline"|"unsplash", "alt": ..., "from": 기사URL}, ...]
"""
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / "cache" / "article_images"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

DAILYSYNC_DB = os.getenv(
    "DAILYSYNC_DB_PATH",
    r"F:\ai-news-digest\ai-news-digest\data\app.db",
)
UNSPLASH_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()

# 메모리 캐시 (5분 TTL)
_MEM_CACHE: dict[str, tuple[float, list[dict]]] = {}
_MEM_LOCK = threading.Lock()
_TTL_SEC = 300

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
TIMEOUT = 8

# 작은 아이콘·tracking 픽셀 등 거르기 위한 휴리스틱
SKIP_URL_PATTERNS = [
    r"1x1\.", r"pixel\.gif", r"tracking",
    r"/favicon", r"icon-", r"sprite",
    r"\.svg($|\?)",  # SVG 는 카드 배경에 부적합
    r"/ads?/", r"banner",
]
SKIP_RE = re.compile("|".join(SKIP_URL_PATTERNS), re.I)


def _normalize_url(url: str, base: str = "") -> Optional[str]:
    """상대 URL → 절대, 정상 URL 만 통과."""
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        url = "https:" + url
    elif url.startswith("/") and base:
        url = urljoin(base, url)
    elif not url.startswith(("http://", "https://")):
        if base:
            url = urljoin(base, url)
        else:
            return None
    if SKIP_RE.search(url):
        return None
    p = urlparse(url)
    if not p.netloc:
        return None
    return url


def _fetch_article_html(article_url: str) -> str:
    """기사 페이지 HTML 가져오기 (5초 타임아웃, 작은 응답만)."""
    try:
        r = requests.get(article_url, timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return ""
        ct = r.headers.get("Content-Type", "")
        if "html" not in ct.lower():
            return ""
        # 너무 큰 페이지는 자르기 (3MB)
        return r.text[:3_000_000]
    except Exception:
        return ""


def _extract_from_html(html: str, base_url: str) -> list[dict]:
    """HTML 한 페이지에서 이미지 후보 추출."""
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    seen: set[str] = set()

    def _add(url: Optional[str], source: str, alt: str = "") -> None:
        if not url:
            return
        url = _normalize_url(url, base_url)
        if not url or url in seen:
            return
        seen.add(url)
        out.append({"url": url, "source": source, "alt": alt[:200], "from": base_url})

    # 1) og:image / og:image:secure_url / twitter:image
    for prop in ("og:image", "og:image:secure_url", "og:image:url", "twitter:image", "twitter:image:src"):
        for meta in soup.find_all("meta", attrs={"property": prop}) + soup.find_all("meta", attrs={"name": prop}):
            _add(meta.get("content"), source=prop.replace(":", "_"))

    # 2) link rel="image_src"
    for link in soup.find_all("link", rel=lambda v: v and "image_src" in v):
        _add(link.get("href"), source="link_image_src")

    # 3) 본문 안의 큰 이미지
    # article/main/role=main 우선 시도 — 한국 신문사는 article 태그가 메타용
    # 빈 경우가 많아서 거기서 못 찾으면 문서 전체 폴백 (nav/header/footer 제외).
    main = soup.find("article") or soup.find("main") or soup.find(attrs={"role": "main"})
    main_imgs = main.find_all("img") if main else []
    if len(main_imgs) < 2:
        # fallback — 전체 페이지에서 보되 네비/푸터 제외
        main_imgs = [
            img for img in soup.find_all("img")
            if not img.find_parent(["nav", "header", "footer", "aside"])
        ]

    LOGO_KEYWORDS = ("logo", "icon", "avatar", "profile", "thumb-default",
                     "btn_", "ico_", "sprite", "share")

    for img in main_imgs:
        src = (img.get("src") or img.get("data-src") or
               img.get("data-original") or img.get("data-lazy-src") or
               img.get("data-echo"))
        if not src:
            srcset = img.get("srcset", "")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if not src:
            continue

        # 명백한 로고·아이콘 필터 (URL 안 키워드 검사)
        if any(k in src.lower() for k in LOGO_KEYWORDS):
            continue
        # alt 가 "로고" 류면 거름
        alt = (img.get("alt") or "")
        if alt and any(k in alt.lower() for k in ("로고", "logo", "아이콘", "프로필")):
            continue

        # width/height 힌트로 너무 작은 것 거르기 (완화: 100px 이하만 거름)
        try:
            w = int(img.get("width", "0") or 0)
            h = int(img.get("height", "0") or 0)
            if 0 < w < 100 or 0 < h < 100:
                continue
            # 정사각 + 작음 = 보통 아이콘
            if 0 < w <= 180 and 0 < h <= 180 and abs(w - h) <= 10:
                continue
        except ValueError:
            pass
        _add(src, source="inline", alt=alt)

    return out


def _get_cluster_articles(cluster_id: int) -> list[dict]:
    """데일리싱크 DB read-only → 클러스터의 기사 URL 목록."""
    if not Path(DAILYSYNC_DB).exists():
        return []
    uri = f"file:{DAILYSYNC_DB}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT a.id, a.title, a.url FROM articles a "
            "WHERE a.cluster_id = ? ORDER BY a.published_at DESC LIMIT 10",
            (cluster_id,),
        ).fetchall()
        return [{"id": r["id"], "title": r["title"], "url": r["url"]} for r in rows]
    finally:
        con.close()


def _fetch_wikipedia_images(keyword: str, max_terms: int = 4) -> list[dict]:
    """Wikipedia(ko, en) 에서 토픽 관련 페이지의 대표 이미지(thumbnail) 추출.

    토픽 문자열을 단어 단위로 잘라 각 단어/구로 Wikipedia REST API 호출.
    'pope leo' / '교황' / 'AI' / 'anthropic' 같이 단어가 페이지 제목이면 thumbnail 반환.
    API 키 불필요.
    """
    if not keyword:
        return []
    # 단어 추출 — 한글/영문/숫자 토큰
    tokens = re.findall(r"[A-Za-z가-힣0-9]{2,}", keyword)
    # 불용어/너무 일반적인 단어 제거
    STOP = {"의", "을", "를", "와", "과", "이", "가", "에서", "한", "할", "수", "있는",
            "the", "a", "an", "and", "or", "of", "in", "for", "to", "on", "is",
            "AI", "ai"}
    cand = [t for t in tokens if t not in STOP and len(t) >= 2][:max_terms]
    if not cand:
        return []
    out: list[dict] = []
    for term in cand:
        for lang in ("ko", "en"):
            try:
                r = requests.get(
                    f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{term}",
                    timeout=TIMEOUT,
                    headers={"User-Agent": USER_AGENT},
                )
                if r.status_code != 200:
                    continue
                j = r.json()
                thumb = (j.get("originalimage") or j.get("thumbnail") or {})
                src = thumb.get("source")
                if not src:
                    continue
                page = j.get("content_urls", {}).get("desktop", {}).get("page", "")
                out.append({
                    "url": src,
                    "source": f"wikipedia_{lang}",
                    "alt": j.get("description") or j.get("title", ""),
                    "from": page,
                    "credit": f"Wikipedia ({lang})",
                })
                break  # 같은 term 으로 두 언어 다 받지는 않음
            except Exception:
                continue
    return out


def _fetch_unsplash(keyword: str, count: int = 4) -> list[dict]:
    """Unsplash 키워드 검색 (옵션, UNSPLASH_ACCESS_KEY 필요)."""
    if not UNSPLASH_KEY or not keyword:
        return []
    try:
        r = requests.get(
            "https://api.unsplash.com/search/photos",
            params={"query": keyword, "per_page": count, "orientation": "portrait"},
            headers={"Authorization": f"Client-ID {UNSPLASH_KEY}",
                     "Accept-Version": "v1"},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        items = r.json().get("results", [])
        return [
            {
                "url": item["urls"]["regular"],
                "source": "unsplash",
                "alt": item.get("alt_description") or item.get("description") or keyword,
                "from": item.get("links", {}).get("html", ""),
                "credit": item.get("user", {}).get("name", ""),
            }
            for item in items
        ]
    except Exception:
        return []


def _fetch_openverse(keyword: str, count: int = 12) -> list[dict]:
    """Openverse (CC 라이선스 이미지, API 키 불필요) 키워드 검색.

    키가 없어도 동작하는 기본 이미지 검색 소스. Unsplash 키가 없을 때 폴백.
    """
    if not keyword:
        return []
    try:
        r = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": keyword, "page_size": count, "mature": "false"},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        if r.status_code != 200:
            return []
        out = []
        for it in r.json().get("results", []):
            u = it.get("url")
            if not u:
                continue
            out.append({
                "url": u,
                "source": "openverse",
                "alt": it.get("title") or keyword,
                "from": it.get("foreign_landing_url") or "",
                "credit": it.get("creator") or "",
            })
        return out
    except Exception:
        return []


def search_images(keyword: str, count: int = 12) -> list[dict]:
    """키워드로 이미지 검색 — Unsplash(키 있으면) + Openverse + Wikipedia 통합.

    API 키가 전혀 없어도 Openverse/Wikipedia 로 동작한다. URL 기준 중복 제거.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    out: list[dict] = []
    for fetch in (lambda: _fetch_unsplash(keyword, count),
                  lambda: _fetch_openverse(keyword, count),
                  lambda: _fetch_wikipedia_images(keyword)):
        try:
            out += fetch() or []
        except Exception:
            pass
    seen, uniq = set(), []
    for im in out:
        u = im.get("url")
        if not u or u in seen:
            continue
        seen.add(u)
        uniq.append(im)
    return uniq[:count]


def _get_cluster_meta(cluster_id: int) -> dict:
    """클러스터의 topic + agreed_facts + categories 를 한 번에 가져옴 (키워드 풀)."""
    if not Path(DAILYSYNC_DB).exists():
        return {}
    con = sqlite3.connect(f"file:{DAILYSYNC_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute(
            "SELECT topic, agreed_facts, divergences, categories "
            "FROM clusters WHERE id=?", (cluster_id,),
        ).fetchone()
    finally:
        con.close()
    if not r:
        return {}

    def _safe(v, default):
        if v is None or v == "":
            return default
        try:
            return json.loads(v)
        except Exception:
            return default

    return {
        "topic": r["topic"] or "",
        "facts": _safe(r["agreed_facts"], []),
        "divergences": _safe(r["divergences"], []),
        "categories": _safe(r["categories"], []),
    }


# 한국어 조사·어미 (긴 것부터 매칭)
_KO_SUFFIXES = sorted([
    "에서는", "에서도", "에서의", "에서", "으로의", "으로", "에게", "한테",
    "라는", "이라는", "보다", "까지", "부터", "마저", "조차",
    "께서", "께", "에게", "처럼", "같이", "마다",
    "이라고", "라고", "이라며", "라며",
    "들이", "들은", "들을", "들의", "들과",
    "의", "이", "가", "을", "를", "은", "는", "와", "과", "도", "만",
    "에", "로",
], key=len, reverse=True)


def _strip_korean_suffix(token: str) -> str:
    """조사·어미가 붙은 단어에서 핵심 명사만 추출. 영문/숫자는 그대로."""
    if not re.search(r"[가-힣]", token):
        return token
    for suf in _KO_SUFFIXES:
        if token.endswith(suf) and len(token) > len(suf) + 1:
            return token[:-len(suf)]
    # 동사/형용사 활용형 거름 (~하는, ~한, ~된, ~되는, ~인, ~될)
    for verb in ("하는", "되는", "한", "된", "할", "될", "인", "이며"):
        if token.endswith(verb) and len(token) > len(verb) + 1:
            return token[:-len(verb)]
    return token


def _extract_keywords(meta: dict, extra_keyword: str = "") -> list[str]:
    """클러스터 메타에서 검색용 키워드 풀 생성 (Claude 미사용).

    개선점:
      - 한국어 조사·어미 자동 제거
      - topic 통째로(복합 명사구) + 분리 토큰 둘 다 후보
      - facts 에서 인명·기업명 우선 추출
    """
    STOP = {
        # 한글 일반 동사·접속어·시간 표현
        "이번", "오늘", "내일", "어제", "지난", "올해", "최근", "현재",
        "통해", "위해", "대한", "한다", "있다", "없다", "한편", "또한",
        "이를", "이는", "그러나", "하지만", "있는", "하는", "되는",
        "촉구", "발표", "출시", "공개", "도입", "확대", "강화", "협력", "체결",
        "급증", "심화", "구축", "시급", "관리", "체계",
        # 너무 일반적
        "AI", "ai", "기업", "서비스", "기술", "회사", "산업", "업계",
        "시장", "현상", "현재", "최근", "출신", "리더", "리더들",
        # 영문 stop
        "the", "a", "an", "and", "or", "of", "in", "for", "to", "on",
        "is", "are", "with", "by", "as", "be", "this", "that",
    }
    out: list[str] = []
    seen: set[str] = set()

    def _push(token: str):
        if not token:
            return
        token = token.strip().strip("·-,.")
        if not token or len(token) < 2:
            return
        if token in STOP or token.lower() in STOP:
            return
        key = token.lower()
        if key in seen:
            return
        seen.add(key)
        out.append(token)

    def _add_text(text: str):
        # 1) 전체 텍스트도 후보 (Wikipedia opensearch 가 복합어 매칭 잘 함)
        # 정제: 콤마/마침표/괄호 제거, 앞뒤 공백 줄임
        clean = re.sub(r"[,.\(\)\[\]\"'·]+", " ", text or "").strip()
        if 4 < len(clean) < 50:
            _push(clean)
        # 2) 토큰 분해 (한글/영문/숫자)
        for t in re.findall(r"[A-Za-z가-힣0-9][\w가-힣\-]+", text or ""):
            tl = _strip_korean_suffix(t.strip().strip("·-"))
            if re.fullmatch(r"\d+", tl) and len(tl) < 4:
                continue
            if re.fullmatch(r"[A-Za-z]+", tl) and len(tl) < 3:
                continue
            if re.fullmatch(r"[가-힣]+", tl) and len(tl) < 2:
                continue
            _push(tl)

    if extra_keyword:
        _add_text(extra_keyword)
    _add_text(meta.get("topic", ""))
    for f in (meta.get("facts") or [])[:5]:
        _add_text(f)
    for c in (meta.get("categories") or []):
        _add_text(c)
    return out[:14]


def get_cluster_images(cluster_id: int, keyword: str = "") -> list[dict]:
    """클러스터의 모든 기사 이미지 + Wikipedia + (옵션) Unsplash.

    캐시: 메모리 5분 + 디스크.
    """
    cache_key = f"cluster_{cluster_id}"
    now = time.time()
    with _MEM_LOCK:
        ent = _MEM_CACHE.get(cache_key)
        if ent and (now - ent[0]) < _TTL_SEC:
            return ent[1]

    disk_path = CACHE_DIR / f"{cache_key}.json"
    if disk_path.exists() and (now - disk_path.stat().st_mtime) < _TTL_SEC:
        try:
            data = json.loads(disk_path.read_text(encoding="utf-8"))
            with _MEM_LOCK:
                _MEM_CACHE[cache_key] = (now, data)
            return data
        except Exception:
            pass

    articles = _get_cluster_articles(cluster_id)
    meta = _get_cluster_meta(cluster_id)
    all_images: list[dict] = []
    seen_urls: set[str] = set()

    # 1) 각 기사 페이지 크롤 (og:image + inline)
    for art in articles:
        html = _fetch_article_html(art["url"])
        if not html:
            continue
        for img in _extract_from_html(html, art["url"]):
            if img["url"] in seen_urls:
                continue
            seen_urls.add(img["url"])
            img["article_title"] = art["title"]
            all_images.append(img)
        if len(all_images) >= 30:
            break

    # 2) 키워드 풀 생성 (topic + agreed_facts + categories — Claude 미사용)
    keywords = _extract_keywords(meta, extra_keyword=keyword)

    # 2-a) Wikipedia (API 키 불필요)
    if keywords:
        wiki_imgs = _fetch_wikipedia_for_keywords(keywords)
        for img in wiki_imgs:
            if img["url"] in seen_urls:
                continue
            seen_urls.add(img["url"])
            all_images.append(img)

    # 2-b) Unsplash (옵션)
    if UNSPLASH_KEY and keywords:
        # 가장 의미있는 키워드 3개로 검색 (각 4장씩)
        for kw in keywords[:3]:
            for img in _fetch_unsplash(kw, count=4):
                if img["url"] in seen_urls:
                    continue
                seen_urls.add(img["url"])
                all_images.append(img)

    # 캐시 저장
    with _MEM_LOCK:
        _MEM_CACHE[cache_key] = (now, all_images)
    try:
        disk_path.write_text(json.dumps(all_images, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

    return all_images


def _wiki_opensearch(keyword: str, lang: str = "ko") -> list[str]:
    """Wikipedia opensearch — 부분 매칭으로 페이지 제목 후보 반환 (최대 3개)."""
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "opensearch",
                "search": keyword,
                "limit": 3,
                "namespace": 0,
                "format": "json",
            },
            timeout=5,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return data[1] if len(data) >= 2 else []
    except Exception:
        return []


def _wiki_summary(title: str, lang: str = "ko") -> dict | None:
    """페이지 제목 → summary API (thumbnail 포함)."""
    try:
        r = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{title}",
            timeout=5,
            headers={"User-Agent": USER_AGENT},
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _fetch_wikipedia_for_keywords(keywords: list[str], max_results: int = 8) -> list[dict]:
    """키워드 풀 → opensearch 로 페이지 후보 → summary thumbnail 추출.

    각 키워드를 한·영 양쪽에서 opensearch 한 뒤, 매칭된 페이지 제목으로 summary 조회.
    중복 페이지·이미지 자동 제거.
    """
    out: list[dict] = []
    seen_pages: set[str] = set()
    seen_imgs: set[str] = set()
    calls = 0
    MAX_CALLS = 30

    for kw in keywords:
        if len(out) >= max_results or calls >= MAX_CALLS:
            break
        for lang in ("ko", "en"):
            if len(out) >= max_results or calls >= MAX_CALLS:
                break
            calls += 1
            titles = _wiki_opensearch(kw, lang=lang)
            for title in titles[:2]:
                page_key = f"{lang}:{title}"
                if page_key in seen_pages:
                    continue
                seen_pages.add(page_key)
                calls += 1
                j = _wiki_summary(title, lang=lang)
                if not j or j.get("type") == "disambiguation":
                    continue
                thumb = j.get("originalimage") or j.get("thumbnail") or {}
                src = thumb.get("source")
                if not src or src in seen_imgs:
                    continue
                seen_imgs.add(src)
                out.append({
                    "url": src,
                    "source": f"wikipedia_{lang}",
                    "alt": j.get("description") or j.get("title", ""),
                    "from": j.get("content_urls", {}).get("desktop", {}).get("page", ""),
                    "credit": f"Wikipedia ({lang}) — {title}",
                    "keyword": kw,
                    "page_title": title,
                })
                if len(out) >= max_results:
                    break
    return out


# ---- 이미지 프록시 캐시 ----
PROXY_CACHE_DIR = ROOT / "cache" / "img_proxy"
PROXY_CACHE_DIR.mkdir(parents=True, exist_ok=True)
PROXY_MAX_BYTES = 8 * 1024 * 1024  # 8MB
PROXY_TTL_SEC = 7 * 86400  # 7일


def fetch_image_to_cache(url: str) -> Optional[Path]:
    """외부 이미지 다운로드 → 디스크 캐시. 캐시 hit 시 경로만 반환."""
    h = hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]
    # 확장자 추정 (URL 끝)
    ext = ".jpg"
    m = re.search(r"\.(jpg|jpeg|png|gif|webp)(\?|$)", url, re.I)
    if m:
        ext = "." + m.group(1).lower().replace("jpeg", "jpg")
    path = PROXY_CACHE_DIR / f"{h}{ext}"
    if path.exists() and (time.time() - path.stat().st_mtime) < PROXY_TTL_SEC:
        return path

    try:
        r = requests.get(url, stream=True, timeout=TIMEOUT,
                         headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            return None
        ct = r.headers.get("Content-Type", "")
        if not ct.startswith("image/"):
            return None
        total = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > PROXY_MAX_BYTES:
                    f.close()
                    path.unlink(missing_ok=True)
                    return None
                f.write(chunk)
        return path
    except Exception:
        return None
