from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.editorial.services.ad_link_exclusion_service import (
    AdLinkExclusionService,
    normalize_ad_link,
    normalized_ad_link_is_excluded,
)
from src.master import MasterBot
from src.panel_markups import build_ad_link_exclusions_panel, build_main_panel


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Example.com/path/", "example.com/path"),
        ("http://www.example.com/path", "example.com/path"),
        ("example.com/path#section", "example.com/path"),
        ("@Some_Channel", "t.me/some_channel"),
        ("https://telegram.me/Some_Channel", "t.me/some_channel"),
        ("https://example.com:443/path", "example.com/path"),
    ],
)
def test_normalize_ad_link(value: str, expected: str) -> None:
    assert normalize_ad_link(value) == expected


@pytest.mark.parametrize("value", ["", "not a link", "ftp://example.com/file", "@bad"])
def test_normalize_ad_link_rejects_invalid_input(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_ad_link(value)


@pytest.mark.parametrize(
    ("link", "exclusion", "expected"),
    [
        (
            "https://ya.cc/t/OqZhkpIBB3ZVQo/?erid=j1SUm4f7YS6wocevC",
            "https://ya.cc",
            True,
        ),
        ("https://example.com/offer/details", "example.com/offer", True),
        ("https://example.com/offer?id=1", "example.com/offer", True),
        ("https://example.com/offer?id=1&source=ad", "example.com/offer?id=1", True),
        ("https://example.com/offering", "example.com/offer", False),
        ("https://ya.cc.evil.example/t/123", "ya.cc", False),
        ("https://notya.cc/t/123", "ya.cc", False),
    ],
)
def test_normalized_ad_link_exclusion_uses_safe_url_prefix(
    link: str,
    exclusion: str,
    expected: bool,
) -> None:
    assert normalized_ad_link_is_excluded(link, {exclusion}) is expected


def test_main_panel_places_ad_exclusions_after_extra_functions() -> None:
    callbacks = [button.callback_data for row in build_main_panel(False).keyboard for button in row]

    assert callbacks.index("panel:ad_link_exclusions") == callbacks.index("panel:extra") + 1


def test_ad_exclusions_panel_has_requested_actions() -> None:
    markup = build_ad_link_exclusions_panel()

    assert [button.text for row in markup.keyboard for button in row] == [
        "Ввести ссылку для исключения",
        "Удалить исключённую ссылку",
        "Вернуться в панель",
    ]
    assert [button.callback_data for row in markup.keyboard for button in row] == [
        "panel:add_ad_link_exclusion",
        "panel:delete_ad_link_exclusion",
        "panel:main",
    ]


@pytest.mark.asyncio
async def test_ad_exclusions_screen_displays_saved_links() -> None:
    master = object.__new__(MasterBot)
    master.editorial_actions = SimpleNamespace(
        list_ad_link_exclusions=AsyncMock(
            return_value=[
                SimpleNamespace(url="https://first.example"),
                SimpleNamespace(url="https://second.example/path"),
            ]
        )
    )
    master.main_bot = SimpleNamespace(send_message=AsyncMock())

    await master._show_ad_link_exclusions_panel(42)

    sent_text = master.main_bot.send_message.await_args.kwargs["text"]
    assert "1. https://first.example" in sent_text
    assert "2. https://second.example/path" in sent_text
    assert "здесь не отображаются" in sent_text
    assert master.main_bot.send_message.await_args.kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_add_exclusion_stores_normalized_link() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    service = AdLinkExclusionService()

    exclusion, created = await service.add_exclusion(
        session,
        url="http://www.Advertiser.Example/offer/",
        created_by=42,
    )

    assert created is True
    assert exclusion.url == "http://www.Advertiser.Example/offer/"
    assert exclusion.normalized_url == "advertiser.example/offer"
    assert exclusion.created_by == 42
    session.add.assert_called_once_with(exclusion)
    session.commit.assert_awaited_once()
    session.refresh.assert_awaited_once_with(exclusion)


@pytest.mark.asyncio
async def test_delete_exclusion_reports_missing_link() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)

    with pytest.raises(ValueError, match="нет в списке"):
        await AdLinkExclusionService().delete_exclusion(session, url="example.com")
