"""Category-equivalent facts for required owned activity in a mission."""

# Exact, whole activity labels. A narrower term such as "luxury skincare store"
# never acquires a generic storefront binding merely by containing "store".
ACTIVITY_ALIASES = {
    "company.owned_storefront": (
        "online store", "ecommerce store", "e-commerce store", "online storefront",
        "operate an online store", "sell products online",
    ),
    "company.news_publisher": (
        "news publisher", "digital news publisher", "online newspaper", "news publication",
        "publish news online", "publish editorial content",
    ),
    "company.editorial_blog": (
        "company blog", "corporate blog", "active company blog", "maintain a company blog",
        "publish a company blog", "publish editorial content",
    ),
    "company.event_program": (
        "public event program", "public events programme", "public event calendar",
        "public event schedule", "publish a public event schedule",
    ),
}


def activity_keys(terms):
    labels = set(terms)
    return [key for key, aliases in ACTIVITY_ALIASES.items() if labels.intersection(aliases)]
