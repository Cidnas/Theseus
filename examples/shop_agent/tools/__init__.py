"""The complete set of database functions exposed to the shop agent."""

from .customers import find_customer
from .inventory import check_inventory
from .orders import get_order_items, list_customer_orders
from .products import get_product, search_products


TOOLS = [
    find_customer,
    search_products,
    get_product,
    list_customer_orders,
    get_order_items,
    check_inventory,
]

__all__ = [
    "TOOLS",
    "check_inventory",
    "find_customer",
    "get_order_items",
    "get_product",
    "list_customer_orders",
    "search_products",
]
