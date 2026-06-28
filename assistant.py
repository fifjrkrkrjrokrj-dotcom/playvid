import os
import asyncio
from typing import Optional
from pyrogram import Client
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream, StreamEnded
from pytgcalls.types.stream import VideoQuality, AudioQuality
from pytgcalls.filters import stream_end

import config
import queue_manager

# Initialize the assistant client using session string
assistant_app = Client(
    name="assistant",
    api_id=int(config.API_ID),
    api_hash=config.API_HASH,
    session_string=config.SESSION_STRING
)

# Initialize PyTgCalls with assistant app
call_py = PyTgCalls(assistant_app)

async def play_video(chat_id: int, state: queue_manager.PlaybackState, bot_client: Client, is_first: bool = False):
    """
    Downloads and starts playing the current video in the queue.
    Handles the VC leave-and-rejoin cycle to ensure stability and avoid Telegram VC glitches.
    """
    # 1. Advance index if we are not playing the very first video
    if not is_first:
        has_next = state.next_index()
        if not has_next:
            # Autoplay mode check: if queue ends and autoplay is enabled, load all videos
            if state.autoplay:
                import database
                playlists = await database.get_playlists(state.user_id)
                all_videos = []
                for pl in playlists:
                    all_videos.extend(pl.get("videos", []))
                
                if all_videos:
                    state.queue = all_videos
                    state.current_index = 0
                    asyncio.create_task(play_video(chat_id, state, bot_client, is_first=True))
                    return
            
            # Queue finished
            state.cleanup_local_file()
            try:
                await call_py.leave_call(chat_id)
            except Exception:
                pass
            
            # Update user DM dashboard
            if state.active_msg_id and state.user_id:
                try:
                    from bot import get_playback_dashboard_markup
                    await bot_client.edit_message_text(
                        chat_id=state.user_id,
                        message_id=state.active_msg_id,
                        text="🎵 **Playback finished!** All videos in the selected playlists have been played successfully.",
                        reply_markup=get_playback_dashboard_markup(state, finished=True)
                    )
                except Exception as e:
                    print(f"Error sending playlist end msg: {e}")
            
            state.clear()
            queue_manager.delete_playback(chat_id)
            return

    video = state.get_current_video()
    if not video:
        return

    state.is_playing = False
    state.is_paused = False

    # Ensure the assistant is in the group chat (bot is admin, so it can invite)
    try:
        assistant_me = await assistant_app.get_me()
        await bot_client.add_chat_members(chat_id, assistant_me.id)
    except Exception as e:
        # Ignore if already in group or privacy settings prevent adding
        pass

    # Force the assistant client to resolve/cache the group chat details
    try:
        await assistant_app.get_chat(chat_id)
    except Exception as e:
        print(f"Error caching group chat details for assistant: {e}")

    # Update dashboard state to "Downloading..."
    if state.active_msg_id and state.user_id:
        try:
            await bot_client.edit_message_text(
                chat_id=state.user_id,
                message_id=state.active_msg_id,
                text=f"📥 **Downloading `{video['name']}` from Telegram...**\n\nThis might take a moment depending on the file size."
            )
        except Exception:
            pass

    # Ensure downloads directory exists
    os.makedirs("downloads", exist_ok=True)
    local_path = os.path.join("downloads", f"{video['file_unique_id']}.mp4")

    # Clean up previous local file just in case
    state.cleanup_local_file()

    # Point to the local HTTP streaming port as fallback
    fallback_url = f"http://127.0.0.1:{config.PORT}/stream/{video['file_id']}"
    play_path = fallback_url

    # Try downloading to local disk for stable playback on platforms with restricted network latency (like Railway)
    try:
        # download_media handles timeouts and retries internally
        downloaded_file = await bot_client.download_media(
            message=video['file_id'],
            file_name=local_path
        )
        if downloaded_file and os.path.exists(downloaded_file):
            state.local_file_path = downloaded_file
            play_path = downloaded_file
    except Exception as e:
        print(f"Failed to download media locally, falling back to on-the-fly stream: {e}")

    # User Requirement: Assistant leaves the VC before rejoining to avoid voice chat stream glitches
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    
    # Wait for Telegram gateway update
    await asyncio.sleep(1.5)

    # Start playback
    try:
        await call_py.play(
            chat_id,
            MediaStream(
                play_path,
                video_parameters=VideoQuality.HD_720p,
                audio_parameters=AudioQuality.HIGH
            )
        )
        
        # Adjust volume call if default was changed
        if state.volume != 100:
            try:
                await call_py.change_volume_call(chat_id, state.volume)
            except Exception:
                pass
                
        state.is_playing = True
        state.is_paused = False
        
        # Update control dashboard in DM
        if state.active_msg_id and state.user_id:
            try:
                from bot import get_playback_dashboard_markup, get_playback_text
                await bot_client.edit_message_text(
                    chat_id=state.user_id,
                    message_id=state.active_msg_id,
                    text=get_playback_text(video, state),
                    reply_markup=get_playback_dashboard_markup(state)
                )
            except Exception as e:
                print(f"Error updating playing dashboard: {e}")
                
    except Exception as e:
        print(f"Error starting video VC stream: {e}")
        if state.active_msg_id and state.user_id:
            try:
                await bot_client.send_message(
                    chat_id=state.user_id,
                    text=f"❌ **Failed to start voice chat playback of `{video['name']}`**: `{str(e)}`"
                )
            except Exception:
                pass
        state.cleanup_local_file()
        await asyncio.sleep(2)
        asyncio.create_task(play_video(chat_id, state, bot_client, is_first=False))

async def skip_track(chat_id: int, bot_client: Client) -> bool:
    """Manually skips the current track to the next one, utilizing the exit/rejoin sequence."""
    state = queue_manager.get_playback(chat_id)
    if not state:
        return False
        
    async with state.skip_lock:
        state.is_playing = False
        if state.download_task and not state.download_task.done():
            state.download_task.cancel()
            
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
            
        # Play the next video
        asyncio.create_task(play_video(chat_id, state, bot_client, is_first=False))
    return True

# PyTgCalls Stream End Event Handler
@call_py.on_update(stream_end())
async def on_stream_end_handler(client: PyTgCalls, update: StreamEnded):
    chat_id = update.chat_id
    state = queue_manager.get_playback(chat_id)
    if state and state.is_playing:
        from bot import app as bot_app
        async with state.skip_lock:
            state.is_playing = False
            asyncio.create_task(play_video(chat_id, state, bot_app, is_first=False))
