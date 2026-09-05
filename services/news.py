"""News aggregation service — fetches from NewsAPI and Google News RSS."""

from __future__ import annotations

import logging
from datetime import datetime

import feedparser
import httpx

from config import Config

logger = logging.getLogger(__name__)

# ─── NewsAPI ──────────────────────────────────────────────────────────────────

NEWSAPI_BASE = "https://newsapi.org/v2"


async def fetch_newsapi_headlines(
    categories: list[str] | None = None,
    country: str = "us",
    page_size: int = 5,
) -> list[dict]:
    """Fetch top headlines from NewsAPI across multiple categories.

    Args:
        categories: List of categories (general, technology, business, science).
        country: Country code for headlines.
        page_size: Number of articles per category.

    Returns:
        List of article dicts with title, description, source, url.
    """
    if not Config.NEWS_API_KEY:
        logger.warning("NEWS_API_KEY not set, skipping NewsAPI.")
        return []

    categories = categories or Config.NEWS_CATEGORIES
    all_articles: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for category in categories:
            try:
                resp = await client.get(
                    f"{NEWSAPI_BASE}/top-headlines",
                    params={
                        "country": country,
                        "category": category,
                        "pageSize": page_size,
                        "apiKey": Config.NEWS_API_KEY,
                    },
                )
                resp.raise_for_status()
                data = resp.json()

                for article in data.get("articles", []):
                    if article.get("title") and "[Removed]" not in article["title"]:
                        all_articles.append({
                            "title": article["title"],
                            "description": article.get("description", ""),
                            "source": article.get("source", {}).get("name", "Unknown"),
                            "url": article.get("url", ""),
                            "category": category,
                            "published_at": article.get("publishedAt", ""),
                        })

                logger.info("Fetched %d articles from NewsAPI [%s]", len(data.get("articles", [])), category)

            except httpx.HTTPError as e:
                logger.error("NewsAPI error for category '%s': %s", category, e)
            except Exception:
                logger.exception("Unexpected error fetching NewsAPI [%s]", category)

    return all_articles


# ─── Google News RSS (Fallback) ──────────────────────────────────────────────

GOOGLE_NEWS_RSS = "https://news.google.com/rss"
GOOGLE_NEWS_TOPICS = {
    "world": "WORLD",
    "technology": "TECHNOLOGY",
    "business": "BUSINESS",
    "science": "SCIENCE",
}


async def fetch_google_news_rss(topics: list[str] | None = None, limit: int = 5) -> list[dict]:
    """Fetch headlines from Google News RSS feeds.

    Used as a fallback when NewsAPI quota is exceeded.

    Args:
        topics: List of topic names (world, technology, business, science).
        limit: Max articles per topic.

    Returns:
        List of article dicts.
    """
    topics = topics or list(GOOGLE_NEWS_TOPICS.keys())
    all_articles: list[dict] = []

    async with httpx.AsyncClient(timeout=30) as client:
        for topic in topics:
            topic_code = GOOGLE_NEWS_TOPICS.get(topic, topic.upper())
            url = f"{GOOGLE_NEWS_RSS}/topics/{topic_code}"

            try:
                resp = await client.get(url)
                resp.raise_for_status()
                feed = feedparser.parse(resp.text)

                for entry in feed.entries[:limit]:
                    all_articles.append({
                        "title": entry.get("title", ""),
                        "description": entry.get("summary", ""),
                        "source": entry.get("source", {}).get("title", "Google News"),
                        "url": entry.get("link", ""),
                        "category": topic,
                        "published_at": entry.get("published", ""),
                    })

                logger.info("Fetched %d articles from Google News RSS [%s]", len(feed.entries[:limit]), topic)

            except Exception:
                logger.exception("Google News RSS error for topic '%s'", topic)

    return all_articles


# ─── Combined Fetcher ────────────────────────────────────────────────────────


async def fetch_all_news() -> str:
    """Fetch news from all sources and format as raw text for AI curation.

    Tries NewsAPI first, falls back to Google News RSS.

    Returns:
        Formatted string with all raw articles for the AI curator.
    """
    articles = await fetch_newsapi_headlines()

    # Fallback to Google News if NewsAPI returned nothing
    if not articles:
        logger.info("NewsAPI returned no articles, falling back to Google News RSS.")
        articles = await fetch_google_news_rss()

    if not articles:
        return "No news articles available at this time."

    # Deduplicate by title similarity (simple approach)
    seen_titles: set[str] = set()
    unique_articles: list[dict] = []
    for article in articles:
        # Simple dedup: lowercase first 50 chars of title
        key = article["title"][:50].lower().strip()
        if key not in seen_titles:
            seen_titles.add(key)
            unique_articles.append(article)

    # Format for AI curation
    lines = [f"=== RAW NEWS ARTICLES ({datetime.now().strftime('%Y-%m-%d')}) ===\n"]
    for i, article in enumerate(unique_articles, 1):
        lines.append(f"--- Article {i} ---")
        lines.append(f"Title: {article['title']}")
        lines.append(f"Source: {article['source']}")
        lines.append(f"Category: {article['category']}")
        lines.append(f"Description: {article['description']}")
        lines.append(f"URL: {article['url']}")
        lines.append(f"Published: {article['published_at']}")
        lines.append("")

    logger.info("Prepared %d unique articles for curation.", len(unique_articles))
    return "\n".join(lines)
