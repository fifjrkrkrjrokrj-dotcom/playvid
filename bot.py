import os
import asyncio
from pyrogram import Client, filters
from pyrogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
)
from pyrogram.errors import MessageNotModified

import config
import database
import queue_manager

# Initialize bot app
app = Client(
    name="bot",
    api_id=int(config.API_ID),
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN
)

# User states dictionary to manage wizards in DM: user_id -> {action, playlist_id, selected_playlists}
user_states = {}

# ==========================================
# UI Layout Markup Generators
# ==========================================

def get_playback_dashboard_markup(state, preparing=False, finished=False):
    """Generates the inline keyboard markup for the video playback control dashboard."""
    if finished:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📂 My Playlists", callback_data="view_playlists"),
            InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")
        ]])
        
    if preparing:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("⏳ Preparing Stream...", callback_data="noop_status")
        ], [
            InlineKeyboardButton("⏹ Stop Playback", callback_data="stop_playback")
        ]])
        
    pause_resume_btn = (
        InlineKeyboardButton("▶️ Resume", callback_data="play_resume") 
        if state.is_paused 
        else InlineKeyboardButton("⏸ Pause", callback_data="play_resume")
    )
    
    loop_label = "🔄 Loop: Off"
    if state.loop_mode == "queue":
        loop_label = "🔄 Loop: Queue"
    elif state.loop_mode == "single":
        loop_label = "🔄 Loop: Single"
        
    autoplay_label = "📻 Autoplay: On" if state.autoplay else "📻 Autoplay: Off"
        
    return InlineKeyboardMarkup([
        [pause_resume_btn, InlineKeyboardButton("Next ⏭", callback_data="skip_track")],
        [
            InlineKeyboardButton("🔈 Vol -", callback_data="vol_down"),
            InlineKeyboardButton("🔊 Vol +", callback_data="vol_up"),
            InlineKeyboardButton(loop_label, callback_data="loop_toggle")
        ],
        [
            InlineKeyboardButton(autoplay_label, callback_data="autoplay_toggle"),
            InlineKeyboardButton("⏹ Stop Playback", callback_data="stop_playback")
        ],
        [
            InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh_dashboard")
        ]
    ])

def get_playback_text(video, state):
    """Renders the standard Now Playing text status for the dashboard."""
    status = "Paused" if state.is_paused else "Now Playing"
    autoplay_status = "On" if state.autoplay else "Off"
    return (
        f"🎥 **{status} in Voice Chat**\n\n"
        f"📌 **Title**: `{video['name']}`\n"
        f"⏱ **Duration**: `{video['duration']}s`\n"
        f"🔊 **Volume**: `{state.volume}%` | 🔄 **Loop**: `{state.loop_mode.capitalize()}` | 📻 **Autoplay**: `{autoplay_status}`\n\n"
        f"Connected Chat ID: `{state.chat_id}`"
    )

# ==========================================
# Navigation & View Controllers
# ==========================================

async def send_start_message(client: Client, chat_id: int, message_id: int = None):
    """Renders the main home menu for the bot."""
    text = (
        "👋 **Welcome to VPlay Bot!**\n\n"
        "I stream videos from your custom playlists directly into your group's Voice Chat.\n\n"
        "🔧 **Setup Flow**:\n"
        "1. Add this bot to your group as an Admin.\n"
        "2. Send `/connect` in the group.\n"
        "3. Click the linkage button to register your controls.\n"
        "4. Create playlists and send/forward videos here in DM!"
    )
    
    conn = await database.get_user_connection(chat_id)
    if conn:
        text += f"\n\n🔗 **Connected Group**: **{conn['connected_group_title']}**"
    else:
        text += "\n\n❌ **No group connected yet. Use /connect in your group to start.**"
        
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📂 Playlists", callback_data="view_playlists"),
            InlineKeyboardButton("🎥 Stream VC", callback_data="vplaying_menu")
        ],
        [
            InlineKeyboardButton("ℹ️ Help Instructions", callback_data="help_menu")
        ]
    ])
    
    try:
        if message_id:
            await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        else:
            await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except MessageNotModified:
        pass

