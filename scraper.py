"""
三平台爬虫:Mercari (煤炉)、Yahoo 拍卖、Rakuten (乐天)

每个平台用 Playwright 模拟浏览器访问,
通过 CSS selector 提取商品标题、价格、链接、图片。
"""

import time
import re
import logging
import urllib.parse

from playwright.sync_api import sync_playwright


logger = logging.getLogger(__name__)

# 用通用 PC UA,降低被识别为爬虫的概率
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# 每个平台之间的间隔,防触发限流
PLATFORM_DELAY = 3

PLATFORMS = ["Mercari", "Yahoo", "Rakuten"]


# ==========================================
# 价格解析
# ==========================================
def extract_price(text):
    """从混乱文本中提取价格,优先匹配带 ¥ 或 円 的格式"""
    if not text:
        return None

    match = re.search(r'[¥￥]\s*([\d,]+)|([\d,]+)\s*円', text)
    if match:
        return f"¥{match.group(1) or match.group(2)}"

    # 兜底:匹配连续 3 位以上数字
    match = re.search(r'([\d,]{3,})', text)
    if match:
        return f"¥{match.group(1)}"

    return None


# ==========================================
# URL 构造
# ==========================================
def build_search_url(platform, keyword):
    """根据平台和关键词构造搜索 URL"""
    safe_keyword = urllib.parse.quote(keyword)

    if platform == "Mercari":
        return (
            f"https://jp.mercari.com/search?"
            f"keyword={safe_keyword}&status=on_sale&sort=created_time&order=desc"
        )
    elif platform == "Yahoo":
        return (
            f"https://auctions.yahoo.co.jp/search/search?"
            f"p={safe_keyword}&va={safe_keyword}&exflg=1&b=1&n=50&s1=new&o1=d"
        )
    elif platform == "Rakuten":
        return f"https://search.rakuten.co.jp/search/mall/{safe_keyword}/?s=4"

    raise ValueError(f"未知平台: {platform}")


# ==========================================
# 平台解析器
# ==========================================
def parse_mercari(page, max_items):
    """解析煤炉的商品卡片"""
    items = []
    cards = page.locator('li[data-testid="item-cell"]').all()

    for card in cards[:max_items]:
        try:
            link_el = card.locator('a').first
            link = link_el.get_attribute('href', timeout=1000)
            if not link:
                continue

            img_el = card.locator('img').first
            title = img_el.get_attribute('alt', timeout=1000) or ""
            img_url = img_el.get_attribute('src', timeout=1000) or ""

            price = extract_price(card.inner_text())
            if not price or not title.strip():
                continue

            items.append({
                "title": title.strip(),
                "price": price,
                "url": f"https://jp.mercari.com{link}",
                "img_url": img_url
            })
        except Exception:
            continue

    return items


def parse_yahoo(page, max_items):
    """解析雅虎拍卖的商品卡片"""
    items = []
    cards = page.locator('li.Product').all()

    for card in cards[:max_items]:
        try:
            link_el = card.locator('a.Product__titleLink').first
            link = link_el.get_attribute('href', timeout=1000)
            if not link:
                continue

            title = link_el.inner_text()
            img_url = card.locator('img').first.get_attribute('src', timeout=1000) or ""
            price = extract_price(card.inner_text())

            if not price or not title.strip():
                continue

            items.append({
                "title": title.strip(),
                "price": price,
                "url": link,
                "img_url": img_url
            })
        except Exception:
            continue

    return items


def parse_rakuten(page, max_items):
    """解析乐天的商品卡片 (HTML 结构多变,多 selector 兼容)"""
    items = []
    cards = page.locator(
        '.searchresultitem, [class*="searchresultitem"], .item'
    ).all()

    for card in cards[:max_items]:
        try:
            link_el = card.locator('a').first
            link = link_el.get_attribute('href', timeout=1000)
            if not link:
                continue

            title_el = card.locator('[class*="title"], h2').first
            title = title_el.inner_text() if title_el.count() > 0 else ""

            img_url = card.locator('img').first.get_attribute('src', timeout=1000) or ""
            price = extract_price(card.inner_text())

            if not price or len(title.strip()) < 3:
                continue

            items.append({
                "title": title.strip(),
                "price": price,
                "url": link,
                "img_url": img_url
            })
        except Exception:
            continue

    return items


PARSERS = {
    "Mercari": parse_mercari,
    "Yahoo": parse_yahoo,
    "Rakuten": parse_rakuten,
}


# ==========================================
# 主流程
# ==========================================
def scrape_multiple_keywords(keywords, max_items_per_platform=30):
    """
    遍历所有 (平台, 关键词) 组合,返回合并后的商品列表。

    每条商品包含: category / title / price / url / img_url
    """
    logger.info(f"开始抓取 {len(keywords)} 个关键词,{len(PLATFORMS)} 个平台")
    all_items = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={'width': 1280, 'height': 800}
        )
        page = context.new_page()

        for platform in PLATFORMS:
            logger.info(f"平台: {platform}")

            for keyword in keywords:
                items = _scrape_one(page, platform, keyword, max_items_per_platform)
                for item in items:
                    item["category"] = f"[{platform}] {keyword}"
                all_items.extend(items)
                time.sleep(PLATFORM_DELAY)

        browser.close()

    logger.info(f"抓取完成,共 {len(all_items)} 条")
    return all_items


def _scrape_one(page, platform, keyword, max_items):
    """抓取单个 (平台, 关键词) 组合"""
    url = build_search_url(platform, keyword)
    logger.info(f"  {keyword}")

    try:
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)

        # 模拟滚动,加载更多商品和图片懒加载
        for _ in range(3):
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(1000)

        parser = PARSERS[platform]
        items = parser(page, max_items)
        logger.info(f"    抓到 {len(items)} 条")
        return items

    except Exception as e:
        logger.warning(f"    跳过 (页面加载失败): {e}")
        return []