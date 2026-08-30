from dataclasses import dataclass


@dataclass(frozen=True)
class Product:
    key: str
    title: str
    description: str
    price_rub: int
    emoji: str


# Безопасный пример каталога физических товаров.
# Никотиновая/вейп-продукция намеренно не включена.
PRODUCTS: dict[str, Product] = {
    "stickers": Product(
        key="stickers",
        title="Набор стикеров",
        description="Фирменный набор виниловых стикеров.",
        price_rub=390,
        emoji="✨",
    ),
    "case": Product(
        key="case",
        title="Чехол",
        description="Фирменный защитный чехол.",
        price_rub=1290,
        emoji="📱",
    ),
    "merch": Product(
        key="merch",
        title="Футболка",
        description="Фирменная футболка Limuzinov Shop.",
        price_rub=2490,
        emoji="👕",
    ),
}

SONG_ORDER_STARS = 350