async def send_playlists_menu(client: Client, user_id: int, chat_id: int, message_id: int = None):
    """Renders the lists of playlists."""
    playlists = await database.get_playlists(user_id)
    text = "📂 **Your Video Playlists**\n\nSelect a playlist to manage or click below to create one:"
    
    buttons = []
    for pl in playlists:
        pl_id = str(pl["_id"])
        video_count = len(pl.get("videos", []))
        buttons.append([
            InlineKeyboardButton(f"📁 {pl['name']} ({video_count} vids)", callback_data=f"view_pl_{pl_id}")
        ])
        
    buttons.append([InlineKeyboardButton("➕ Create Playlist", callback_data="create_playlist")])
    buttons.append([InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")])
    
    markup = InlineKeyboardMarkup(buttons)
    try:
        if message_id:
            await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        else:
            await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except MessageNotModified:
        pass

async def send_playlist_detail(client: Client, user_id: int, playlist_id: str, chat_id: int, message_id: int = None):
    """Renders details of a single playlist."""
    pl = await database.get_playlist_by_id(playlist_id)
    if not pl:
        await send_playlists_menu(client, user_id, chat_id, message_id)
        return
        
    text = f"📁 **Playlist**: **{pl['name']}**\n"
    text += f"📅 Created: `{pl['created_at'].strftime('%Y-%m-%d %H:%M')}`\n\n"
    
    videos = pl.get("videos", [])
    if not videos:
        text += "🚫 This playlist is currently empty. Add some videos!"
    else:
        text += "📋 **Videos List:**\n"
        for idx, vid in enumerate(videos, start=1):
            text += f"{idx}. `{vid['name']}` ({vid['duration']}s)\n"
            
    markup = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📥 Add Videos", callback_data=f"add_vid_{playlist_id}"),
            InlineKeyboardButton("❌ Delete Playlist", callback_data=f"del_pl_{playlist_id}")
        ],
        [
            InlineKeyboardButton("⬅️ Back to Playlists", callback_data="view_playlists")
        ]
    ])
    
    try:
        if message_id:
            await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        else:
            await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except MessageNotModified:
        pass

