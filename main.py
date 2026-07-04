"""
Telethon listener — jalan terus di Railway (sama tempat bot Picel Mini App lu jalan).

Yang dilakukan:
1. Dengerin pesan baru masuk di PRIVATE channel -> generate teaser blur -> upload ke Supabase
   -> insert row ke tabel `posts` -> crosspost teaser ke FREE channel dengan link join.
2. Dengerin pesan baru di FREE channel -> insert row biasa (is_locked=False) ke `posts`.

Butuh:
  pip install telethon pillow supabase python-dotenv

ENV VARS (taruh di Railway):
  TG_API_ID, TG_API_HASH, TG_SESSION_STRING   -> akun userbot yang jadi member kedua channel
  BOT_TOKEN                                    -> bot terpisah yang JADI ADMIN di free channel
                                                   (biar bisa kirim inline button "Buka Private")
  FREE_CHANNEL_ID, PRIVATE_CHANNEL_ID          -> id numerik channel (bukan username)
  PRIVATE_JOIN_LINK                            -> link invite channel private (t.me/+xxxx)
  SUPABASE_URL, SUPABASE_SERVICE_KEY           -> service role key, JANGAN dipakai di frontend
"""

import os
import io
from datetime import datetime

from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from PIL import Image, ImageFilter
from supabase import create_client

# ---------- config ----------
TG_API_ID = int(os.environ["TG_API_ID"])
TG_API_HASH = os.environ["TG_API_HASH"]
TG_SESSION_STRING = os.environ["TG_SESSION_STRING"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

FREE_CHANNEL_ID = int(os.environ["FREE_CHANNEL_ID"])
PRIVATE_CHANNEL_ID = int(os.environ["PRIVATE_CHANNEL_ID"])
PRIVATE_JOIN_LINK = os.environ["PRIVATE_JOIN_LINK"]

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

BLUR_RADIUS = 22       # makin gede makin gak keliatan bentuk aslinya
TEASER_MAX_SIDE = 480  # resize dulu sebelum blur biar file kecil + blur lebih rata

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# userbot -> baca isi channel (harus member kedua channel)
client = TelegramClient(StringSession(TG_SESSION_STRING), TG_API_ID, TG_API_HASH)
# bot -> yang ngirim teaser ke free channel (harus admin di free channel)
bot = TelegramClient("bot_session", TG_API_ID, TG_API_HASH)


def make_teaser_bytes(photo_bytes: bytes) -> bytes:
    img = Image.open(io.BytesIO(photo_bytes)).convert("RGB")
    img.thumbnail((TEASER_MAX_SIDE, TEASER_MAX_SIDE))
    img = img.filter(ImageFilter.GaussianBlur(BLUR_RADIUS))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=70)
    return out.getvalue()


def upload_to_storage(bucket: str, path: str, data: bytes, content_type="image/jpeg") -> str:
    supabase.storage.from_(bucket).upload(
        path, data, {"content-type": content_type, "upsert": "true"}
    )
    return supabase.storage.from_(bucket).get_public_url(path)


@client.on(events.NewMessage(chats=PRIVATE_CHANNEL_ID))
async def on_private_post(event):
    if not event.photo and not event.video:
        return  # skip text-only messages

    msg = event.message
    media_bytes = await client.download_media(msg, file=bytes)

    # 1. upload full-res ke bucket private (akses dibatasi, jangan public)
    ext = "mp4" if event.video else "jpg"
    full_path = f"{msg.id}.{ext}"
    supabase.storage.from_("private-media").upload(
        full_path, media_bytes, {"upsert": "true"}
    )

    # 2. bikin teaser blur (untuk video, ambil thumbnail-nya dulu — Telethon kasih
    #    thumb di msg.video.thumbs biasanya, di sini disederhanakan pakai frame video kalau ada)
    source_for_blur = media_bytes if event.photo else await client.download_media(
        msg, thumb=-1, file=bytes
    )
    teaser_bytes = make_teaser_bytes(source_for_blur)
    teaser_url = upload_to_storage("public-media", f"teaser_{msg.id}.jpg", teaser_bytes)

    # 3. simpan ke Supabase
    supabase.table("posts").insert({
        "telegram_msg_id": msg.id,
        "source_channel": "private",
        "is_locked": True,
        "media_type": "video" if event.video else "photo",
        "teaser_url": teaser_url,
        "caption": msg.text or "",
        "created_at": datetime.utcnow().isoformat(),
    }).execute()

    # 4. crosspost teaser ke free channel, pakai BOT biar bisa kasih inline button
    await bot.send_file(
        FREE_CHANNEL_ID,
        io.BytesIO(teaser_bytes),
        caption="🔒 Drop baru di Private Channel. Yang mau lihat full versi, gas join.",
        buttons=[Button.url("Buka Private Channel", PRIVATE_JOIN_LINK)],
    )

    supabase.table("posts").update({"teaser_posted": True}).eq(
        "telegram_msg_id", msg.id
    ).execute()


@client.on(events.NewMessage(chats=FREE_CHANNEL_ID))
async def on_free_post(event):
    if not event.photo and not event.video:
        return

    msg = event.message
    media_bytes = await client.download_media(msg, file=bytes)
    ext = "mp4" if event.video else "jpg"
    path = f"{msg.id}.{ext}"
    media_url = upload_to_storage(
        "public-media", path, media_bytes,
        content_type="video/mp4" if event.video else "image/jpeg",
    )

    supabase.table("posts").insert({
        "telegram_msg_id": msg.id,
        "source_channel": "free",
        "is_locked": False,
        "media_type": "video" if event.video else "photo",
        "media_url": media_url,
        "caption": msg.text or "",
        "created_at": datetime.utcnow().isoformat(),
    }).execute()


async def main():
    await client.start()
    await bot.start(bot_token=BOT_TOKEN)
    print("Listener jalan, dengerin free & private channel...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    with client:
        client.loop.run_until_complete(main())
