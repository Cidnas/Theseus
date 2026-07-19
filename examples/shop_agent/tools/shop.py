"""Ordinary shop functions with no dependency on codeagent or Codex."""


def get_item_price(item: str) -> float:
    """Return the shop price for an item."""

    prices = {
        "coffee": 3.50,
        "notebook": 6.25,
        "pen": 1.20,
    }
    try:
        return prices[item.lower()]
    except KeyError as error:
        raise ValueError(f"unknown item: {item}") from error