async def send_vplaying_menu(client: Client, user_id: int, chat_id: int, message_id: int = None):
    """Renders the playlist selection picker for starting playback."""
    conn = await database.get_user_connection(user_id)
    if not conn:
        text = (
            "❌ **No connected group chat found!**\n\n"
            "You must link a group voice chat to control playback. Please:\n"
            "1. Add the bot to your group as an Admin.\n"
            "2. Send `/connect` inside the group.\n"
            "3. Click the authorize link in your DM."
        )
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("ℹ️ Help Instructions", callback_data="help_menu")],
            [InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")]
        ])
        
        try:
            if message_id:
                await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
            else:
                await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except MessageNotModified:
            pass
        return

    # Prepare session cache for selection
    state = user_states.setdefault(user_id, {})
    selected = state.setdefault("selected_playlists", set())
    
    playlists = await database.get_playlists(user_id)
    if not playlists:
        text = "🚫 **You don't have any playlists yet!**\n\nPlease create a playlist first and add videos to it."
        markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Create Playlist", callback_data="create_playlist")],
            [InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")]
        ])
        try:
            if message_id:
                await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
            else:
                await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        except MessageNotModified:
            pass
        return

    text = (
        f"🎥 **Voice Chat Playback Selection**\n\n"
        f"Connected Group: **{conn['connected_group_title']}**\n\n"
        f"Select the playlists you wish to stream. You can select single, multiple, or all:"
    )
    
    buttons = []
    total_videos = 0
    for pl in playlists:
        pl_id = str(pl["_id"])
        video_count = len(pl.get("videos", []))
        is_checked = pl_id in selected
        prefix = "✅ " if is_checked else "⬜ "
        buttons.append([
            InlineKeyboardButton(f"{prefix}{pl['name']} ({video_count} vids)", callback_data=f"toggle_pl_{pl_id}")
        ])
        if is_checked:
            total_videos += video_count
            
    all_selected = all(str(pl["_id"]) in selected for pl in playlists)
    toggle_all_label = "⬜ Select All" if not all_selected else "✅ Deselect All"
    buttons.append([InlineKeyboardButton(toggle_all_label, callback_data="toggle_all_pl")])
    
    if selected and total_videos > 0:
        buttons.append([InlineKeyboardButton(f"▶️ Start VC Playback ({total_videos} videos)", callback_data="start_playback")])
        
    buttons.append([InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")])
    
    markup = InlineKeyboardMarkup(buttons)
    try:
        if message_id:
            await client.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=markup)
        else:
            await client.send_message(chat_id=chat_id, text=text, reply_markup=markup)
    except MessageNotModified:
        pass

# ==========================================
# Helper to extract media attributes
# ==========================================

def extract_video_info(message: Message):
    """Parses incoming media types to extract necessary fields for streaming and DB storage."""
    file_id = None
    file_unique_id = None
    name = None
    duration = 180  # Default to 3 mins fallback
    file_size = 0
    
    if message.video:
        v = message.video
        file_id = v.file_id
        file_unique_id = v.file_unique_id
        duration = v.duration or 180
        file_size = v.file_size or 0
        if v.file_name:
            name = os.path.splitext(v.file_name)[0]
        elif v.title:
            name = v.title
            
    elif message.document:
        d = message.document
        if d.mime_type and d.mime_type.startswith("video/"):
            file_id = d.file_id
            file_unique_id = d.file_unique_id
            file_size = d.file_size or 0
            if d.file_name:
                name = os.path.splitext(d.file_name)[0]
                
    elif message.animation:
        a = message.animation
        file_id = a.file_id
        file_unique_id = a.file_unique_id
        duration = a.duration or 10
        file_size = a.file_size or 0
        if a.file_name:
            name = os.path.splitext(a.file_name)[0]
            
    if file_id and not name:
        name = f"Video_{file_unique_id}"
        
    return file_id, file_unique_id, name, duration, file_size

# ==========================================
# Pyrogram Message Event Handlers
# ==========================================

@app.on_message(filters.command("connect") & filters.group)
async def group_connect_handler(client: Client, message: Message):
    """Handles the /connect command when issued inside a group call environment."""
    # Enforce admin restriction
    try:
        member = await client.get_chat_member(message.chat.id, message.from_user.id)
        status_str = str(member.status).lower()
        if "admin" not in status_str and "owner" not in status_str and "creator" not in status_str:
            await message.reply(
                "❌ **Only group administrators can link the bot to their DM dashboard.**",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Dismiss", callback_data="dismiss_msg")
                ]])
            )
            return
    except Exception:
        pass # Allow fallback for cases like anonymous group channels

    bot_me = await client.get_me()
    group_id = message.chat.id
    url = f"https://t.me/{bot_me.username}?start=connect_{group_id}"
    
    await message.reply(
        f"🔗 **Link group to your DM control panel**\n\nClick the button below to authorize connection and play videos.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔗 Connect in DM", url=url)
        ]])
    )

