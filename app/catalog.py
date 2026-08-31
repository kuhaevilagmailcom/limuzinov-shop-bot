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


PRODUCT_SEEDS: tuple[SeedProduct, ...] = (
    SeedProduct(
        key="song",
        title="Создать песню",
        description="Персональная песня по вашему сюжету, настроению и пожеланиям.",
        price_rub=None,
        price_stars=350,
        emoji="🎵",
        kind="digital",
        requires_brief=True,
    ),
    SeedProduct(
        key="stickers",
        title="Набор стикеров",
        description="Фирменный набор виниловых стикеров.",
        price_rub=390,
        price_stars=None,
        emoji="✨",
    ),
    SeedProduct(
        key="case",
        title="Чехол",
        description="Фирменный защитный чехол.",
        price_rub=1290,
        price_stars=None,
        emoji="📱",
    ),
    SeedProduct(
        key="merch",
        title="Футболка",
        description="Фирменная футболка Limuzinov Shop.",
        price_rub=2490,
        price_stars=None,
        emoji="👕",
    ),
)
