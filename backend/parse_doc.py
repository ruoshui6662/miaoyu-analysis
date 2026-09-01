# -*- coding: utf-8 -*-
"""临时：解析 tophubdata 文档页，找 API 端点与用法。"""
import re
import json

html = open(r"D:\AI编程\舆情\thd_doc.html", encoding="utf-8", errors="replace").read()
out = {"title": "", "urls": [], "text_hits": {}}
t = re.search(r"<title>(.*?)</title>", html, re.S)
out["title"] = t.group(1).strip()[:60] if t else "?"
out["urls"] = list(dict.fromkeys(re.findall(r"https?://[^\"'<> \\)]+", html)))[:25]
text = re.sub(r"<[^>]+>", " ", html)
text = re.sub(r"\s+", " ", text)
for frag in ["1000", "每日", "密钥", "调用", "sign", "timestamp", "实时"]:
    i = text.find(frag)
    if i > 0:
        out["text_hits"][frag] = text[max(0, i - 90): i + 140]
with open(r"D:\AI编程\舆情\thd_parsed.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print("saved")