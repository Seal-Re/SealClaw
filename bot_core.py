import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from duckduckgo_search import DDGS
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
import websockets


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bot-core")


WS_URL = os.getenv("WS_URL", "ws://host.docker.internal:3001")
OB11_ACCESS_TOKEN = os.getenv("OB11_ACCESS_TOKEN", "").strip()

STEAM_API_KEY = os.getenv("STEAM_API_KEY", "").strip()

LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or None
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip() or None
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.2")

# Video ingestion is subtitle-first. Vision is an optional best-effort enhancement.
VISION_ENABLED = os.getenv("VISION_ENABLED", "0").strip() == "1"

# Heybox (xiaoheihe) style learning: cookie-based scraping, distilled to lightweight prompt hints.
HEYBOX_COOKIE = os.getenv("HEYBOX_COOKIE", "").strip()
HEYBOX_STYLE_ENABLED = os.getenv("HEYBOX_STYLE_ENABLED", "1").strip() != "0"
HEYBOX_FETCH_INTERVAL = int(os.getenv("HEYBOX_FETCH_INTERVAL", "86400"))

PROACTIVE_INTERVAL_GROUP = int(os.getenv("PROACTIVE_INTERVAL_GROUP", "3600"))
PROACTIVE_INTERVAL_USER = int(os.getenv("PROACTIVE_INTERVAL_USER", "1800"))

STEAM_AUDIT_INTERVAL = int(os.getenv("STEAM_AUDIT_INTERVAL", "120"))
STEAM_AUDIT_PUSH_COOLDOWN = int(os.getenv("STEAM_AUDIT_PUSH_COOLDOWN", "600"))

ADMIN_QQ_LIST = {
    s.strip() for s in (os.getenv("ADMIN_QQ_LIST", "").split(",")) if s.strip()
}

DB_PATH = os.getenv("DB_PATH", os.path.join("data", "bot.db"))


STEAM_ID64_RE = re.compile(r"^\d{17}$")


def _now_ts() -> int:
    return int(datetime.now().timestamp())


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


_STYLE_STOP = {
    "的",
    "了",
    "啊",
    "吗",
    "吧",
    "我",
    "你",
    "他",
    "她",
    "它",
    "我们",
    "你们",
    "他们",
    "这个",
    "那个",
    "怎么",
    "什么",
    "就是",
    "不是",
    "可以",
    "一下",
    "一个",
    "现在",
    "今天",
    "感觉",
    "真的",
    "有点",
}


_STYLE_BANNED = {
    # Minimal safety rail: keep it from going toxic.
    "傻逼",
    "傻b",
    "sb",
    "nmsl",
    "草泥马",
    "操你",
}


_SLANG_SEEDS = {
    "绷不住",
    "离谱",
    "寄",
    "赢麻了",
    "就这",
    "真没绷住",
    "太真实",
    "太顶了",
    "抽象",
    "逆天",
}


