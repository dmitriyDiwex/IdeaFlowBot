import json
from pathlib import Path
from datetime import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from src.master import MasterBot

FIXTURE = json.loads((Path(__file__).parent/'fixtures/spbgu_confession_card.json').read_text(encoding='utf8'))


def _master():
    master=MasterBot.__new__(MasterBot)
    master.bots_work=[]
    master._is_general_admin=lambda _: True
    master.legacy_reader=SimpleNamespace(get_bot_binding=AsyncMock(return_value=None))
    slots=[SimpleNamespace(id=id,weekday=day,slot_time=time.fromisoformat(at),is_auto_managed=auto)
           for id,day,at,auto in FIXTURE['slots']]
    master.editorial_actions=SimpleNamespace(
        sync_channel_activity_from_bindings=AsyncMock(),
        get_channel=AsyncMock(return_value=SimpleNamespace(**FIXTURE['channel'])),
        list_channel_slots=AsyncMock(return_value=slots),
        list_channel_ad_blackouts=AsyncMock(return_value=[]),
        get_channel_settings_snapshot=AsyncMock(return_value=FIXTURE['settings']),
        is_channel_notifications_enabled=AsyncMock(return_value=False),
        is_channel_moderation_feed_enabled=AsyncMock(return_value=False),
    )
    master._get_channel_label=AsyncMock(return_value=FIXTURE['channel']['title'])
    async def send(chat_id,text,**kwargs):
        assert len(text.encode('utf-16-le'))//2 <= 4096, 'Telegram message is too long'
    master.main_bot=SimpleNamespace(send_message=AsyncMock(side_effect=send))
    return master


@pytest.mark.parametrize('view',['card','slots'])
async def test_spbgu_slots_are_loaded_only_from_slot_settings(view):
    master=_master()
    if view=='card':
        await master._show_confession_channel_slots(42,323,user_id=42)
    else:
        await master._show_channel_slots_menu(42,323)
    calls=master.main_bot.send_message.await_args_list
    text=''.join(call.args[1] for call in calls)
    if view == 'card':
        assert len(calls) == 1
        master.editorial_actions.list_channel_slots.assert_not_awaited()
        assert 'Слоты:' not in text
        assert all(f'#{slot_id} ' not in text for slot_id, *_ in FIXTURE['slots'])
    else:
        assert len(calls) > 1
        master.editorial_actions.list_channel_slots.assert_awaited_once_with(323)
        for slot_id, *_ in FIXTURE['slots']:
            assert text.count(f'#{slot_id} ') == 1
    assert all(call.args[0]==42 for call in calls)
    assert all(call.kwargs.get('reply_markup') is None for call in calls[:-1])
    assert calls[-1].kwargs['reply_markup'] is not None
    callback_data={button['callback_data'] for row in calls[-1].kwargs['reply_markup'].to_dict()['inline_keyboard'] for button in row}
    if view == 'card':
        assert 'channel:slots:323' in callback_data
        assert 'channel:params:323' in callback_data
    else:
        assert 'channel:add_slot:323' in callback_data


async def test_small_confession_card_still_sends_one_message():
    master=_master()
    master.editorial_actions.list_channel_slots.return_value=master.editorial_actions.list_channel_slots.return_value[:2]
    await master._show_confession_channel_slots(42,323,user_id=42)
    master.main_bot.send_message.assert_awaited_once()
    assert master.main_bot.send_message.await_args.kwargs['reply_markup'] is not None


async def test_long_panel_text_preserves_unicode_and_button_actions():
    master=_master()
    text='Заголовок 🧡\n'+('📸'*2200)+'\nКонец'
    markup=SimpleNamespace()
    await master._send_panel_text(42,text,reply_markup=markup)
    calls=master.main_bot.send_message.await_args_list
    assert ''.join(call.args[1] for call in calls)==text
    assert all(len(call.args[1].encode('utf-16-le'))//2 <=4096 for call in calls)
    assert calls[-1].kwargs['reply_markup'] is markup
