# Mod queue alerts (Devvit app)

When a post or comment in a subreddit is reported, this app posts one embed to a Discord webhook: the title or text, author, report count and latest reason. Each item alerts once, however many more reports it gets.

The embed's footer carries `rmfd:v1 <fullname> r/<subreddit>`, which the Discord bot in this repository reads to repost the alert with its action buttons.

## Settings

- `discordWebhook` (secret): the webhook URL of the alert channel.
- `discordBotUserId`: the Discord user ID of the bot. The alert mentions it, which is what lets a bot without the Message Content intent read the embed. Leave empty for no mention.

## Develop

```
npm install
npm test
npx devvit playtest r/<test subreddit>
```
