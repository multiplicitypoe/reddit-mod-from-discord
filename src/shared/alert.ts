// Builds the Discord message for a reported post or comment. The footer carries a marker the
// Discord bot looks for: it reposts the alert as its own message (a webhook can't have buttons).
export type Reported = {
  fullname: string // t3_... or t1_...
  kind: 'post' | 'comment'
  subreddit: string
  author: string | null
  permalink: string // path only
  title: string | null // posts only
  text: string // post body or comment body
  reportReason: string // from this report
  totalReports: number
  createdAtMs: number
}

export const MARKER = 'rmfd:v1'
const POST_COLOR = 0xff4500
const COMMENT_COLOR = 0x5865f2

const clip = (s: string, max: number) => (s.length > max ? `${s.slice(0, max - 1).trimEnd()}…` : s)

// Keeps a quoted body from pinging anyone or breaking out of the quote.
export function quote(text: string, max: number): string {
  const body = clip(text.replace(/\r\n?/g, '\n').trim(), max)
  if (!body) return '*(no text)*'
  return body
    .replace(/@(everyone|here)/g, '@​$1')
    .split('\n')
    .map(line => `> ${line}`)
    .join('\n')
}

export function footerFor(fullname: string, subreddit: string): string {
  return `${MARKER} ${fullname} r/${subreddit}`
}

export function parseFooter(footer: string): {fullname: string; subreddit: string} | null {
  const m = new RegExp(`^${MARKER} (t[13]_[a-z0-9]+) r/([A-Za-z0-9_]+)$`).exec(footer)
  return m ? {fullname: m[1]!, subreddit: m[2]!} : null
}

// A bot without the Message Content intent sees a webhook message's embed only if the message
// mentions the bot, so the alert mentions it when the bot's user id is configured.
export const isUserId = (id: string) => /^\d{15,25}$/.test(id)

export function buildAlert(r: Reported, botUserId?: string) {
  const isPost = r.kind === 'post'
  const reports = r.totalReports > 0 ? r.totalReports : 1
  const mention = botUserId && isUserId(botUserId) ? botUserId : null
  return {
    username: 'Mod queue',
    ...(mention ? {content: `<@${mention}>`} : {}),
    allowed_mentions: mention ? {users: [mention]} : {parse: [] as string[]},
    embeds: [
      {
        title: clip(isPost ? `Reported post: ${r.title ?? '(untitled)'}` : 'Reported comment', 256),
        url: `https://www.reddit.com${r.permalink}`,
        description: quote(r.text, 1000),
        color: isPost ? POST_COLOR : COMMENT_COLOR,
        fields: [
          {name: 'Author', value: r.author ? `u/${r.author}` : 'unknown', inline: true},
          {name: 'Reports', value: String(reports), inline: true},
          {name: 'Latest reason', value: clip(r.reportReason || 'no reason given', 200), inline: false},
        ],
        footer: {text: footerFor(r.fullname, r.subreddit)},
        timestamp: new Date(r.createdAtMs || Date.now()).toISOString(),
      },
    ],
  }
}

export function isDiscordWebhook(url: string): boolean {
  return /^https:\/\/(?:discord\.com|discordapp\.com)\/api\/webhooks\/\d+\/[\w-]+$/.test(url)
}
