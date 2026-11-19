import {context, reddit as realReddit, redis as realRedis, settings} from '@devvit/web/server'
import {type Reported, buildAlert, isDiscordWebhook} from '../shared/alert.ts'

export type Deps = {
  redis: {
    incrBy(key: string, value: number): Promise<number>
    del(key: string): Promise<unknown>
    expire(key: string, seconds: number): Promise<unknown>
  }
  webhookUrl(): Promise<string | undefined>
  // Reports an item needs before it's sent, the same numbers the Discord bot uses to make a card.
  thresholds(): Promise<{post: number; comment: number}>
  botUserId(): Promise<string | undefined>
  userName(userId: string): Promise<string | null>
  send(url: string, body: unknown): Promise<{status: number; retryAfterS?: number}>
  sleep(ms: number): Promise<void>
}

const ALERT_TTL_S = 30 * 24 * 60 * 60
const alertKey = (fullname: string) => `alerted:${fullname}`
const countKey = (fullname: string) => `reports:${fullname}`
const asCount = (v: unknown, fallback: number) => {
  const n = Math.floor(Number(v))
  return Number.isFinite(n) && n >= 1 ? n : fallback
}

const realDeps: Deps = {
  redis: realRedis as unknown as Deps['redis'],
  webhookUrl: () => settings.get<string>('discordWebhook'),
  async thresholds() {
    return {
      post: asCount(await settings.get('postReportThreshold'), 3),
      comment: asCount(await settings.get('commentReportThreshold'), 5),
    }
  },
  botUserId: () => settings.get<string>('discordBotUserId'),
  async userName(userId) {
    try {
      return (await realReddit.getUserById(userId as `t2_${string}`))?.username ?? null
    } catch {
      return null
    }
  },
  async send(url, body) {
    const res = await fetch(url, {
      method: 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(body),
    })
    let retryAfterS: number | undefined
    if (res.status === 429) {
      const data = (await res.json().catch(() => ({}))) as {retry_after?: number}
      retryAfterS = data.retry_after
    }
    return {status: res.status, retryAfterS}
  },
  sleep: ms => new Promise(r => setTimeout(r, ms)),
}

// One alert per reported item. Returns what happened, for the log.
export async function alertOnce(r: Reported, deps: Deps = realDeps): Promise<'sent' | 'duplicate' | 'no-webhook' | 'failed' | 'below-threshold'> {
  const url = await deps.webhookUrl()
  if (!url || !isDiscordWebhook(url)) return 'no-webhook'

  // Below the threshold the Discord bot makes no card, so nothing is sent. Reddit's count can lag
  // the report that triggered this, so the app also counts the reports it has seen.
  const seen = await deps.redis.incrBy(countKey(r.fullname), 1)
  await deps.redis.expire(countKey(r.fullname), ALERT_TTL_S)
  const reports = Math.max(r.totalReports || 0, seen)
  const t = await deps.thresholds()
  if (reports < (r.kind === 'post' ? t.post : t.comment)) return 'below-threshold'
  r = {...r, totalReports: reports}

  // incrBy is atomic: whoever gets 1 owns this item's alert. The key is released if sending fails.
  const key = alertKey(r.fullname)
  if ((await deps.redis.incrBy(key, 1)) !== 1) return 'duplicate'
  await deps.redis.expire(key, ALERT_TTL_S)

  const body = buildAlert({...r, author: r.author}, await deps.botUserId())
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const res = await deps.send(url, body)
      if (res.status >= 200 && res.status < 300) return 'sent'
      if (res.status === 429 && res.retryAfterS !== undefined && res.retryAfterS <= 10) {
        await deps.sleep(res.retryAfterS * 1000)
        continue
      }
      break
    } catch {
      // fall through to release the key
    }
  }
  await deps.redis.del(key)
  return 'failed'
}

type PostEvent = {
  post?: {id: string; title: string; selftext: string; authorId: string; numReports: number; createdAt: number; permalink: string}
  subreddit?: {name: string}
  reason: string
}
type CommentEvent = {
  comment?: {id: string; body: string; author: string; numReports: number; createdAt: number; permalink: string}
  subreddit?: {name: string}
  reason: string
}

export async function onPostReport(event: PostEvent, deps: Deps = realDeps) {
  const p = event.post
  if (!p) return 'ignored'
  return alertOnce(
    {
      fullname: p.id.startsWith('t3_') ? p.id : `t3_${p.id}`,
      kind: 'post',
      subreddit: event.subreddit?.name ?? context.subredditName,
      author: await deps.userName(p.authorId),
      permalink: p.permalink,
      title: p.title,
      text: p.selftext,
      reportReason: event.reason,
      totalReports: p.numReports,
      createdAtMs: p.createdAt,
    },
    deps,
  )
}

export async function onCommentReport(event: CommentEvent, deps: Deps = realDeps) {
  const c = event.comment
  if (!c) return 'ignored'
  const author = c.author.startsWith('t2_') ? await deps.userName(c.author) : c.author
  return alertOnce(
    {
      fullname: c.id.startsWith('t1_') ? c.id : `t1_${c.id}`,
      kind: 'comment',
      subreddit: event.subreddit?.name ?? context.subredditName,
      author,
      permalink: c.permalink,
      title: null,
      text: c.body,
      reportReason: event.reason,
      totalReports: c.numReports,
      createdAtMs: c.createdAt,
    },
    deps,
  )
}
