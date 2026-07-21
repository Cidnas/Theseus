"""Product catalog tools for the shop database."""

from ..database import fetch_all, fetch_one


def search_products(query: str) -> list[dict[str, object]]:
    """Search product names, SKUs, and categories when a product ID is unknown."""

    term = query.strip()
    if not term:
        raise ValueError("product search query cannot be empty")
    pattern = f"%{term}%"
    return fetch_all(
        """
        SELECT
            id AS product_id,
            sku,
            name,
            category,
            price_cents / 100.0 AS price
        FROM products
        WHERE name LIKE ? OR sku LIKE ? OR category LIKE ?
        ORDER BY name
        LIMIT 10
        """,
        (pattern, pattern, pattern),
    )


def get_product(product_id: int) -> dict[str, object]:
    """Return catalog details for one known product ID; this does not return stock."""

    product = fetch_one(
        """
        SELECT
            id AS product_id,
            sku,
            name,
            category,
            price_cents / 100.0 AS price
        FROM products
        WHERE id = ?
        """,
        (product_id,),
    )
    if product is None:
        raise ValueError(f"unknown product ID: {product_id}")
    return product