@app.on_message(filters.private & filters.incoming)
async def private_message_handler(client: Client, message: Message):
    """Intercepts and parses messages sent to the bot in private DMs."""
    user_id = message.from_user.id
    state = user_states.setdefault(user_id, {})
    action = state.get("action")
    
    # 1. Handle Playlist creation naming step
    if action == "AWAITING_PLAYLIST_NAME":
        if not message.text or message.text.startswith("/"):
            await message.reply(
                "❌ **Invalid name.** Please enter a plain text name for the playlist.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel Creation", callback_data="view_playlists")
                ]])
            )
            return
            
        playlist_name = message.text.strip()
        playlist_id = await database.create_playlist(user_id, playlist_name)
        state["action"] = None
        
        await message.reply(
            f"✅ Playlist **{playlist_name}** created successfully!",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📥 Add Videos", callback_data=f"add_vid_{playlist_id}"),
                    InlineKeyboardButton("📂 View Detail", callback_data=f"view_pl_{playlist_id}")
                ],
                [
                    InlineKeyboardButton("⬅️ Playlists List", callback_data="view_playlists")
                ]
            ])
        )
        return
        
    # 2. Handle Video uploading/forwarding stream
    elif action == "ADDING_VIDEOS":
        playlist_id = state.get("playlist_id")
        file_id, file_unique_id, name, duration, file_size = extract_video_info(message)
        
        if file_id:
            success = await database.add_video_to_playlist(
                playlist_id, file_id, file_unique_id, name, duration, file_size
            )
            
            pl = await database.get_playlist_by_id(playlist_id)
            count = len(pl["videos"]) if pl else 0
            
            if success:
                await message.reply(
                    f"📥 Added: **{name}**\nTotal tracks: `{count}`",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Done Adding", callback_data="done_adding")
                    ]])
                )
            else:
                await message.reply(
                    f"⚠️ Video already in playlist or failed to add.\nTotal tracks: `{count}`",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("✅ Done Adding", callback_data="done_adding")
                    ]])
                )
        else:
            await message.reply(
                "❌ **Invalid media format.** Please upload or forward a Video, Document (video type), or Animation.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Done Adding", callback_data="done_adding")
                ]])
            )
        return

    # 3. Handle commands/default inputs
    if message.text:
        text = message.text.strip()
        
        if text.startswith("/start"):
            # Parse start arguments manually by splitting the text
            parts = text.split()
            if len(parts) > 1:
                arg = parts[1]
                if arg.startswith("connect_"):
                    try:
                        group_id = int(arg.replace("connect_", ""))
                        chat = await client.get_chat(group_id)
                        await database.set_user_connection(user_id, group_id, chat.title)
                        
                        await message.reply(
                            f"✅ **Linked successfully!**\n\n"
                            f"Connected to: **{chat.title}**\n"
                            f"Group ID: `{group_id}`\n\n"
                            f"You can now manage playlists and play videos in this voice chat.",
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("📂 My Playlists", callback_data="view_playlists")],
                                [InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")]
                            ])
                        )
                        return
                    except Exception as e:
                        await message.reply(
                            f"❌ **Failed to connect:** {e}\n"
                            f"Make sure the bot has admin privileges in the group and you clicked a valid connect link.",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")
                            ]])
                        )
                        return
                        
            await send_start_message(client, user_id)
            
        elif text == "/vplaying" or text.startswith("/play"):
            await send_vplaying_menu(client, user_id, user_id)
            
        elif text == "/vplaylist":
            await send_playlists_menu(client, user_id, user_id)
            
        else:
            # Enforce button restriction
            await message.reply(
                "💬 I only respond to interactive buttons. Choose an option from the menu below:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Open Menu", callback_data="back_home")
                ]])
            )

# ==========================================
# Pyrogram Callback Query Routing
# ==========================================

