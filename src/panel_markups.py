from __future__ import annotations

from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup


def build_main_panel(is_general_admin: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("\u041e\u0431\u043d\u043e\u0432\u0438\u0442\u044c \u0432\u0445\u043e\u0434\u044f\u0449\u0438\u0435", callback_data="panel:import"),
        InlineKeyboardButton("\u041f\u043e\u0441\u0442\u0443\u043f\u0438\u0432\u0448\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f", callback_data="panel:submissions"),
    )
    markup.add(
        InlineKeyboardButton("\u0412\u0441\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u044f", callback_data="panel:all_submissions"),
        InlineKeyboardButton("\u0427\u0435\u0440\u043d\u043e\u0432\u0438\u043a\u0438 \u043d\u0430 review", callback_data="panel:content"),
    )
    markup.add(
        InlineKeyboardButton("\u041f\u0430\u0441\u0442\u044b", callback_data="panel:pastes"),
        InlineKeyboardButton("\u041a\u0430\u043d\u0430\u043b\u044b \u0438 \u0441\u043b\u043e\u0442\u044b", callback_data="panel:channels"),
    )
    markup.add(InlineKeyboardButton("Признавашки", callback_data="panel:confessions"))
    markup.add(
        InlineKeyboardButton("Теги", callback_data="panel:tags"),
    )
    markup.add(
        InlineKeyboardButton("\u041c\u043e\u0438 \u043a\u0430\u043d\u0430\u043b\u044b", callback_data="panel:my_channels")
    )
    markup.add(
        InlineKeyboardButton("\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c scheduler", callback_data="panel:scheduler"),
        InlineKeyboardButton("\u0417\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c publisher", callback_data="panel:publisher"),
    )
    markup.add(InlineKeyboardButton("\u0410\u0432\u0442\u043e\u0441\u043b\u043e\u0442\u044b", callback_data="panel:auto_slots"))
    markup.add(InlineKeyboardButton("\u041f\u0440\u043e\u0444\u0438\u043b\u0438", callback_data="panel:profiles"))
    if is_general_admin:
        markup.add(
            InlineKeyboardButton("\u0421\u0430\u0431\u0431\u043e\u0442\u044b", callback_data="panel:subbots"),
            InlineKeyboardButton("\u041c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u044b", callback_data="panel:admins"),
        )
    markup.add(
        InlineKeyboardButton("\u0414\u043e\u043f\u043e\u043b\u043d\u0438\u0442\u0435\u043b\u044c\u043d\u044b\u0435 \u0444\u0443\u043d\u043a\u0446\u0438\u0438", callback_data="panel:extra")
    )
    markup.add(
        InlineKeyboardButton(
            "Добавить исключения для рекламы", callback_data="panel:ad_link_exclusions"
        )
    )
    return markup