def _clean_style_text(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return _truncate(s, 400)


def _extract_style_tokens(text: str, *, limit: int = 24) -> List[str]:
    t = _clean_style_text(text).lower()
    if not t:
        return []
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []

    toks: List[str] = []
    for part in t.split(" "):
        if not part:
            continue
        if part in _STYLE_STOP or part in _STYLE_BANNED:
            continue
        if part.isdigit():
            continue
        if _CJK_RE.search(part):
            if 2 <= len(part) <= 6:
                toks.append(part)
            elif len(part) > 6:
                toks.append(part[:6])
        else:
            if 2 <= len(part) <= 18:
                toks.append(part)

    freq: Dict[str, int] = {}
    for tok in toks:
        if tok in _STYLE_STOP or tok in _STYLE_BANNED:
            continue
        if len(tok) < 2:
            continue
        freq[tok] = freq.get(tok, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ranked[: max(1, int(limit))]]


def _extract_style_templates(texts: List[str], *, limit: int = 8) -> List[str]:
    # Heuristic templates: short, reusable sentence patterns.
    patterns = [
        (re.compile(r"(就这\??)"), "就这？"),
        (re.compile(r"(\w{0,4}绷不住\w{0,4})"), "绷不住了"),
        (re.compile(r"(太\w{1,4}了)"), "太{形容词}了"),
        (re.compile(r"(别\w{1,8}了)"), "别{动词}了"),
        (re.compile(r"(\w{0,4}离谱\w{0,4})"), "离谱"),
        (re.compile(r"(真\w{1,8})"), "真{评价}"),
    ]
    found: List[str] = []
    for t in texts:
        s = _clean_style_text(t)
        if not s:
            continue
        for rx, tpl in patterns:
            if rx.search(s):
                if tpl not in found:
                    found.append(tpl)
                if len(found) >= limit:
                    return found
    return found[:limit]


def _style_prompt_from_profile(style: Dict[str, Any]) -> str:
    hot = style.get("hot_words") or []
    hot = [str(x) for x in hot if x and str(x) not in _STYLE_BANNED][:12]
    templates = style.get("templates") or []
    templates = [str(x) for x in templates if x][:8]
    slang = style.get("slang") or []
    slang = [str(x) for x in slang if x and str(x) not in _STYLE_BANNED][:10]

    parts: List[str] = [
        "小黑盒风格注入（仅写作风格，不得当作事实依据）：",
        "- 短句，少废话，别客服腔。",
        "- 允许轻微调侃，但禁止攻击性辱骂。",
        "- 事实必须来自工具/来源；不确定就说不确定。",
    ]
    if hot:
        parts.append("- 热词：" + "、".join(hot))
    if slang:
        parts.append("- 口癖：" + "、".join(slang))
    if templates:
        parts.append("- 句式：" + " / ".join(templates))
    return "\n".join(parts)


def _pick_profile_topics(*, kw_weights: Dict[str, float], steam_games: Optional[List[Dict[str, Any]]]) -> List[str]:
    # Heuristic: prefer Steam top game names; otherwise use keyword weights.
    topics: List[str] = []
    if steam_games:
        for g in steam_games[:3]:
            name = (g or {}).get("name")
            if name and isinstance(name, str):
                name = name.strip()
                if name and name not in topics:
                    topics.append(name)
            if len(topics) >= 2:
                break
    if len(topics) < 1 and kw_weights:
        for k, _ in sorted(kw_weights.items(), key=lambda x: (-float(x[1]), x[0])):
            k = (k or "").strip()
            if 2 <= len(k) <= 24 and k not in topics and k not in _STYLE_STOP:
                topics.append(k)
            if len(topics) >= 2:
                break
    return topics[:2]


async def _fetch_heybox_style_texts(topic: str, *, limit: int = 16) -> Dict[str, Any]:
    """Best-effort scrape: search on xiaoheihe.cn, then fetch a few pages and extract short comment-like lines.

    We intentionally avoid private APIs for stability.
    """
    if not HEYBOX_COOKIE:
        return {"ok": False, "error": "HEYBOX_COOKIE missing"}
    topic = _truncate((topic or "").strip(), 60)
    if not topic:
        return {"ok": False, "error": "empty topic"}

    # Use web search (DDG) to find relevant Heybox pages, then fetch and extract.
    q = f"{topic} 小黑盒 评论 最热 site:xiaoheihe.cn"
    sr = await DDGSearchProvider().search_structured(q, max_results=6, timelimit="y")
    urls = [r.get("url") for r in (sr.get("results") or []) if (r.get("url") or "").startswith("http")]
    urls = [u for u in urls if "xiaoheihe.cn" in (u or "")][:4]
    if not urls:
        return {"ok": False, "error": "no heybox urls", "sources": []}

    headers = {
        "Cookie": HEYBOX_COOKIE,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SealClaw/1.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }

    texts: List[str] = []
    evidences: List[Dict[str, Any]] = []
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            for u in urls:
                try:
                    async with session.get(u, headers=headers, allow_redirects=True) as resp:
                        raw = await resp.text(errors="ignore")
                        text = _strip_html(raw)
                        # Extract short lines that look like comments.
                        for line in (text or "").splitlines():
                            line = _clean_style_text(line)
                            if not line:
                                continue
                            if len(line) < 6 or len(line) > 120:
                                continue
                            if any(b in line for b in _STYLE_BANNED):
                                continue
                            texts.append(line)
                            if len(texts) >= limit:
                                break
                        evidences.append({"url": str(resp.url), "status": resp.status})
                except Exception:
                    continue
                if len(texts) >= limit:
                    break
    except Exception as e:
        return {"ok": False, "error": f"heybox fetch failed: {e}", "sources": urls}

    # Dedup preserve order.
    out: List[str] = []
    seen = set()
    for t in texts:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return {"ok": True, "texts": out[:limit], "sources": urls, "evidences": evidences}


def _distill_heybox_style(texts: List[str]) -> Dict[str, Any]:
    texts = [t for t in (texts or []) if isinstance(t, str)]
    joined = "\n".join(texts)
    hot = _extract_style_tokens(joined, limit=24)

    # Slang: seeds + discovered hot words.
    slang: List[str] = []
    for s in list(_SLANG_SEEDS) + hot:
        s = (s or "").strip()
        if not s:
            continue
        if s in _STYLE_BANNED:
            continue
        if s in joined and s not in slang:
            slang.append(s)
        if len(slang) >= 12:
            break

    templates = _extract_style_templates(texts, limit=8)
    return {
        "hot_words": hot[:24],
        "slang": slang[:12],
        "templates": templates[:8],
        "banned": sorted(_STYLE_BANNED),
        "tone_rules": {"short_sentences": True, "max_len": 220},
    }


def extract_keywords(text: str, *, limit: int = 12) -> List[str]:
    """Very lightweight keyword extraction for user profile.

    Core goal: capture game/community tokens without heavy NLP deps.
    """
    t = (text or "").strip().lower()
    if not t:
        return []

    # Remove obvious noise.
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"[@#]\S+", " ", t)
    t = re.sub(r"[^0-9a-z\u4e00-\u9fff\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return []

    stop = {
        "的",
        "了",
        "啊",
        "吗",
        "吧",
        "我",
        "你",
        "他",
        "她",
        "它",
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "怎么",
        "什么",
        "就是",
        "不是",
        "可以",
        "一下",
        "一个",
        "现在",
        "今天",
        "感觉",
        "真的",
        "有点",
        "btw",
        "lol",
    }

    # Tokenize: keep alnum words; for CJK we keep short contiguous chunks.
    tokens: List[str] = []
    for part in t.split(" "):
        if not part:
            continue
        if _CJK_RE.search(part):
            # keep 2-6 len chunks as pseudo-words
            if 2 <= len(part) <= 6:
                tokens.append(part)
            elif len(part) > 6:
                tokens.append(part[:6])
        else:
            if 2 <= len(part) <= 24:
                tokens.append(part)

    freq: Dict[str, int] = {}
    for tok in tokens:
        if tok in stop:
            continue
        if tok.isdigit() and len(tok) > 6:
            continue
        if len(tok) < 2:
            continue
        freq[tok] = freq.get(tok, 0) + 1

    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [k for k, _ in ranked[: max(1, int(limit))]]


def _today_ymd() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def _truncate(s: str, n: int) -> str:
    if s is None:
        return ""
    s = str(s)
    return s if len(s) <= n else s[:n]


def _env_proxy_present() -> bool:
    return bool(os.getenv("HTTP_PROXY") or os.getenv("HTTPS_PROXY"))


def _ddg_proxies() -> Optional[Dict[str, str]]:
    hp = os.getenv("HTTP_PROXY")
    sp = os.getenv("HTTPS_PROXY")
    if not (hp or sp):
        return None
    proxies: Dict[str, str] = {}
    if hp:
        proxies["http"] = hp
    if sp:
        proxies["https"] = sp
    return proxies


class BaseSearchProvider:
    async def search_structured(
        self,
        query: str,
        *,
        max_results: int = 8,
        timelimit: Optional[str] = None,
    ) -> Dict[str, Any]:
        raise NotImplementedError


class DDGSearchProvider(BaseSearchProvider):
    async def search_structured(
        self,
        query: str,
        *,
        max_results: int = 8,
        timelimit: Optional[str] = None,
    ) -> Dict[str, Any]:
        query = _truncate(query or "", 120)
        proxies = _ddg_proxies()

        def _run() -> List[Dict[str, Any]]:
            with DDGS(proxies=proxies, timeout=10) as ddgs:
                return list(ddgs.text(query, max_results=max_results, timelimit=timelimit))

        try:
            rows = await asyncio.to_thread(_run)
            out: List[Dict[str, Any]] = []
            for r in rows:
                href = _norm_url(r.get("href") or "")
                if not href:
                    continue
                out.append(
                    {
                        "title": _truncate(r.get("title") or "", 120),
                        "url": href,
                        "snippet": _truncate(r.get("body") or "", 280),
                        "date": _truncate(r.get("date") or r.get("published") or "", 40),
                        "trust": _source_trust(href),
                    }
                )
            return {"ok": True, "query": query, "results": out}
        except Exception as e:
            return {"ok": False, "query": query, "error": str(e), "results": []}


def _norm_url(u: str) -> str:
    return (u or "").strip()


def _source_trust(url: str) -> float:
    """Trust score for verification. Tone sources are deliberately lower."""
    u = (url or "").lower()
    # Official / platform data
    if "steampowered.com" in u or "steamcommunity.com" in u:
        return 1.0
    if "steamdb.info" in u:
        return 0.9
    # Major media / docs
    if "github.com" in u:
        return 0.85
    if "ign.com" in u or "eurogamer.net" in u:
        return 0.8
    # Community content (use for hints / tone, not as hard facts)
    if "bilibili.com" in u:
        return 0.75
    if "xiaoheihe.cn" in u:
        return 0.55
    if "reddit.com" in u:
        return 0.55
    return 0.6


def _default_expires_seconds(url: str) -> int:
    """Freshness policy. Community sources expire faster."""
    u = (url or "").lower()
    if "steampowered.com" in u or "steamdb.info" in u:
        return 7 * 24 * 3600
    if "github.com" in u:
        return 14 * 24 * 3600
    if "bilibili.com" in u or "xiaoheihe.cn" in u or "reddit.com" in u:
        return 2 * 24 * 3600
    return 3 * 24 * 3600


_URL_IN_PARENS_RE = re.compile(r"\((https?://[^\s)]+)\)")


def _extract_urls(text: str) -> List[str]:
    if not text:
        return []
    urls = [m.group(1).strip() for m in _URL_IN_PARENS_RE.finditer(text)]
    # Dedup preserve order.
    out: List[str] = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _strip_html(html: str) -> str:
    if not html:
        return ""
    s = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    s = re.sub(r"(?is)<br\s*/?>", "\n", s)
    s = re.sub(r"(?is)</p>", "\n", s)
    s = re.sub(r"(?is)<[^>]+>", " ", s)
    s = re.sub(r"[\t\r\f\v]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r" {2,}", " ", s)
    return s.strip()


def _norm_bili_url(u: str) -> str:
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        return "https:" + u
    return u


async def _fetch_bilibili_view(bvid: str) -> Dict[str, Any]:
    """Fetch Bilibili video metadata needed for subtitle pipeline.

    Never throws.
    """
    bvid = (bvid or "").strip()
    if not bvid:
        return {"ok": False, "error": "empty bvid"}

    url = "https://api.bilibili.com/x/web-interface/view"
    params = {"bvid": bvid}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SealClaw/1.0",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Referer": f"https://www.bilibili.com/video/{bvid}",
    }
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                status = int(resp.status)
                data = await resp.json(content_type=None)
                if status in (403, 412):
                    return {"ok": False, "status": status, "error": "bilibili blocked"}
                if status != 200:
                    return {"ok": False, "status": status, "error": "bilibili view http error"}
                blob = (data or {}).get("data") or {}
                pages = blob.get("pages") or []
                cid = None
                if pages and isinstance(pages, list):
                    cid = (pages[0] or {}).get("cid")
                return {
                    "ok": True,
                    "status": status,
                    "bvid": bvid,
                    "cid": cid,
                    "title": blob.get("title") or "",
                    "pic": _norm_bili_url(blob.get("pic") or ""),
                    "raw": blob,
                }
    except Exception as e:
        return {"ok": False, "error": f"bilibili view failed: {e}"}


async def _fetch_bilibili_subtitles(bvid: str) -> str:
    """Fetch Bilibili CC/AI subtitles and stitch into plain text.

    Anti-scrape on Bilibili is strict; MUST degrade gracefully on 403/412.
    Returns a non-empty string (either subtitles or a fixed failure message).
    """
    try:
        meta = await _fetch_bilibili_view(bvid)
        status = int(meta.get("status") or 0)
        if not meta.get("ok"):
            if status in (403, 412):
                return "无法获取视频字幕"
            return "无法获取视频字幕"

        cid = meta.get("cid")
        if not cid:
            return "无法获取视频字幕"

        url = "https://api.bilibili.com/x/player/v2"
        params = {"bvid": (bvid or "").strip(), "cid": str(cid)}
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) SealClaw/1.0",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
            "Referer": f"https://www.bilibili.com/video/{(bvid or '').strip()}",
        }
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, params=params, headers=headers) as resp:
                if int(resp.status) in (403, 412):
                    return "无法获取视频字幕"
                if int(resp.status) != 200:
                    return "无法获取视频字幕"
                data = await resp.json(content_type=None)

            subtitles = (((data or {}).get("data") or {}).get("subtitle") or {}).get("subtitles") or []
            if not subtitles:
                # No CC/AI subtitles on this video.
                return "无法获取视频字幕"

            # Prefer the first available subtitle track.
            s0 = subtitles[0] if isinstance(subtitles, list) and subtitles else {}
            sub_url = _norm_bili_url((s0 or {}).get("subtitle_url") or "")
            if not sub_url:
                return "无法获取视频字幕"

            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.get(sub_url, headers=headers, allow_redirects=True) as resp:
                    if int(resp.status) in (403, 412):
                        return "无法获取视频字幕"
                    raw = await resp.json(content_type=None)
                    body = (raw or {}).get("body") or []
                    lines: List[str] = []
                    for row in body:
                        if not isinstance(row, dict):
                            continue
                        c = (row.get("content") or "").strip()
                        if c:
                            lines.append(c)
                    txt = "\n".join(lines).strip()
                    return txt if txt else "无法获取视频字幕"
    except Exception:
        # Hard degrade. Never break the event loop.
        return "无法获取视频字幕"


async def _fetch_url_text(url: str) -> Dict[str, Any]:
    """Fetch a URL and return a compact text representation.

    Never throws; returns {ok, url, ...}.
    """
    url = _norm_url(url)
    if not url:
        return {"ok": False, "error": "empty url"}
    timeout = aiohttp.ClientTimeout(total=12)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url, allow_redirects=True) as resp:
                ct = (resp.headers.get("content-type") or "").lower()
                raw = await resp.text(errors="ignore")
                text = raw
                if "text/html" in ct or "application/xhtml" in ct or ("<html" in raw.lower()):
                    text = _strip_html(raw)
                text = _truncate(text, 3500)
                return {
                    "ok": resp.status == 200,
                    "status": resp.status,
                    "url": str(resp.url),
                    "content_type": ct,
                    "text": text,
                }
    except Exception as e:
        return {"ok": False, "error": f"fetch failed: {e}", "url": url}


async def _ddg_search_structured(query: str, *, max_results: int, timelimit: Optional[str]) -> Dict[str, Any]:
    # Back-compat wrapper; use DDGSearchProvider for new code.
    return await DDGSearchProvider().search_structured(query, max_results=max_results, timelimit=timelimit)


_VER_RE = re.compile(r"\bv?\d+\.\d+(?:\.\d+)?\b")
_DATE_RE = re.compile(r"\b20\d{2}[-./]\d{1,2}[-./]\d{1,2}\b")
_CN_DATE_RE = re.compile(r"\b20\d{2}年\d{1,2}月\d{1,2}日\b")


def _extract_fact_tokens(text: str) -> Dict[str, List[str]]:
    if not text:
        return {"versions": [], "dates": []}
    versions = sorted({m.group(0) for m in _VER_RE.finditer(text)})
    dates = sorted({m.group(0) for m in _DATE_RE.finditer(text)} | {m.group(0) for m in _CN_DATE_RE.finditer(text)})
    return {"versions": versions[:8], "dates": dates[:8]}


