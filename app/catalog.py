from dataclasses import dataclass


@dataclass(frozen=True)
class SeedProduct:
    key: str
    title: str
    description: str
    price_rub: int | None
    price_stars: int | None
    emoji: str
    kind: str = "physical"
    requires_brief: bool = False


REMOVED_PRODUCT_KEYS = ("song", "stickers", "case", "merch")
PRODUCT_SEEDS: tuple[SeedProduct, ...] = ()
