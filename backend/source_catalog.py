# -*- coding: utf-8 -*-
"""内置信源目录（C1a 播种数据）。

结构对齐规划《信源精细化规划.md》：
L0 等级 S/A/B/C/D ｜ L2 类别 ｜ L3 条目（name/host/category/level/stype/extra）
stype: site(网站) | hotlist(热榜) | feed(JSON Feed/RSS)
extra: {url, filter_site} 等采集参数（字段名与后端采集器约定）。
"""
from __future__ import annotations

# 类别元数据（设置页分组展示用）：key -> (中文名, 图标)
CATEGORIES: dict[str, dict] = {
    "gov":            {"label": "政府权威", "icon": "🏛️"},
    "media":          {"label": "权威媒体", "icon": "📰"},
    "finance":        {"label": "财经综合", "icon": "📈"},
    "tech":           {"label": "科技行业", "icon": "💻"},
    "portal":         {"label": "门户平台", "icon": "🧭"},
    "social":         {"label": "社区社交", "icon": "💬"},
    "overseas_main":  {"label": "海外主流媒体", "icon": "🌍"},
    "overseas_cn":    {"label": "境外华文", "icon": "🌏"},
    "overseas_comm":  {"label": "海外社区", "icon": "🗣️"},
    "hotlist":        {"label": "热榜榜单", "icon": "🔥"},
}

# Buzzing 中属于海外主流媒体的来源（_site_identifier 白名单，实测切片）
BUZZ_MEDIA_SITES = (
    "bbc,reuters,news,nytimes,ft,wsj,bloombergnew,economist,economistnew,theguardian,"
    "atlantic,newyorker,bloomberg,politico,axios,sky,businessinsider,yahoo,finance,"
    "reutersnew,googlenews,china,arstechnica"
).split(",")
# 属于海外社区/线索的来源
BUZZ_COMM_SITES = ("hn,showhn,askhn,lobste,ph,dev").split(",")

