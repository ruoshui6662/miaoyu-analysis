"""采集与清洗：SearXNG 搜索 → 正文抓取 → 去重 → 三级可信度标记。

纯 stdlib + requests，不依赖 lxml 等被本机企业策略拦截的 C 扩展。
"""
from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse, urlunparse

import requests

from config import DATA_DIR_TASKS, ENABLED_GROUPS, HTTP_TIMEOUT, USER_AGENT
from searx_client import SearxClient

# 等级 → 三级可信度映射（S/A→high、B→mid、C/D→low）
_LEVEL_CRED = {"S": "high", "A": "high", "B": "mid", "C": "low", "D": "low"}


def _enabled_site_levels() -> dict:
    """已启用网站类信源 host → level（C1a：勾选信源构成域名白名单与等级）。"""
    try:
        from db import enabled_sites
        return enabled_sites()
    except Exception:
        return {}

# ---------- 可信度规则 ----------

OFFICIAL_DOMAINS = (
    "gov.cn", "peopleapp.com", "people.com.cn", "xinhuanet.com", "news.cn",
    "cctv.com", "cnr.cn", "gmw.cn", "rmzxb.com.cn", "12371.cn", "ce.cn",
)

AUTHORITATIVE_MEDIA = (
    "people.com.cn", "peopleapp.com", "xinhuanet.com", "news.cn", "cctv.com",
    "thepaper.cn", "jiemian.com", "yicai.com", "caixin.com", "21jingji.com",
    "chinanews.com", "china.com.cn", "huanqiu.com", "cnr.cn", "gmw.cn",
    "stcn.com", "zqrb.cn", "takungpao.com", "hwtz.cn", "cyol.com",
)

KOL_PLATFORMS = (
    "zhihu.com", "weibo.com", "weibo.cn", "bilibili.com", "xiaohongshu.com",
    "tieba.baidu.com", "toutiao.com", "douyin.com", "kuaishou.com",
    "weixin.qq.com", "mp.weixin.qq.com", "wemp.app",
)

AGGREGATORS = ("baijiahao", "sohu.com", "163.com", "qq.com", "sina.com.cn", "zhidian", "jianshu.com")

# 域名 → 媒体名（长的先匹配：如 mp.weixin.qq.com 优先于 qq.com）
MEDIA_NAME_MAP = {
    "mp.weixin.qq.com": "微信公众号",
    "peopleapp.com": "人民日报", "people.com.cn": "人民日报",
    "xinhuanet.com": "新华社", "news.cn": "新华社",
    "cctv.com": "央视新闻", "cnr.cn": "央广网", "gmw.cn": "光明网",
    "chinanews.com": "中国新闻网", "cyol.com": "中国青年报",
    "huanqiu.com": "环球网", "rmzxb.com.cn": "人民政协报",
    "thepaper.cn": "澎湃新闻", "jiemian.com": "界面新闻",
    "yicai.com": "第一财经", "caixin.com": "财新网",
    "21jingji.com": "21世纪经济报道", "ce.cn": "中国经济网",
    "weibo.com": "微博", "weibo.cn": "微博",
    "zhihu.com": "知乎", "bilibili.com": "B站",
    "xiaohongshu.com": "小红书", "douyin.com": "抖音",
    "tieba.baidu.com": "百度贴吧", "toutiao.com": "今日头条",
    "baijiahao.baidu.com": "百家号（自媒体）",
    "mp.weixin.qq.com": "微信公众号",
    "sogou.com": "搜狗", "hk01.com": "香港01", "bastillepost.com": "巴士的报",
    "stheadline.com": "星岛头条", "sohu.com": "搜狐新闻",
    "163.com": "网易新闻", "qq.com": "腾讯新闻", "sina.com.cn": "新浪新闻",
}


def source_name(url: str, title: str = "") -> str:
    """推断来源媒体名：优先域名映射表，其次标题后缀（- XX / | XX）。"""
    d = domain_of(url)
    for k, name in sorted(MEDIA_NAME_MAP.items(), key=lambda kv: -len(kv[0])):
        if d == k or d.endswith("." + k):
            return name
    # 标题后缀提取，如 "...- 香港01" / "...| 星島頭條"
    m = re.search(r"[\s]?[-—|｜]\s*([\u4e00-\u9fffA-Za-z0-9·]{2,16})\s*$", title or "")
    if m:
        return m.group(1).strip()
    return ""


def domain_of(url: str) -> str:
    return (urlparse(url).netloc or "").lower()


def credibility(url: str, engine: str = "") -> str:
    """三级可信度：high 官方/权威媒体；mid KOL/用户平台；low 聚合/自媒体/待验证。"""
    d = domain_of(url)
    if any(d == o or d.endswith("." + o) for o in OFFICIAL_DOMAINS):
        return "high"
    if any(d == a or d.endswith("." + a) for a in AUTHORITATIVE_MEDIA):
        return "high"
    if any(d == k or d.endswith("." + k) for k in KOL_PLATFORMS):
        return "mid"
    if any(a in d for a in AGGREGATORS) or not d:
        return "low"
    return "mid"


