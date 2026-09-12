from types import SimpleNamespace

import pytest

from src.utils import Utils


def _message(text=None, caption=None, entities=None, caption_entities=None, reply_markup=None):
    return SimpleNamespace(
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        reply_markup=reply_markup,
    )


def _text_link(url: str):
    return SimpleNamespace(type="text_link", url=url)


@pytest.mark.asyncio
async def test_check_link_ignores_own_channel_text_link() -> None:
    message = _message(
        text="Подслушано РУТ МИИТ",
        entities=[_text_link("https://t.me/MIITrussia")],
    )

    assert not await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_detects_external_text_link() -> None:
    message = _message(
        text="Реклама",
        entities=[_text_link("https://example.com")],
    )

    assert await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_detects_external_link_when_own_link_is_present() -> None:
    message = _message(
        text="https://t.me/MIITrussia https://example.com",
    )

    assert await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_ignores_own_raw_channel_link() -> None:
    message = _message(text="https://t.me/MIITrussia")

    assert not await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_ignores_own_private_channel_link() -> None:
    message = _message(text="https://t.me/c/1234567890/42")

    assert not await Utils.check_link(message, ignored_channel_ref=-1001234567890)


@pytest.mark.asyncio
async def test_check_link_detects_external_inline_button() -> None:
    message = _message(
        text="Реклама",
        reply_markup=SimpleNamespace(
            keyboard=[[SimpleNamespace(url="https://advertiser.example", login_url=None, web_app=None)]],
        ),
    )

    assert await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_ignores_own_channel_inline_button() -> None:
    message = _message(
        text="Наш канал",
        reply_markup=SimpleNamespace(
            keyboard=[[SimpleNamespace(url="https://t.me/MIITrussia/123", login_url=None, web_app=None)]],
        ),
    )

    assert not await Utils.check_link(message, ignored_channel_ref="@MIITrussia")


@pytest.mark.asyncio
async def test_check_link_ignores_manually_excluded_link_variants() -> None:
    message = _message(text="Партнёр: http://www.Advertiser.Example/offer/.")

    assert not await Utils.check_link(
        message,
        ignored_channel_ref="@MIITrussia",
        ignored_links={"https://advertiser.example/offer"},
    )


@pytest.mark.asyncio
async def test_check_link_ignores_child_url_of_excluded_domain() -> None:
    message = _message(text="https://ya.cc/t/OqZhkpIBB3ZVQo/?erid=j1SUm4f7YS6wocevC")

    assert not await Utils.check_link(
        message,
        ignored_channel_ref="@MIITrussia",
        ignored_links={"https://ya.cc"},
    )


@pytest.mark.asyncio
async def test_check_link_manual_exclusion_suppresses_other_external_links() -> None:
    message = _message(text="https://advertiser.example https://other.example")

    assert not await Utils.check_link(
        message,
        ignored_channel_ref="@MIITrussia",
        ignored_links={"advertiser.example"},
    )