def build_extra_panel(is_general_admin: bool = False) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 \u0411\u0414", callback_data="panel:db_export"))
    markup.add(InlineKeyboardButton("\u0412\u044b\u0433\u0440\u0443\u0437\u043a\u0430 \u0441\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0438", callback_data="panel:stats_export"))
    if is_general_admin:
        markup.add(
            InlineKeyboardButton(
                "Статистика по администраторам",
                callback_data="panel:admin_stats_export",
            )
        )
    markup.add(InlineKeyboardButton("SQL -> CSV", callback_data="panel:sql_export"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_ad_link_exclusions_panel() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(
            "Ввести ссылку для исключения",
            callback_data="panel:add_ad_link_exclusion",
        ),
        InlineKeyboardButton(
            "Удалить исключённую ссылку",
            callback_data="panel:delete_ad_link_exclusion",
        ),
        InlineKeyboardButton(
            "Вернуться в панель",
            callback_data="panel:main",
        ),
    )
    return markup


def build_confessions_menu(is_general_admin: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    if is_general_admin:
        markup.add(
            InlineKeyboardButton(
                "Подключить саббота",
                callback_data="confessions:connect_publisher",
            ),
            InlineKeyboardButton(
                "Добавить чат для паст",
                callback_data="confessions:connect_storage",
            ),
        )
    markup.add(
        InlineKeyboardButton("Пасты", callback_data="confessions:pastes"),
        InlineKeyboardButton("Паблики", callback_data="confessions:channels:0"),
    )
    markup.add(InlineKeyboardButton("Назад в панель", callback_data="panel:main"))
    return markup


def build_confession_paste_actions(
    paste_id: int,
    *,
    has_previous: bool,
    has_next: bool,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    nav_buttons = []
    if has_previous:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"confession_paste:prev:{paste_id}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"confession_paste:next:{paste_id}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(
        InlineKeyboardButton(
            "Удалить пасту",
            callback_data=f"confession_paste:delete:{paste_id}",
        )
    )
    markup.add(InlineKeyboardButton("Назад к признавашкам", callback_data="panel:confessions"))
    return markup


def build_confession_paste_delete_confirm(paste_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.row(
        InlineKeyboardButton(
            "Да, удалить",
            callback_data=f"confession_paste:delete_confirm:{paste_id}",
        ),
        InlineKeyboardButton(
            "Отмена",
            callback_data=f"confession_paste:delete_cancel:{paste_id}",
        ),
    )
    return markup


def build_empty_confession_paste_actions() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("Назад к признавашкам", callback_data="panel:confessions"))
    return markup


def build_confession_channels_actions(
    channel_buttons: list[tuple[int, str]],
    *,
    page: int,
    has_previous: bool,
    has_next: bool,
    is_general_admin: bool,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    if is_general_admin:
        markup.add(InlineKeyboardButton("Подключить паблик", callback_data="confessions:add_channel"))
    for channel_id, title in channel_buttons:
        markup.add(
            InlineKeyboardButton(title, callback_data=f"confession_channel:view:{channel_id}")
        )
    nav_buttons = []
    if has_previous:
        nav_buttons.append(
            InlineKeyboardButton("⬅️ Назад", callback_data=f"confessions:channels:{page - 1}")
        )
    if has_next:
        nav_buttons.append(
            InlineKeyboardButton("Вперёд ➡️", callback_data=f"confessions:channels:{page + 1}")
        )
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(InlineKeyboardButton("Назад к признавашкам", callback_data="panel:confessions"))
    return markup


def build_confession_channel_actions(channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Добавить слоты", callback_data=f"confession_channel:add_slot:{channel_id}"),
        InlineKeyboardButton("Удалить слоты", callback_data=f"confession_channel:delete_slots:{channel_id}"),
        InlineKeyboardButton(
            "Поставить рекламное окно",
            callback_data=f"confession_channel:add_ad_blackout:{channel_id}",
        ),
        InlineKeyboardButton(
            "Удалить рекламное окно",
            callback_data=f"confession_channel:delete_ad_blackout:{channel_id}",
        ),
    )
    markup.add(InlineKeyboardButton("Назад к пабликам", callback_data="confessions:channels:0"))
    return markup


def build_profiles_panel(profile_buttons: list[tuple[str, str, bool]] | None = None) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for slug, title, is_active in profile_buttons or []:
        state = "" if is_active else " [off]"
        markup.add(InlineKeyboardButton(f"{title} ({slug}){state}", callback_data=f"profiles:view:{slug}"))
    markup.add(
        InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u0440\u043e\u0444\u0438\u043b\u044c", callback_data="profiles:add"),
        InlineKeyboardButton("\u0421\u0438\u043d\u0445\u0440\u043e\u043d\u0438\u0437\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0434\u043f\u0438\u0441\u0447\u0438\u043a\u043e\u0432", callback_data="panel:profile_sync"),
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_profile_actions(slug: str) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("\u041d\u0430\u0441\u0442\u0440\u043e\u0438\u0442\u044c \u043f\u0440\u043e\u0444\u0438\u043b\u044c", callback_data=f"profiles:settings:{slug}"),
        InlineKeyboardButton("\u0412\u044b\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u043f\u0440\u043e\u0444\u0438\u043b\u044c", callback_data=f"profiles:assign:{slug}"),
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u043a \u043f\u0440\u043e\u0444\u0438\u043b\u044f\u043c", callback_data="panel:profiles"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_submission_actions(
    submission_id: int,
    has_next: bool,
    is_anonymous: bool,
    allow_moderation: bool = True,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    if allow_moderation:
        markup.add(
            InlineKeyboardButton("Approve", callback_data=f"submission:approve:{submission_id}"),
            InlineKeyboardButton("Publish now", callback_data=f"submission:publish:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("Save as paste", callback_data=f"submission:paste:{submission_id}"),
            InlineKeyboardButton("Hold", callback_data=f"submission:hold:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("\u0410\u043d\u043e\u043d" if is_anonymous else "\u041d\u0435 \u0430\u043d\u043e\u043d", callback_data=f"submission:toggle_anon:{submission_id}"),
            InlineKeyboardButton("\u0420\u0435\u043a\u043b\u0430\u043c\u0430", callback_data=f"submission:advertise:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("\u041e\u0442\u0432\u0435\u0442\u0438\u0442\u044c", callback_data=f"submission:reply:{submission_id}"),
            InlineKeyboardButton("Ban user", callback_data=f"submission:ban:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("Reject", callback_data=f"submission:reject:{submission_id}"),
            InlineKeyboardButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", callback_data=f"submission_all:delete:{submission_id}"),
        )
        markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    else:
        markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    if has_next:
        markup.add(InlineKeyboardButton("\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", callback_data=f"submission:next:{submission_id}"))
    return markup


def build_submission_history_actions(
    submission_id: int,
    has_next: bool,
    is_anonymous: bool,
    allow_moderation: bool = True,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    if allow_moderation:
        markup.add(
            InlineKeyboardButton("Approve", callback_data=f"submission:approve:{submission_id}"),
            InlineKeyboardButton("Publish now", callback_data=f"submission:publish:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("Save as paste", callback_data=f"submission:paste:{submission_id}"),
            InlineKeyboardButton("Hold", callback_data=f"submission:hold:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("\u0410\u043d\u043e\u043d" if is_anonymous else "\u041d\u0435 \u0430\u043d\u043e\u043d", callback_data=f"submission:toggle_anon:{submission_id}"),
            InlineKeyboardButton("\u0420\u0435\u043a\u043b\u0430\u043c\u0430", callback_data=f"submission:advertise:{submission_id}"),
        )
        markup.add(
            InlineKeyboardButton("\u041e\u0442\u0432\u0435\u0442\u0438\u0442\u044c", callback_data=f"submission:reply:{submission_id}"),
            InlineKeyboardButton("Ban user", callback_data=f"submission:ban:{submission_id}"),
        )
        markup.add(InlineKeyboardButton("Reject", callback_data=f"submission:reject:{submission_id}"))
    markup.add(InlineKeyboardButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", callback_data=f"submission_all:delete:{submission_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    if has_next:
        markup.add(InlineKeyboardButton("\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0435\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435", callback_data=f"submission_all:next:{submission_id}"))
    return markup


def build_content_actions(content_item_id: int, has_next: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("Approve", callback_data=f"content:approve:{content_item_id}"),
        InlineKeyboardButton("Publish now", callback_data=f"content:publish:{content_item_id}"),
    )
    markup.add(
        InlineKeyboardButton("Hold", callback_data=f"content:hold:{content_item_id}"),
        InlineKeyboardButton("Reject", callback_data=f"content:reject:{content_item_id}"),
    )
    markup.add(InlineKeyboardButton("\u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c", callback_data=f"content:edit:{content_item_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    if has_next:
        markup.add(InlineKeyboardButton("\u0421\u043b\u0435\u0434\u0443\u044e\u0449\u0438\u0439 \u0447\u0435\u0440\u043d\u043e\u0432\u0438\u043a", callback_data=f"content:next:{content_item_id}"))
    return markup


def build_channels_actions(
    channel_buttons: list[tuple[int, str]],
    *,
    page: int = 0,
    has_previous: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    for channel_id, title in channel_buttons:
        markup.add(InlineKeyboardButton(title, callback_data=f"channel:view:{channel_id}"))
    nav_buttons = []
    if has_previous:
        nav_buttons.append(
            InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data=f"panel:channels:{page - 1}")
        )
    if has_next:
        nav_buttons.append(
            InlineKeyboardButton("\u0412\u043f\u0435\u0440\u0435\u0434 \u27a1\ufe0f", callback_data=f"panel:channels:{page + 1}")
        )
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(InlineKeyboardButton("Перейти к №/тегу", callback_data="channel:jump"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_my_channels_actions(channel_buttons: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    if channel_buttons:
        markup.add(
            InlineKeyboardButton(
                "\u0421\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435 \u0432\u043e \u0432\u0441\u0435 \u043a\u0430\u043d\u0430\u043b\u044b",
                callback_data="my_channels:all",
            )
        )
        for channel_id, title in channel_buttons:
            markup.add(InlineKeyboardButton(title, callback_data=f"my_channels:channel:{channel_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_channel_actions(
    channel_id: int,
    notifications_enabled: bool,
    moderation_feed_enabled: bool,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    notify_label = "\u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f" if notifications_enabled else "\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u0443\u0432\u0435\u0434\u043e\u043c\u043b\u0435\u043d\u0438\u044f"
    moderation_feed_label = (
        "\u0412\u044b\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439"
        if moderation_feed_enabled
        else "\u0412\u043a\u043b\u044e\u0447\u0438\u0442\u044c \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0438\u0435 \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0439"
    )
    markup.add(
        InlineKeyboardButton(notify_label, callback_data=f"channel:notify_toggle:{channel_id}"),
        InlineKeyboardButton(moderation_feed_label, callback_data=f"channel:feed_toggle:{channel_id}"),
        InlineKeyboardButton("\u041f\u043e\u0441\u0442\u0430\u0432\u0438\u0442\u044c \u0440\u0435\u043a\u043b\u0430\u043c\u043d\u043e\u0435 \u043e\u043a\u043d\u043e", callback_data=f"channel:ad_blackout:{channel_id}"),
        InlineKeyboardButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0440\u0435\u043a\u043b\u0430\u043c\u043d\u043e\u0435 \u043e\u043a\u043d\u043e", callback_data=f"channel:delete_ad_blackout:{channel_id}"),
        InlineKeyboardButton("\u0421\u0433\u0435\u043d\u0435\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c \u043f\u043e\u0441\u0442\u044b", callback_data=f"channel:generate:{channel_id}"),
        InlineKeyboardButton("\u041d\u0430\u0441\u0442\u0440\u043e\u0439\u043a\u0430 \u0441\u043b\u043e\u0442\u043e\u0432", callback_data=f"channel:slots:{channel_id}"),
        InlineKeyboardButton("Почему не публикует?", callback_data=f"channel:paste_diagnostics:{channel_id}"),
        InlineKeyboardButton("\u0418\u0437\u043c\u0435\u043d\u0435\u043d\u0438\u0435 \u043f\u0430\u0440\u0430\u043c\u0435\u0442\u0440\u043e\u0432", callback_data=f"channel:params:{channel_id}"),
        InlineKeyboardButton("Теги паст", callback_data=f"channel:paste_tags:{channel_id}"),
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u043a \u043a\u0430\u043d\u0430\u043b\u0430\u043c", callback_data="panel:channels"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_channel_slots_actions(channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u043b\u043e\u0442", callback_data=f"channel:add_slot:{channel_id}"),
        InlineKeyboardButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u043b\u043e\u0442\u044b", callback_data=f"channel:delete_slots:{channel_id}"),
        InlineKeyboardButton("\u0421\u043e\u0437\u0434\u0430\u0442\u044c \u0441\u0442\u0430\u043d\u0434\u0430\u0440\u0442\u043d\u044b\u0435 \u0441\u043b\u043e\u0442\u044b", callback_data=f"channel:seed:{channel_id}"),
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u043a \u043a\u0430\u043d\u0430\u043b\u0443", callback_data=f"channel:view:{channel_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_paste_actions(paste_id: int, has_previous: bool, has_next: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    nav_buttons = []
    if has_previous:
        nav_buttons.append(InlineKeyboardButton("⬅️", callback_data=f"paste:prev:{paste_id}"))
    if has_next:
        nav_buttons.append(InlineKeyboardButton("➡️", callback_data=f"paste:next:{paste_id}"))
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(
        InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u0430\u0441\u0442\u0443", callback_data="paste:add"),
        InlineKeyboardButton("\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u043f\u0430\u0441\u0442\u0443", callback_data=f"paste:delete:{paste_id}"),
    )
    markup.add(InlineKeyboardButton("Теги пасты", callback_data=f"paste:tags:{paste_id}"))
    markup.add(InlineKeyboardButton("Перейти к № пасты", callback_data="paste:jump"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_empty_paste_actions() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u0430\u0441\u0442\u0443", callback_data="paste:add"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_admin_menu(admin_buttons: list[int]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043c\u043e\u0434\u0435\u0440\u0430\u0442\u043e\u0440\u0430", callback_data="admin:add"))
    for admin_id in admin_buttons:
        markup.add(InlineKeyboardButton(f"\u0423\u0434\u0430\u043b\u0438\u0442\u044c {admin_id}", callback_data=f"admin:remove:{admin_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_subbot_menu(
    subbot_buttons: list[tuple[str, int]],
    *,
    page: int = 0,
    has_previous: bool = False,
    has_next: bool = False,
) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0430\u0431\u0431\u043e\u0442\u0430", callback_data="subbot:add"))
    for username, channel_id in subbot_buttons:
        markup.add(InlineKeyboardButton(f"\u0423\u0434\u0430\u043b\u0438\u0442\u044c @{username}", callback_data=f"subbot:remove:{username}:{channel_id}"))
    nav_buttons = []
    if has_previous:
        nav_buttons.append(
            InlineKeyboardButton("\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data=f"panel:subbots:{page - 1}")
        )
    if has_next:
        nav_buttons.append(
            InlineKeyboardButton("\u0412\u043f\u0435\u0440\u0435\u0434 \u27a1\ufe0f", callback_data=f"panel:subbots:{page + 1}")
        )
    if nav_buttons:
        markup.row(*nav_buttons)
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_tags_actions(tag_buttons: list[tuple[str, str, bool]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("Добавить тег", callback_data="tag:add"))
    markup.add(InlineKeyboardButton("Массовые правила паст", callback_data="tag:global_rules"))
    for slug, title, is_active in tag_buttons:
        state = "" if is_active else " [выкл]"
        markup.add(InlineKeyboardButton(f"{title} ({slug}){state}", callback_data=f"tag:view:{slug}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_tag_actions(slug: str, is_active: bool) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Добавить ключевые слова", callback_data=f"tag:add_keywords:{slug}"),
        InlineKeyboardButton("Ключевые слова", callback_data=f"tag:keywords:{slug}"),
        InlineKeyboardButton("Выключить тег" if is_active else "Включить тег", callback_data=f"tag:toggle:{slug}"),
    )
    markup.add(InlineKeyboardButton("Назад к тегам", callback_data="panel:tags"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_tag_keywords_actions(slug: str, keyword_buttons: list[tuple[int, str, bool]]) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(InlineKeyboardButton("Добавить ключевые слова", callback_data=f"tag:add_keywords:{slug}"))
    for keyword_id, keyword, is_active in keyword_buttons:
        state = "" if is_active else " [выкл]"
        markup.add(InlineKeyboardButton(f"Удалить: {keyword}{state}", callback_data=f"tag:remove_keyword:{slug}:{keyword_id}"))
    markup.add(InlineKeyboardButton("Назад к тегу", callback_data=f"tag:view:{slug}"))
    return markup


def build_paste_tag_actions(paste_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Добавить ручной тег", callback_data=f"paste_tags:add:{paste_id}"),
        InlineKeyboardButton("Убрать ручной тег", callback_data=f"paste_tags:remove:{paste_id}"),
        InlineKeyboardButton("Сделать тег основным", callback_data=f"paste_tags:primary:{paste_id}"),
        InlineKeyboardButton("Пересчитать авто-теги", callback_data=f"paste_tags:refresh:{paste_id}"),
    )
    markup.add(InlineKeyboardButton("Назад к пастам", callback_data="panel:pastes"))
    return markup


def build_channel_paste_tag_actions(channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Добавить обязательный тег", callback_data=f"channel_paste_tags:add_include:{channel_id}"),
        InlineKeyboardButton("Добавить запрещённый тег", callback_data=f"channel_paste_tags:add_exclude:{channel_id}"),
        InlineKeyboardButton("Удалить обязательный тег", callback_data=f"channel_paste_tags:remove_include:{channel_id}"),
        InlineKeyboardButton("Удалить запрещённый тег", callback_data=f"channel_paste_tags:remove_exclude:{channel_id}"),
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u043a \u043a\u0430\u043d\u0430\u043b\u0443", callback_data=f"channel:view:{channel_id}"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_global_paste_tag_rule_actions() -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("Поставить Include для всех", callback_data="global_paste_tags:add_include"),
        InlineKeyboardButton("Убрать Include для всех", callback_data="global_paste_tags:remove_include"),
        InlineKeyboardButton("Поставить Exclude для всех", callback_data="global_paste_tags:add_exclude"),
        InlineKeyboardButton("Убрать Exclude для всех", callback_data="global_paste_tags:remove_exclude"),
    )
    markup.add(InlineKeyboardButton("Назад к тегам", callback_data="panel:tags"))
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_subbot_remove_confirm(username: str, channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("\u0414\u0430", callback_data=f"subbot:remove_confirm:{username}:{channel_id}"),
        InlineKeyboardButton("\u041d\u0435\u0442", callback_data="subbot:remove_cancel"),
    )
    return markup


def build_channel_history_import_start_actions(channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("\u0418\u043c\u043f\u043e\u0440\u0442 \u0438\u0441\u0442\u043e\u0440\u0438\u0438 \u043a\u0430\u043d\u0430\u043b\u0430", callback_data=f"channel_history:start:{channel_id}")
    )
    markup.add(InlineKeyboardButton("\u041d\u0430\u0437\u0430\u0434 \u0432 \u043f\u0430\u043d\u0435\u043b\u044c", callback_data="panel:main"))
    return markup


def build_channel_history_import_progress_actions(channel_id: int) -> InlineKeyboardMarkup:
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton("\u0417\u0430\u0432\u0435\u0440\u0448\u0438\u0442\u044c \u0438\u043c\u043f\u043e\u0440\u0442", callback_data=f"channel_history:finish:{channel_id}"),
        InlineKeyboardButton("\u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c \u0438\u043c\u043f\u043e\u0440\u0442", callback_data=f"channel_history:cancel:{channel_id}"),
    )
    return markup