def _detect_dispute(evidences: List[Dict[str, Any]]) -> Optional[str]:
    """Detect obvious disagreements across high-trust sources.

    MVP heuristic: compare version/date tokens.
    """
    # Only compare fact-like evidences; tone sources are not suitable for factual disputes.
    hi = [
        e
        for e in evidences
        if float(e.get("trust") or 0) >= 0.8 and str(e.get("use_case") or "fact") != "tone"
    ]
    if len(hi) < 2:
        return None
    tokens = [(_extract_fact_tokens(e.get("text") or ""), e) for e in hi]
    vers = [t[0]["versions"] for t in tokens if t[0]["versions"]]
    dates = [t[0]["dates"] for t in tokens if t[0]["dates"]]
    if len(vers) >= 2 and vers[0] != vers[1]:
        return "多个高可信来源的版本号信息不一致，可能存在争议/改动。"
    if len(dates) >= 2 and dates[0] != dates[1]:
        return "多个高可信来源的日期信息不一致，可能存在争议/改动。"
    return None


def _infer_use_case(url: str) -> str:
    """Classify sources into fact vs tone.

    Tone sources are allowed to influence phrasing but should not be treated as hard evidence.
    """
    u = (url or "").lower()
    if "xiaoheihe.cn" in u or "reddit.com" in u or "bilibili.com" in u:
        return "tone"
    return "fact"


def _format_sources(urls: List[str], *, limit: int = 6) -> str:
    u = []
    for x in urls:
        x = _norm_url(x)
        if x and x not in u:
            u.append(x)
    u = u[: max(1, int(limit))]
    if not u:
        return ""
    return "\n".join([f"[来源: {x}]" for x in u])


def _ensure_sources(text: str, urls: List[str]) -> str:
    """Append missing source lines.

    Guardrail: SealClaw outputs must carry [来源: URL] when using web/KB.
    """
    base = (text or "").rstrip()
    if not urls:
        return base
    missing = [u for u in urls if u and u not in base]
    if not missing:
        return base
    addon = _format_sources(missing)
    return (base + "\n" + addon).strip()


