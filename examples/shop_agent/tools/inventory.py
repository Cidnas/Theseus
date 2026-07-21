"""Inventory tools for the shop database."""

from ..database import fetch_one


def check_inventory(
    product_id: int,
    requested_quantity: int = 1,
) -> dict[str, object]:
    """Check whether a requested quantity is available for one known product ID."""

    if requested_quantity <= 0:
        raise ValueError("requested quantity must be greater than zero")

    inventory = fetch_one(
        """
        SELECT
            products.id AS product_id,
            products.name AS product_name,
            inventory.units_in_stock
        FROM inventory
        JOIN products ON products.id = inventory.product_id
        WHERE products.id = ?
        """,
        (product_id,),
    )
    if inventory is None:
        raise ValueError(f"unknown product ID: {product_id}")

    units_in_stock = int(inventory["units_in_stock"])
    return {
        **inventory,
        "requested_quantity": requested_quantity,
        "can_fulfill": units_in_stock >= requested_quantity,
    }
