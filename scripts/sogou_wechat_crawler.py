#!/usr/bin/env python3
"""
搜狗微信搜索爬虫 — 搜索 AR/XR 相关公众号文章并推送到 Bark

用法:
  python scripts/sogou_wechat_crawler.py              # 使用 config/frequency_words.txt 过滤
  python scripts/sogou_wechat_crawler.py --dry-run     # 只打印匹配文章，不推送
  python scripts/sogou_wechat_crawler.py --push-all    # 推送所有搜索结果（不过滤）

配置:
  BARK_URL 环境变量，默认 https://api.day.app/FdQjGhRFW3rK9WnesKzAXB/
"""

import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

# ── 配置 ──────────────────────────────────────────────────────────
BARK_URL = os.environ.get(
    "BARK_URL",
    "https://api.day.app/FdQjGhRFW3rK9WnesKzAXB/",
)
SOGOU_KEYWORDS = [
    "Rokid",
    "XREAL",
    "雷鸟 AR",
    "影目 AR",
    "AR眼镜",
    "AI眼镜",
    "智能眼镜",
    "增强现实",
    "空间计算",
]
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
SEEN_URLS = set()


# ── 频率词加载（复现 TrendRadar 逻辑）─────────────────────────────
def load_frequency_words(filepath: str = "config/frequency_words.txt"):
    """加载 frequency_words.txt，返回 (word_groups, global_filters)"""
    if not os.path.exists(filepath):
        return [], []

    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    groups_raw = content.split("\n\n")
    word_groups = []
    global_filters = []
    in_global = False
    in_word_groups = False

    for block in groups_raw:
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n")
        first = lines[0].strip()

        if first == "[GLOBAL_FILTER]":
            in_global = True
            in_word_groups = False
            for line in lines[1:]:
                line = line.strip()
                if line and not line.startswith("#"):
                    global_filters.append(line)
            continue
        elif first == "[WORD_GROUPS]":
            in_global = False
            in_word_groups = True
            continue
        elif first.startswith("[") and first.endswith("]"):
            in_global = False
            in_word_groups = False
            group_name = first[1:-1]
            keywords = []
            for line in lines[1:]:
                line = line.strip()
                if line and not line.startswith("#"):
                    keywords.append(line)
            word_groups.append((group_name, keywords))
        else:
            if in_global:
                for line in lines:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        global_filters.append(line)
            elif in_word_groups:
                pass
            else:
                pass

    return word_groups, global_filters


def matches_filter(title: str, word_groups, global_filters) -> tuple:
    """
    检查标题是否匹配关键词过滤。
    返回 (matched: bool, group_name: str or None)
    匹配逻辑：
    - 如果标题包含任意 global_filter 词 → 不匹配
    - + 开头的词是必须词（该组内所有 + 词必须全部出现）
    - 普通词：任意一个存在即匹配
    - /正则/ => 别名 格式
    """
    title_lower = title.lower()

    # 全局过滤
    for fw in global_filters:
        fw_stripped = fw.strip()
        if not fw_stripped:
            continue
        if fw_stripped.startswith("/") and "=>" in fw_stripped:
            pattern = fw_stripped.split("=>")[0].strip().strip("/")
            if re.search(pattern, title, re.IGNORECASE):
                return False, None
        elif fw_stripped in title or fw_stripped.lower() in title_lower:
            return False, None

    # 词组匹配
    for group_name, keywords in word_groups:
        required = [k for k in keywords if k.startswith("+")]
        optional = [k for k in keywords if not k.startswith("+") and not k.startswith("/") and not k.startswith("!") and not k.startswith("@")]
        regex_rules = [k for k in keywords if k.startswith("/") and "=>" in k]
        negations = [k for k in keywords if k.startswith("!")]

        # 必须词检查
        if required:
            required_texts = [r[1:] for r in required]
            if not all(t in title or t.lower() in title_lower for t in required_texts):
                continue

        # 过滤词检查
        has_negation = False
        for neg in negations:
            neg_text = neg[1:]
            if neg_text in title or neg_text.lower() in title_lower:
                has_negation = True
                break
        if has_negation:
            continue

        # 正则检查
        matched_regex = False
        for rule in regex_rules:
            pattern = rule.split("=>")[0].strip().strip("/")
            if re.search(pattern, title, re.IGNORECASE):
                matched_regex = True
                break

        # 普通词检查
        matched_optional = False
        for opt in optional:
            if opt in title or opt.lower() in title_lower:
                matched_optional = True
                break

        if matched_regex or matched_optional or (not regex_rules and not optional and not required):
            return True, group_name

    return False, None