CATALOG: list[dict] = [
    # ---------- 政府权威（S，事实锚点） ----------
    {"name": "中国政府网", "host": "gov.cn", "category": "gov", "level": "S"},
    {"name": "中央纪委国家监委", "host": "ccdi.gov.cn", "category": "gov", "level": "S"},
    {"name": "最高人民法院", "host": "court.gov.cn", "category": "gov", "level": "S"},
    {"name": "最高人民检察院", "host": "spp.gov.cn", "category": "gov", "level": "S"},
    {"name": "国家发展改革委", "host": "ndrc.gov.cn", "category": "gov", "level": "S"},
    {"name": "外交部", "host": "mfa.gov.cn", "category": "gov", "level": "S"},
    {"name": "工业和信息化部", "host": "miit.gov.cn", "category": "gov", "level": "S"},
    {"name": "教育部", "host": "moe.gov.cn", "category": "gov", "level": "S"},
    {"name": "公安部", "host": "mps.gov.cn", "category": "gov", "level": "S"},
    {"name": "财政部", "host": "mof.gov.cn", "category": "gov", "level": "S"},
    {"name": "市场监管总局", "host": "samr.gov.cn", "category": "gov", "level": "S"},
    {"name": "国家统计局", "host": "stats.gov.cn", "category": "gov", "level": "S"},
    {"name": "中国人民银行", "host": "pbc.gov.cn", "category": "gov", "level": "S"},
    {"name": "国防部", "host": "mod.gov.cn", "category": "gov", "level": "S"},

    # ---------- 权威媒体（S/A） ----------
    {"name": "人民日报", "host": "people.com.cn,peopleapp.com", "category": "media", "level": "S"},
    {"name": "新华社", "host": "xinhuanet.com,news.cn", "category": "media", "level": "S"},
    {"name": "央视新闻", "host": "cctv.com", "category": "media", "level": "S"},
    {"name": "央广网", "host": "cnr.cn", "category": "media", "level": "S"},
    {"name": "光明网", "host": "gmw.cn", "category": "media", "level": "S"},
    {"name": "中国新闻网", "host": "chinanews.com", "category": "media", "level": "S"},
    {"name": "环球网", "host": "huanqiu.com", "category": "media", "level": "A"},
    {"name": "中国青年报", "host": "cyol.com", "category": "media", "level": "S"},
    {"name": "人民政协报", "host": "rmzxb.com.cn", "category": "media", "level": "A"},
    {"name": "凤凰网", "host": "ifeng.com", "category": "media", "level": "A"},
    {"name": "中国网", "host": "china.com.cn", "category": "media", "level": "A"},
    {"name": "经济参考报", "host": "jjckb.cn", "category": "media", "level": "A"},
    {"name": "证券时报", "host": "stcn.com", "category": "media", "level": "A"},
    {"name": "财联社", "host": "cls.cn", "category": "media", "level": "A"},
    {"name": "观察者网", "host": "guancha.cn", "category": "media", "level": "A"},
    {"name": "参考消息", "host": "cankaoxiaoxi.com", "category": "media", "level": "A"},

    # ---------- 财经综合（A/B） ----------
    {"name": "澎湃新闻", "host": "thepaper.cn", "category": "finance", "level": "A"},
    {"name": "界面新闻", "host": "jiemian.com", "category": "finance", "level": "A"},
    {"name": "第一财经", "host": "yicai.com", "category": "finance", "level": "A"},
    {"name": "财新网", "host": "caixin.com", "category": "finance", "level": "A"},
    {"name": "21世纪经济报道", "host": "21jingji.com", "category": "finance", "level": "A"},
    {"name": "中国经济网", "host": "ce.cn", "category": "finance", "level": "A"},
    {"name": "每日经济新闻", "host": "nbd.com.cn", "category": "finance", "level": "B"},
    {"name": "新京报", "host": "bjnews.com.cn", "category": "finance", "level": "A"},
    {"name": "南方都市报", "host": "nddaily.com", "category": "finance", "level": "B"},
    {"name": "封面新闻", "host": "thecover.cn", "category": "finance", "level": "B"},
    {"name": "红星新闻", "host": "redstar.com.cn", "category": "finance", "level": "B"},

    # ---------- 科技行业（B） ----------
    {"name": "36氪", "host": "36kr.com", "category": "tech", "level": "B"},
    {"name": "虎嗅", "host": "huxiu.com", "category": "tech", "level": "B"},
    {"name": "钛媒体", "host": "tmtpost.com", "category": "tech", "level": "B"},
    {"name": "爱范儿", "host": "ifanr.com", "category": "tech", "level": "B"},
    {"name": "晚点LatePost", "host": "latepost.com", "category": "tech", "level": "B"},
    {"name": "雷锋网", "host": "leiphone.com", "category": "tech", "level": "B"},
    {"name": "IT之家", "host": "ithome.com", "category": "tech", "level": "B"},

    # ---------- 门户平台（C） ----------
    {"name": "微信公众号", "host": "mp.weixin.qq.com", "category": "portal", "level": "C"},
    {"name": "百度百家号", "host": "baijiahao.baidu.com", "category": "portal", "level": "C"},
    {"name": "今日头条", "host": "toutiao.com", "category": "portal", "level": "C"},
    {"name": "搜狐新闻", "host": "sohu.com", "category": "portal", "level": "C"},
    {"name": "网易新闻", "host": "163.com", "category": "portal", "level": "C"},
    {"name": "腾讯新闻", "host": "qq.com", "category": "portal", "level": "C"},
    {"name": "新浪新闻", "host": "sina.com.cn", "category": "portal", "level": "C"},

    # ---------- 社区社交（C） ----------
    {"name": "微博", "host": "weibo.com,weibo.cn", "category": "social", "level": "C"},
    {"name": "知乎", "host": "zhihu.com", "category": "social", "level": "C"},
    {"name": "B站", "host": "bilibili.com", "category": "social", "level": "C"},
    {"name": "小红书", "host": "xiaohongshu.com", "category": "social", "level": "C"},
    {"name": "百度贴吧", "host": "tieba.baidu.com", "category": "social", "level": "C"},
    {"name": "豆瓣", "host": "douban.com", "category": "social", "level": "C"},
    {"name": "V2EX", "host": "v2ex.com", "category": "social", "level": "C"},

    # ---------- 海外主流媒体（A，Buzzing 媒体切片 + 华文） ----------
    {"name": "Buzzing·海外主流媒体", "host": "", "category": "overseas_main", "level": "A",
     "stype": "feed", "enabled": False,
     "extra": {"url": "https://www.buzzing.cc/feed.json", "filter_site": BUZZ_MEDIA_SITES}},
    {"name": "BBC", "host": "bbc.com,bbc.co.uk", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "路透社 Reuters", "host": "reuters.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "纽约时报 NYT", "host": "nytimes.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "金融时报 FT", "host": "ft.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "华尔街日报 WSJ", "host": "wsj.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "彭博 Bloomberg", "host": "bloomberg.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "经济学人 Economist", "host": "economist.com", "category": "overseas_main", "level": "A", "enabled": False},
    {"name": "卫报 The Guardian", "host": "theguardian.com", "category": "overseas_main", "level": "A", "enabled": False},

    # ---------- 境外华文（A） ----------
    {"name": "香港01", "host": "hk01.com", "category": "overseas_cn", "level": "A", "enabled": False},
    {"name": "星岛头条", "host": "stheadline.com", "category": "overseas_cn", "level": "A", "enabled": False},
    {"name": "联合早报", "host": "zaobao.com.sg", "category": "overseas_cn", "level": "A", "enabled": False},
    {"name": "明报", "host": "mingpao.com", "category": "overseas_cn", "level": "A", "enabled": False},
    {"name": "FT中文网", "host": "ftchinese.com", "category": "overseas_cn", "level": "A", "enabled": False},

    # ---------- 海外社区（C） ----------
    {"name": "Buzzing·海外社区", "host": "", "category": "overseas_comm", "level": "C",
     "stype": "feed", "enabled": False,
     "extra": {"url": "https://www.buzzing.cc/feed.json", "filter_site": BUZZ_COMM_SITES}},

    # ---------- 热榜榜单（tophub 聚合解析，2026-09-01 实测 id） ----------
    {"name": "微博热搜", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "weibo", "newsnow_id": "weibo", "tophub_id": "KqndgxeLl9"}},
    {"name": "知乎热榜", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "zhihu", "newsnow_id": "zhihu", "tophub_id": "mproPpoq6O"}},
    {"name": "B站热门", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "bilibili", "newsnow_id": "bilibili", "tophub_id": "74KvxwokxM"}},
    {"name": "百度热搜", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "baidu", "newsnow_id": "baidu", "tophub_id": "Jb0vmloB1G"}},
    {"name": "抖音热点", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "douyin", "newsnow_id": "douyin", "tophub_id": "DpQvNABoNE"}},
    {"name": "今日头条热榜", "host": "", "category": "hotlist", "level": "C",
     "stype": "hotlist", "extra": {"board": "toutiao", "newsnow_id": "toutiao", "tophub_id": "x9ozB4KoXb"}},
]
