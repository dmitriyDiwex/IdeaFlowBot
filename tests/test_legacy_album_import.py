import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import JSON, MetaData, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core_database.models.bots_data import BotsData
from src.core_database.models.sender_info import SenderData
from src.editorial.db.base import EditorialBase
from src.editorial.models.channel import Channel
from src.editorial.models.content import ContentItem
from src.editorial.models.enums import ContentItemStatus, PublicationStatus, SubmissionStatus
from src.editorial.models.publication import PublicationLog
from src.editorial.models.submission import Submission
from src.editorial.services.import_legacy import LegacyImporter
from src.editorial.services.legacy_moderation_sync import LegacyModerationSyncService
from src.editorial.services.legacy_source import LegacyCollectorReader, LegacySenderRow
from src.editorial.services.publisher import PublisherService
from src.editorial.services.telegram_resilience import PartialTelegramCopyError

NOW = datetime(2026, 10, 8, 4, 59, tzinfo=timezone.utc)
GAPS = json.loads((Path(__file__).parent/'fixtures/itmo_album_import_gaps.json').read_text())


@pytest.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine('sqlite+aiosqlite:///'+(tmp_path/'albums.sqlite').as_posix())
    metadata = MetaData()
    for table in EditorialBase.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for column in clone.columns:
            if isinstance(column.type, JSONB):
                column.type = JSON()
    SenderData.__table__.to_metadata(metadata)
    BotsData.__table__.to_metadata(metadata)
    names = {'channels','submissions','content_items','content_item_sources','reviews',
             'moderation_cases','moderation_case_events','tag_definitions','tag_keywords',
             'publication_log','channel_ad_blackouts','sender_info','bots_data'}
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: metadata.create_all(c, tables=[metadata.tables[n] for n in names]))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr('src.editorial.services.legacy_source.legacy_db_helper.engine', engine)
    monkeypatch.setattr('src.editorial.services.legacy_moderation_sync.session_factory', factory)
    async with factory() as session:
        session.add(Channel(id=338, tg_channel_id=-1002319242564, title='ITMO',
                            short_code='itmo', is_active=True, timezone='Europe/Moscow'))
        session.add(BotsData(id=1, channel_id=-1002319242564,
                            bot_username='Itmo_anon_bot', bot_api_token='1:test'))
        await session.commit()
        yield session
    await engine.dispose()


def _sender(id, message_id, *, chat_id=1000, album='album-1', caption=None):
    return SenderData(id=id, user_id=chat_id, channel_id=-1002319242564,
                      bot_username='Itmo_anon_bot', username='', first_name='',
                      message_id=message_id, chat_id=chat_id, text_post=caption or '',
                      content_type='photo' if album else 'text', media_group_id=album,
                      review_chat_id=-1002354447662, review_message_id=22875,
                      timestamp=int(NOW.timestamp()))


async def _seed(db, *, size=2, status=SubmissionStatus.CONTENT_CREATED):
    db.add_all([_sender(51609+i,63444+i,caption='Album caption' if i==0 else None)
                for i in range(size)])
    db.add(_sender(54420,99999,album=None))
    await db.commit()
    reader = LegacyCollectorReader()
    rows = await reader.fetch_sender_rows_by_ids([51609,54420])
    importer = LegacyImporter(reader)
    for row in rows:
        payload = await importer._build_submission_payload(db,row,338)
        payload.update(status=status,is_anonymous=False,reviewed_at=NOW,moderator_note='Existing decision')
        db.add(Submission(**payload))
    await db.commit()
    return await db.scalar(select(Submission).where(Submission.legacy_row_id==51609))


def _publisher(*, copied_ids):
    adapter = SimpleNamespace(copy_messages=AsyncMock(return_value=copied_ids),
                              edit_message_caption=AsyncMock())
    sync = SimpleNamespace(mark_content_item_published=AsyncMock(),
                           reconcile_published_review_statuses=AsyncMock())
    service = PublisherService(telegram_adapter=adapter, legacy_publication_status=sync)
    service.should_add_channel_signature=AsyncMock(return_value=False)
    return service, adapter


@pytest.mark.parametrize('gap', GAPS, ids=[g['album'] for g in GAPS])
async def test_snapshot_album_gaps_are_backfilled_without_reopening_moderation(db,gap):
    for row in gap['rows']:
        db.add(_sender(row['id'],row['message_id'],chat_id=gap['source_chat_id'],
                       album=gap['album'],caption=row['caption']))
    db.add(_sender(54420,99999,album=None))
    await db.commit()
    importer=LegacyImporter()
    for saved in gap['existing']:
        row=(await importer.legacy_reader.fetch_sender_rows_by_ids([saved['legacy_row_id']]))[0]
        payload=await importer._build_submission_payload(db,row,338)
        payload.update(status=saved['status'],is_anonymous=saved['is_anonymous'],
                       reviewed_at=NOW if saved['reviewed'] else None,moderator_note='Existing decision')
        db.add(Submission(**payload))
    high=(await importer.legacy_reader.fetch_sender_rows_by_ids([54420]))[0]
    db.add(Submission(**await importer._build_submission_payload(db,high,338)))
    await db.commit()
    original=gap['existing'][0]
    result=await importer.import_new(db,limit=1)
    assert result.imported==1
    while (await importer.import_new(db,limit=2)).imported:
        pass
    subs=list((await db.scalars(select(Submission).where(Submission.media_group_id==gap['album']))).all())
    assert {s.source_message_id for s in subs}=={r['message_id'] for r in gap['rows']}
    assert {s.status for s in subs}=={SubmissionStatus(original['status'])}
    assert {s.is_anonymous for s in subs}=={original['is_anonymous']}
    assert len(subs)==len(gap['rows'])
    assert (await importer.import_new(db)).imported==0
    assert await db.scalar(select(func.count()).select_from(ContentItem))==0