@app.on_callback_query()
async def callback_query_handler(client: Client, query: CallbackQuery):
    """Routes button actions to their corresponding states and logic blocks."""
    user_id = query.from_user.id
    data = query.data
    message_id = query.message.id
    
    state = user_states.setdefault(user_id, {})
    
    if data == "back_home":
        state["action"] = None
        await send_start_message(client, user_id, message_id)
        await query.answer()
        
    elif data == "view_playlists":
        state["action"] = None
        await send_playlists_menu(client, user_id, user_id, message_id)
        await query.answer()
        
    elif data == "create_playlist":
        state["action"] = "AWAITING_PLAYLIST_NAME"
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="view_playlists")
        ]])
        await client.edit_message_text(
            chat_id=user_id,
            message_id=message_id,
            text="➕ **Create Playlist**\n\nPlease type/send the name for your new playlist:",
            reply_markup=markup
        )
        await query.answer()
        
    elif data.startswith("view_pl_"):
        playlist_id = data.replace("view_pl_", "")
        await send_playlist_detail(client, user_id, playlist_id, user_id, message_id)
        await query.answer()
        
    elif data.startswith("del_pl_"):
        playlist_id = data.replace("del_pl_", "")
        await database.delete_playlist(playlist_id)
        await send_playlists_menu(client, user_id, user_id, message_id)
        await query.answer("Playlist deleted!")
        
    elif data.startswith("add_vid_"):
        playlist_id = data.replace("add_vid_", "")
        pl = await database.get_playlist_by_id(playlist_id)
        if pl:
            state["action"] = "ADDING_VIDEOS"
            state["playlist_id"] = playlist_id
            
            markup = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Done Adding", callback_data="done_adding")
            ]])
            await client.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=(
                    f"📥 **Adding Videos to Playlist**: **{pl['name']}**\n\n"
                    f"Please upload or forward video files directly here in my DM.\n"
                    f"I will automatically title and insert each track.\n\n"
                    f"Click the button below when you are finished."
                ),
                reply_markup=markup
            )
        await query.answer()
        
    elif data == "done_adding":
        playlist_id = state.get("playlist_id")
        state["action"] = None
        if playlist_id:
            await send_playlist_detail(client, user_id, playlist_id, user_id, message_id)
        else:
            await send_playlists_menu(client, user_id, user_id, message_id)
        await query.answer("Video upload mode deactivated.")
        
    elif data == "vplaying_menu":
        await send_vplaying_menu(client, user_id, user_id, message_id)
        await query.answer()
        
    elif data.startswith("toggle_pl_"):
        playlist_id = data.replace("toggle_pl_", "")
        selected = state.setdefault("selected_playlists", set())
        if playlist_id in selected:
            selected.remove(playlist_id)
        else:
            selected.add(playlist_id)
        await send_vplaying_menu(client, user_id, user_id, message_id)
        await query.answer()
        
    elif data == "toggle_all_pl":
        playlists = await database.get_playlists(user_id)
        selected = state.setdefault("selected_playlists", set())
        playlist_ids = {str(pl["_id"]) for pl in playlists}
        
        if playlist_ids.issubset(selected):
            selected.difference_update(playlist_ids)
        else:
            selected.update(playlist_ids)
            
        await send_vplaying_menu(client, user_id, user_id, message_id)
        await query.answer()
        
    elif data == "start_playback":
        conn = await database.get_user_connection(user_id)
        if not conn:
            await query.answer("No connected group chat found!", show_alert=True)
            return
            
        group_id = conn["connected_group_id"]
        selected = state.get("selected_playlists", set())
        if not selected:
            await query.answer("Please select at least one playlist!", show_alert=True)
            return
            
        # Gather videos
        all_videos = []
        for pl_id in selected:
            pl = await database.get_playlist_by_id(pl_id)
            if pl:
                all_videos.extend(pl.get("videos", []))
                
        if not all_videos:
            await query.answer("Selected playlists contain no videos!", show_alert=True)
            return
            
        from assistant import play_video
        existing_state = queue_manager.get_playback(group_id)
        if existing_state:
            existing_state.clear()
            queue_manager.delete_playback(group_id)
            
        p_state = queue_manager.create_playback(group_id, user_id)
        p_state.add_to_queue(all_videos)
        p_state.active_msg_id = message_id
        
        await query.answer("Initializing playback...")
        asyncio.create_task(play_video(group_id, p_state, client, is_first=True))
        
    elif data == "play_resume":
        conn = await database.get_user_connection(user_id)
        if not conn:
            await query.answer("No connected group chat!", show_alert=True)
            return
        p_state = queue_manager.get_playback(conn["connected_group_id"])
        if not p_state:
            await query.answer("No active playback!", show_alert=True)
            return
            
        from assistant import call_py
        try:
            if p_state.is_paused:
                await call_py.resume(p_state.chat_id)
                p_state.is_paused = False
                await query.answer("Playback resumed.")
            else:
                await call_py.pause(p_state.chat_id)
                p_state.is_paused = True
                await query.answer("Playback paused.")
                
            video = p_state.get_current_video()
            await client.edit_message_text(
                chat_id=user_id,
                message_id=message_id,
                text=get_playback_text(video, p_state),
                reply_markup=get_playback_dashboard_markup(p_state)
            )
        except Exception as e:
            await query.answer(f"Failed toggle: {e}", show_alert=True)
            
    elif data == "skip_track":
        conn = await database.get_user_connection(user_id)
        if not conn:
            await query.answer("No connected group chat!", show_alert=True)
            return
        p_state = queue_manager.get_playback(conn["connected_group_id"])
        if not p_state:
            await query.answer("No active playback!", show_alert=True)
            return
            
        from assistant import skip_track
        await query.answer("Skipping to next track...")
        await skip_track(p_state.chat_id, client)
        
    elif data == "stop_playback":
        conn = await database.get_user_connection(user_id)
        if not conn:
            await query.answer("No connected group chat!", show_alert=True)
            return
        p_state = queue_manager.get_playback(conn["connected_group_id"])
        if not p_state:
            await send_start_message(client, user_id, message_id)
            await query.answer("No active playback.")
            return
            
        from assistant import call_py
        try:
            await call_py.leave_call(p_state.chat_id)
        except Exception:
            pass
        p_state.clear()
        queue_manager.delete_playback(p_state.chat_id)
        
        await client.edit_message_text(
            chat_id=user_id,
            message_id=message_id,
            text="⏹ **Playback stopped by user.**",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")
            ]])
        )
        await query.answer("Playback stopped.")
        
    elif data == "vol_up":
        conn = await database.get_user_connection(user_id)
        if conn:
            p_state = queue_manager.get_playback(conn["connected_group_id"])
            if p_state:
                p_state.volume = min(p_state.volume + 20, 200)
                from assistant import call_py
                try:
                    await call_py.change_volume_call(p_state.chat_id, p_state.volume)
                except Exception:
                    pass
                video = p_state.get_current_video()
                await client.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=get_playback_text(video, p_state),
                    reply_markup=get_playback_dashboard_markup(p_state)
                )
                await query.answer(f"Volume: {p_state.volume}%")
                return
        await query.answer("No active playback.")
        
    elif data == "vol_down":
        conn = await database.get_user_connection(user_id)
        if conn:
            p_state = queue_manager.get_playback(conn["connected_group_id"])
            if p_state:
                p_state.volume = max(p_state.volume - 20, 0)
                from assistant import call_py
                try:
                    await call_py.change_volume_call(p_state.chat_id, p_state.volume)
                except Exception:
                    pass
                video = p_state.get_current_video()
                await client.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=get_playback_text(video, p_state),
                    reply_markup=get_playback_dashboard_markup(p_state)
                )
                await query.answer(f"Volume: {p_state.volume}%")
                return
        await query.answer("No active playback.")
        
    elif data == "loop_toggle":
        conn = await database.get_user_connection(user_id)
        if conn:
            p_state = queue_manager.get_playback(conn["connected_group_id"])
            if p_state:
                modes = ["off", "queue", "single"]
                current_idx = modes.index(p_state.loop_mode)
                p_state.loop_mode = modes[(current_idx + 1) % len(modes)]
                
                video = p_state.get_current_video()
                await client.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=get_playback_text(video, p_state),
                    reply_markup=get_playback_dashboard_markup(p_state)
                )
                await query.answer(f"Loop: {p_state.loop_mode.capitalize()}")
                return
        await query.answer("No active playback.")
        
    elif data == "autoplay_toggle":
        conn = await database.get_user_connection(user_id)
        if conn:
            p_state = queue_manager.get_playback(conn["connected_group_id"])
            if p_state:
                p_state.autoplay = not p_state.autoplay
                video = p_state.get_current_video()
                await client.edit_message_text(
                    chat_id=user_id,
                    message_id=message_id,
                    text=get_playback_text(video, p_state),
                    reply_markup=get_playback_dashboard_markup(p_state)
                )
                await query.answer(f"Autoplay: {'Enabled' if p_state.autoplay else 'Disabled'}")
                return
        await query.answer("No active playback.")
        
    elif data == "help_menu":
        help_text = (
            "ℹ️ **How to Use VPlay Bot**\n\n"
            "1. **Add Bot as Admin**: Add me to your group and give me administrator rights.\n"
            "2. **Connect**: Send `/connect` command inside your group.\n"
            "3. **Authorize**: Click the connect button sent in the group, which redirects you to DM and authorizes the linkage.\n"
            "4. **Manage Playlists**: Create playlists and upload/forward your video files to me.\n"
            "5. **Play**: Select which playlist(s) to stream and start streaming into the Voice Chat."
        )
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("⬅️ Back Home", callback_data="back_home")
        ]])
        await client.edit_message_text(chat_id=user_id, message_id=message_id, text=help_text, reply_markup=markup)
        await query.answer()
        
    elif data == "dismiss_msg":
        await client.delete_messages(chat_id=query.message.chat.id, message_ids=message_id)
        await query.answer()
        
    elif data == "refresh_dashboard":
        conn = await database.get_user_connection(user_id)
        if conn:
            p_state = queue_manager.get_playback(conn["connected_group_id"])
            if p_state:
                video = p_state.get_current_video()
                if video:
                    await client.edit_message_text(
                        chat_id=user_id,
                        message_id=message_id,
                        text=get_playback_text(video, p_state),
                        reply_markup=get_playback_dashboard_markup(p_state)
                    )
        await query.answer("Refreshed status.")
