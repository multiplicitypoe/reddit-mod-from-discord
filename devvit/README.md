# Reddit Mod from Discord: Devvit app

This is the Reddit side of [Reddit Mod from Discord](../README.md), running on Reddit's Developer Platform. When a post or comment is reported, the app posts it to the mod channel's webhook with the title or text, the author, the report count and the latest reason. Each item is posted once, however many more reports it gets.

The Discord bot in this repository picks the message up by its footer, `rmfd:v1 <fullname> r/<subreddit>`, posts its own message for the item with the moderation buttons, and deletes the original.

## Settings

- `discordWebhook` (secret): the webhook URL of the mod channel.
- `discordBotUserId`: the Discord user ID of the bot. Each message mentions the bot, which lets a bot without the Message Content intent read it. Leave empty for no mention.

## Develop

```
npm install
npm test
npx devvit playtest r/<test subreddit>
```
