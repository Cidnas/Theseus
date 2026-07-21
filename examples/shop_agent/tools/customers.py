"""Customer lookup tools for the shop database."""

from ..database import fetch_one


def find_customer(email: str) -> dict[str, object]:
    """Find one customer by exact email address and return their customer ID."""

    customer = fetch_one(
        """
        SELECT id AS customer_id, name, email
        FROM customers
        WHERE lower(email) = lower(?)
        """,
        (email.strip(),),
    )
    if customer is None:
        raise ValueError(f"no customer found for email: {email}")
    return customer