# ---------- 正文抽取（纯 stdlib） ----------

_SKIP_TAGS = {"script", "style", "noscript", "iframe", "svg", "template", "form"}
_BLOCK_TAGS = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "tr", "section", "article", "blockquote", "pre"}


class TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.chunks: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self.skip += 1
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self.skip = max(0, self.skip - 1)
        if tag in _BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if not self.skip:
            self._buf.append(data)

    def _flush(self):
        t = re.sub(r"\s+", " ", "".join(self._buf)).strip()
        if t:
            self.chunks.append(t)
        self._buf = []

    def text(self) -> str:
        self._flush()
        return "\n".join(self.chunks)


def _decode(resp) -> str:
    """手动解码：优先 Content-Type charset；utf-8 失败后回退 gb18030（中网页常见）。"""
    ct = resp.headers.get("Content-Type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    if m:
        try:
            return resp.content.decode(m.group(1), errors="replace")
        except LookupError:
            pass
    for enc in ("utf-8", "gb18030"):
        try:
            return resp.content.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return resp.text


def fetch_page(url: str, timeout: int = HTTP_TIMEOUT) -> tuple[str, int]:
    """抓取页面，返回 (html, status)。失败返回 ("", status)。"""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Referer": "https://www.baidu.com/"},
            timeout=timeout,
        )
        if resp.status_code == 200 and resp.content:
            return _decode(resp), 200
        return "", resp.status_code
    except requests.RequestException:
        return "", 0


def extract_article_text(html: str, max_chars: int = 6000) -> str:
    """从 HTML 抽取正文：按块聚合后取信息密度最高的前缀。"""
    ex = TextExtractor()
    try:
        ex.feed(html)
    except Exception:
        return ""
    text = ex.text()
    if not text.strip():
        return ""
    # 中文页通常正文集中在正文区，简单策略：取最长连续块及前后若干块
    return text[: max_chars * 2][:max_chars] if len(text) > max_chars else text


# ---------- 去重 ----------

def _norm_title(t: str) -> str:
    return re.sub(r"[\s\u3000\-—_··\"“”’‘，。！？、：；（）()【】\[\]]", "", t or "").lower()


def dedupe(items: list[dict], title_threshold: float = 0.86) -> list[dict]:
    """URL 规范化去重 + 标题相似度去重。保留 content/可信度更优者。"""
    seen_urls: set[str] = set()
    seen_titles: list[tuple[str, int]] = []
    out: list[dict] = []
    for it in items:
        u = urlparse(it.get("url", ""))
        norm = urlunparse((u.scheme, u.netloc, u.path, "", "", ""))
        if not norm or norm in seen_urls:
            continue
        t = _norm_title(it.get("title", ""))
        if not t:
            continue
        dup = None
        for pt, idx in seen_titles:
            if _similar(t, pt) >= title_threshold:
                dup = idx
                break
        if dup is not None:
            old = out[dup]
            # 保留下载到正文的、可信度更高的
            if (it.get("body") and not old.get("body")) or _rank(it) > _rank(old):
                out[dup] = it
            continue
        seen_urls.add(norm)
        seen_titles.append((t, len(out)))
        out.append(it)
    return out


