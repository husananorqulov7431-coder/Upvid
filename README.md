# Upvid — YouTube Telegram bot

Telegram bot that downloads one YouTube video per link, including Shorts, and sends the best available quality that can be delivered through Telegram's hosted Bot API.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. Open this repository's [Actions secrets settings](https://github.com/husananorqulov7431-coder/Upvid/settings/secrets/actions).
3. Click **New repository secret**. Set **Name** to `BOT_TOKEN` and **Secret** to the token from BotFather. Do not commit the token to a file.
4. Open the **Actions** tab. If asked, enable GitHub Actions for the repository.
5. Select **Upvid Telegram Bot** → **Run workflow** to start it immediately.
6. Open the latest run and check its logs. Then send `/start` to your Telegram bot and send it a YouTube video link.

## How quality is handled

- yt-dlp selects the best available video and audio tracks.
- If the original MP4 uses H.264/AAC and fits Telegram's upload limit, the bot sends it unchanged.
- When only a container change is needed, the bot remuxes without re-encoding.
- If a large video can be reduced to the upload limit without an extreme quality loss, it is encoded to a compatible MP4.
- If fitting the video would destroy too much quality, the bot returns the original YouTube link instead of sending a badly degraded copy.

The hosted Telegram Bot API has a file-upload limit of about 50 MB. GitHub-hosted Actions jobs are limited to 6 hours, so this repository restarts the polling bot on a schedule every six hours. **This is not guaranteed 24/7 hosting**; scheduled starts can be delayed and the bot may be unavailable between runs. For uninterrupted service and larger files, use a VPS and consider a local Telegram Bot API server.

Use this bot only for videos you are authorized to download. It does not bypass age, paid-content, private-video, or other access restrictions.

## Troubleshooting

- **`BOT_TOKEN topilmadi`**: make sure the repository secret is named exactly `BOT_TOKEN`.
- **No workflow appears under Actions**: enable Actions for this repository and make sure `.github/workflows/bot.yml` is on the default branch.
- **Bot is not responding**: open Actions → the latest workflow run and inspect the logs; ensure no other app is using the same token.
- **YouTube extraction fails**: rerun with the latest yt-dlp version; YouTube extraction can change and occasionally require a dependency update.