# ── 搜狗搜索 ─────────────────────────────────────────────────────
def sogou_search(keyword: str) -> list:
    """搜索 Sogou 微信，返回文章列表"""
    url = f"https://weixin.sogou.com/weixin?type=2&query={urllib.request.quote(keyword)}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  [WARN] 搜索 {keyword} 失败: {e}")
        return []

    articles = []
    # 匹配文章块: <div class="news-box"> 下的每个 .news-list2
    blocks = re.findall(
        r'<div class="txt-box[^"]*">(.*?)</div>\s*</div>\s*</div>',
        html, re.DOTALL
    )

    for block in blocks:
        # 提取标题
        title_match = re.search(r'<a[^>]+target="_blank"[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL)
        if not title_match:
            continue
        title = re.sub(r'<[^>]+>', "", title_match.group(1)).strip()
        if not title:
            continue

        # 提取链接
        link_match = re.search(r'href="(https?://mp\.weixin\.qq\.com[^"]+)"', block)
        if not link_match:
            link_match = re.search(r'href="(/[^"]+)"', block)
            if link_match:
                url_full = "https://weixin.sogou.com" + link_match.group(1)
            else:
                continue
        else:
            url_full = link_match.group(1)

        # 去重
        if url_full in SEEN_URLS:
            continue
        SEEN_URLS.add(url_full)

        # 提取摘要
        summary_match = re.search(r'<p class="str_info[^"]*">(.*?)</p>', block, re.DOTALL)
        summary = re.sub(r'<[^>]+>', "", summary_match.group(1)).strip() if summary_match else ""

        # 提取时间
        time_match = re.search(r'(\d{4}-\d{2}-\d{2})', block)
        pub_date = time_match.group(1) if time_match else ""

        # 提取来源公众号
        source_match = re.search(r'<a[^>]+rel="nofollow"[^>]*>\s*(.*?)\s*</a>', block, re.DOTALL)
        source = re.sub(r'<[^>]+>', "", source_match.group(1)).strip() if source_match else ""

        articles.append({
            "title": title,
            "url": url_full,
            "summary": summary,
            "date": pub_date,
            "source": source,
            "search_keyword": keyword,
        })

    print(f"  搜索 '{keyword}': 找到 {len(blocks)} 个块, {len(articles)} 篇文章")
    return articles


def send_bark(articles: list):
    """发送匹配文章到 Bark"""
    if not articles:
        print("  没有匹配文章，不推送")
        return

    # 分组显示
    lines = []
    for a in articles[:10]:  # 最多 10 条
        lines.append(f"  {a.get('group_name', '')} | {a['title'][:40]}")
        if a['source']:
            lines[-1] += f" [{a['source']}]"

    content = "\n".join(lines)
    title = f"微信搜索 {len(articles)} 条 AR/XR 文章"

    payload = json.dumps({
        "title": title,
        "content": content,
        "group": "TrendRadar-WeChat",
    }).encode("utf-8")

    req = urllib.request.Request(
        BARK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        print(f"  Bark 推送成功: {resp.status}")
    except Exception as e:
        print(f"  Bark 推送失败: {e}")


def main():
    dry_run = "--dry-run" in sys.argv
    push_all = "--push-all" in sys.argv

    # 加载频率词
    word_groups, global_filters = load_frequency_words()
    print(f"加载频率词: {len(word_groups)} 组, {len(global_filters)} 个全局过滤词")
    for name, kws in word_groups:
        print(f"  [{name}]: {len(kws)} 个关键词")

    # 搜索每个关键词
    all_articles = []
    for kw in SOGOU_KEYWORDS:
        articles = sogou_search(kw)
        all_articles.extend(articles)
        time.sleep(1.5)  # 间隔，避免被封

    print(f"\n总共找到 {len(all_articles)} 篇文章（去重后）")

    # 频率词过滤
    matched = []
    for a in all_articles:
        if push_all:
            matched.append(a)
            continue
        is_match, group_name = matches_filter(a["title"], word_groups, global_filters)
        if is_match:
            a["group_name"] = group_name or "未分组"
            matched.append(a)

    matched.sort(key=lambda x: x.get("group_name", ""))
    print(f"关键词过滤后: {len(matched)} 篇匹配文章\n")

    for a in matched:
        print(f"  [{a.get('group_name', '?')}] {a['title'][:60]}")
        if a["source"]:
            print(f"    ← {a['source']} | {a['date']}")
        print(f"    {a['url']}")
        print()

    # 推送
    if not dry_run and matched:
        send_bark(matched)
    elif dry_run:
        print("[DRY RUN] 不推送")


if __name__ == "__main__":
    main()
