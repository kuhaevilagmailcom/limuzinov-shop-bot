import unittest
from types import SimpleNamespace

from channel_backfill import entity_ref, normalized_content, parse_author, publication_link


class ChannelBackfillParsingTests(unittest.TestCase):
    def test_private_channel_id_is_converted_to_integer(self):
        self.assertEqual(entity_ref("-1001234567890"), -1001234567890)
        self.assertEqual(entity_ref("@PublicChannel"), "@PublicChannel")

    def test_parses_sender_footer(self):
        parsed = parse_author(
            "Пост\n\n━━━━━━━━━━━━━━\n👤 Отправитель: @Poster_1 · ID 123456\n👤 Имя: Иван Иванов"
        )
        self.assertEqual(parsed, (123456, "Poster_1", "Иван Иванов"))

    def test_parses_sender_without_username(self):
        parsed = parse_author("Отправитель: без username · ID 99\nИмя: Пользователь")
        self.assertEqual(parsed, (99, None, "Пользователь"))

    def test_extracts_legacy_link_from_entity(self):
        message = SimpleNamespace(
            raw_text="Удалить пост",
            entities=[SimpleNamespace(url="https://t.me/PozorMgnBot?start=remove_42_abc-DEF_1")],
            buttons=None,
        )
        self.assertEqual(publication_link(message), (42, "abc-DEF_1"))

    def test_content_key_ignores_service_footers(self):
        moderation = "Текст поста\n\n━━━━━━━━━━━━━━\nОтправитель: @name · ID 1"
        publication = "Текст поста\n\nThis channel fully complies with all platform rules"
        self.assertEqual(normalized_content(moderation), normalized_content(publication))