def _similar(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _rank(it: dict) -> int:
    return {"low": 0, "mid": 1, "high": 2}.get(it.get("credibility"), 0)


# ---------- 编排 ----------

def _enabled_groups() -> list[dict]:
    """按设置页勾选（ENABLED_GROUPS）过滤信源组；全不勾时保留全部。"""
    enabled = [g for g in ENGINE_GROUPS if g["name"] in ENABLED_GROUPS]
    return enabled or ENGINE_GROUPS


def _relevant(item: dict, topic: str, add_terms: list[str] | None = None) -> bool:
    """主题相关性过滤：标题+摘要 需命中主题词或其 4-gram 核心片段（适配变体表述）。

    例：主题"外卖骑手困在系统里" → 4-gram 含"外卖骑手"，可命中
    《外卖骑手被算法困在系统里》这类变体标题。
    """
    terms = [t for t in (add_terms or []) + [topic] if len(t) >= 2]
    if not terms:
        return True
    hay = (item.get("title", "") + item.get("snippet", "")).lower()
    lo = [t.lower() for t in terms]
    if any(t in hay for t in lo):
        return True
    # 宽松匹配：所有 >=5 字词的 4-gram 片段（去空白）
    core: set[str] = set()
    for t in lo:
        t2 = t.replace(" ", "")
        if len(t2) >= 5:
            for i in range(len(t2) - 3):
                core.add(t2[i:i + 4])
                if len(core) > 60:
                    return any(c in hay for c in core)
    return any(c in hay for c in core)


# ---------- 多信源组：引擎受限时互补（2026-08-31 实测标定） ----------
# news 分类(ddg news) 质量最高；sogou wechat 打开微信深水区；baidu/bing 主流但易被限流；bilibili 视频舆情
ENGINE_GROUPS: list[dict] = [
    {"name": "news", "categories": "news"},
    {"name": "wechat", "engines": ["sogou wechat"]},
    {"name": "web", "engines": ["baidu", "bing", "google cse"]},
    {"name": "video", "engines": ["bilibili", "wikipedia"]},
]

# ---------- 编排 ----------

def collect_topic(
    topic: str,
    keywords: list[str],
    groups: list[dict] | None = None,
    time_range: str = "",
    fetch_top: int = 20,
    max_items_per_query: int = 30,
    group_hits_limit: int = 3,
) -> dict:
    """多信源组完整采集：搜索（按组）→ 相关性过滤 → 抓正文（top N）→ 去重 → 可信度。

    - 每个引擎组分别跑前 group_hits_limit 个关键词，兼顾覆盖与速度
    - 引擎受限（CAPTCHA/超时）按组记录，报告可声明数据局限
    """
    client = SearxClient()
    groups = groups or _enabled_groups()  # 按设置页勾选过滤（ENABLED_GROUPS）
    query_log: list[dict] = []
    raw: list[dict] = []

    for g in groups:
        group_kws = keywords[: group_hits_limit]
        ok_q = fail_q = 0
        unresponsive: list = []
        for kw in group_kws:
            data = client.search(
                kw,
                engines=g.get("engines"),
                categories=g.get("categories", "general"),
                time_range=time_range,
            )
            if data.get("error"):
                fail_q += 1
                continue
            ok_q += 1
            unresponsive.extend(data.get("unresponsive") or [])
            for r in data["results"][:max_items_per_query]:
                r["query"] = kw
                r["group"] = g["name"]
                raw.append(r)
        query_log.append({
            "group": g["name"],
            "engines": g.get("engines") or g.get("categories"),
            "ok_queries": ok_q,
            "failed_queries": fail_q,
            "unresponsive": unresponsive[:8],
        })
        time.sleep(0.3)

    items: list[dict] = []
    site_levels = _enabled_site_levels()
    hosts_sorted = sorted(site_levels, key=len, reverse=True)  # 长域名优先（如 mp.weixin.qq.com 先于 qq.com）
    for r in raw:
        dom = domain_of(r.get("url", ""))
        lvl = ""
        for h in hosts_sorted:
            if dom == h or dom.endswith("." + h):
                lvl = site_levels[h]
                break
        if site_levels and not lvl:
            continue  # 已勾选信源白名单，但本条未命中 → 过滤（勾选即聚焦）
        item = {
            "title": r["title"],
            "url": r["url"],
            "snippet": r["content"],
            "published": r["published"],
            "engine": r["engine"],
            "credibility": credibility(r.get("url", ""), r.get("engine", "")),
            "group": r.get("group", ""),
            "source_name": source_name(r.get("url", ""), r.get("title", "")),
            "level": lvl,
            "body": "",
        }
        if lvl:
            item["credibility"] = _LEVEL_CRED.get(lvl, item["credibility"])
        if _relevant(item, topic, keywords):
            items.append(item)

    if not items:
        return {"keywords": keywords, "total_raw": len(raw), "total_after_dedupe": 0,
                "body_fetched": 0, "items": [], "credibility_dist": {}, "query_log": query_log,
                "warning": "采集结果全部未通过相关性过滤或信源全部受限，请换关键词/时段后重试"}

    # 优先抓高可信 + 有发布时间的结果正文
    items.sort(key=lambda x: (x["credibility"] != "high", not bool(x["published"])))
    fetched = 0
    for it in items:
        if fetched >= fetch_top:
            break
        if it["body"]:
            continue
        html, status = fetch_page(it["url"])
        if status == 200:
            body = extract_article_text(html)
            if len(body) > 80:  # 有效正文（>80 字符）
                it["body"] = body
                fetched += 1

    deduped = dedupe(items)
    deduped.sort(key=lambda x: (_rank(x), bool(x["body"])), reverse=True)
    return {
        "keywords": keywords,
        "total_raw": len(raw),
        "total_after_dedupe": len(deduped),
        "body_fetched": sum(1 for x in deduped if x["body"]),
        "items": deduped,
        "credibility_dist": {
            c: sum(1 for x in deduped if x["credibility"] == c) for c in ("high", "mid", "low")
        },
        "query_log": query_log,
    }


if __name__ == "__main__":
    import json

    res = collect_topic("假装上班", ["假装上班公司", "假装上班公司 舆情", "付费上班 争议"])
    print(f"原始 {res['total_raw']} 条 → 去重后 {res['total_after_dedupe']} 条，"
          f"抓到正文 {res['body_fetched']} 条，可信度分布 {res['credibility_dist']}")
    if res.get("warning"):
        print("警告:", res["warning"])
    print("信源组状态:", [(q["group"], q["ok_queries"], q["failed_queries"]) for q in res.get("query_log", [])])
    for it in res["items"][:12]:
        print(f"[{it['credibility']}/{it['group']}] {it['title'][:35]} | {len(it['body'])}字 | {it['published'][:10]}")
    out = DATA_DIR_TASKS / "sample_collect.json"
    json.dump(res, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("素材已存:", out)