async def test_approval_imports_whole_album_even_when_first_submission_already_exists(db):
    anchor=await _seed(db,status=SubmissionStatus.NEW)
    item=await LegacyModerationSyncService().approve_review_message(
        channel_tg_id=-1002319242564,review_chat_id=-1002354447662,
        review_message_id=22875,reviewer_id=42,
    )
    assert item.status==ContentItemStatus.APPROVED
    db.expire_all()
    subs=list((await db.scalars(select(Submission).where(Submission.media_group_id=='album-1'))).all())
    assert len(subs)==2
    assert {s.status for s in subs}=={SubmissionStatus.CONTENT_CREATED}
    assert {s.is_anonymous for s in subs}=={False}
    assert await db.scalar(select(func.count()).select_from(ContentItem))==1
    again=await LegacyModerationSyncService().approve_review_message(
        channel_tg_id=-1002319242564,review_chat_id=-1002354447662,
        review_message_id=22875,reviewer_id=42,
    )
    assert again.id==item.id


async def test_publisher_repairs_gap_before_copying_and_keeps_source_message_order(db):
    anchor=await _seed(db)
    publisher,adapter=_publisher(copied_ids=[17808,17809])
    item=SimpleNamespace(origin_submission_id=anchor.id,body_text='Album caption',id=88280)
    channel=await db.get(Channel,338)
    assert await publisher._publish_submission_based_item(db,item,channel,'1:test')==17808
    assert adapter.copy_messages.await_args.kwargs['message_ids']==[63444,63445]
    assert await db.scalar(select(func.count()).select_from(Submission).where(Submission.media_group_id=='album-1'))==2


async def test_incomplete_legacy_source_album_is_blocked_before_sending(db):
    anchor=await _seed(db,size=1)
    publisher,adapter=_publisher(copied_ids=[17808])
    with pytest.raises(ValueError,match='incomplete in sender_info'):
        await publisher._publish_submission_based_item(
            db,SimpleNamespace(origin_submission_id=anchor.id,body_text='caption',id=88280),
            await db.get(Channel,338),'1:test',
        )
    adapter.copy_messages.assert_not_awaited()


async def test_inconsistent_album_reference_is_blocked_before_sending(db):
    anchor=await _seed(db)
    anchor.source_message_id=12345
    await db.commit()
    publisher,adapter=_publisher(copied_ids=[17808,17809])
    with pytest.raises(ValueError,match='inconsistent submission references'):
        await publisher._publish_submission_based_item(
            db,SimpleNamespace(origin_submission_id=anchor.id,body_text='caption',id=88280),
            await db.get(Channel,338),'1:test',
        )
    adapter.copy_messages.assert_not_awaited()


async def test_partial_copy_is_failed_with_delivery_evidence_and_is_not_retried(db):
    anchor=await _seed(db)
    item=ContentItem(channel_id=338,source_type='submission',origin_submission_id=anchor.id,
                     body_text='Album caption',normalized_text='album',text_hash='album-hash',
                     status=ContentItemStatus.SCHEDULED,scheduled_for=NOW)
    db.add(item); await db.flush()
    log=PublicationLog(channel_id=338,content_item_id=item.id,scheduled_for=NOW,
                       publish_status=PublicationStatus.SCHEDULED,created_at=NOW)
    db.add(log);await db.commit()
    publisher,adapter=_publisher(copied_ids=[17808])
    result=await publisher.run(db,now=NOW,limit=1)
    assert result.sent==0 and result.failed==1
    assert log.publish_status==PublicationStatus.FAILED
    assert log.telegram_message_id==17808
    assert '1 of 2' in log.error_text
    assert item.status==ContentItemStatus.HOLD
    assert (await publisher.run(db,now=NOW,limit=1)).attempted==0
    adapter.copy_messages.assert_awaited_once()


async def test_separate_collector_fallback_finds_older_gap_and_filters_service_copies(db,monkeypatch):
    await _seed(db)
    reader=LegacyCollectorReader()
    source=await reader.fetch_sender_rows()
    service_copy=LegacySenderRow(**{**vars_from(source[0]),'id':55000,'chat_id':-10055,
                                   'review_chat_id':None,'review_message_id':None})
    unknown=LegacySenderRow(**{**vars_from(source[0]),'id':55001,'channel_id':-999})
    reader.fetch_sender_rows=AsyncMock(side_effect=[source+[service_copy,unknown],[]])
    monkeypatch.setattr('src.editorial.services.legacy_source.legacy_db_helper.engine',
                        SimpleNamespace(url='separate-collector'))
    rows=await reader.fetch_unimported_sender_rows(db,limit=200)
    assert [r.id for r in rows]==[51610]
    assert reader.fetch_sender_rows.await_args_list[1].kwargs['after_id']==55001


def vars_from(row):
    return {name:getattr(row,name) for name in LegacySenderRow.__dataclass_fields__}
