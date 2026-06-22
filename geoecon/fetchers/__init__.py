"""Multi-strategy discovery fetchers (rss / scrape / api), dispatched by fetch_type."""
from .base import DiscoveredItem, Fetcher, get_fetcher

__all__ = ["DiscoveredItem", "Fetcher", "get_fetcher"]
