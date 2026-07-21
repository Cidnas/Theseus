"""Order history tools for the shop database."""

from ..database import fetch_all, fetch_one


def list_customer_orders(customer_id: int) -> list[dict[str, object]]:
    """List a known customer's orders newest first, without their line items."""

    customer = fetch_one(
        "SELECT id FROM customers WHERE id = ?",
        (customer_id,),
    )
    if customer is None:
        raise ValueError(f"unknown customer ID: {customer_id}")

    return fetch_all(
        """
        SELECT
            orders.id AS order_id,
            orders.ordered_at,
            orders.status,
            SUM(order_items.quantity) AS item_count
        FROM orders
        JOIN order_items ON order_items.order_id = orders.id
        WHERE orders.customer_id = ?
        GROUP BY orders.id
        ORDER BY orders.ordered_at DESC
        """,
        (customer_id,),
    )


def get_order_items(order_id: int) -> list[dict[str, object]]:
    """Return product IDs, names, quantities, and prices for one known order ID."""

    order = fetch_one("SELECT id FROM orders WHERE id = ?", (order_id,))
    if order is None:
        raise ValueError(f"unknown order ID: {order_id}")

    return fetch_all(
        """
        SELECT
            products.id AS product_id,
            products.sku,
            products.name AS product_name,
            order_items.quantity,
            order_items.unit_price_cents / 100.0 AS unit_price
        FROM order_items
        JOIN products ON products.id = order_items.product_id
        WHERE order_items.order_id = ?
        ORDER BY products.name
        """,
        (order_id,),
    )