class DB:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self.conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_bindings (
              qq_id TEXT PRIMARY KEY,
              steam_id TEXT,
              last_push_hash TEXT,
              push_enabled INTEGER DEFAULT 1
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS group_subscriptions (
              group_id TEXT PRIMARY KEY,
              last_push_hash TEXT,
              push_enabled INTEGER DEFAULT 1
            )
            """
        )

        # SealClaw core: Steam play audit (polling) persistence.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS steam_audit_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              qq_id TEXT NOT NULL,
              steam_id TEXT NOT NULL,
              ts INTEGER NOT NULL,
              persona_state INTEGER,
              game_id TEXT,
              game_name TEXT,
              raw_json TEXT,
              dedupe_hash TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS steam_last_state (
              qq_id TEXT PRIMARY KEY,
              last_ts INTEGER,
              last_persona_state INTEGER,
              last_game_id TEXT,
              last_game_name TEXT,
              last_hash TEXT,
              last_push_ts INTEGER
            )
            """
        )

        # SealClaw core: user profile (play history + chat keywords).
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              qq_id TEXT NOT NULL,
              ts INTEGER NOT NULL,
              text TEXT,
              keywords_json TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS user_profile (
              qq_id TEXT PRIMARY KEY,
              steam_id TEXT,
              keyword_weights_json TEXT,
              updated_at INTEGER
            )
            """
        )

        # SealClaw core: knowledge base (RAG-lite) with freshness + trust.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS kb_items (
              url TEXT PRIMARY KEY,
              title TEXT,
              snippet TEXT,
              text TEXT,
              media_type TEXT,
              source TEXT,
              trust_score REAL,
              use_case TEXT,
              fetched_at INTEGER,
              expires_at INTEGER,
              content_hash TEXT
            )
            """
        )

        # Lightweight schema migration for existing DBs.
        try:
            cols = {r[1] for r in cur.execute("PRAGMA table_info(kb_items)").fetchall()}
            if "use_case" not in cols:
                cur.execute("ALTER TABLE kb_items ADD COLUMN use_case TEXT DEFAULT 'fact'")
        except Exception:
            pass

        # SealClaw core: Heybox style profile (per user), distilled prompt hints.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS heybox_style_profiles (
              qq_id TEXT PRIMARY KEY,
              style_json TEXT,
              source_urls_json TEXT,
              updated_at INTEGER
            )
            """
        )

        # SealClaw core: cooldown gating for event-driven proactive pushes.
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS cooldowns (
              qq_id TEXT NOT NULL,
              key TEXT NOT NULL,
              last_ts INTEGER NOT NULL,
              PRIMARY KEY(qq_id, key)
            )
            """
        )

        # Optional: FTS5 acceleration. If unavailable, code falls back to LIKE search.
        self.fts_enabled = False
        try:
            cur.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS kb_items_fts
                USING fts5(url UNINDEXED, title, text)
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS kb_items_ai AFTER INSERT ON kb_items BEGIN
                  INSERT INTO kb_items_fts(url, title, text) VALUES (new.url, new.title, new.text);
                END;
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS kb_items_ad AFTER DELETE ON kb_items BEGIN
                  DELETE FROM kb_items_fts WHERE url = old.url;
                END;
                """
            )
            cur.execute(
                """
                CREATE TRIGGER IF NOT EXISTS kb_items_au AFTER UPDATE ON kb_items BEGIN
                  DELETE FROM kb_items_fts WHERE url = old.url;
                  INSERT INTO kb_items_fts(url, title, text) VALUES (new.url, new.title, new.text);
                END;
                """
            )
            cur.execute(
                """
                INSERT INTO kb_items_fts(url, title, text)
                SELECT url, title, text FROM kb_items
                WHERE url NOT IN (SELECT url FROM kb_items_fts)
                """
            )
            self.fts_enabled = True
        except sqlite3.OperationalError:
            self.fts_enabled = False
        self.conn.commit()

    async def execute(self, sql: str, params: Tuple[Any, ...] = ()) -> None:
        # SQLite is synchronous; lock + retry avoids sporadic "database is locked" under asyncio.
        for attempt in range(3):
            try:
                async with self.lock:
                    self.conn.execute(sql, params)
                    self.conn.commit()
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e).lower() and attempt < 2:
                    await asyncio.sleep(0.05 * (2**attempt))
                    continue
                raise

    async def fetchone(self, sql: str, params: Tuple[Any, ...] = ()) -> Optional[sqlite3.Row]:
        async with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchone()

    async def fetchall(self, sql: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
        async with self.lock:
            cur = self.conn.execute(sql, params)
            return cur.fetchall()

    async def upsert_binding(self, qq_id: str, steam_id: str) -> None:
        await self.execute(
            """
            INSERT INTO user_bindings(qq_id, steam_id, push_enabled)
            VALUES(?, ?, 1)
            ON CONFLICT(qq_id) DO UPDATE SET steam_id=excluded.steam_id
            """,
            (qq_id, steam_id),
        )

    async def delete_binding(self, qq_id: str) -> None:
        await self.execute("DELETE FROM user_bindings WHERE qq_id=?", (qq_id,))

    async def set_push_enabled(self, qq_id: str, enabled: bool) -> None:
        await self.execute(
            """
            INSERT INTO user_bindings(qq_id, push_enabled)
            VALUES(?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET push_enabled=excluded.push_enabled
            """,
            (qq_id, 1 if enabled else 0),
        )

    async def get_binding(self, qq_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetchone("SELECT * FROM user_bindings WHERE qq_id=?", (qq_id,))
        return dict(row) if row else None

    async def iter_push_users(self) -> List[Dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT * FROM user_bindings WHERE push_enabled=1 AND steam_id IS NOT NULL AND steam_id != ''"
        )
        return [dict(r) for r in rows]

    async def set_group_push_enabled(self, group_id: str, enabled: bool) -> None:
        await self.execute(
            """
            INSERT INTO group_subscriptions(group_id, push_enabled)
            VALUES(?, ?)
            ON CONFLICT(group_id) DO UPDATE SET push_enabled=excluded.push_enabled
            """,
            (group_id, 1 if enabled else 0),
        )

    async def iter_push_groups(self) -> List[Dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM group_subscriptions WHERE push_enabled=1")
        return [dict(r) for r in rows]

    async def update_group_last_push_hash(self, group_id: str, h: str) -> None:
        await self.execute(
            """
            UPDATE group_subscriptions SET last_push_hash=? WHERE group_id=?
            """,
            (h, group_id),
        )

    async def update_last_push_hash(self, qq_id: str, h: str) -> None:
        await self.execute(
            """
            UPDATE user_bindings SET last_push_hash=? WHERE qq_id=?
            """,
            (h, qq_id),
        )

    async def get_steam_last_state(self, qq_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetchone("SELECT * FROM steam_last_state WHERE qq_id=?", (qq_id,))
        return dict(row) if row else None

    async def upsert_steam_last_state(
        self,
        qq_id: str,
        *,
        ts: int,
        persona_state: Optional[int],
        game_id: Optional[str],
        game_name: Optional[str],
        h: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO steam_last_state(
              qq_id, last_ts, last_persona_state, last_game_id, last_game_name, last_hash
            ) VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET
              last_ts=excluded.last_ts,
              last_persona_state=excluded.last_persona_state,
              last_game_id=excluded.last_game_id,
              last_game_name=excluded.last_game_name,
              last_hash=excluded.last_hash
            """,
            (
                qq_id,
                ts,
                persona_state,
                game_id,
                game_name,
                h,
            ),
        )

    async def update_steam_last_push_ts(self, qq_id: str, ts: int) -> None:
        await self.execute(
            """
            UPDATE steam_last_state SET last_push_ts=? WHERE qq_id=?
            """,
            (ts, qq_id),
        )

    async def insert_steam_audit_event(
        self,
        qq_id: str,
        steam_id: str,
        *,
        ts: int,
        persona_state: Optional[int],
        game_id: Optional[str],
        game_name: Optional[str],
        raw_json: str,
        dedupe_hash: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO steam_audit_events(
              qq_id, steam_id, ts, persona_state, game_id, game_name, raw_json, dedupe_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (qq_id, steam_id, ts, persona_state, game_id, game_name, raw_json, dedupe_hash),
        )

    async def insert_chat_event(self, qq_id: str, *, ts: int, text: str, keywords: List[str]) -> None:
        await self.execute(
            """
            INSERT INTO chat_events(qq_id, ts, text, keywords_json)
            VALUES(?, ?, ?, ?)
            """,
            (qq_id, ts, _truncate(text, 2000), json.dumps(keywords, ensure_ascii=False)),
        )

    async def get_user_profile(self, qq_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetchone("SELECT * FROM user_profile WHERE qq_id=?", (qq_id,))
        return dict(row) if row else None

    async def get_heybox_style_profile(self, qq_id: str) -> Optional[Dict[str, Any]]:
        row = await self.fetchone("SELECT * FROM heybox_style_profiles WHERE qq_id=?", (qq_id,))
        return dict(row) if row else None

    async def upsert_heybox_style_profile(
        self,
        qq_id: str,
        *,
        style: Dict[str, Any],
        source_urls: List[str],
        updated_at: int,
    ) -> None:
        await self.execute(
            """
            INSERT INTO heybox_style_profiles(qq_id, style_json, source_urls_json, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET
              style_json=excluded.style_json,
              source_urls_json=excluded.source_urls_json,
              updated_at=excluded.updated_at
            """,
            (
                str(qq_id),
                json.dumps(style or {}, ensure_ascii=False, sort_keys=True),
                json.dumps([_norm_url(u) for u in (source_urls or []) if _norm_url(u)], ensure_ascii=False),
                int(updated_at),
            ),
        )

    async def upsert_user_profile(
        self,
        qq_id: str,
        *,
        steam_id: Optional[str],
        keyword_weights: Dict[str, float],
        updated_at: int,
    ) -> None:
        await self.execute(
            """
            INSERT INTO user_profile(qq_id, steam_id, keyword_weights_json, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(qq_id) DO UPDATE SET
              steam_id=excluded.steam_id,
              keyword_weights_json=excluded.keyword_weights_json,
              updated_at=excluded.updated_at
            """,
            (
                qq_id,
                steam_id or "",
                json.dumps(keyword_weights, ensure_ascii=False, sort_keys=True),
                updated_at,
            ),
        )

    async def upsert_kb_item(
        self,
        *,
        url: str,
        title: str,
        snippet: str,
        text: str,
        media_type: str,
        source: str,
        trust_score: float,
        use_case: str,
        fetched_at: int,
        expires_at: int,
        content_hash: str,
    ) -> None:
        await self.execute(
            """
            INSERT INTO kb_items(
              url, title, snippet, text, media_type, source, trust_score, use_case, fetched_at, expires_at, content_hash
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
              title=excluded.title,
              snippet=excluded.snippet,
              text=excluded.text,
              media_type=excluded.media_type,
              source=excluded.source,
              trust_score=excluded.trust_score,
              use_case=excluded.use_case,
              fetched_at=excluded.fetched_at,
              expires_at=excluded.expires_at,
              content_hash=excluded.content_hash
            """,
            (
                _norm_url(url),
                _truncate(title, 200),
                _truncate(snippet, 600),
                _truncate(text, 6000),
                _truncate(media_type, 32),
                _truncate(source, 32),
                float(trust_score),
                _truncate(use_case or "fact", 16),
                int(fetched_at),
                int(expires_at),
                _truncate(content_hash, 64),
            ),
        )

    async def get_cooldown_ts(self, qq_id: str, key: str) -> int:
        row = await self.fetchone(
            "SELECT last_ts FROM cooldowns WHERE qq_id=? AND key=?",
            (str(qq_id), str(key)),
        )
        return int(row[0]) if row and row[0] is not None else 0

    async def set_cooldown_ts(self, qq_id: str, key: str, ts: int) -> None:
        await self.execute(
            """
            INSERT INTO cooldowns(qq_id, key, last_ts)
            VALUES(?, ?, ?)
            ON CONFLICT(qq_id, key) DO UPDATE SET last_ts=excluded.last_ts
            """,
            (str(qq_id), str(key), int(ts)),
        )

    async def in_cooldown(self, qq_id: str, key: str, *, window_sec: int, now_ts: Optional[int] = None) -> bool:
        now_ts = int(now_ts or _now_ts())
        last = await self.get_cooldown_ts(str(qq_id), str(key))
        return (now_ts - int(last)) < max(0, int(window_sec))

    async def search_kb(self, query: str, *, limit: int = 4, now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        # Prefer FTS5 if present; fall back to LIKE search.
        q = (query or "").strip().lower()
        if not q:
            return []
        now_ts = int(now_ts or _now_ts())

        if getattr(self, "fts_enabled", False):
            toks = [t for t in re.split(r"[^0-9a-z\u4e00-\u9fff]+", q) if 2 <= len(t) <= 24][:6]
            fts_q = " ".join(toks) if toks else q[:12]
            try:
                sql = (
                    "SELECT k.url, k.title, k.snippet, k.text, k.media_type, k.source, k.trust_score, k.use_case, k.fetched_at, k.expires_at "
                    "FROM kb_items_fts f JOIN kb_items k ON k.url=f.url "
                    "WHERE k.expires_at > ? AND kb_items_fts MATCH ? "
                    "ORDER BY k.trust_score DESC, k.fetched_at DESC LIMIT ?"
                )
                rows = await self.fetchall(sql, (now_ts, fts_q, int(limit)))
                return [dict(r) for r in rows]
            except Exception:
                pass

        # LIKE fallback
        # Use a few tokens to reduce scan.
        toks = [t for t in re.split(r"\s+", q) if 2 <= len(t) <= 24][:4]
        if not toks:
            toks = [q[:12]]
        where = " AND ".join(["(lower(title) LIKE ? OR lower(text) LIKE ?)" for _ in toks])
        params: List[Any] = []
        for t in toks:
            like = f"%{t}%"
            params.extend([like, like])
        sql = (
            "SELECT url, title, snippet, text, media_type, source, trust_score, use_case, fetched_at, expires_at "
            "FROM kb_items WHERE expires_at > ? AND "
            + where
            + " ORDER BY trust_score DESC, fetched_at DESC LIMIT ?"
        )
        params = [now_ts] + params
        params.append(int(limit))
        rows = await self.fetchall(sql, tuple(params))
        return [dict(r) for r in rows]


class SteamClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.timeout = aiohttp.ClientTimeout(total=10)

    async def _get_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.api_key:
            return {"ok": False, "error": "STEAM_API_KEY missing"}
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, trust_env=True) as session:
                async with session.get(url, params=params) as resp:
                    data = await resp.json(content_type=None)
                    return {"ok": resp.status == 200, "status": resp.status, "data": data}
        except Exception as e:
            return {"ok": False, "error": f"steam request failed: {e}"}

    async def get_recent_games(self, steam_id: str) -> Dict[str, Any]:
        url = "https://api.steampowered.com/IPlayerService/GetRecentlyPlayedGames/v1/"
        params = {"key": self.api_key, "steamid": steam_id}
        r = await self._get_json(url, params)
        if not r.get("ok"):
            return r
        games = r.get("data", {}).get("response", {}).get("games", []) or []
        # Keep a stable, compact shape for LLM.
        out = [
            {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "playtime_2weeks": g.get("playtime_2weeks", 0),
                "playtime_forever": g.get("playtime_forever", 0),
            }
            for g in games
        ]
        return {"ok": True, "games": out}

    async def get_favorite_games(self, steam_id: str) -> Dict[str, Any]:
        url = "https://api.steampowered.com/IPlayerService/GetOwnedGames/v1/"
        params = {
            "key": self.api_key,
            "steamid": steam_id,
            "include_appinfo": 1,
            "include_played_free_games": 1,
        }
        r = await self._get_json(url, params)
        if not r.get("ok"):
            return r
        games = r.get("data", {}).get("response", {}).get("games", []) or []
        games.sort(key=lambda x: x.get("playtime_forever", 0), reverse=True)
        top = games[:10]
        out = [
            {
                "appid": g.get("appid"),
                "name": g.get("name"),
                "playtime_forever": g.get("playtime_forever", 0),
            }
            for g in top
        ]
        return {"ok": True, "games": out}

    async def get_game_deals(self, app_id: int) -> Dict[str, Any]:
        url = f"https://store.steampowered.com/api/appdetails?appids={app_id}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, trust_env=True) as session:
                async with session.get(url) as resp:
                    data = await resp.json(content_type=None)
                    blob = data.get(str(app_id), {}) if isinstance(data, dict) else {}
                    if not blob.get("success"):
                        return {"ok": False, "status": resp.status, "error": "appdetails not success"}
                    d = blob.get("data", {}) or {}
                    po = d.get("price_overview")
                    return {
                        "ok": True,
                        "appid": app_id,
                        "name": d.get("name"),
                        "is_free": d.get("is_free"),
                        "price_overview": po,
                    }
        except Exception as e:
            return {"ok": False, "error": f"appdetails failed: {e}"}

    async def get_player_summaries(self, steam_id: str) -> Dict[str, Any]:
        """Steam: Get current online status & current game.

        Uses ISteamUser/GetPlayerSummaries/v2 (SteamID64).
        NOTE: Field names may differ; keep raw blob for debugging.
        """
        url = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
        params = {"key": self.api_key, "steamids": steam_id}
        r = await self._get_json(url, params)
        if not r.get("ok"):
            return r
        players = r.get("data", {}).get("response", {}).get("players", []) or []
        p = players[0] if players else {}
        # Keep a compact view; keep raw for troubleshooting / future mapping.
        return {
            "ok": True,
            "steam_id": steam_id,
            "persona_state": p.get("personastate"),
            "game_id": p.get("gameid") or p.get("game_id"),
            "game_name": p.get("gameextrainfo") or p.get("game_name"),
            "raw": p,
        }


def _steam_state_hash(*, persona_state: Any, game_id: Any, game_name: Any) -> str:
    # A stable fingerprint for state-change detection.
    blob = json.dumps(
        {"persona_state": persona_state, "game_id": game_id, "game_name": game_name},
        ensure_ascii=False,
        sort_keys=True,
    )
    return _md5(blob)


def _steam_state_push_text(*, persona_state: Optional[int], game_name: Optional[str]) -> Optional[str]:
    # Keep it short and community-like.
    g = (game_name or "").strip()
    if g:
        return f"开玩了：{g}"
    # persona_state mapping is not guaranteed; keep neutral.
    if persona_state is None:
        return None
    if int(persona_state) == 0:
        return "下线了"
    return "上线了"


async def steam_audit_loop(bot: "OneBotClient") -> None:
    """SealClaw core: periodic Steam play audit.

    - Polls Steam online / current game for bound users.
    - Persists to SQLite.
    - Pushes only on state change, with cooldown.
    """
    backoff = 1
    while True:
        sleep_for = max(15, STEAM_AUDIT_INTERVAL)
        try:
            # Don't spam pushes / logs before WS is ready.
            if not bot.connected.is_set():
                await asyncio.sleep(2)
                continue
            users = await bot.db.iter_push_users()
            now_ts = int(datetime.now().timestamp())

            for u in users:
                qq_id = str(u.get("qq_id"))
                steam_id = str(u.get("steam_id"))
                if not steam_id:
                    continue

                r = await bot.steam.get_player_summaries(steam_id)
                if not r.get("ok"):
                    continue

                persona_state = r.get("persona_state")
                game_id = r.get("game_id")
                game_name = r.get("game_name")
                h = _steam_state_hash(persona_state=persona_state, game_id=game_id, game_name=game_name)

                raw_json = json.dumps(r.get("raw") or {}, ensure_ascii=False)
                await bot.db.insert_steam_audit_event(
                    qq_id,
                    steam_id,
                    ts=now_ts,
                    persona_state=int(persona_state) if persona_state is not None else None,
                    game_id=str(game_id) if game_id is not None else None,
                    game_name=str(game_name) if game_name is not None else None,
                    raw_json=raw_json,
                    dedupe_hash=h,
                )

                last = await bot.db.get_steam_last_state(qq_id)
                last_hash = (last or {}).get("last_hash")
                await bot.db.upsert_steam_last_state(
                    qq_id,
                    ts=now_ts,
                    persona_state=int(persona_state) if persona_state is not None else None,
                    game_id=str(game_id) if game_id is not None else None,
                    game_name=str(game_name) if game_name is not None else None,
                    h=h,
                )

                if h == last_hash:
                    continue

                # P6: Steam state-change event -> targeted search trigger (best-effort).
                try:
                    old_game_id = str((last or {}).get("last_game_id") or "").strip()
                    old_game_name = str((last or {}).get("last_game_name") or "").strip()
                    new_game_id = str(game_id) if game_id is not None else ""
                    new_game_name = str(game_name) if game_name is not None else ""
                    if new_game_id and (new_game_id != old_game_id or (new_game_name and new_game_name != old_game_name)):
                        asyncio.create_task(
                            bot._auto_push_game_intel(
                                str(qq_id),
                                game_name=new_game_name or old_game_name,
                                game_id=new_game_id,
                            )
                        )
                except Exception:
                    log.exception("steam -> targeted search trigger failed")

                # Cooldown gating.
                last_push_ts = int((last or {}).get("last_push_ts") or 0)
                if now_ts - last_push_ts < max(0, STEAM_AUDIT_PUSH_COOLDOWN):
                    continue

                text = _steam_state_push_text(
                    persona_state=int(persona_state) if persona_state is not None else None,
                    game_name=str(game_name) if game_name is not None else None,
                )
                if text:
                    try:
                        await bot.private_push(qq_id, text)
                        await bot.db.update_steam_last_push_ts(qq_id, now_ts)
                        await asyncio.sleep(1)
                    except Exception:
                        log.exception("steam audit push failed")

            backoff = 1
        except Exception:
            log.exception("steam audit loop error")
            backoff = min(60, max(1, backoff * 2))
            sleep_for = max(15, backoff)

        await asyncio.sleep(sleep_for)


def search_gaming_info(query: str, category: str, timelimit: Optional[str] = None) -> str:
    """Search gaming info via DuckDuckGo.

    Hard limits:
    - query is truncated to <= 100 chars to avoid HTTP 400.
    - output is truncated to <= 2000 chars.

    timelimit: "d"|"w"|"m"|"y"|None
    category: "guide" | "news_deal"
    """

    query = _truncate(query or "", 100)
    if category == "guide":
        query += " site:gamersky.com OR site:3dmgame.com OR site:nga.cn OR site:taptap.cn OR site:bilibili.com"
    elif category == "news_deal":
        query += " site:xiaoheihe.cn OR site:gcores.com OR site:steampowered.com"

    proxies = _ddg_proxies()
    parts: List[str] = []
    try:
        with DDGS(proxies=proxies, timeout=10) as ddgs:
            results = ddgs.text(query, max_results=8, timelimit=timelimit)
            for r in results:
                # duckduckgo-search returns keys: title, href, body, date (may vary)
                date = r.get("date") or r.get("published") or ""
                body = r.get("body") or ""
                href = r.get("href") or ""
                title = r.get("title") or ""
                line = f"[{date}] {title} {body} ({href})".strip()
                parts.append(line)
    except Exception as e:
        return _truncate(f"[search error] {e}", 2000)

    text = "\n".join(parts).strip() or "(no results)"
    return _truncate(text, 2000)


def _tool_schemas() -> List[Dict[str, Any]]:
    # Tool descriptions double as guardrails.
    return [
        {
            "type": "function",
            "function": {
                "name": "get_recent_games",
                "description": "Steam: Get recently played games in last 2 weeks for a steam_id (SteamID64). Returns list with appid+name+playtime. Use this to confirm appid before calling get_game_deals(app_id).",
                "parameters": {
                    "type": "object",
                    "properties": {"steam_id": {"type": "string"}},
                    "required": ["steam_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_favorite_games",
                "description": "Steam: Get owned games sorted by total playtime (Top10). Returns appid+name+playtime_forever. Use this to map game name to appid when needed.",
                "parameters": {
                    "type": "object",
                    "properties": {"steam_id": {"type": "string"}},
                    "required": ["steam_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_game_deals",
                "description": "Steam Store: Get price_overview for a Steam app_id (int). IMPORTANT: app_id must be numeric; do NOT pass game name. If unsure, call get_recent_games/get_favorite_games first to confirm appid.",
                "parameters": {
                    "type": "object",
                    "properties": {"app_id": {"type": "integer"}},
                    "required": ["app_id"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_gaming_info",
                "description": "Search gaming info across the web (DuckDuckGo). When querying seasons/builds/patch meta or hot topics, you MUST set timelimit (d/w/m/y) to ensure freshness. query will be truncated to <=100 chars. Output <=2000 chars and includes date field when available.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "category": {"type": "string", "enum": ["guide", "news_deal"]},
                        "timelimit": {"type": ["string", "null"], "enum": ["d", "w", "m", "y", None]},
                    },
                    "required": ["query", "category"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "search_verified",
                "description": "Search web, fetch top sources, verify disagreements, and return a grounded answer draft. IMPORTANT: you MUST cite every claim with [来源: URL] lines from sources list. If sources disagree, you MUST state it explicitly as '存在争议' and include both URLs.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "timelimit": {"type": ["string", "null"], "enum": ["d", "w", "m", "y", None]},
                        "max_results": {"type": "integer"},
                    },
                    "required": ["query"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ingest_bilibili_video",
                "description": "Bilibili: Subtitle-first video ingestion (MVP). Fetch CC/AI subtitles, distill core guide/fun summary, and store into kb_items with media_type=video & use_case=fact. MUST append [来源: B站视频URL]. If API blocked (403/412), degrade gracefully without crashing.",
                "parameters": {
                    "type": "object",
                    "properties": {"bvid": {"type": "string"}},
                    "required": ["bvid"],
                },
            },
        },
    ]


def _system_prompt() -> str:
    today = _today_ymd()
    return (
        "你是一个精通全平台游戏的资深玩家与数据分析师。\n"
        f"今天日期：{today}\n"
        "输出风格：短句、直给、带点社区调侃；别用客服腔/敬语套话。\n"
        "强制指令：当用户询问涉及游戏版本、配装、赛季攻略时，你必须具备强烈的版本意识。\n"
        "你必须优先判断搜索结果的时间有效性；如资料属于历史版本，必须在回答开头使用 Markdown 引用（> ）发出版本过期警告。\n"
        "额外提醒：查询 Steam 价格前，必须先通过 Steam 最近/常玩列表确认目标 AppID（数字），不要把游戏名当 app_id。\n"
        "硬性规则：只要引用或总结了外部网页/知识库内容，回答末尾必须包含至少一条形如 [来源: URL] 的来源行。\n"
        "硬性规则：若不同高可信来源关键事实不一致，必须明确提示“存在争议”，并同时列出双方来源。"
    )


class OneBotClient:
    def __init__(self, db: DB, steam: SteamClient):
        self.db = db
        self.steam = steam
        self.ws = None
        self.ws_lock = asyncio.Lock()
        self.connected = asyncio.Event()
        self.search_provider: BaseSearchProvider = DDGSearchProvider()
        self._heybox_lock = asyncio.Lock()
        self._heybox_refreshing: set[str] = set()

        # Explicitly inject OpenAI http_client. This is often required in containerized environments
        # so the SDK honors proxy env vars consistently.
        # LLM client is optional: the bot can still run command handling and WS I/O without it.
        self.llm = None
        if LLM_API_KEY:
            http_client = DefaultAsyncHttpxClient(trust_env=True)
            kwargs: Dict[str, Any] = {
                "api_key": LLM_API_KEY,
                "http_client": http_client,
                "max_retries": 2,
            }
            if LLM_BASE_URL:
                kwargs["base_url"] = LLM_BASE_URL
            self.llm = AsyncOpenAI(**kwargs)

    def _profile_shift_significant(self, old: Dict[str, float], new: Dict[str, float]) -> bool:
        """Heuristic: detect significant shifts in keyword weights.

        Used for event-driven Heybox style refresh (P6).
        """
        try:
            old = old or {}
            new = new or {}
            if not old:
                return bool(new)

            def topk(d: Dict[str, float], k: int) -> List[str]:
                return [
                    str(x[0])
                    for x in sorted(d.items(), key=lambda x: (-float(x[1]), str(x[0])))[:k]
                    if str(x[0]).strip()
                ]

            a = set(topk(old, 8))
            b = set(topk(new, 8))
            if not a or not b:
                return True
            jaccard = 1.0 - (len(a & b) / max(1, len(a | b)))
            if jaccard >= 0.60:
                return True

            # L1 change over top tokens.
            keys = set(topk(old, 16)) | set(topk(new, 16))
            l1 = 0.0
            for k in keys:
                l1 += abs(float(new.get(k, 0.0)) - float(old.get(k, 0.0)))
            return l1 >= 12.0
        except Exception:
            return False

    async def _maybe_refresh_heybox_on_profile_shift(
        self,
        qq_id: str,
        *,
        old_weights: Dict[str, float],
        new_weights: Dict[str, float],
        steam_id: Optional[str],
    ) -> None:
        try:
            if not HEYBOX_STYLE_ENABLED or not HEYBOX_COOKIE:
                return
            if not self._profile_shift_significant(old_weights, new_weights):
                return

            # Cooldown: do not refresh too often on chat bursts.
            key = "heybox_style:profile_shift"
            if await self.db.in_cooldown(qq_id, key, window_sec=12 * 3600):
                return
            await self.db.set_cooldown_ts(qq_id, key, _now_ts())
            asyncio.create_task(self._refresh_heybox_style(str(qq_id), kw_weights=new_weights, steam_id=steam_id))
        except Exception:
            log.exception("heybox shift trigger failed")

    async def _auto_push_game_intel(self, qq_id: str, *, game_name: str, game_id: Optional[str]) -> None:
        """Event-driven targeted search push (P6). Best-effort; never throws."""
        try:
            if not self.llm:
                return
            game_name = (game_name or "").strip()
            if not game_name:
                return

            # Cooldown per-qq per-game.
            gid = (game_id or "").strip()
            key = f"auto_push_game:{gid or game_name.lower()}"
            if await self.db.in_cooldown(str(qq_id), key, window_sec=12 * 3600):
                return
            await self.db.set_cooldown_ts(str(qq_id), key, _now_ts())

            # Use P3 verified search (KB-first -> web -> store to KB).
            q = _truncate(f"{game_name} 攻略 更新 新闻", 120)
            sr = await self._dispatch_tool(
                "search_verified",
                {"query": q, "timelimit": "w", "max_results": 6},
                steam_id=None,
            )
            if not (sr or {}).get("ok"):
                return
            sources = [str(u) for u in (sr.get("sources") or []) if str(u).startswith("http")][:6]
            evidences = sr.get("evidences") or []

            style_prompt = ""
            try:
                profile = await self.db.get_user_profile(str(qq_id))
                kw_weights: Dict[str, float] = {}
                if profile and profile.get("keyword_weights_json"):
                    kw_weights = json.loads(profile.get("keyword_weights_json") or "{}") or {}
                binding = await self.db.get_binding(str(qq_id))
                sid = (binding or {}).get("steam_id")
                style_prompt = await self._get_heybox_style_prompt(str(qq_id), kw_weights=kw_weights, steam_id=sid)
            except Exception:
                style_prompt = ""

            sys2 = (
                "你在做自动情报推送，必须短、直给、可执行。\n"
                "只总结证据里能站得住的点；不确定就说不确定。\n"
                "输出 6-10 行，每行 <= 40 字。\n"
                "必须包含至少 2 条要点：版本/时间、关键改动/攻略要点/热议。"
            )
            messages: List[Dict[str, Any]] = [{"role": "system", "content": _system_prompt()}]
            if style_prompt:
                messages.append({"role": "system", "content": style_prompt})
            messages.append({"role": "system", "content": sys2})
            messages.append(
                {
                    "role": "user",
                    "content": f"游戏：{game_name}\n\n证据（JSON，可能截断）：\n" + _truncate(json.dumps(evidences, ensure_ascii=False), 9000),
                }
            )

            content, _ = await self._llm_chat_stream(
                model=LLM_MODEL,
                messages=messages,
                tools=[],
                tool_choice="none",
                temperature=0.4,
            )
            text_out = (content or "").strip()
            if not text_out:
                return

            text_out = _ensure_sources(text_out, sources)
            await self.private_push(str(qq_id), text_out)
        except Exception:
            log.exception("auto push game intel failed")

    async def ingest_bilibili_video(self, bvid: str) -> Dict[str, Any]:
        """P5: Subtitle-first video ingestion for Bilibili (MVP).

        - Fetch subtitles -> LLM distill -> store into kb_items with media_type=video, use_case=fact.
        - Vision is optional and best-effort, gated by VISION_ENABLED.
        """
        bvid = (bvid or "").strip()
        if not bvid:
            return {"ok": False, "error": "empty bvid"}
        if not self.llm:
            return {"ok": False, "error": "LLM not configured"}

        meta = await _fetch_bilibili_view(bvid)
        url = f"https://www.bilibili.com/video/{bvid}"
        if not meta.get("ok"):
            # Degrade gracefully; do not crash.
            st = int(meta.get("status") or 0)
            if st in (403, 412):
                return {"ok": False, "error": "bilibili blocked", "status": st, "url": url}
            return {"ok": False, "error": meta.get("error") or "bilibili meta failed", "url": url}

        subtitles = await _fetch_bilibili_subtitles(bvid)
        if not subtitles or subtitles.strip() == "无法获取视频字幕":
            return {"ok": False, "error": "无法获取视频字幕", "url": url}

        title = (meta.get("title") or "").strip() or f"B站视频 {bvid}"
        cover = _norm_bili_url(meta.get("pic") or "")

        sys2 = (
            "你在做视频字幕的知识入库提炼。\n"
            "目标：提炼‘核心攻略/乐子总结’，可用于以后检索与复用。\n"
            "输出 10-18 行，短句，按要点分行。\n"
            "若涉及版本/时间窗口，必须明确写出。\n"
            "不要编造字幕里没有的东西。"
        )
        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": _system_prompt()},
            {"role": "system", "content": sys2},
            {"role": "user", "content": f"标题：{title}\n视频URL：{url}\n\n字幕：\n" + _truncate(subtitles, 12000)},
        ]

        distilled, _ = await self._llm_chat_stream(
            model=LLM_MODEL,
            messages=messages,
            tools=[],
            tool_choice="none",
            temperature=0.35,
        )
        distilled = (distilled or "").strip()
        if not distilled:
            return {"ok": False, "error": "empty llm output", "url": url}

        # Optional vision placeholder (best-effort, may be unsupported by gateway).
        vision_note = ""
        if VISION_ENABLED and cover:
            try:
                mm_messages: List[Dict[str, Any]] = [
                    {"role": "system", "content": "你将基于封面图片补充极少量信息。只允许描述肉眼可见内容；不确定就说不确定。输出 1-3 行。"},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": f"视频标题：{title}\n请仅描述封面可见信息，禁止猜剧情。"},
                            {"type": "image_url", "image_url": {"url": cover}},
                        ],
                    },
                ]
                vtxt, _ = await self._llm_chat_stream(
                    model=LLM_MODEL,
                    messages=mm_messages,
                    tools=[],
                    tool_choice="none",
                    temperature=0.2,
                )
                vtxt = (vtxt or "").strip()
                if vtxt:
                    vision_note = "\n\n（封面可见信息，可能不完整）\n" + _truncate(vtxt, 500)
            except Exception:
                vision_note = ""

        text = (distilled + vision_note).strip()
        text = _ensure_sources(text, [url])
        fetched_at = _now_ts()
        expires_at = fetched_at + _default_expires_seconds(url)
        trust = float(_source_trust(url))
        ch = _md5(title + "\n" + text)
        await self.db.upsert_kb_item(
            url=url,
            title=title,
            snippet=_truncate(text, 260),
            text=text,
            media_type="video",
            source="bilibili",
            trust_score=trust,
            use_case="fact",
            fetched_at=fetched_at,
            expires_at=expires_at,
            content_hash=ch,
        )
        return {"ok": True, "url": url, "title": title, "sources": [url], "bvid": bvid}

    async def send_action(self, action: str, params: Dict[str, Any]) -> None:
        if not self.connected.is_set() or not self.ws:
            raise RuntimeError("websocket not connected")
        payload = {
            "action": action,
            "params": params,
            "echo": f"e{int(datetime.now().timestamp()*1000)}",
        }
        data = json.dumps(payload, ensure_ascii=False)
        async with self.ws_lock:
            await self.ws.send(data)

    async def reply(self, event: Dict[str, Any], text: str) -> None:
        msg_type = event.get("message_type")
        text = _truncate(text, 1500)
        if msg_type == "group":
            await self.send_action("send_group_msg", {"group_id": int(event.get("group_id")), "message": text})
        else:
            await self.send_action("send_private_msg", {"user_id": int(event.get("user_id")), "message": text})

    async def group_push(self, group_id: str, text: str) -> None:
        await self.send_action("send_group_msg", {"group_id": int(group_id), "message": _truncate(text, 1500)})

    async def private_push(self, qq_id: str, text: str) -> None:
        await self.send_action("send_private_msg", {"user_id": int(qq_id), "message": _truncate(text, 1500)})

    def _extract_text(self, event: Dict[str, Any]) -> str:
        if isinstance(event.get("raw_message"), str):
            return event["raw_message"].strip()
        msg = event.get("message")
        if isinstance(msg, str):
            return msg.strip()
        if isinstance(msg, list):
            parts = []
            for seg in msg:
                if not isinstance(seg, dict):
                    continue
                if seg.get("type") == "text":
                    parts.append((seg.get("data") or {}).get("text", ""))
            return "".join(parts).strip()
        return ""

    async def handle_event(self, event: Dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return
        text = self._extract_text(event)
        if not text:
            return

        qq_id = str(event.get("user_id"))
        msg_type = event.get("message_type")

        # User profile: store chat keywords (lightweight, always-on).
        try:
            kws = extract_keywords(text)
            await self.db.insert_chat_event(qq_id, ts=_now_ts(), text=text, keywords=kws)
            await self._update_user_profile_keywords(qq_id, kws)
        except Exception:
            log.exception("profile update failed")

        # All slash-commands are admin-only.
        if text.startswith("/"):
            if qq_id not in ADMIN_QQ_LIST:
                await self.reply(event, "⚠️ 权限不足，该指令仅限管理员使用。")
                return

            # /bind [QQ号] [SteamID64]
            m = re.match(r"^/bind\s+(\d+)\s+(\d{17})\s*$", text)
            if m:
                target_qq, steam_id = m.group(1), m.group(2)
                if not STEAM_ID64_RE.match(steam_id):
                    await self.reply(event, "SteamID64 必须为 17 位纯数字")
                    return
                try:
                    await self.db.upsert_binding(target_qq, steam_id)
                    await self.reply(event, "✅ 绑定成功！系统将基于您的独立库存生成专属推荐。")
                except Exception as e:
                    log.exception("bind failed")
                    await self.reply(event, f"绑定失败：{e}")
                return

            # /unbind [QQ号]
            m = re.match(r"^/unbind\s+(\d+)\s*$", text.strip())
            if m:
                target_qq = m.group(1)
                try:
                    await self.db.delete_binding(target_qq)
                    await self.reply(event, "已解绑")
                except Exception as e:
                    log.exception("unbind failed")
                    await self.reply(event, f"解绑失败：{e}")
                return

            # /autopush [QQ号] on|off
            m = re.match(r"^/autopush\s+(\d+)\s+(on|off)\s*$", text.strip(), flags=re.I)
            if m:
                target_qq = m.group(1)
                enabled = m.group(2).lower() == "on"
                try:
                    await self.db.set_push_enabled(target_qq, enabled)
                    await self.reply(event, f"自动推送已{'开启' if enabled else '关闭'}")
                except Exception as e:
                    log.exception("autopush failed")
                    await self.reply(event, f"设置失败：{e}")
                return

            # /group_sub [群号] on|off
            m = re.match(r"^/group_sub\s+(\d+)\s+(on|off)\s*$", text.strip(), flags=re.I)
            if m:
                group_id = m.group(1)
                enabled = m.group(2).lower() == "on"
                try:
                    await self.db.set_group_push_enabled(group_id, enabled)
                    await self.reply(event, f"群资讯推送已{'开启' if enabled else '关闭'}")
                except Exception as e:
                    log.exception("group_sub failed")
                    await self.reply(event, f"设置失败：{e}")
                return

            await self.reply(event, "未知管理员指令")
            return

        # LLM path
        try:
            reply = await self.chat_with_tools(qq_id, text, message_type=msg_type)
            await self.reply(event, reply)
        except Exception as e:
            log.exception("llm reply failed")
            await self.reply(event, f"系统繁忙：{e}")

    async def chat_with_tools(
        self,
        qq_id: str,
        user_message: str,
        *,
        brief: bool = False,
        message_type: Optional[str] = None,
    ) -> str:
        binding = await self.db.get_binding(qq_id)
        steam_id = (binding or {}).get("steam_id")

        profile = await self.db.get_user_profile(qq_id)
        kw_weights: Dict[str, float] = {}
        try:
            if profile and profile.get("keyword_weights_json"):
                kw_weights = json.loads(profile["keyword_weights_json"]) or {}
        except Exception:
            kw_weights = {}

        content = user_message
        # Only private chat gets personalized Steam context.
        if steam_id and (message_type != "group"):
            content = f"[System Context: 该用户的 Steam ID 是 {steam_id}]\n用户消息：{user_message}"

        if kw_weights and (message_type != "group"):
            top_kws = ", ".join([k for k, _ in sorted(kw_weights.items(), key=lambda x: (-float(x[1]), x[0]))[:8]])
            content = f"[User Profile: 最近聊天关键词={top_kws}]\n" + content

        # Heybox style injection: best-effort refresh in background, prompt injection if available.
        style_prompt = ""
        try:
            style_prompt = await self._get_heybox_style_prompt(qq_id, kw_weights=kw_weights, steam_id=steam_id)
        except Exception:
            style_prompt = ""

        messages: List[Dict[str, Any]] = [{"role": "system", "content": _system_prompt()}]
        if style_prompt:
            messages.append({"role": "system", "content": style_prompt})
        messages.append({"role": "user", "content": content})

        if not self.llm:
            return "LLM 未配置：请设置 LLM_API_KEY 后重试。"

        tools = _tool_schemas()
        max_turns = 6
        for _ in range(max_turns):
            # Some OpenAI-compatible gateways require stream=true.
            msg_content, tool_calls = await self._llm_chat_stream(
                model=LLM_MODEL,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.3 if brief else 0.6,
            )

            if tool_calls:
                messages.append({"role": "assistant", "content": msg_content or "", "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = (tc or {}).get("function") if isinstance(tc, dict) else None
                    name = (fn or {}).get("name")
                    args = json.loads((fn or {}).get("arguments") or "{}")
                    out = await self._dispatch_tool(name, args, steam_id=steam_id)
                    messages.append({"role": "tool", "tool_call_id": (tc or {}).get("id"), "content": json.dumps(out, ensure_ascii=False)})
                continue

            # Guardrail: if model used web/KB tool, enforce source lines.
            text_out = (msg_content or "").strip() or "(empty reply)"
            if isinstance(messages, list) and any(m.get("role") == "tool" for m in messages):
                # If any tool indicates dispute, ensure the final reply states it.
                try:
                    tool_payloads = [m for m in reversed(messages) if m.get("role") == "tool"]
                    dispute_msg = ""
                    srcs: List[str] = []
                    for tp in tool_payloads:
                        blob = json.loads(tp.get("content") or "{}")
                        if not srcs:
                            srcs = blob.get("sources") or []
                        if not dispute_msg:
                            dispute_msg = (blob.get("dispute") or "").strip()
                        if dispute_msg and srcs:
                            break
                    if dispute_msg and "存在争议" not in text_out:
                        text_out = f"存在争议：{dispute_msg}\n" + text_out
                except Exception:
                    srcs = []

                # If the model forgot to include the required source lines, append tool-provided sources.
                if "[来源:" not in text_out:
                    # Find last tool output with sources.
                    if "srcs" not in locals() or not srcs:
                        tool_payloads = [m for m in reversed(messages) if m.get("role") == "tool"]
                        srcs = []
                        for tp in tool_payloads:
                            try:
                                blob = json.loads(tp.get("content") or "{}")
                                srcs = blob.get("sources") or []
                                if srcs:
                                    break
                            except Exception:
                                continue
                    text_out = _ensure_sources(text_out, [str(s) for s in (srcs or [])])

            return text_out
        return "工具调用轮次过多，已停止。请缩小问题范围后重试。"

    async def _update_user_profile_keywords(self, qq_id: str, keywords: List[str]) -> None:
        """SealClaw core: keep a lightweight keyword-weight profile.

        - Exponential decay to keep it fresh.
        - No heavy NLP / embeddings yet.
        """
        if not keywords:
            return
        profile = await self.db.get_user_profile(qq_id)
        old: Dict[str, float] = {}
        steam_id = ""
        if profile:
            steam_id = str(profile.get("steam_id") or "")
            try:
                old = json.loads(profile.get("keyword_weights_json") or "{}") or {}
            except Exception:
                old = {}
        else:
            binding = await self.db.get_binding(qq_id)
            steam_id = str((binding or {}).get("steam_id") or "")

        # Decay + add.
        decayed: Dict[str, float] = {k: float(v) * 0.97 for k, v in old.items() if float(v) > 0.05}
        for kw in keywords:
            decayed[kw] = float(decayed.get(kw, 0.0)) + 1.0

        # Cap size.
        if len(decayed) > 80:
            for k, _ in sorted(decayed.items(), key=lambda x: float(x[1]))[: len(decayed) - 80]:
                decayed.pop(k, None)

        await self.db.upsert_user_profile(qq_id, steam_id=steam_id, keyword_weights=decayed, updated_at=_now_ts())

        # P6: profile shift -> trigger Heybox style refresh (best-effort).
        asyncio.create_task(
            self._maybe_refresh_heybox_on_profile_shift(
                str(qq_id),
                old_weights=old,
                new_weights=decayed,
                steam_id=steam_id or None,
            )
        )

    async def _llm_chat_stream(
        self,
        *,
        model: str,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        tool_choice: str,
        temperature: float,
    ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
        """Call chat.completions with stream=True and aggregate deltas.

        Returns: (content, tool_calls)
        - content: full assistant text
        - tool_calls: SDK tool_call objects (same shape as non-stream response)
        """

        content_parts: List[str] = []
        tool_acc: Dict[int, Dict[str, Any]] = {}

        stream = await self.llm.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=True,
        )

        async for event in stream:
            try:
                choice = (event.choices or [None])[0]
                if not choice:
                    continue
                delta = getattr(choice, "delta", None)
                if not delta:
                    continue

                # Text deltas
                d_content = getattr(delta, "content", None)
                if d_content:
                    content_parts.append(d_content)

                # Tool call deltas (OpenAI streaming format)
                d_tool_calls = getattr(delta, "tool_calls", None)
                if not d_tool_calls:
                    continue
                for tc in d_tool_calls:
                    idx = getattr(tc, "index", None)
                    if idx is None:
                        continue
                    slot = tool_acc.setdefault(
                        int(idx),
                        {
                            "id": getattr(tc, "id", None),
                            "type": getattr(tc, "type", "function"),
                            "function": {"name": None, "arguments": ""},
                        },
                    )

                    # ID/type may arrive in early chunks only
                    if getattr(tc, "id", None):
                        slot["id"] = tc.id
                    if getattr(tc, "type", None):
                        slot["type"] = tc.type

                    fn = getattr(tc, "function", None)
                    if fn:
                        name = getattr(fn, "name", None)
                        if name:
                            slot["function"]["name"] = name
                        args_part = getattr(fn, "arguments", None)
                        if args_part:
                            slot["function"]["arguments"] += args_part
            except Exception:
                # Keep streaming robust: ignore malformed chunks from non-standard gateways.
                continue

        # Rebuild tool_calls into SDK objects by parsing the accumulated JSON arguments.
        # We reuse the SDK's internal class by creating a fake non-stream response shape is difficult,
        # so we keep the dicts and let the existing code access attributes via dot where possible.
        # However, current code expects tc.function.name / tc.function.arguments.
        tool_calls_out: Optional[List[Dict[str, Any]]] = None
        if tool_acc:
            tool_calls_out = []
            for idx in sorted(tool_acc.keys()):
                tc = tool_acc[idx]
                fn = tc.get("function") or {}
                tool_calls_out.append(
                    {
                        "id": tc.get("id") or f"call_{idx}",
                        "type": tc.get("type") or "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": fn.get("arguments") or "{}",
                        },
                    }
                )

        return "".join(content_parts), tool_calls_out

    async def _dispatch_tool(self, name: str, args: Dict[str, Any], *, steam_id: Optional[str]) -> Any:
        try:
            if name == "get_recent_games":
                sid = args.get("steam_id") or steam_id
                return await self.steam.get_recent_games(str(sid))
            if name == "get_favorite_games":
                sid = args.get("steam_id") or steam_id
                return await self.steam.get_favorite_games(str(sid))
            if name == "get_game_deals":
                return await self.steam.get_game_deals(int(args.get("app_id")))
            if name == "search_gaming_info":
                t = search_gaming_info(
                    query=str(args.get("query", "")),
                    category=str(args.get("category", "news_deal")),
                    timelimit=args.get("timelimit"),
                )
                return {"ok": True, "text": t, "sources": _extract_urls(t)}
            if name == "search_verified":
                q = str(args.get("query", "")).strip()
                tl = args.get("timelimit")
                max_results = int(args.get("max_results") or 6)

                # 1) KB first (fresh only)
                kb_hits = await self.db.search_kb(q, limit=3, now_ts=_now_ts())
                if kb_hits:
                    urls = [h.get("url") or "" for h in kb_hits]
                    return {
                        "ok": True,
                        "from": "kb",
                        "query": q,
                        "sources": urls,
                        "evidences": kb_hits,
                        "dispute": _detect_dispute(
                            [
                                {
                                    "text": h.get("text"),
                                    "trust": h.get("trust_score"),
                                    "use_case": h.get("use_case") or "fact",
                                }
                                for h in kb_hits
                            ]
                        ),
                    }

                # 2) Web search -> fetch top pages -> store to KB
                sr = await self.search_provider.search_structured(
                    q,
                    max_results=max(3, min(10, max_results)),
                    timelimit=tl,
                )
                results = (sr.get("results") or []) if sr.get("ok") else []
                top = results[: max(3, min(6, len(results)))]

                evidences: List[Dict[str, Any]] = []
                for r in top:
                    url = _norm_url(r.get("url") or "")
                    if not url:
                        continue
                    ft = await _fetch_url_text(url)
                    if not ft.get("ok"):
                        continue
                    text = ft.get("text") or ""
                    title = r.get("title") or ""
                    snippet = r.get("snippet") or ""
                    trust = float(r.get("trust") or _source_trust(url))
                    use_case = _infer_use_case(url)
                    fetched_at = _now_ts()
                    expires_at = fetched_at + _default_expires_seconds(url)
                    ch = _md5(title + "\n" + snippet + "\n" + text)
                    await self.db.upsert_kb_item(
                        url=url,
                        title=title,
                        snippet=snippet,
                        text=text,
                        media_type="text",
                        source="web",
                        trust_score=trust,
                        use_case=use_case,
                        fetched_at=fetched_at,
                        expires_at=expires_at,
                        content_hash=ch,
                    )
                    evidences.append(
                        {
                            "url": ft.get("url") or url,
                            "title": title,
                            "snippet": snippet,
                            "text": text,
                            "trust": trust,
                            "use_case": use_case,
                        }
                    )

                urls = [e.get("url") or "" for e in evidences]
                dispute = _detect_dispute(evidences)
                return {
                    "ok": True,
                    "from": "web",
                    "query": q,
                    "sources": urls,
                    "evidences": [
                        {
                            "url": e.get("url"),
                            "title": e.get("title"),
                            "snippet": e.get("snippet"),
                            "trust": e.get("trust"),
                            "use_case": e.get("use_case") or "fact",
                            # Keep evidence text short for LLM context.
                            "text": _truncate(e.get("text") or "", 1600),
                        }
                        for e in evidences
                    ],
                    "dispute": dispute,
                }
            if name == "ingest_bilibili_video":
                bvid = str(args.get("bvid") or "").strip()
                return await self.ingest_bilibili_video(bvid)
            return {"ok": False, "error": f"unknown tool: {name}"}
        except Exception as e:
            return {"ok": False, "error": f"tool {name} failed: {e}"}

    async def _get_heybox_style_prompt(
        self,
        qq_id: str,
        *,
        kw_weights: Dict[str, float],
        steam_id: Optional[str],
    ) -> str:
        if not HEYBOX_STYLE_ENABLED:
            return ""

        # Refresh in background if stale.
        profile = await self.db.get_heybox_style_profile(str(qq_id))
        last_ts = int((profile or {}).get("updated_at") or 0)
        now_ts = _now_ts()
        stale = (not last_ts) or (now_ts - last_ts >= max(3600, int(HEYBOX_FETCH_INTERVAL)))
        if stale and HEYBOX_COOKIE:
            asyncio.create_task(self._refresh_heybox_style(str(qq_id), kw_weights=kw_weights, steam_id=steam_id))

        if not profile or not (profile.get("style_json") or "").strip():
            return ""
        try:
            style = json.loads(profile.get("style_json") or "{}") or {}
        except Exception:
            style = {}
        return _style_prompt_from_profile(style)

    async def _refresh_heybox_style(self, qq_id: str, *, kw_weights: Dict[str, float], steam_id: Optional[str]) -> None:
        # Best-effort, rate-limited by in-memory set. Failures should not affect chat.
        if not HEYBOX_COOKIE:
            return
        async with self._heybox_lock:
            if qq_id in self._heybox_refreshing:
                return
            self._heybox_refreshing.add(qq_id)

        try:
            steam_games: Optional[List[Dict[str, Any]]] = None
            if steam_id:
                r = await self.steam.get_recent_games(str(steam_id))
                if r.get("ok"):
                    steam_games = r.get("games") or []

            topics = _pick_profile_topics(kw_weights=kw_weights, steam_games=steam_games)
            if not topics:
                return

            all_texts: List[str] = []
            all_sources: List[str] = []
            for t in topics:
                fr = await _fetch_heybox_style_texts(t, limit=16)
                if not fr.get("ok"):
                    continue
                all_texts.extend(fr.get("texts") or [])
                all_sources.extend(fr.get("sources") or [])
                if len(all_texts) >= 18:
                    break
            if not all_texts:
                return

            style = _distill_heybox_style(all_texts)
            await self.db.upsert_heybox_style_profile(
                qq_id,
                style=style,
                source_urls=all_sources,
                updated_at=_now_ts(),
            )
        except Exception:
            log.exception("heybox style refresh failed")
        finally:
            async with self._heybox_lock:
                self._heybox_refreshing.discard(qq_id)


async def proactive_user_loop(bot: OneBotClient) -> None:
    while True:
        try:
            if not bot.llm:
                # Avoid spamming users with "LLM 未配置" messages.
                await asyncio.sleep(max(10, PROACTIVE_INTERVAL_USER))
                continue
            users = await bot.db.iter_push_users()
            for u in users:
                qq_id = str(u.get("qq_id"))
                steam_id = str(u.get("steam_id"))
                last_hash = (u.get("last_push_hash") or "").strip()

                # Steam -> pick most played in last 2 weeks
                recent = await bot.steam.get_recent_games(steam_id)
                games = recent.get("games") if recent.get("ok") else None
                if not games:
                    continue
                games_sorted = sorted(games, key=lambda g: g.get("playtime_2weeks", 0), reverse=True)
                top = games_sorted[0]
                game_name = top.get("name") or ""
                if not game_name:
                    continue

                # Web info (fresh)
                info = search_gaming_info(query=f"{game_name} 促销 更新", category="news_deal", timelimit="w")
                h = _md5(info)
                if h == last_hash:
                    continue

                brief = await bot.chat_with_tools(
                    qq_id,
                    "这是一份为您定制的游戏简报。请基于最新资讯用 5-8 行给出摘要，并在必要时提示版本/时间有效性。\n\n资讯：\n"
                    + info,
                    brief=True,
                )
                try:
                    await bot.private_push(qq_id, brief)
                    await bot.db.update_last_push_hash(qq_id, h)
                    await asyncio.sleep(5)
                except Exception:
                    log.exception("push failed")

        except Exception:
            log.exception("proactive loop error")

        await asyncio.sleep(max(10, PROACTIVE_INTERVAL_USER))


async def proactive_group_loop(bot: OneBotClient) -> None:
    while True:
        try:
            if not bot.llm:
                # Avoid spamming groups with "LLM 未配置" messages.
                await asyncio.sleep(max(10, PROACTIVE_INTERVAL_GROUP))
                continue
            groups = await bot.db.iter_push_groups()
            for g in groups:
                group_id = str(g.get("group_id"))
                last_hash = (g.get("last_push_hash") or "").strip()

                info = search_gaming_info(
                    query="今日热门游戏大作更新与促销",
                    category="news_deal",
                    timelimit="d",
                )
                h = _md5(info)
                if h == last_hash:
                    continue

                brief = await bot.chat_with_tools(
                    "0",
                    "请把以下资讯整理为 150 字以内的综合简报（偏群公告风格），必要时提示信息时效性。\n\n资讯：\n" + info,
                    brief=True,
                    message_type="group",
                )
                try:
                    await bot.group_push(group_id, brief)
                    await bot.db.update_group_last_push_hash(group_id, h)
                    await asyncio.sleep(5)
                except Exception:
                    log.exception("group push failed")
        except Exception:
            log.exception("proactive group loop error")

        await asyncio.sleep(max(10, PROACTIVE_INTERVAL_GROUP))


async def connect_loop(bot: OneBotClient) -> None:
    backoff = 1
    headers = {}
    if OB11_ACCESS_TOKEN:
        headers["Authorization"] = f"Bearer {OB11_ACCESS_TOKEN}"

    while True:
        try:
            log.info("connecting ws: %s", WS_URL)
            # websockets renamed header parameter across major versions:
            # - websockets<=13 uses extra_headers
            # - websockets>=16 uses additional_headers
            sig = inspect.signature(websockets.connect)
            kw: Dict[str, Any] = {
                "open_timeout": 10,
                "ping_interval": 20,
                "ping_timeout": 20,
            }
            if "additional_headers" in sig.parameters:
                kw["additional_headers"] = headers
            else:
                kw["extra_headers"] = headers

            async with websockets.connect(WS_URL, **kw) as ws:
                bot.ws = ws
                bot.connected.set()
                backoff = 1
                log.info("ws connected")
                async for raw in ws:
                    try:
                        if isinstance(raw, (bytes, bytearray)):
                            raw = raw.decode("utf-8", errors="ignore")
                        event = json.loads(raw)
                        # fire-and-forget handler, keep recv loop hot
                        asyncio.create_task(bot.handle_event(event))
                    except Exception:
                        log.exception("bad event")
        except Exception:
            log.exception("ws connection error")
        finally:
            bot.connected.clear()
            bot.ws = None

        await asyncio.sleep(min(30, backoff))
        backoff = min(30, backoff * 2)


async def main() -> None:
    if not LLM_API_KEY:
        log.warning("LLM_API_KEY is empty; LLM replies will fail")
    if _env_proxy_present():
        log.info("proxy env detected: HTTP_PROXY/HTTPS_PROXY")

    db = DB(DB_PATH)
    steam = SteamClient(STEAM_API_KEY)
    bot = OneBotClient(db, steam)

    # Start proactive tasks
    asyncio.create_task(steam_audit_loop(bot))
    asyncio.create_task(proactive_user_loop(bot))
    asyncio.create_task(proactive_group_loop(bot))
    await connect_loop(bot)


if __name__ == "__main__":
    asyncio.run(main())
